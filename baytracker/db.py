"""
db.py -- SQLite connection helpers and the complete database schema.

Design principles (from the spec):

* The **events** table is the append-only single source of truth. Every other
  table is either *configuration* (reasons, divisions, bays, settings...) or a
  *structural slot* (the bay rows). Current operating state is never stored; it
  is always re-derived by replaying the events (see state.py).

* The schema is created with ``CREATE TABLE IF NOT EXISTS`` so init_db.py is
  non-destructive and safe to run against an existing, populated database
  (Appendix B3). migrate.py adds new tables/columns the same way (Appendix B4).

* Identifiers that humans read -- work_order and product_number -- are stored as
  TEXT so leading zeros and odd formats survive untouched (e.g. "0347").
"""

import sqlite3
from pathlib import Path
from typing import Optional

from . import config


# The schema version this code expects. migrate.py bumps the stored value as it
# applies additive changes; it is informational/forward-only and never triggers
# a destructive reset.
#   v2 (2026-06): added recipients + notification_outbox (delay notifications).
SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# The schema. One big string of CREATE ... IF NOT EXISTS statements.
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
-- =====================================================================
-- events : the append-only log. The ONLY source of truth. Never UPDATE or
-- DELETE a row here; history is corrected by appending CORRECTION events.
-- =====================================================================
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- When the row was written, server-side, ISO 8601 to the second.
    -- For a CORRECTION this is when the correction was made (not the time
    -- being corrected -- that goes in corrected_ts).
    ts                  TEXT    NOT NULL,
    type                TEXT    NOT NULL,   -- START|MOVE|COMPLETE_BAY|MATE|DELAY_START|DELAY_CLEAR|PAUSE|RESUME|UNIT_COMPLETE|SCRAP|CORRECTION
    bay_id              INTEGER,            -- bay this event concerns (source bay for MOVE; continuing bay for MATE)
    target_bay_id       INTEGER,            -- MOVE: destination bay.  MATE: the bay being released/freed.
    work_order          TEXT,               -- TEXT to preserve leading zeros
    product_number      TEXT,               -- TEXT to preserve leading zeros / odd formats
    component_label     TEXT,               -- optional, e.g. "Half A" (distinguishes 2 parallel streams)

    -- Delay-specific fields. We snapshot the reason's label/division/control tag
    -- onto the DELAY_START row so that later renaming or retiring a reason can
    -- never rewrite history.
    delay_reason_id     INTEGER,            -- FK delay_reasons.id
    reason_label        TEXT,               -- snapshot of the reason text
    division            TEXT,               -- snapshot of the reason's division
    in_out_of_control   TEXT,               -- snapshot: 'in' | 'out' | NULL

    note                TEXT,               -- required (>=1 char) on delays; optional elsewhere
    initials            TEXT,               -- who performed the action (required on logged actions)

    -- Correction plumbing (see state.py for how these are applied):
    supersedes_event_id INTEGER,            -- the event whose effective time this CORRECTION overrides
    corrected_ts        TEXT,               -- the new effective timestamp the correction applies
    acts_as             TEXT                -- 'DELAY_CLEAR'|'COMPLETE_BAY'|'UNIT_COMPLETE' when a correction closes a forgotten-open item
);

CREATE INDEX IF NOT EXISTS idx_events_type        ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_work_order  ON events(work_order);
CREATE INDEX IF NOT EXISTS idx_events_bay         ON events(bay_id);
CREATE INDEX IF NOT EXISTS idx_events_supersedes  ON events(supersedes_event_id);

-- =====================================================================
-- bays : structural slots only. Created on first run (one row per configured
-- bay). Holds NO operational data -- a bay is simply IDLE until a real event
-- is logged against it. This is the sole auto-created record type (Appendix C3).
-- =====================================================================
CREATE TABLE IF NOT EXISTS bays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,            -- "Bay 1"
    sort_order  INTEGER NOT NULL,            -- 1..N, drives left-to-right / top-to-bottom placement
    is_extra    INTEGER NOT NULL DEFAULT 0,  -- 1 = an enabled top-row "extra" bay
    grid_col    INTEGER,                     -- for extras: which top-row column (1-based) it sits above
    active      INTEGER NOT NULL DEFAULT 1   -- 0 = hidden from the grid (never hard-deleted)
);

-- =====================================================================
-- divisions : the owning area for a delay reason. Starts EMPTY (Appendix C4).
-- =====================================================================
CREATE TABLE IF NOT EXISTS divisions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT    NOT NULL UNIQUE,
    active  INTEGER NOT NULL DEFAULT 1
);

-- =====================================================================
-- delay_reasons : the editable dropdown. Starts with ONLY the mandatory
-- pinned "Other" row (the single permitted default, Appendix C4). Reasons are
-- soft-retired (active=0), never hard-deleted, so history stays valid.
-- =====================================================================
CREATE TABLE IF NOT EXISTS delay_reasons (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    label             TEXT    NOT NULL,
    division_id       INTEGER,                       -- FK divisions.id (NULL for "Other")
    in_out_of_control TEXT,                          -- 'in' | 'out' | NULL
    active            INTEGER NOT NULL DEFAULT 1,    -- 0 = soft-retired
    is_other          INTEGER NOT NULL DEFAULT 0,    -- 1 = the mandatory pinned "Other" row
    sort_order        INTEGER NOT NULL DEFAULT 0     -- ascending; "Other" forced to the bottom in the UI
);

