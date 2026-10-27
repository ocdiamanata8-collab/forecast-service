"""Uji kunci versi requirements.txt.

    python tests/test_requirements_lock.py

Tiga pemeriksaan, semuanya jalan di lingkungan mana pun (tidak memeriksa
versi lingkungan aktif — itu tugas lingkungan build):

1. Semua baris paket memakai ``==`` (terkunci), tanpa ``>=``/``~=``.
2. Setiap paket pihak ketiga yang DIIMPOR kode ada di requirements.txt.
3. Tidak ada paket di requirements.txt yang tidak dipakai kode.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []

#: Modul standar Python yang tidak perlu ada di requirements.
STDLIB = {
    "abc", "argparse", "asyncio", "base64", "builtins", "contextlib",
    "dataclasses", "datetime", "functools", "gzip", "hashlib", "io", "json", "logging",
    "math", "os", "pathlib", "pickle", "re", "secrets", "shutil", "subprocess",
    "sys", "tempfile", "threading", "time", "typing", "urllib", "uuid",
    "warnings", "zoneinfo", "__future__",
}


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def parse_requirements() -> dict[str, str]:
    """Kembalikan {nama_paket_tanpa_extra: pin} dari requirements.txt."""
    reqs: dict[str, str] = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # buang extra seperti uvicorn[standard]
        name = re.split(r"[\[<>=!~\s]", line)[0]
        reqs[name.lower().replace("_", "-")] = line
    return reqs


def collect_imports() -> set[str]:
    """Paket pihak ketiga yang benar-benar diimpor kode aplikasi."""
    packages: set[str] = set()
    targets = list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(
            r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_]*)", text, re.M
        ):
            top = match.group(1)
            if top in STDLIB or top in {"app", "tests", "scripts"}:
                continue
            packages.add(top.lower().replace("_", "-"))
    return packages


def main() -> int:
    reqs = parse_requirements()

    print("\n[1] SEMUA BARIS TERKUNCI DENGAN ==")
    unlocked = [
        line for line in reqs.values()
        if not re.match(r"^[A-Za-z0-9_.-]+(\[[^\]]+\])?==[0-9][^\s]*$", line)
    ]
    check(unlocked == [], f"tidak ada pin longgar (ditemukan: {unlocked})")
    check(len(reqs) >= 5, f"minimal 5 paket terkunci (dapat {len(reqs)})")

    print("\n[2] PAKET YANG DIIMPOR ADA DI requirements.txt")
    # sklearn/joblib dipakai opsional di proyek lain; di service ini tidak.
    imported = collect_imports()
    # 'starlette' diimpor hanya untuk tipe dalam FastAPI (transitive fastapi).
    missing = sorted(imported - set(reqs) - {"starlette", "pytest"})
    check(missing == [], f"semua impor terkunci (hilang: {missing})")

    print("\n[3] TIDAK ADA PAKET YANG TIDAK DIPAKAI")
    # Diizinkan tanpa diimpor langsung:
    #   uvicorn  -> dipanggil sebagai bin (app.main / Dockerfile CMD)
    #   pytest / httpx -> hanya untuk test
    #   scipy    -> transitive statsmodels, dipin sengaja (lihat [4])
    directly_imported = imported - {"scipy"}
    unused = sorted(set(reqs) - directly_imported - {"pytest", "httpx", "uvicorn", "scipy"})
    check(unused == [], f"tidak ada dependensi menganggur (ditemukan: {unused})")

    print("\n[4] DEPENDENSI KRITIS TIDAK BOLEH KELUAR")
    # scipy harus ikut terkunci: statsmodels 0.14.4 pecah pada scipy>=1.17
    # (argumen `disp` dihapus dari fmin_l_bfgs_b) — insiden yang sudah terjadi.
    check("scipy" in reqs, f"scipy terkunci (ditemukan: {reqs.get('scipy', 'HILANG')})")
    check("statsmodels" in reqs, "statsmodels terkunci (pemanggil ARIMA)")
    check("pandas" in reqs and "numpy" in reqs, "pandas & numpy terkunci")

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("requirements.txt TERKUNCI DAN SELARAS KODE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
