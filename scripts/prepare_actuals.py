"""Siapkan `data/actuals.csv` dari CSV proyek — untuk endpoint /forecast/actual.

    python scripts/prepare_actuals.py [--src PATH] [--out PATH]

Satu-satunya yang menyalin data aktual dari luar folder service ke dalam
`data/actuals.csv`. Konsekuensi aturan "browser tidak boleh membaca CSV":

- `app/actuals.py` hanya membaca `data/actuals.csv` (di dalam repo service).
- Path CSV proyek TIDAK PERNAH muncul di respons API — client cukup tahu
  nama berkas lewat field `source`.
- Bila file ini tidak ada, endpoint membalas 503 dengan pesan menyuruh
  menjalankan script ini (bukan `points:[]` yang menyesatkan).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_SRC = (
    ROOT.parent
    / "Data-20261001T082404Z-1-001"
    / "Data"
    / "Processed"
    / "B1_daily_master_2016_2026.csv"
)
DEFAULT_OUT = ROOT / "data" / "actuals.csv"

#: Kolom aktual irradiance harian — W/m² (solcast), BUKAN `ghi_cams`
#: (Wh/m² per hari) — lihat SPESIFIKASI_KONTRAK_MODEL.md §4.3.
SOURCE_COLUMN = "ghi_solcast_mean"
TARGET_COLUMN = "actual_irradiance"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.src.exists():
        print(f"ERROR: CSV proyek tidak ditemukan: {args.src}")
        return 2

    frame = pd.read_csv(args.src, parse_dates=["tanggal"])
    if SOURCE_COLUMN not in frame.columns:
        print(f"ERROR: kolom '{SOURCE_COLUMN}' tidak ada; kolom: {list(frame.columns)}")
        return 2

    out = frame[["tanggal", SOURCE_COLUMN]].dropna(subset=[SOURCE_COLUMN])
    out = out.rename(columns={SOURCE_COLUMN: TARGET_COLUMN})
    out = out.sort_values("tanggal")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out, index=False, date_format="%Y-%m-%d")

    print(f"Actuals tersimpan: {args.out.relative_to(ROOT)}")
    print(f"  {len(out)} baris  {out['tanggal'].min().date()} -> {out['tanggal'].max().date()}")
    print(f"  kolon: tanggal,{TARGET_COLUMN} (W/m²)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
