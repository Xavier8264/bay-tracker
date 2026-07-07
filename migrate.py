"""
migrate.py -- apply forward-only, additive, idempotent schema changes.

This is how the accumulated log survives future schema changes intact
(Appendix B4). It is invoked by update.ps1 *after* the database has been backed
up. Rules for every migration you add here:

  * ADD ONLY. Add a table or a column; never drop, rename-in-place, or rewrite
    existing data. (To "remove" a column, just stop using it.)
  * IDEMPOTENT. Safe to run repeatedly -- guard each change so re-running does
    nothing the second time.
  * NEVER DESTRUCTIVE. If you are tempted to delete data, you are doing it wrong;
    add a new column/table and migrate forward instead.

How to add a migration: append a function to MIGRATIONS that takes a live
connection and applies its change idempotently. The helpers below
(``_column_exists`` / ``_add_column_if_missing``) make the common case a
one-liner. The current schema (version 1) is created by db.create_schema, so
there are no migrations yet -- this file is the scaffold for future ones.
"""

import sqlite3

from baytracker import config, db


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    return any(r["name"] == column for r in rows)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ... ADD COLUMN, but only if the column isn't already there."""
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl};")
        conn.commit()
        print(f"[migrate] Added column {table}.{column}")


# ---------------------------------------------------------------------------
# The ordered list of migrations. Each is an idempotent callable(conn).
# ---------------------------------------------------------------------------

def _add_action_group_to_events(conn):
    """v3: group the rows of one operator action so the console Undo can reverse
    a multi-row action (a shift changeover) atomically. (db.create_schema also
    applies this on startup; running it here keeps the migration history honest.)"""
    _add_column_if_missing(conn, "events", "action_group", "TEXT")


def _add_incidents(conn):
    """v4: the EHS accident / near-miss log, plus the two notification_outbox
    columns its leadership alerts need. (db.create_schema also creates these on
    startup; running it here keeps the migration history honest.) All additive
    and idempotent: CREATE TABLE IF NOT EXISTS + ADD COLUMN only-if-missing."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                TEXT    NOT NULL,
            type              TEXT    NOT NULL,
            occurred_at       TEXT,
            location          TEXT,
            reported_by       TEXT,
            severity          TEXT,
            potential         TEXT,
            person            TEXT,
            injury            TEXT,
            medical           TEXT,
            what_happened     TEXT,
            immediate_action  TEXT,
            equipment         TEXT,
            prelim_sent_at    TEXT,
            finalized_at      TEXT,
            created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_incidents_ts ON incidents(ts);
    """)
    _add_column_if_missing(conn, "notification_outbox", "incident_id", "INTEGER")
    _add_column_if_missing(conn, "notification_outbox", "kind", "TEXT")
    conn.commit()


MIGRATIONS: list = [_add_action_group_to_events, _add_incidents]


def main() -> None:
    config.ensure_data_dirs()
    conn = db.connect()
    try:
        # Ensure the base schema is present first (no-op if it already is).
        db.create_schema(conn)

        applied = 0
        for migration in MIGRATIONS:
            migration(conn)
            applied += 1

        # Record the latest schema version we know about (informational).
        conn.execute("DELETE FROM schema_version;")
        conn.execute("INSERT INTO schema_version (version) VALUES (?);", (db.SCHEMA_VERSION,))
        conn.commit()
    finally:
        conn.close()

    print(f"[migrate] Up to date. Schema version = {db.SCHEMA_VERSION}. "
          f"{len(MIGRATIONS)} migration(s) defined.")


if __name__ == "__main__":
    main()
