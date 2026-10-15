"""Verifikasi build context Dockerfile — pengganti `docker build` bila Docker absen.

    python scripts/check_docker_context.py

Docker tidak selalu tersedia (mis. di mesin dev Windows ini), tapi kegagalan
build container justru paling mahal ditemukan di server. Checker ini
memeriksa hal yang TIDAK bisa dilihat dari membaca Dockerfile:

  [1] Setiap path yang dibaca ``app/*`` saat runtime tercakup COPY.
      Empat path ini PERNAH terlewat dan merusak image:
        app/main.py            -> templates/index.html   (GET /  404)
        app/actuals.py         -> data/actuals.csv       (/forecast/actual 503)
        app/api/model.py       -> data/evaluation/report.json
        app/version.py         -> model/VERSION           (startup menagihnya)
  [2] requirements.txt ada dan semua pin `==` (wheel harus tersedia per versi).
  [3] File yang TIDAK boleh ter-bake ke image (.env, *.pem, kunci) tidak masuk
      konteks build.
  [4] Dockerfile tidak memakai `COPY . .` tanpa .dockerignore.

Ini PENGUAT, bukan pengganti: jalankan juga `docker build` bila Docker ada.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def parse_copies(dockerfile: str) -> list[tuple[str, str]]:
    """Kembalikan pasangan (sumber, tujuan) dari setiap baris COPY."""
    copies: list[tuple[str, str]] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or stripped.startswith("#"):
            continue
        # Buang flag --chown=... / --chmod=...
        tokens = [t for t in stripped.split()[1:] if not t.startswith("--")]
        if len(tokens) >= 2:
            copies.append((tokens[0], tokens[-1]))
    return copies


def runtime_paths() -> list[tuple[str, str]]:
    """(path, file pemakai) yang dibaca app/ saat runtime.

    Sumber: scan literal ``/ "dir" / "file"`` pada konstanta Path di app/.
    Diperbarui bila ada pemakaian file baru — tambahkan di sini.
    """
    return [
        ("model/VERSION", "app/version.py"),
        ("model/artifacts.json", "app/artifacts.py"),
        ("templates/index.html", "app/main.py"),
        ("static/monitoring-adapter.js", "app/main.py (mount /static; dashboard & monitoring import)"),
        ("data/actuals.csv", "app/actuals.py"),
        ("data/evaluation/report.json", "app/api/model.py"),
        ("requirements.txt", "Dockerfile (pip install)"),
    ]


def main() -> int:
    dockerfile = ROOT / "Dockerfile"
    if not dockerfile.exists():
        print("Dockerfile tidak ada")
        return 1
    text = dockerfile.read_text(encoding="utf-8")

    print("\n[1] Runtime path tercakup COPY")
    copies = parse_copies(text)
    print(f"    COPY ditemukan: {[(s, d) for s, d in copies]}")
    covered: list[str] = []
    for src, _ in copies:
        p = ROOT / src.rstrip("/")
        if p.is_dir():
            covered.extend(str(q.relative_to(ROOT)).replace("\\", "/")
                           for q in p.rglob("*") if q.is_file())
        elif p.is_file():
            covered.append(src.replace("\\", "/"))

    for path, user in runtime_paths():
        exists = (ROOT / path).exists()
        in_build = any(path.startswith(c.rstrip("/") + "/") or path == c.rstrip("/")
                       for c, _ in copies) or path in covered
        ok = exists and in_build
        detail = ""
        if not exists:
            detail = " (file tidak ada di disk)"
        elif not in_build:
            detail = f" (TIDAK dicopy — {user} akan rusak saat runtime)"
        check(ok, f"{path}{detail}")

    print("\n[2] requirements.txt ada & semua pin terkunci")
    req = ROOT / "requirements.txt"
    check(req.exists(), "requirements.txt ada")
    if req.exists():
        lines = [ln.strip() for ln in req.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        unlocked = [ln for ln in lines if "==" not in ln]
        check(not unlocked, f"semua paket pakai == (longgar: {unlocked})")
        check(len(lines) >= 5, f"minimal 5 paket terkunci (dapat {len(lines)})")

    print("\n[3] Konteks build bersih dari kredensial")
    dgi = ROOT / ".dockerignore"
    check(dgi.exists(), ".dockerignore ada")
    if dgi.exists():
        dgi_text = dgi.read_text(encoding="utf-8")
        for must in (".env", ".git", "__pycache__", ".venv", "*.key"):
            check(must in dgi_text, f".dockerignore mengabaikan {must}")
    # Tidak ada .env di folder service (sudah dibersihkan fase lalu).
    check(not (ROOT / ".env").exists(), "tidak ada .env di folder service")

    print("\n[4] Dockerfile tidak memakai COPY . .")
    blind_copy = any(src == "." for src, _ in copies)
    check(not blind_copy,
          "tidak ada COPY . . (COPY eksplisit mencegah konteks kotor ikut ter-bake)"
          if not blind_copy else "COPY . . terdeteksi — tambahkan .dockerignore menyeluruh")

    print("\n[5] Pin Python konsisten dengan requirements")
    from_match = re.search(r"^FROM\s+python:([\d.]+)", text, re.M)
    check(bool(from_match), f"FROM python ditemukan ({from_match.group(1) if from_match else '?'})")

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        print("\nJalankan `docker build -t forecast-service .` di mesin yang punya Docker.")
        return 1
    print("BUILD CONTEXT LENGKAP — semua path runtime tercakup COPY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
