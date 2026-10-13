"""Uji model/VERSION sebagai SATU-SATUNYA sumber kebenaran versi.

    python tests/test_version.py

Dua hal yang dibuktikan:

1. ``model/VERSION`` terbaca dan menjadi nilai versi di seluruh API.
2. Tidak ada file lain yang masih menyimpan literal versi.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import version as ver  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def main() -> int:
    print("\n[1] FILE TERBACA")
    check(ver.VERSION_FILE.exists(), f"model/VERSION ada ({ver.VERSION_FILE.name})")
    first_line = ver.VERSION_FILE.read_text(encoding="utf-8").splitlines()[0].strip()
    check(bool(first_line), f"baris pertama berisi versi: '{first_line}'")
    version = ver.model_version()
    check(version == first_line, f"model_version() == baris file ({version})")
    check(ver.SEMVER_PATTERN.match(version) is not None, "berformat semver")
    check(ver.model_version() == version, "hasil konsisten antar pemanggilan (cached)")

    print("\n[2] METADATA IKUT TERBACA")
    meta = ver.metadata()
    check("evaluation_status" in meta, f"evaluation_status={meta.get('evaluation_status')}")
    check("order" in meta, f"order={meta.get('order')}")
    check(ver.field("algorithm") == "ARIMA", "field('algorithm') == ARIMA")
    check(ver.field("tidak_ada", "x") == "x", "default bila key tak ada")
    check(ver.load_version()[0] == version, "load_version() selaras dengan model_version()")

    print("\n[3] SATU SUMBER — tidak ada literal versi di file lain")
    # test_version.py sendiri dikecualikan: literal di dalamnya adalah CONTOH
    # pola semver untuk diuji, bukan versi yang dipakai.
    literal_re = re.compile(r'"v\d+\.\d+(\.\d+)?(-[\w.]+)?"')
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if path.name in {"version.py", "test_version.py"} or ".pytest_cache" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in literal_re.finditer(text):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    check(offenders == [], f"tidak ada hardcode versi (ditemukan: {offenders})")

    print("\n[4] ENGIN BUKAN SUMBER VERSI")
    engine_src = (ROOT / "app" / "engine.py").read_text(encoding="utf-8")
    check("MODEL_VERSION" not in engine_src.split('"""')[0],
          "engine tidak mendefinisikan MODEL_VERSION di bagian ekspor")
    check(not re.search(r'^MODEL_VERSION\s*=', engine_src, re.M),
          "engine tidak punya assignment MODEL_VERSION")

    print("\n[5] VALIDASI — file rusak tertangkap")
    # Modek: versi bukan semver -> VersionFileError.
    backup = ver.VERSION_FILE.read_text(encoding="utf-8")
    try:
        ver.VERSION_FILE.write_text("bukan-versi\nalgorithm=ARIMA\n", encoding="utf-8")
        ver.load_version.cache_clear()
        raised = False
        try:
            ver.load_version()
        except ver.VersionFileError:
            raised = True
        check(raised, "versi non-semver -> VersionFileError")

        ver.VERSION_FILE.write_text("#v1.0\n", encoding="utf-8")
        ver.load_version.cache_clear()
        raised = False
        try:
            ver.load_version()
        except ver.VersionFileError:
            raised = True
        check(raised, "file hanya komentar -> VersionFileError")

        ver.VERSION_FILE.write_text("", encoding="utf-8")
        ver.load_version.cache_clear()
        raised = False
        try:
            ver.load_version()
        except ver.VersionFileError:
            raised = True
        check(raised, "file kosong -> VersionFileError")
    finally:
        ver.VERSION_FILE.write_text(backup, encoding="utf-8")
        ver.load_version.cache_clear()

    # Terakhir: kembalikan bersih sebelum laporan ditutup.
    check(ver.model_version() == first_line,
          f"file dipulihkan, versi kembali {ver.model_version()}")

    print("\n[6] POLA SEMVER SEARAH KONTRAK & SCHEMA")
    from app.schemas import SEMVER as SCHEMA_PATTERN

    check(SCHEMA_PATTERN == ver.SEMVER_PATTERN.pattern,
          f"skema Pydantic == pola version ({SCHEMA_PATTERN})")
    check(re.match(SCHEMA_PATTERN, ver.model_version()) is not None,
          f"versi file diterima skema ({ver.model_version()})")
    # v1.0 dan v1.0.0 dua-duanya sah.
    check(re.match(SCHEMA_PATTERN, "v1.0") is not None, "v1.0 diterima")
    check(re.match(SCHEMA_PATTERN, "v1.0.0") is not None, "v1.0.0 diterima")
    check(re.match(SCHEMA_PATTERN, "1.0") is None, "tanpa 'v' ditolak")

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("model/VERSION = SATU-SATUNYA SUMBER KEBENARAN VERSI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
