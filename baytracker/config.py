"""
config.py -- where persistent data lives, and a few process-wide constants.

THE most important rule in the whole project (Appendix B2):

    *The database lives OUTSIDE the repository folder.*

Git operations (clone / checkout / pull) only ever touch files inside the repo.
Because the SQLite file lives in a *different* folder -- the one named by the
BAYTRACKER_DATA environment variable -- updating the code physically cannot
touch the accumulated log. That separation is the primary guarantee against
data loss during updates, so everything that needs the data folder asks for it
here and nowhere else.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Data directory resolution
# ---------------------------------------------------------------------------
# The data folder is taken from the BAYTRACKER_DATA environment variable. If it
# is not set we fall back to "C:\BayTrackerData" on Windows (the documented
# default in the README) or "~/BayTrackerData" elsewhere, so a developer can run
# the app with zero configuration. setup.ps1 sets BAYTRACKER_DATA permanently on
# the production PC.
#
# NOTE for whoever deploys this: do NOT point BAYTRACKER_DATA at a OneDrive /
# Dropbox / synced folder. Cloud sync clients corrupt live SQLite files. Use a
# plain local path such as C:\BayTrackerData and back it up on a schedule
# instead (see Appendix A6 / B8).

def _default_data_dir() -> Path:
    if os.name == "nt":
        # System drive, usually C:. Avoids hard-coding the letter.
        system_drive = os.environ.get("SystemDrive", "C:")
        return Path(f"{system_drive}\\BayTrackerData")
    return Path.home() / "BayTrackerData"


DATA_DIR: Path = Path(os.environ.get("BAYTRACKER_DATA", str(_default_data_dir())))

# Sub-locations within the data folder.
DB_PATH: Path = DATA_DIR / "baytracker.db"
BACKUP_DIR: Path = DATA_DIR / "backups"
EXPORT_DIR: Path = DATA_DIR / "exports"          # working area for generated CSV/XLSX
SECRET_KEY_PATH: Path = DATA_DIR / "secret_key"  # Flask session signing key (see below)


def ensure_data_dirs() -> None:
    """Create the data folder and its sub-folders if they don't exist yet.

    Safe to call repeatedly. Called by init_db.py and at app startup so the
    very first run on a clean PC just works.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def get_secret_key() -> bytes:
    """Return a stable secret key used to sign Flask session cookies.

    The PIN-gate for /stats and /admin stores "you are unlocked" in the session
    cookie, which Flask signs with this key. We persist the key in the data
    folder so that restarting the server doesn't log everyone out, but we also
    never commit it (it lives outside the repo). Generated once on first use.
    """
    ensure_data_dirs()
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_bytes()
    key = os.urandom(32)
    SECRET_KEY_PATH.write_bytes(key)
    return key


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------
# Every timestamp in the system is generated SERVER-SIDE (never trusted from a
# client device) and stored as local plant wall-clock time, formatted ISO 8601
# to the second: "YYYY-MM-DD HH:MM:SS". Local wall-clock is the correct basis
# here because shifts, breaks and operating hours are all expressed in plant
# local time. We deliberately keep seconds in storage/exports for precision even
# though the UI never displays them.
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
