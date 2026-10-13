"""Simulasi isi image Docker tanpa Docker.

Salin persis berkas yang di-COPY Dockerfile ke folder terpisah, jalankan
aplikasi dari sana dengan CWD = folder itu. Tujuan: membuktikan aplikasi
tidak membaca file di luar konteks build.

    python scripts/_image_sim.py [--prepare|--clean]
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Salinan ini memakai path yang sama dengan di Dockerfile (WORKDIR /srv).
# Nama folder di sini berbeda, tapi isi identik.
COPY_DIRS = ["app", "model", "templates", "data", "scripts"]
COPY_FILES = ["requirements.txt"]
SIM_ROOT = Path(tempfile.gettempdir()) / "forecast_svc_sim"


def prepare() -> None:
    if SIM_ROOT.exists():
        shutil.rmtree(SIM_ROOT)
    SIM_ROOT.mkdir(parents=True)
    # ignore_patterns mengikuti .dockerignore: model.pkl, __pycache__,
    # .pytest_cache dikeluarkan bahkan dari folder yang di-COPY.
    for d in COPY_DIRS:
        shutil.copytree(ROOT / d, SIM_ROOT / d,
                        ignore=shutil.ignore_patterns("model.pkl", "__pycache__",
                                                      ".pytest_cache"))
    for f in COPY_FILES:
        shutil.copy2(ROOT / f, SIM_ROOT / f)

    print(f"Folder simulasi: {SIM_ROOT}")
    print("Isi:", ", ".join(sorted(p.name for p in SIM_ROOT.iterdir())))
    pkl = SIM_ROOT / "model" / "model.pkl"
    print(f"model.pkl ikut image? {pkl.exists()}  (harusnya False)")
    print(f".env ikut? {(SIM_ROOT / '.env').exists()}  (harusnya False)")
    print(f".git ikut? {(SIM_ROOT / '.git').exists()}  (harusnya False)")
    print(f"templates/index.html ikut? {(SIM_ROOT / 'templates' / 'index.html').exists()}  (harusnya True)")
    print(f"data/actuals.csv ikut? {(SIM_ROOT / 'data' / 'actuals.csv').exists()}  (harusnya True)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "--prepare"
    if cmd == "--prepare":
        prepare()
    elif cmd == "--clean":
        shutil.rmtree(SIM_ROOT, ignore_errors=True)
        print("dibersihkan")
    else:
        print("pakai: --prepare | --clean")
        raise SystemExit(1)
