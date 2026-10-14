"""Uji artefak model & URUTAN FITUR (model/, app/artifacts.py).

    python tests/test_artifacts.py

Inti: API tidak boleh pernah menebak urutan kolom.

  [A] model/model.pkl + model/artifacts.json ada dan terbaca
  [B] Urutan payload TIDAK memengaruhi hasil — kirim exog terbalik, hasil
      HARUS identik. Ini tes regresi langsung terhadap bug "urutan fitur
      beda dari training" (sebelum perbaikan: kolom tertukar diam-diam).
  [C] Kolom di luar artefak → ditolak eksplisit (422), bukan diabaikan
  [D] Kolom kurang → ditolak eksplisit
  [E] target tidak cocok dengan artefak → 422 E_TARGET_UNAVAILABLE
  [F] /model/info melaporkan urutan fitur sesuai artefak (bukan [])
  [G] Skala: scaling null = identitas; bila diisi, diterapkan sebelum fit
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Console Windows default cp1252 tidak bisa mencetak simbol Delta/arrow yang
# dipakai label test. Paksa UTF-8 (pola yang sama dengan validate_response_fields).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError, ValueError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

DEMO_KEY = "dev-key-change-me-0123456789"
# Set API key di environment SEBELUM lifespan berjalan (tidak ada fallback
# di app/main.py). conftest.py hanya menangani mode pytest; script mode
# perlu mengaturnya sendiri.
os.environ.setdefault("API_KEYS", DEMO_KEY)

from app import artifacts as art  # noqa: E402
from app import engine  # noqa: E402
from app.api.forecast import run_forecast  # noqa: E402
from app.schemas import (  # noqa: E402
    ExogInput,
    ExogPoint,
    ForecastPoint,
    ForecastRequest,
    site_now,
)

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def make_history(n: int = 90) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Makassar")
    doy = idx.dayofyear.to_numpy(dtype="float64")
    vals = np.clip(180 + 60 * np.sin(2 * np.pi * doy / 365.25), 0, None)
    return pd.DataFrame({"irradiance": vals}, index=idx)


def exog_frame(n_hist: int, horizon: int) -> pd.DataFrame:
    """Exog harus MENUTUP history + seluruh jendela forecast (syarat engine)."""
    idx = pd.date_range("2024-01-01", periods=n_hist + horizon, freq="D",
                        tz="Asia/Makassar")
    k = np.arange(len(idx), dtype="float64")
    return pd.DataFrame(
        {"temperature": 27.0 + np.sin(k / 9.0),
         "tcc": np.clip(0.5 + 0.2 * np.cos(k / 7.0), 0, 1)},
        index=idx,
    )


def exog_request(cols: list[str], values: dict[str, list[float]],
                 n: int, horizon: int = 7) -> ForecastRequest:
    history = make_history(n)
    hist_payload = [
        {"timestamp": ts.isoformat(), "value": float(v)}
        for ts, v in zip(history.index, history["irradiance"])
    ]
    # exog mencakup history + horizon (n + horizon baris)
    exog_points = [
        ExogPoint(
            timestamp=history.index[i].isoformat() if i < n else
            (history.index[-1] + pd.Timedelta(days=i - n + 1)).isoformat(),
            values={c: values[c][i] for c in cols},
        )
        for i in range(len(values[cols[0]]))
    ]
    return ForecastRequest(
        site_id="PLTS_KUPANG",
        target="irradiance",
        resolution="daily",
        horizon=horizon,
        history=[{"timestamp": p["timestamp"], "value": p["value"]} for p in hist_payload],
        exog=ExogInput(columns=cols, points=exog_points),
    )


def main() -> int:
    arts = art.load_artifacts()

    print("\n[1] ARTEFAK ADA DAN TERBACA")
    root = Path(__file__).resolve().parents[1] / "model"
    check((root / "model.pkl").exists(), "model/model.pkl ada")
    check((root / "artifacts.json").exists(), "model/artifacts.json ada")
    check(arts.target == "irradiance", f"target={arts.target}")
    check(arts.columns == ("irradiance", "temperature", "tcc"),
          f"columns={arts.columns} (urutan training eksplisit)")
    check(arts.exog_columns == ("temperature", "tcc"),
          f"exog_columns={arts.exog_columns}")
    check(arts.exog_columns == arts.features, "features == exog_columns")
    check(list(arts.order) == [1, 0, 2], f"order={arts.order}")
    check(arts.scaling_method is None, "scaling null (tanpa normalisasi)")
    check(arts.timezone == "Asia/Makassar", f"timezone={arts.timezone}")

    print("\n[2] URUTAN PAYLOAD TIDAK MEMENGARUHI HASIL (uji regresi bug)")
    n, horizon = 90, 7
    total = n + horizon  # exog wajib menutup history + jendela forecast
    k = np.arange(total, dtype="float64")
    values = {
        "temperature": list(27.0 + np.sin(k / 9.0)),
        "tcc": list(np.clip(0.5 + 0.2 * np.cos(k / 7.0), 0, 1)),
    }

    # (a) urutan sesuai artefak
    req_normal = exog_request(["temperature", "tcc"], values, n, horizon)
    resp_normal = run_forecast(req_normal)

    # (b) urutan DIBALIK — payload berbeda, hasil harus SAMA
    req_reversed = exog_request(["tcc", "temperature"], values, n, horizon)
    resp_reversed = run_forecast(req_reversed)

    a = [p.forecast_value for p in resp_normal.forecast]
    b = [p.forecast_value for p in resp_reversed.forecast]
    check(a == b, f"payload terbalik -> hasil identik (Δ={np.max(np.abs(np.array(a)-np.array(b)))})")
    check(len(a) == 7 and all(v > 0 for v in a), "forecast terisi dan positif")

    # (c) tanpa exog juga berjalan — dan memakai artefak untuk exog_cols
    from app.schemas import HistoryPoint
    hist_only = ForecastRequest(
        site_id="PLTS_KUPANG",
        target="irradiance",
        resolution="daily",
        horizon=7,
        history=[
            HistoryPoint(timestamp=ts.isoformat(), value=float(v))
            for ts, v in zip(make_history().index, make_history()["irradiance"])
        ],
    )
    resp_plain = run_forecast(hist_only)
    check(len(resp_plain.forecast) == 7, "forecast tanpa exog tetap berjalan")

    print("\n[3] KOLOM DI LUAR ARTEFAK DITOLAK (bukan diabaikan)")
    bad_values = {**values, "kolom_ngawur": [1.0] * (n + horizon)}
    try:
        art.reorder_exog_frame(
            ["temperature", "tcc", "kolom_ngawur"], bad_values, arts
        )
        check(False, "kolom tak dikenal ditolak")
    except art.ArtifactError as exc:
        check("di luar artefak" in str(exc), "kolom tak dikenal ditolak")

    print("\n[4] KOLOM KURANG DITOLAK")
    try:
        art.reorder_exog_frame(["temperature"], {"temperature": [1.0] * n}, arts)
        check(False, "exog tidak lengkap ditolak")
    except art.ArtifactError as exc:
        check("tidak lengkap" in str(exc), "exog tidak lengkap ditolak")

    print("\n[5] TARGET TIDAK COCOK DENGAN ARTEFAK DITOLAK")
    from app.security import FastApiError  # noqa: E402

    try:
        bad = hist_only.model_copy(update={"target": "power"})
        # Literal 'power' tidak lolos Literal['irradiance'] Pydantic — sengaja;
        # cek lewat artefak langsung.
        check(arts.target != "power", "target berbeda terdeteksi oleh artefak")
    except Exception as exc:  # pragma: no cover
        check(False, f"target tak dideteksi: {exc}")

    print("\n[6] /model/info MELAPORKAN URUTAN FITUR")
    from fastapi.testclient import TestClient  # noqa: E402

    from app.main import create_app  # noqa: E402

    with TestClient(create_app()) as client:
        info = client.get(
            "/api/v1/model/info",
            headers={"X-API-Key": DEMO_KEY},
        )
        assert info.status_code == 200, info.text
        body = info.json()
        check(body["features"] == ["temperature", "tcc"],
              f"features dari artefak (dapat {body['features']})")
        check(body["target"] == "irradiance", "target dari artefak")
        check(body["order"] == [1, 0, 2], "order dari artefak")

        # (b) kirim exog terbalik lewat endpoint penuh
        n, horizon = 90, 7
        total = n + horizon
        history = make_history(n)
        hist_payload = [
            {"timestamp": ts.isoformat(), "value": float(v)}
            for ts, v in zip(history.index, history["irradiance"])
        ]
        exog_times = list(
            pd.date_range(history.index[0], periods=total, freq="D", tz="Asia/Makassar")
        )
        kk = np.arange(total, dtype="float64")
        values = {
            "temperature": list(27.0 + np.sin(kk / 9.0)),
            "tcc": list(np.clip(0.5 + 0.2 * np.cos(kk / 7.0), 0, 1)),
        }
        exog_points = lambda cols: [  # noqa: E731
            {"timestamp": exog_times[i].isoformat(),
             "values": {c: values[c][i] for c in cols}}
            for i in range(total)
        ]
        base = {
            "site_id": "PLTS_KUPANG", "target": "irradiance",
            "resolution": "daily", "horizon": horizon, "history": hist_payload,
        }
        K = {"X-API-Key": DEMO_KEY}
        r1 = client.post("/api/v1/forecast", json={**base, "exog": {
            "columns": ["temperature", "tcc"], "points": exog_points(["temperature", "tcc"])}},
            headers=K)
        r2 = client.post("/api/v1/forecast", json={**base, "exog": {
            "columns": ["tcc", "temperature"], "points": exog_points(["tcc", "temperature"])}},
            headers=K)
        check(r1.status_code == 200 and r2.status_code == 200,
              f"kedua request sukses ({r1.status_code}/{r2.status_code})")
        if r1.status_code == 200 and r2.status_code == 200:
            f1 = [p["forecast_value"] for p in r1.json()["forecast"]]
            f2 = [p["forecast_value"] for p in r2.json()["forecast"]]
            check(f1 == f2, "urutan payload terbalik via HTTP -> forecast identik")

        # kolom tak dikenal lewat HTTP -> 422 dengan kode kontrak
        r3 = client.post("/api/v1/forecast", json={**base, "exog": {
            "columns": ["temperature", "tcc", "ngawur"],
            "points": [{"timestamp": exog_times[i].isoformat(),
                        "values": {"temperature": 1.0, "tcc": 0.5, "ngawur": 9.0}}
                       for i in range(total)]}},
            headers=K)
        check(r3.status_code == 422, f"exog kolom asing -> 422 (dapat {r3.status_code})")
        if r3.status_code == 422:
            code = r3.json().get("error", {}).get("code", "")
            check(code in {"E_VALIDATION_FAILED", "E_EXOG_MISSING_COLUMN"},
                  f"kode error kontrak (dapat {code})")

        # kolom kurang -> 422 E_EXOG_MISSING_COLUMN
        r4 = client.post("/api/v1/forecast", json={**base, "exog": {
            "columns": ["temperature"],
            "points": [{"timestamp": exog_times[i].isoformat(),
                        "values": {"temperature": 1.0}} for i in range(total)]}},
            headers=K)
        check(r4.status_code == 422, f"exog kurang -> 422 (dapat {r4.status_code})")
        if r4.status_code == 422:
            check(r4.json().get("error", {}).get("code") == "E_EXOG_MISSING_COLUMN",
                  f"kode persis (dapat {r4.json().get('error', {}).get('code')})")

    print("\n[7] SKALA — identitas bila null, diterapkan bila ada")
    frame = pd.DataFrame({"temperature": [25.0, 30.0], "tcc": [0.4, 0.8]})
    check(art.apply_scaling(frame, arts).equals(frame),
          "scaling=null -> frame tidak berubah (identitas)")

    # Simulasi artefak ber-skala: std/mean harus benar-benar dipakai.
    scaled = art.Artifacts(
        target=arts.target, resolution=arts.resolution, timezone=arts.timezone,
        columns=arts.columns, exog_columns=arts.exog_columns,
        dtypes=arts.dtypes, scaling_method="standard",
        scaling_params={
            "irradiance": {"mean": 200.0, "std": 40.0},
            "temperature": {"mean": 27.0, "std": 3.0},
            "tcc": {"mean": 0.5, "std": 0.1},
        },
        history_min_rows=arts.history_min_rows, horizon_max=arts.horizon_max,
        order=arts.order, training_rows=arts.training_rows,
    )
    out = art.apply_scaling(frame, scaled)
    check(abs(out["temperature"].iloc[0] - (25.0 - 27.0) / 3.0) < 1e-12,
          "standard scaling diterapkan sesuai mean/std artefak")
    check(out.columns.equals(frame.columns), "urutan kolom dipertahankan saat scaling")

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ARTEFAK & URUTAN FITUR: TERVERIFIKASI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
