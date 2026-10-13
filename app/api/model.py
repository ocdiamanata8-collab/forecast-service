"""Endpoint model: info (§5.4) dan performance (§5.5)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request

from app import evaluation as ev
from app import engine, preprocessing as pp
from app.artifacts import ArtifactError, load_artifacts
from app.service import UNIT_BY_TARGET
from app.schemas import (
    HorizonMetrics,
    ModelInfoResponse,
    PerformanceResponse,
    ThresholdsResponse,
)
from app.security import (
    FastApiError,
    assert_known_site,
    require_api_key,
    require_model_ready,
)
from app.version import metadata as version_metadata, model_version as current_model_version

router = APIRouter(prefix="/api/v1", tags=["model"])

_MODEL_DIR = Path(__file__).resolve().parents[2] / "model"
_EVAL_DIR = Path(__file__).resolve().parents[2] / "data" / "evaluation"


def _training_meta() -> dict[str, str]:
    """Metadata ``model/VERSION`` lewat satu-satunya pembaca: app/version.py.

    Model ARIMA tidak punya artefak ``.pkl`` — order dikunci konstan di
    ``app.engine``. Berkas ``model/VERSION`` adalah sumber kebenaran versi
    dan metadata; parser file TIDAK boleh diduplikasi di sini.
    """
    return version_metadata()


@router.get("/model/info", response_model=ModelInfoResponse)
def get_model_info(
    _: str = Depends(require_api_key),
    __: None = Depends(require_model_ready),
) -> ModelInfoResponse:
    """§5.4 — versi & metadata model (baca konstanta, tanpa file model biner)."""
    meta = _training_meta()
    trained_at: datetime | None = None
    training_range: dict[str, datetime] | None = None
    if "trained_at" in meta:
        try:
            trained_at = datetime.fromisoformat(meta["trained_at"])
        except ValueError:
            trained_at = None
    if "training_range" in meta:
        # Format artefak: "<from>/<to>" — PASTI satu garis miring. Dulu diparse
        # sebagai 3 bagian (start, _, end) sehingga selalu ValueError dan
        # training_range diam-diam jadi None — data telusur hilang tanpa error.
        parts = meta["training_range"].split("/", 1)
        try:
            if len(parts) == 2:
                training_range = {
                    "from": datetime.fromisoformat(parts[0]),
                    "to": datetime.fromisoformat(parts[1]),
                }
            else:
                training_range = None
        except ValueError:
            training_range = None

    # Fitur & urutannya dari artefak training, bukan ditebak/dikosongkan.
    # Ketiadaan artefak = konfigurasi rusak → 503, bukan respons kosong
    # yang membuat konsumen berpikir model memang tanpa fitur.
    try:
        arts = load_artifacts()
    except ArtifactError as exc:
        raise FastApiError("E_SERVICE_UNAVAILABLE", str(exc), status=503) from exc

    return ModelInfoResponse(
        model_version=current_model_version(),
        target=arts.target,
        resolution=arts.resolution,
        # Konteks satuan (§3.1): konsumen tidak perlu menebak dari target.
        unit=UNIT_BY_TARGET.get(arts.target, ""),
        algorithm="ARIMA",
        order=list(arts.order),
        timezone=arts.timezone,
        history_min_rows=arts.history_min_rows,
        horizon_max=arts.horizon_max,
        trained_at=trained_at,
        training_range=training_range,
        # Urutan eksplisit sesuai training — konsumen tidak perlu menebak.
        features=list(arts.exog_columns),
        # Jumlah baris yang benar-benar dilatih (artefak, bukan tebakan).
        training_rows=arts.training_rows,
        exog_supported=True,
        deterministic=True,
        evaluation_status=meta.get("evaluation_status", "UNKNOWN"),  # type: ignore[arg-type]
        engine="app/engine.py",
        preprocessing="app/preprocessing.py",
    )


@router.get("/model/performance", response_model=PerformanceResponse)
def get_performance(
    request: Request,
    site_id: str = Query(pattern=r"^[A-Z]{2,4}[_A-Z0-9]{2,12}$"),
    resolution: str = Query(default="daily", pattern="^(daily|hourly)$"),
    _: str = Depends(require_api_key),
) -> PerformanceResponse:
    """§5.5 — MAE/RMSE/MAPE + ambang + status GO/NO-GO.

    Sumber angka: :mod:`app.evaluation` (backtest holdout terakhir). Bila
    pipeline evaluasi belum pernah dijalankan untuk site ini, endpoint tetap
    ``200 OK`` dengan ``status="UNKNOWN"`` dan ``horizons: []`` (kontrak:
    metrik nol ≠ resource tidak ada).
    """
    assert_known_site(request, site_id)
    cache = getattr(request.app.state, "performance_cache", None)
    report = None if cache is None else cache.get((site_id, resolution))

    if report is None:
        # Fallback: laporan yang pernah disimpan `scripts/run_evaluation.py`.
        report_json = _EVAL_DIR / "report.json"
        if report_json.exists():
            try:
                report = json.loads(report_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = None

    if report is None:
        # Belum pernah dievaluasi: evaluated_at TIDAK boleh diisi waktu
        # request — itu akan membuat "run terakhir" selalu tampak baru.
        return PerformanceResponse(
            site_id=site_id,
            target="irradiance",
            resolution=resolution,
            unit="W/m2",
            model_version=current_model_version(),
            evaluated_at=None,
            data_rows=0,
            evaluation_range=None,
            n_samples=0,
            thresholds=_thresholds_response(ev.DEFAULT_THRESHOLDS),
            status="UNKNOWN",
            failures=[],
            horizons=[],
        )

    if isinstance(report, dict):
        return _performance_from_json(site_id, resolution, report)

    # Objek BacktestReport dari cache (dihitung saat itu juga, tanpa stempel
    # waktu). evaluated_at tidak dikarang — lihat catatan di schema.
    return PerformanceResponse(
        site_id=site_id,
        target=report.target,
        resolution=resolution,
        unit="W/m2",
        model_version=current_model_version(),
        evaluated_at=None,
        data_rows=report.n_rows,
        evaluation_range={
            "from": report.holdout_start,
            "to": report.holdout_end,
        },
        n_samples=report.n_origins,
        thresholds=_thresholds_response(report.thresholds),
        status="GO" if report.passed else "NO_GO",
        failures=list(report.failures),
        horizons=[
            HorizonMetrics(
                horizon=b.horizon,
                mae=round(b.mae, 3),
                rmse=round(b.rmse, 3),
                mape=round(b.mape, 2),
                skill=round(b.skill, 3),
            )
            for b in report.blocks
        ],
    )


def _performance_from_json(
    site_id: str, resolution: str, payload: dict[str, object]
) -> PerformanceResponse:
    """Petakan laporan `report.json` (dari scripts/run_evaluation.py) ke kontrak §5.5.

    Sumber kebenaran konteks:
    - ``evaluated_at`` = ``generated_at`` laporan — STAMPEL WAKTU BACKTEST
      ASLI, bukan waktu request. Ini yang membuat "run terakhir" bermakna.
    - ``data_rows`` = ``n_rows`` laporan — jumlah baris dataset yang dievaluasi
      (berbeda dari ``n_samples`` = jumlah origin forecast).
    """
    thresholds = dict(payload.get("thresholds", {}))
    blocks = payload.get("blocks", [])
    evaluated_at: datetime | None = None
    if payload.get("generated_at"):
        try:
            evaluated_at = datetime.fromisoformat(str(payload["generated_at"]))
        except ValueError:
            evaluated_at = None
        # report.json lama menyimpan waktu naive. Kontrak §1.5 melarang
        # timestamp tanpa offset — lokalisisasi eksplisit ke zona site
        # (bukan menebak; script evaluasi jalan di zona site).
        if evaluated_at is not None and evaluated_at.tzinfo is None:
            evaluated_at = evaluated_at.replace(tzinfo=ZoneInfo("Asia/Makassar"))
    return PerformanceResponse(
        site_id=site_id,
        target="irradiance",
        resolution=resolution,
        unit="W/m2",
        model_version=current_model_version(),
        evaluated_at=evaluated_at,
        data_rows=int(payload.get("n_rows", 0)),
        evaluation_range={
            "from": datetime.fromisoformat(str(payload["holdout_start"])),
            "to": datetime.fromisoformat(str(payload["holdout_end"])),
        },
        n_samples=int(payload.get("n_origins", 0)),
        thresholds=ThresholdsResponse(
            max_mae=float(thresholds.get("max_mae", ev.DEFAULT_THRESHOLDS.max_mae)),
            max_mape=float(thresholds.get("max_mape", ev.DEFAULT_THRESHOLDS.max_mape)),
            max_mape_long=float(
                thresholds.get("max_mape_long", ev.DEFAULT_THRESHOLDS.max_mape_long)
            ),
            min_skill=float(thresholds.get("min_skill", ev.DEFAULT_THRESHOLDS.min_skill)),
            primary_horizon=int(
                thresholds.get("primary_horizon", ev.DEFAULT_THRESHOLDS.primary_horizon)
            ),
        ),
        status=str(payload.get("status", "UNKNOWN")),  # type: ignore[arg-type]
        failures=[str(f) for f in payload.get("failures", [])],
        horizons=[
            HorizonMetrics(
                horizon=int(b["horizon"]),
                mae=round(float(b["mae"]), 3),
                rmse=round(float(b["rmse"]), 3),
                mape=round(float(b["mape"]), 2),
                skill=round(float(b["skill"]), 3),
            )
            for b in blocks
        ],
    )


def _thresholds_response(th: ev.Thresholds) -> ThresholdsResponse:
    return ThresholdsResponse(
        max_mae=th.max_mae,
        max_mape=th.max_mape,
        max_mape_long=th.max_mape_long,
        min_skill=th.min_skill,
        primary_horizon=th.primary_horizon,
    )
