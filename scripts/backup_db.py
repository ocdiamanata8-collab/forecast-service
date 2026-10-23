"""Backup database forecast — Fase 6 · item 6·06 (backup terjadwal).

Menjalankan ``pg_dump`` (format custom + gzip) ke folder lokal, beri nama
berstempel waktu, dan (opsional) buang file backup yang sudah terlalu lama.
Tidak ada dependensi Python tambahan — memakai ``pg_dump`` yang sudah ikut
terpasang di PostgreSQL (atau di container, lihat catatan di bawah).

Jalankan:

    DATABASE_URL=postgresql://... python scripts/backup_db.py --out backups
    DATABASE_URL=postgresql://... python scripts/backup_db.py --out backups --keep-days 30

Keamanan (6·06 / §6·05):
- Password TIDAK pernah masuk argumen proses (terlihat di ``ps``); diambil
  dari ``DATABASE_URL`` lalu disetel lewat env ``PGPASSWORD`` hanya untuk
  proses ``pg_dump``.
- Folder ``backups/`` (dan pola ``*.dump``/``*.dump.gz``) sudah masuk
  ``.gitignore``; bila memakai folder lain, tambahkan sendiri. Backup = data
  yang bisa membongkar isi database; perlakukan seperti kredensial.

"Backup terjadwal": script ini DIRANCANG dipanggil cron/scheduler eksternal:

    # contoh crontab (03:00 harian, simpan 30 hari)
    0 3 * * * cd /srv/forecast-service && DATABASE_URL=... \
      python scripts/backup_db.py --out backups --keep-days 30 >> backups/backup.log 2>&1

Host tanpa ``pg_dump`` (mis. hanya Docker Desktop): pakai ``--via-docker``
untuk menjalankan ``pg_dump`` di dalam container uji (nama container lewat
``--container``), atau pasang klien PostgreSQL lokal.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote

#: Pola file backup — dipakai juga saat membersihkan (``--keep-days``).
BACKUP_GLOB = "forecast-*.dump.gz"


def dsn_to_pg_env(dsn: str) -> tuple[dict[str, str], dict[str, str]]:
    """Pecah DSN → (env untuk pg_dump, parameter default). Password masuk env,
    bukan argumen (lihat modul docstring)."""
    u = urlparse(dsn)
    if u.scheme not in {"postgresql", "postgres"} or not u.hostname:
        raise SystemExit(
            "DATABASE_URL tidak valid — format: "
            "postgresql://user:pass@host:5432/dbname"
        )
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    params = {
        "PGHOST": u.hostname,
        "PGPORT": str(u.port or 5432),
        "PGUSER": unquote(u.username or "postgres"),
        "PGDATABASE": (u.path or "/forecast").lstrip("/"),
    }
    return {**env, **params}, params


def find_pg_dump(via_docker: bool, container: str, env: dict[str, str],
                 pg_params: dict[str, str]) -> list[str]:
    """Bangun perintah ``pg_dump`` sesuai mode.

    Mode Docker: flag ``-e`` WAJIB diletakkan SEBELUM nama container
    (``docker exec -i -e K=V <container> pg_dump``) — sesudahnya, flag itu
    dibaca sebagai argumen pg_dump. Kredensial diteruskan eksplisit karena
    env proses host tidak otomatis masuk ke dalam container.

    Host socket Unix di dalam container memakai user OS ("root") yang tidak
    ada di Postgres → karena itu PGHOST dipaksa ke 127.0.0.1 (TCP) agar
    kredensial dari DSN yang dipakai.
    """
    if via_docker:
        cmd = ["docker", "exec", "-i"]
        for key, value in pg_params.items():
            # Docker container berkomunikasi ke localhost port internal 5432,
            # bukan port host yang ada di DSN (mis. 15432).
            if key == "PGPORT":
                value = "5432"
            cmd.extend(["-e", f"{key}={value}"])
        if "PGPASSWORD" in env:
            cmd.extend(["-e", f"PGPASSWORD={env['PGPASSWORD']}"])
        # PGHOST dipaksa TCP — socket Unix akan memakai user OS container.
        cmd.extend(["-e", "PGHOST=127.0.0.1"])
        cmd.extend([container, "pg_dump"])
        return cmd
    exe = shutil.which("pg_dump")
    if exe:
        return [exe]
    raise SystemExit(
        "pg_dump tidak ditemukan di PATH. Pilih salah satu:\n"
        "  - pasang klien PostgreSQL lokal (berisi pg_dump), atau\n"
        "  - jalankan dengan --via-docker (pg_dump dari dalam container "
        "postgres, mis. --container forecast-pg)."
    )


def prune_old_backups(out_dir: Path, keep_days: int) -> list[Path]:
    """Hapus file backup berumur > keep_days. Mengembalikan yang dihapus."""
    if keep_days <= 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed: list[Path] = []
    for path in sorted(out_dir.glob(BACKUP_GLOB)):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="Backup database forecast (6·06)")
    ap.add_argument("--out", default="backups", help="folder tujuan (default: backups)")
    ap.add_argument("--keep-days", type=int, default=0,
                    help="hapus backup lokal lebih tua dari N hari (0 = tak disentuh)")
    ap.add_argument("--via-docker", action="store_true",
                    help="jalankan pg_dump di dalam container (tanpa pg_dump lokal)")
    ap.add_argument("--container", default="forecast-pg",
                    help="nama container postgres untuk --via-docker")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL tidak disetel — backup butuh string koneksi (6·05).")
        return 1

    env, pg_params = dsn_to_pg_env(dsn)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"forecast-{stamp}.dump.gz"

    dump_args = ["-Fc", "-Z", "6"]
    if not args.via_docker:
        dump_args.extend(["-f", str(target)])
    cmd = [*find_pg_dump(args.via_docker, args.container, env, pg_params),
           *dump_args]
    # --via-docker: pg_dump menulis custom-format ke stdout (tanpa -f,
    # karena -f /dev/stdout bikin pg_dump mencoba fsync ke pipe → "Invalid
    # argument"), lalu kita bungkus gzip di host. subprocess TIDAK boleh
    # dikasih fileobj GzipFile: .fileno() milik GzipFile meneruskan
    # descriptor file mentah sehingga byte tak terkompresi yang tertulis.
    # stderr dibaca thread terpisah supaya tidak memblokir pipe stdout.
    try:
        if args.via_docker:
            import gzip
            import threading

            docker_proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
            err_buf: list[bytes] = []

            def _drain_stderr() -> None:
                assert docker_proc.stderr is not None
                err_buf.append(docker_proc.stderr.read())

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()
            assert docker_proc.stdout is not None
            with target.open("wb") as raw, gzip.GzipFile(
                    fileobj=raw, mode="wb") as gz:
                while chunk := docker_proc.stdout.read(1 << 20):
                    gz.write(chunk)
            rc = docker_proc.wait(timeout=600)
            stderr_thread.join()
            returncode, stderr = rc, b"".join(err_buf)
        else:
            result = subprocess.run(cmd, env=env, stderr=subprocess.PIPE,
                                    timeout=600)
            returncode, stderr = result.returncode, result.stderr
    except subprocess.TimeoutExpired:
        target.unlink(missing_ok=True)
        print("ERROR: pg_dump melebihi 10 menit — periksa koneksi.")
        return 1

    if returncode != 0:
        target.unlink(missing_ok=True)
        err = stderr.decode(errors="replace").strip()
        print(f"ERROR: pg_dump gagal (exit {returncode}):\n{err}")
        return 1

    size = target.stat().st_size
    print(f"backup OK: {target} ({size:,} bytes)")

    if args.keep_days > 0:
        removed = prune_old_backups(out_dir, args.keep_days)
        print(f"retensi file: {len(removed)} backup lama dihapus "
              f"(keep-days={args.keep_days})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
