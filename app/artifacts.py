"""Pembaca artefak model — sumber kebenaran urutan fitur, skala, dan lingkup.

Sumber file: ``model/artifacts.json`` (ditulis ``scripts/export_model.py``).

Mengapa API HARUS membaca urutan kolom dari file, bukan menebak
----------------------------------------------------------------
``generate_forecast`` menerima ``weather_data`` sebagai DataFrame. Bila API
membangun frame dari request client, urutan kolom ikut urutan JSON client —
dan statsmodels menempatkan exog sesuai **posisi**, bukan nama. Akibatnya
kolom dapat tertukar diam-diam: forecast tetap keluar, angka tetap
kelihatan masuk akal, tapi menghitung dengan fitur yang salah. Inilah
penyebab paling umum "hasil API aneh" yang tidak pernah menghasilkan error.

Aturan yang ditegakkan sini:

1. ``exog_columns`` = urutan training — SATU-SATUNYA urutan yang valid.
2. Permintaan exog dengan kolom di luar daftar ditolak (bukan diabaikan).
3. Permintaan exog dengan urutan berbeda **tidak masalah**: frame dibentuk
   ulang mengikuti ``exog_columns``, jadi posisi selalu benar.
4. ``scaling.method`` null = model bekerja pada satuan asli; bila kelak ada
   normalisasi, ``apply_scaling()`` menerapkannya sebelum fit supaya skala
   request = skala training.

Pembaca ini adalah SATU-SATUNYA yang membaca ``model/artifacts.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

ARTIFACTS_FILE = Path(__file__).resolve().parents[1] / "model" / "artifacts.json"

#: Skema yang dipahami pembaca ini; file dengan nomor lebih baru ditolak
#: (bukan ditebak) agar perubahan skema eksplisit dan teruji.
SUPPORTED_SCHEMA = 1

__all__ = [
    "ARTIFACTS_FILE",
    "ArtifactError",
    "Artifacts",
    "load_artifacts",
    "reorder_exog_frame",
]


class ArtifactError(RuntimeError):
    """Artefak model hilang atau tidak konsisten."""


@dataclass(frozen=True)
class Artifacts:
    """Artefak terurai — immutable supaya tidak berubah selama proses."""

    target: str
    resolution: str
    timezone: str
    columns: tuple[str, ...]
    exog_columns: tuple[str, ...]
    dtypes: dict[str, str]
    scaling_method: str | None
    scaling_params: dict[str, dict[str, float]]
    history_min_rows: int
    horizon_max: int
    order: tuple[int, int, int]
    training_rows: int

    @property
    def features(self) -> tuple[str, ...]:
        """Alias eksplisit untuk konsumen yang mencari nama ini."""
        return self.exog_columns

    def requires_scaling(self) -> bool:
        return self.scaling_method is not None


def _fail(message: str) -> ArtifactError:
    return ArtifactError(
        f"{message} ({ARTIFACTS_FILE}). API menolak menebak urutan kolom — "
        "jalankan scripts/export_model.py untuk membangun ulang artefak."
    )


@lru_cache(maxsize=1)
def load_artifacts() -> Artifacts:
    """Baca dan validasi ``model/artifacts.json`` sekali, lalu cache."""
    if not ARTIFACTS_FILE.exists():
        raise _fail("artefak model tidak ditemukan")

    try:
        raw = json.loads(ARTIFACTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _fail(f"artefak model bukan JSON valid: {exc}") from exc

    schema = raw.get("schema_version")
    if schema != SUPPORTED_SCHEMA:
        raise _fail(f"schema_version={schema}, didukung {SUPPORTED_SCHEMA}")

    def _req(key: str) -> object:
        if key not in raw:
            raise _fail(f"kunci '{key}' hilang")
        return raw[key]

    columns = tuple(str(c) for c in _req("columns"))
    exog_columns = tuple(str(c) for c in _req("exog_columns"))
    target = str(_req("target"))

    if target not in columns:
        raise _fail(f"target '{target}' tidak ada di columns")
    if tuple(c for c in columns if c != target) != exog_columns:
        raise _fail("exog_columns harus berupa columns tanpa target, urutan sama")
    if len(set(columns)) != len(columns):
        raise _fail("columns mengandung duplikat")

    scaling = raw.get("scaling", {}) or {}
    method = scaling.get("method")
    params_raw = scaling.get("params", {}) or {}
    params = {
        str(k): {str(sk): float(sv) for sk, sv in v.items()}
        for k, v in params_raw.items()
    }
    if method is not None and method not in {"standard"}:
        raise _fail(f"scaling.method '{method}' tidak dikenal")
    if method == "standard":
        missing = [c for c in columns if c not in params]
        if missing:
            raise _fail(f"scaling.params tidak lengkap untuk {missing}")
        for col, p in params.items():
            if float(p.get("std", 0.0)) <= 0:
                raise _fail(f"scaling std tidak valid untuk '{col}'")

    try:
        order = tuple(int(x) for x in _req("order"))
    except (TypeError, ValueError) as exc:
        raise _fail(f"order tidak berupa [p, d, q]: {exc}") from exc
    if len(order) != 3:
        raise _fail("order harus 3 bilangan")

    return Artifacts(
        target=target,
        resolution=str(raw.get("resolution", "daily")),
        timezone=str(_req("timezone")),
        columns=columns,
        exog_columns=exog_columns,
        dtypes={str(k): str(v) for k, v in (raw.get("dtypes") or {}).items()},
        scaling_method=method,
        scaling_params=params,
        history_min_rows=int(raw.get("history_min_rows", 30)),
        horizon_max=int(raw.get("horizon_max", 30)),
        order=order,
        training_rows=int(raw.get("training_rows", 0)),
    )


def reorder_exog_frame(
    requested_columns: Sequence[str],
    values_by_column: dict[str, Iterable[float]],
    artifacts: Artifacts | None = None,
) -> pd.DataFrame:
    """Bentuk frame exog dengan URUTAN TRAINING, bukan urutan permintaan.

    Parameters
    ----------
    requested_columns
        Kolom yang dikirim client (urutannya bebas).
    values_by_column
        Nilai per kolom, diambil dari payload.

    Returns
    -------
    DataFrame berkolom ``artifacts.exog_columns`` (urutan terdefinisi).

    Raises
    ------
    ArtifactError
        Bila ada kolom minta di luar daftar training — ditolak eksplisit
        agar fitur tak dikenal tidak diam-diam tergeser ke posisi lain.
    """
    arts = artifacts or load_artifacts()
    requested = list(requested_columns)

    unknown = [c for c in requested if c not in arts.exog_columns]
    if unknown:
        raise _fail(
            f"permintaan exog memuat kolom di luar artefak: {unknown}; "
            f"yang diizinkan: {list(arts.exog_columns)}"
        )
    missing = [c for c in arts.exog_columns if c not in requested]
    if missing:
        raise _fail(
            f"permintaan exog tidak lengkap: hilang {missing} — "
            "semua fitur training wajib dikirim"
        )

    data: dict[str, list[float]] = {}
    for col in arts.exog_columns:  # iterasi menurut urutan training
        data[col] = list(values_by_column[col])
    return pd.DataFrame(data, columns=list(arts.exog_columns))


def apply_scaling(
    frame: pd.DataFrame, artifacts: Artifacts | None = None
) -> pd.DataFrame:
    """Terapkan skala training ke frame request.

    Dengan ``scaling.method == null`` (kondisi sekarang) fungsi ini adalah
    identitas — dipanggil tetap, supaya penambahan normalisasi nanti tidak
    perlu menyentuh endpoint.
    """
    arts = artifacts or load_artifacts()
    if not arts.requires_scaling():
        return frame

    out = frame.copy()
    for col in arts.columns:
        if col not in out.columns:
            continue
        params = arts.scaling_params[col]
        mean, std = float(params["mean"]), float(params["std"])
        if arts.scaling_method == "standard":
            out[col] = (out[col].astype("float64") - mean) / std
    return out
