"""
bootstrap.py -- the ONLY place that inserts rows on a fresh database.

What it is allowed to create is tightly limited by Appendix C ("No Test Data,
No Fabricated Data"):

  * C3 -- structural bay slots: one IDLE row per configured bay so the grid has
          boxes to draw. These hold no operational data.
  * C4 -- the single mandatory "Other" delay reason, pinned to the bottom.
  * Behavioural defaults the spec itself specifies (4x3 grid, ~12s takeover,
          stale-item review thresholds). These are app *behaviour*, not invented
          operational records, and every one is editable in /admin.

It explicitly does NOT seed: divisions, real delay reasons, product numbers,
initials, or any shift / break / operating-hours times. Those all start empty
and are entered by the user with real values.

Every function here is idempotent: it only fills a gap, never overwrites an
existing value. That is what makes init_db.py safe to re-run (Appendix B3).
"""

import sqlite3

from .db import get_setting, set_setting

# Number of standard bays created as structural slots on first run. This is the
# configured bay count from the domain model ("12 standard bays"); extra top-row
# bays are added later from /admin, never seeded here.
DEFAULT_BAY_COUNT = 12

# Behavioural defaults (NOT operational data). Each is editable in /admin.
BEHAVIOURAL_DEFAULTS = {
    "grid_cols": 4,            # standard grid is 4 columns wide
    "standard_rows": 3,        # bays 1..12 fill 3 rows (4x3) when no extras
    "extras_enabled": False,   # default 4x3, no extra top-row bays
    "takeover_seconds": 12,    # full-screen delay takeover duration (~10-15s)
    "stale_delay_minutes": 120,  # a delay open longer than this is flagged for review (confirm in admin)
    "stale_run_minutes": 720,    # a run open longer than this is flagged for review (confirm in admin)
}

# Operational config that MUST start empty/unset (Appendix C4). Listed here so
# the keys exist (as null/empty) and the admin forms have something to bind to,
# but they carry no invented values.
EMPTY_OPERATIONAL_DEFAULTS = {
    "labor_rate": None,            # no cost shown until a real rate is entered
    "pin_stats_hash": None,        # /stats PIN (unset => page is open with a "set a PIN" banner)
    "pin_admin_hash": None,        # /admin PIN
    "break_schedule": [],          # list of {start:"HH:MM", minutes:int, label:str} -- user enters
    "operating_calendar": None,    # None => treat ALL time as counting until hours are entered
    "shifts": [],                  # list of {name:str, start:"HH:MM"} cutoffs -- user enters
    "backup_network_path": None,   # where the scheduled DB backup copy goes
}


def seed(conn: sqlite3.Connection) -> None:
    """Fill only the gaps on a fresh (or partially-configured) database."""
    _seed_bays(conn)
    _seed_other_reason(conn)
    _seed_settings(conn)


def _seed_bays(conn: sqlite3.Connection) -> None:
    """Create the standard structural bay slots if no bays exist yet."""
    count = conn.execute("SELECT COUNT(*) AS n FROM bays;").fetchone()["n"]
    if count > 0:
        return  # already created (possibly customised) -- never touch.
    for i in range(1, DEFAULT_BAY_COUNT + 1):
        conn.execute(
            "INSERT INTO bays (name, sort_order, is_extra, grid_col, active) "
            "VALUES (?, ?, 0, NULL, 1);",
            (f"Bay {i}", i),
        )
    conn.commit()


def _seed_other_reason(conn: sqlite3.Connection) -> None:
    """Ensure the mandatory pinned 'Other' delay reason exists exactly once."""
    row = conn.execute(
        "SELECT id FROM delay_reasons WHERE is_other = 1 LIMIT 1;"
    ).fetchone()
    if row is not None:
        return
    conn.execute(
        "INSERT INTO delay_reasons (label, division_id, in_out_of_control, active, is_other, sort_order) "
        "VALUES ('Other', NULL, NULL, 1, 1, 999999);"
    )
    conn.commit()


def _seed_settings(conn: sqlite3.Connection) -> None:
    """Insert default settings keys ONLY if absent (never overwrite)."""
    for key, value in {**BEHAVIOURAL_DEFAULTS, **EMPTY_OPERATIONAL_DEFAULTS}.items():
        # get_setting returns a sentinel-free default; use a unique sentinel so a
        # legitimately-stored None/[] is not treated as "missing".
        sentinel = object()
        if get_setting(conn, key, sentinel) is sentinel:
            set_setting(conn, key, value)
