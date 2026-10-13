"""Ekspor model referensi + artefak fitur ke ``model/``.

    python scripts/export_model.py [--csv PATH]

Menghasilkan DUA berkas:

- ``model/model.pkl``      — hasil fit ARIMA pada data kanonik (snapshot).
                             ARIMA tidak punya bobot tersimpan di parameter;
                             pickle ini menyimpan objek hasil fit lengkap,
                             dipakai untuk inspeksi/reproduksi, bukan untuk
                             prediksi runtime.
- ``model/artifacts.json`` — fitur, URUTAN KOLOM, skala data, lingkup
                             pen-trainingan. Dibaca API (``app/artifacts.py``)
                             sehingga API TIDAK PERNAH menebak urutan kolom.

Runtime tetap melakukan refit per request (syarat parity dengan skrip —
lihat tests/test_module_parity.py). Snapshot di sini adalah artefak
training, bukan berkas yang dimuat engine.

Satu-satunya tempat yang memuat/menulis ``model.pkl``.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import warnings
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import engine  # noqa: E402
from app.preprocessing import DEFAULT_TIMEZONE  # noqa: E402
from scripts.run_evaluation import DEFAULT_CSV, load_target_frame  # noqa: E402

MODEL_DIR = ROOT / "model"
SCHEMA_VERSION = 1


def build_artifacts(frame: pd.DataFrame, target: str) -> dict[str, object]:
    """Susun metadata fitur — struktur dibaca oleh ``app/artifacts.py``."""
    columns = list(frame.columns)
    if target not in columns:
        raise ValueError(f"target '{target}' tidak ada di frame: {columns}")

    # Urutan kolom EKSPISIT: di sinilah API belajar urutan training.
    # Exog = semua kolom non-target, urutannya sama persis seperti saat fit.
    exog_columns = [c for c in columns if c != target]

    return {
        "schema_version": SCHEMA_VERSION,
        "model_artifact": "model.pkl",
        "target": target,
        "resolution": "daily",
        "timezone": DEFAULT_TIMEZONE,
        # Urutan kolom frame saat training — sumber kebenaran urutan fitur.
        "columns": columns,
        # Urutan exog yang harus dipakai API (subset non-target, urut sama).
        "exog_columns": exog_columns,
        "feature_names": exog_columns,  # alias eksplisit untuk konsumen
        "dtypes": {c: str(frame[c].dtype) for c in columns},
        # Skala data. Saat ini model bekerja pada satuan asli (tanpa
        # normalisasi) — ``method`` null. Bila nanti ada normalisasi, isi
        # mean/std per kolom di sini dan API akan menerapkannya SEBELUM fit.
        "scaling": {
            "method": None,
            "params": {},
        },
        "history_min_rows": engine.MIN_HISTORY,
        "horizon_max": engine.MAX_HORIZON,
        "order": list(engine.MODEL_ORDERS["WITA"]),
        "training_rows": int(len(frame)),
        "training_range": {
            "from": frame.index[0].isoformat(),
            "to": frame.index[-1].isoformat(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--target", default="irradiance")
    parser.add_argument("--out-model", type=Path, default=MODEL_DIR / "model.pkl")
    parser.add_argument("--out-artifacts", type=Path, default=MODEL_DIR / "artifacts.json")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"ERROR: CSV tidak ditemukan: {args.csv}")
        return 2

    frame = load_target_frame(args.csv)
    artifacts = build_artifacts(frame, args.target)

    # Fit snapshot pada data penuh (order WITA, default statsmodels).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from statsmodels.tsa.arima.model import ARIMA

        series = frame[args.target].astype("float64")
        fitted = ARIMA(series, order=engine.DEFAULT_ORDER).fit()

    args.out_model.parent.mkdir(parents=True, exist_ok=True)
    with args.out_model.open("wb") as fh:
        pickle.dump(
            {
                "model_type": "ARIMA",
                "order": engine.DEFAULT_ORDER,
                "fitted_results": fitted,
                "artifacts": artifacts,
            },
            fh,
        )

    args.out_artifacts.write_text(
        json.dumps(artifacts, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("Artefak tersimpan:")
    print(f"  {args.out_model.relative_to(ROOT)}   ({args.out_model.stat().st_size:,} bytes)")
    print(f"  {args.out_artifacts.relative_to(ROOT)}")
    print(f"  columns     : {artifacts['columns']}")
    print(f"  exog_columns: {artifacts['exog_columns']}")
    print(f"  scaling     : {artifacts['scaling']['method']}")
    print(f"  order       : {artifacts['order']}  rows={artifacts['training_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
