"""Audit kebersihan repo sebelum push — cari file sensitif & kunci hardcoded.

    python scripts/check_repo_hygiene.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTS = {".py", ".md", ".html", ".json", ".csv", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".ini"}
EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}
# Literal yang tidak boleh muncul sebagai hardcoded secret.
PATTERNS = [
    (re.compile(r"dev-key-change-me"), "demo key hardcoded"),
    (re.compile(r"X-API-Key:\s*(?!\$\{)(?!(?:\S*(?:GANTI|API_KEY|kunci|placeholder|CONTOH|contoh)))\S{20,}"), "API key panjang di teks"),
    (re.compile(r"(?:password|secret|token)\s*=\s*[\"'][^\"']{12,}[\"']", re.I), "kemungkinan kredensial hardcoded"),
]
SENSITIVE_NAMES = {".env", ".pem", ".key", ".p12", ".pfx", "id_rsa", "id_ed25519"}


def main() -> int:
    issues: list[str] = []
    print("=== [1] File sensitif ===")
    found_any = False
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(d in p.parts for d in EXCLUDE_DIRS):
            continue
        if p.name in SENSITIVE_NAMES or p.suffix in SENSITIVE_NAMES:
            issues.append(f"file sensitif: {p.relative_to(ROOT)}")
            print(f"  {p.relative_to(ROOT)}")
            found_any = True
    if not found_any:
        print("  (tidak ada)")

    print("=== [2] Literal kunci hardcoded ===")
    hit_any = False
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in EXTS:
            continue
        if any(d in p.parts for d in EXCLUDE_DIRS):
            continue
        # Skrip hygiene ini sendiri sengaja memuat pola — jangan mendeteksi
        # diri sendiri.
        if p.name == "check_repo_hygiene.py":
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for pat, label in PATTERNS:
            for m in pat.finditer(text):
                line_no = text[: m.start()].count("\n") + 1
                # demo key di test/fixtures boleh; hanya catat di app/ & scripts/.
                if "demo key" in label and "/tests/" in str(p).replace("\\", "/"):
                    continue
                # Placeholder `<...>` bukan key nyata — lewati.
                line = text.splitlines()[line_no - 1].strip()
                if "<GANTI" in line or "<API_KEY>" in line:
                    continue
                issues.append(f"{p.relative_to(ROOT)}:{line_no} [{label}]")
                print(f"  {p.relative_to(ROOT)}:{line_no}  {label}")
                hit_any = True
    if not hit_any:
        print("  (tidak ada)")

    print("=== [3] Ukuran file >500 KB (kandidat LFS / exclude) ===")
    # Hanya file yang BENAR-BENAR ikut ter-commit yang jadi isu — file yang
    # sudah ada di .gitignore tidak akan masuk repository, jadi tidak perlu LFS.
    gi_patterns: list[str] = []
    gi_path = ROOT / ".gitignore"
    if gi_path.exists():
        gi_patterns = [
            ln.strip().rstrip("/").lstrip("/")
            for ln in gi_path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]

    def is_ignored(rel: str) -> bool:
        rel_posix = rel.replace("\\", "/")
        return any(pat and pat in rel_posix for pat in gi_patterns)

    for p in ROOT.rglob("*"):
        if not p.is_file() or any(d in p.parts for d in EXCLUDE_DIRS):
            continue
        size = p.stat().st_size
        if size <= 500_000:
            continue
        kb = round(size / 1024)
        rel = str(p.relative_to(ROOT))
        if is_ignored(rel):
            print(f"  {rel}  {kb} KB  (sudah di .gitignore — tidak ikut ter-commit)")
        else:
            issues.append(f"file besar ter-commit: {rel} ({kb} KB) — pakai Git LFS / object storage")
            print(f"  {rel}  {kb} KB  ← TER-COMMIT, butuh LFS")

    print("=== [4] .gitignore ada & lengkap ===")
    gi = ROOT / ".gitignore"
    if not gi.exists():
        issues.append(".gitignore tidak ada")
        print("  .gitignore tidak ada")
    else:
        content = gi.read_text(encoding="utf-8")
        required = [".env", "__pycache__/", "data/raw/", "model.pkl"]
        missing = [r for r in required if r not in content]
        if missing:
            issues.append(f".gitignore tidak lengkap: {missing}")
            print(f"  tidak lengkap: {missing}")
        else:
            print("  lengkap")

    print("\n" + "=" * 64)
    if issues:
        print(f"DITEMUKAN {len(issues)} isu:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("REPO BERSIH — siap di-push")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
