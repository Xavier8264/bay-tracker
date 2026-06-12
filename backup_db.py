"""
backup_db.py -- make a safe, consistent copy of the live SQLite database.

The database is the only irreplaceable asset in the whole system (everything
else can be rebuilt from the repo), so backups must be CONSISTENT even while the
server is running and even with WAL mode active. We use SQLite's online backup
API for that -- a plain file copy could miss data still sitting in the -wal file.

Usage:
    python backup_db.py                 # -> BAYTRACKER_DATA\backups\baytracker_<ts>.db
    python backup_db.py --dest PATH      # also copy to PATH (a network share)

It is invoked:
  * by update.ps1 before every update (Appendix B8), and
  * by a scheduled task for the periodic off-machine backup (Appendix A6).
"""

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from baytracker import config, db


def make_backup(dest: Path) -> Path:
    """Write a consistent copy of the live DB to ``dest`` using the backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = db.connect()                  # opens the live DB (WAL-aware)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)             # atomic, consistent online backup
        finally:
            out.close()
    finally:
        src.close()
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the Bay Tracking database.")
    parser.add_argument("--dest", help="Extra destination (e.g. a network share). "
                        "If omitted, only the local timestamped backup is written.")
    args = parser.parse_args()

    config.ensure_data_dirs()
    if not config.DB_PATH.exists():
        print(f"[backup] No database at {config.DB_PATH}; nothing to back up.")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local = config.BACKUP_DIR / f"baytracker_{stamp}.db"
    make_backup(local)
    print(f"[backup] Wrote {local}")

    # An explicit --dest, or the network path configured in /admin, gets a copy too.
    extra = args.dest or db_setting_network_path()
    if extra:
        extra_path = Path(extra)
        try:
            extra_path.mkdir(parents=True, exist_ok=True)
            target = extra_path / local.name
            shutil.copy2(local, target)
            print(f"[backup] Copied to {target}")
        except OSError as exc:
            print(f"[backup] WARNING: could not copy to {extra}: {exc}")


def db_setting_network_path():
    """Read the optional backup network path the admin configured (may be None)."""
    conn = db.connect()
    try:
        return db.get_setting(conn, "backup_network_path", None)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
