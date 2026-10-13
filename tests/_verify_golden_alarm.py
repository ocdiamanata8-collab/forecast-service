"""Uji bahwa test golden MEMANG jadi alarm: sengaja rusakkan engine, test harus GAGAL.

    python tests/_verify_golden_alarm.py

Tanpa pemeriksaan ini, test golden bisa saja lulus terus-menerus karena
salah muat/ter-skip — terlihat hijau padahal tidak mengukur apa pun.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "app" / "engine.py"

PASS_LABEL = "  PASS"
FAIL_LABEL = "  FAIL"
failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"{PASS_LABEL if ok else FAIL_LABEL}  {label}")
    if not ok:
        failures.append(label)


def run_golden() -> int:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "test_engine_golden.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return proc.returncode


def with_engine_mutation(marker: str, body: str) -> bool:
    """Tambahkan mutasi sementara ke engine, jalankan test, kembalikan."""
    original = ENGINE.read_text(encoding="utf-8")
    if marker not in original:
        raise RuntimeError(f"penanda tidak ditemukan di engine: {marker!r}")
    mutated = original.replace(marker, body, 1)
    ENGINE.write_text(mutated, encoding="utf-8")
    try:
        code = run_golden()
        return code != 0  # exit != 0 berarti alarm berbunyi
    finally:
        ENGINE.write_text(original, encoding="utf-8")


def main() -> int:
    print("[A] kondisi normal (engine tidak disentuh) — test harus LULUS")
    check(run_golden() == 0, "test golden lulus pada engine normal")

    print("\n[B] engine dimutasi — test golden harus GAGAL")

    # Mutasi 1: geser hasil 0.5 (perilaku berubah nyata).
    ok = with_engine_mutation(
        '"forecast_value": values.astype("float64"),',
        '"forecast_value": values.astype("float64") + 0.5,',
    )
    check(ok, "geser nilai +0.5 -> terdeteksi")

    # Mutasi 2: ubah horizon-internal (engine harus memegang horizon).
    ok = with_engine_mutation(
        'fitted = ARIMA(',
        'fitted = ARIMA(  # mutate',  # tidak mengubah perilaku, hanya komentar
    )
    # Komentar tidak mengubah perilaku — test seharusnya TETAP lulus.
    check(ok is False, "hanya komentar -> test tetap lulus (tidak alarm palsu)")

    # Mutasi 3: ubah default order (perilaku model berubah total).
    ok = with_engine_mutation(
        'DEFAULT_ORDER: tuple[int, int, int] = MODEL_ORDERS["WITA"]',
        'DEFAULT_ORDER: tuple[int, int, int] = (0, 0, 1)',
    )
    check(ok, "ganti order ARIMA -> terdeteksi")

    print("\n" + "=" * 64)
    if failures:
        print(f"GAGAL: {len(failures)} pemeriksaan")
        for f in failures:
            print(f"  - {f}")
        return 1
    # Pastikan engine benar-benar dipulihkan.
    check(run_golden() == 0, "engine dipulihkan; test golden lulus lagi")
    print("ALARM GOLDEN TERBUKTI BEKERJA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