-- =====================================================================
-- product_numbers : the known short list. Starts EMPTY. Free-text "oddballs"
-- typed at the console are NOT auto-added here (they just live on the event).
-- target_minutes is a DORMANT future hook (Future Hooks): stored, never shown.
-- =====================================================================
CREATE TABLE IF NOT EXISTS product_numbers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT    NOT NULL UNIQUE,        -- TEXT (preserve leading zeros)
    description     TEXT,
    target_minutes  REAL,                           -- dormant; no countdown UI is built
    active          INTEGER NOT NULL DEFAULT 1
);

-- =====================================================================
-- initials_roster : known technician initials. Starts EMPTY. The console
-- autocompletes from this but always allows brand-new initials to be typed.
-- =====================================================================
CREATE TABLE IF NOT EXISTS initials_roster (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    initials  TEXT    NOT NULL UNIQUE,
    name      TEXT,
    active    INTEGER NOT NULL DEFAULT 1
);

-- =====================================================================
-- settings : key/value store for scalar + JSON config. Values are JSON-encoded
-- text. Operational schedule values (breaks/shifts/operating hours) start
-- EMPTY and are entered by the user (Appendix C4). Only behavioural defaults
-- that the spec itself specifies (takeover seconds, 4x3 grid, stale thresholds)
-- are seeded.
-- =====================================================================
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- =====================================================================
-- recipients : people who get a delay alert. Edited live in /admin, exactly
-- like delay_reasons -- no code change to add/remove someone. A recipient has
-- contact details, per-channel on/off switches, and a scope of what they hear
-- about. Soft-retired (active=0), never hard-deleted, so the outbox audit trail
-- can always be traced back to a recipient row.
-- =====================================================================
CREATE TABLE IF NOT EXISTS recipients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT,
    phone         TEXT,                          -- E.164 only, e.g. +14145551234
    notify_email  INTEGER NOT NULL DEFAULT 0,    -- 0/1
    notify_sms    INTEGER NOT NULL DEFAULT 0,    -- 0/1
    bay_scope     TEXT    NOT NULL DEFAULT 'all', -- 'all' or CSV of bays.id, e.g. '7,8,9'
    control_scope TEXT    NOT NULL DEFAULT 'all', -- 'all' or 'out' (out-of-control delays only)
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- =====================================================================
-- notification_outbox : the reliability core. Every intended send is a row.
-- This table IS the queue AND the audit log. The web request that logs a delay
-- only INSERTs 'pending' rows here (never touches the network); a background
-- worker (see notify.py) sends them and retries with backoff. destination is
-- snapshotted at enqueue time so editing a recipient later never rewrites the
-- record of what was actually sent. delay_event_id points at the DELAY_START
-- row in events -- the only "delay record" this app has.
-- =====================================================================
CREATE TABLE IF NOT EXISTS notification_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delay_event_id  INTEGER NOT NULL,                       -- events.id of the DELAY_START row
    recipient_id    INTEGER,                                -- recipients.id (kept even if recipient retired)
    channel         TEXT    NOT NULL,                       -- 'email' | 'sms'
    destination     TEXT    NOT NULL,                       -- snapshot of email/phone at enqueue time
    subject         TEXT,                                   -- email only
    body            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',     -- pending|sending|sent|failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON notification_outbox(status, next_attempt_at);

-- =====================================================================
-- schema_version : single-row table recording the applied schema version so
-- migrate.py can apply forward-only changes idempotently.
-- =====================================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a SQLite connection with sensible, resilience-oriented settings.

    * ``row_factory = sqlite3.Row`` so we can read columns by name.
    * WAL journal mode: lets the many SSE/dashboard *readers* run while the
      console *writes*, and makes crash recovery clean -- a reboot or power blip
      never corrupts the file or loses a committed event.
    * ``foreign_keys`` is left OFF deliberately: we soft-retire config rows
      rather than delete them, and the event log intentionally keeps snapshots
      of (possibly later-retired) reasons, so hard FK enforcement would fight
      the append-only, never-rewrite-history design.
    """
    path = Path(db_path) if db_path else config.DB_PATH
    # Self-heal a missing data folder. If the folder named by BAYTRACKER_DATA
    # was deleted (or never created), SQLite fails with the unhelpful
    # "unable to open database file" on EVERY request. Creating the folder here
    # turns that permanent failure into a clean recovery; mkdir(exist_ok=True)
    # is a no-op whenever the folder is already there.
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")  # durable enough with WAL, much faster on spinning disks
    conn.execute("PRAGMA busy_timeout=30000;")  # wait up to 30s rather than erroring if briefly locked
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every table/index if it does not already exist. Non-destructive."""
    conn.executescript(SCHEMA_SQL)
    # Record the schema version exactly once.
    row = conn.execute("SELECT version FROM schema_version LIMIT 1;").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?);", (SCHEMA_VERSION,))
    conn.commit()


# ---------------------------------------------------------------------------
# Tiny settings helpers (values are JSON-encoded text).
# ---------------------------------------------------------------------------
import json  # noqa: E402  (kept next to the helpers that use it for readability)


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    """Return a decoded setting value, or ``default`` if the key is absent."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?;", (key,)).fetchone()
    if row is None or row["value"] is None:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        # Be forgiving of a hand-edited value; return the raw text.
        return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    """Insert/replace a setting, JSON-encoding the value."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
        (key, json.dumps(value)),
    )
    conn.commit()
