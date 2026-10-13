"""Jalankan backtest pada data proyek, cetak hasil, simpan laporan ke file.

    python scripts/run_evaluation.py [--csv PATH] [--out PATH] [--holdout N]

Fungsi:
- Menghitung metrik sesuai STRATEGI_EVALUASI.md (holdout terakhir, stride 1).
- Mencetak ringkasan GO/NO-GO ke stdout (script, bukan modul — modul dilarang print).
- Menyimpan laporan ke `data/evaluation/report.json` supaya endpoint
  `GET /api/v1/model/performance` bisa mengisinya (lihat app/api/model.py).

Satu-satunya tempat yang boleh membaca CSV mentah — modul `app/*` tidak.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import evaluation as ev  # noqa: E402

DEFAULT_CSV = (
    ROOT.parent
    / "Data-20261001T082404Z-1-001"
    / "Data"
    / "Processed"
    / "B1_daily_master_2016_2026.csv"
)
DEFAULT_OUT = ROOT / "data" / "evaluation" / "report.json"


def load_target_frame(csv_path: Path) -> pd.DataFrame:
    """Baca CSV proyek dan pilih kolom target dengan satuan yang benar.

    `ghi_solcast_mean` (W/m²) dipakai sebagai `irradiance` — bukan
    `ghi_cams`, karena CAMS harian berunit Wh/m² per hari (lihat
    SPESIFIKASI_KONTRAK_MODEL.md §4.3).
    """
    frame = pd.read_csv(csv_path, parse_dates=["tanggal"]).set_index("tanggal")
    frame = frame.sort_index()
    frame = frame.rename(
        columns={"ghi_solcast_mean": "irradiance", "air_temp_mean": "temperature"}
    )
    keep = [c for c in ("irradiance", "temperature", "tcc") if c in frame.columns]
    frame = frame[keep].dropna(subset=["irradiance", "temperature"])
    frame.index = frame.index.tz_localize("Asia/Makassar")
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--holdout", type=int, default=365)
    parser.add_argument("--stride", type=int, default=1)
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"ERROR: CSV tidak ditemukan: {args.csv}")
        return 2

    frame = load_target_frame(args.csv)
    report = ev.rolling_backtest(
        frame,
        target="irradiance",
        horizons=ev.DEFAULT_HORIZONS,
        holdout=args.holdout,
        stride=args.stride,
        order=(1, 0, 2),
    )

    print("=" * 78)
    print(f"ROLLING BACKTEST  target={report.target}  order=ARIMA{report.order}")
    print(
        f"rows={report.n_rows}  holdout={report.holdout} "
        f"({report.holdout_start.date()} -> {report.holdout_end.date()})  "
        f"origins={report.n_origins}  stride={report.stride}"
    )
    print("=" * 78)
    print(f"{'h':>4} {'MAE':>9} {'MAPE%':>8} {'RMSE':>9} {'baseMAE':>9} {'skill':>8}")
    for b in report.blocks:
        print(
            f"{b.horizon:>4} {b.mae:>9.3f} {b.mape:>8.2f} {b.rmse:>9.3f} "
            f"{b.baseline_mae:>9.3f} {b.skill:>8.3f}"
        )
    print("-" * 78)
    th = report.thresholds
    print(
        f"AMBANG: MAE<={th.max_mae}  MAPE<={th.max_mape}%  "
        f"MAPE({th.primary_horizon}h+)<={th.max_mape_long}%  skill>={th.min_skill}"
    )
    print(f"STATUS : {'GO' if report.passed else 'NO-GO'}")
    for f in report.failures:
        print(f"  GAGAL: {f}")

    # Zona site eksplisit (kontrak §1.5) — timestamp naive di report.json
    # membuat API gagal memetakannya ke evaluated_at, dan "run terakhir"
    # di dashboard tidak punya offset. Jalankan script ini ulang untuk
    # menyegarkan laporan lama.
    now = datetime.now(tz=ZoneInfo("Asia/Makassar"))

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "source_csv": str(args.csv),
        "n_rows": report.n_rows,
        "n_origins": report.n_origins,
        "holdout": report.holdout,
        "holdout_start": report.holdout_start.isoformat(),
        "holdout_end": report.holdout_end.isoformat(),
        "order": list(report.order),
        "thresholds": asdict(report.thresholds),
        "status": "GO" if report.passed else "NO_GO",
        "failures": list(report.failures),
        "blocks": [asdict(b) for b in report.blocks],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nLaporan tersimpan: {args.out}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
