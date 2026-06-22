"""
events.py -- the append-only event log: the single source of truth.

This module is intentionally tiny and "dumb". It only knows how to:

  * stamp the current server time (all timestamps are server-side, never from a
    client device -- spec section 2/4), and
  * append a row, and
  * read rows back.

It does NOT validate (that's actions.py, which checks the requested action
against current derived state first) and it does NOT mutate or delete history.
Corrections are themselves just new appended rows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional

from . import config


# The complete set of columns an event row may carry, in a stable order. Keeping
# this in one place means append() and the raw export never drift apart.
EVENT_COLUMNS = [
    "ts", "type", "bay_id", "target_bay_id", "work_order", "product_number",
    "component_label", "delay_reason_id", "reason_label", "division",
    "in_out_of_control", "note", "initials",
    "supersedes_event_id", "corrected_ts", "acts_as",
]

# Every recognised event type (spec section 3).
#   PAUSE / RESUME (2026-06): park an occupied-but-unstaffed bay at a shift
#   changeover. While paused, the bay's clocks freeze (active/cycle/elapsed/total
#   all stop accruing, like a break) and no delay can be flagged on it.
EVENT_TYPES = {
    "START", "MOVE", "COMPLETE_BAY", "MATE",
    "DELAY_START", "DELAY_CLEAR", "UNIT_COMPLETE", "SCRAP", "CORRECTION",
    "PAUSE", "RESUME",
}


def round_to_minute(dt: datetime) -> datetime:
    """Round a datetime to the NEAREST whole minute (seconds >= 30 round up).

    Every newly-logged event is snapped to a minute boundary so that, with all
    starts minute-aligned, every bay's elapsed/total clock rolls to the next
    minute at the same real-clock instant (on the :00 second). We don't need
    second-level precision; uniformity across the floor is what matters.
    """
    floored = dt.replace(second=0, microsecond=0)
    return floored + timedelta(minutes=1) if dt.second >= 30 else floored


def now_ts() -> str:
    """Current server time, rounded to the nearest minute, as an ISO-8601 string.

    Timestamps are minute-aligned so the whole floor changes minutes in lockstep
    (see round_to_minute). Storage still keeps the ``:SS`` field for format
    stability -- it is simply always ``:00``.
    """
    return round_to_minute(datetime.now()).strftime(config.TS_FORMAT)


def parse_ts(s: str) -> datetime:
    """Parse one of our stored timestamps back into a datetime."""
    return datetime.strptime(s, config.TS_FORMAT)


def append(conn: sqlite3.Connection, type: str, **fields) -> sqlite3.Row:
    """Append one event row and return it (including its new id).

    The timestamp is stamped here, server-side, unless the caller passes an
    explicit ``ts`` (used only when replaying tests). Unknown fields are ignored
    so callers can pass a superset comfortably.
    """
    if type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {type!r}")

    values = {col: None for col in EVENT_COLUMNS}
    values["type"] = type
    values["ts"] = fields.get("ts") or now_ts()
    for col in EVENT_COLUMNS:
        if col in fields and fields[col] is not None:
            values[col] = fields[col]

    placeholders = ", ".join("?" for _ in EVENT_COLUMNS)
    columns = ", ".join(EVENT_COLUMNS)
    cur = conn.execute(
        f"INSERT INTO events ({columns}) VALUES ({placeholders});",
        [values[col] for col in EVENT_COLUMNS],
    )
    conn.commit()
    new_id = cur.lastrowid
    return conn.execute("SELECT * FROM events WHERE id = ?;", (new_id,)).fetchone()


def all_events(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every event, in insertion (causal) order. This is what state.py replays."""
    return conn.execute("SELECT * FROM events ORDER BY id ASC;").fetchall()


def events_between(conn: sqlite3.Connection,
                   start: Optional[str], end: Optional[str]) -> List[sqlite3.Row]:
    """Events whose ts falls in [start, end] (inclusive), for filtered exports."""
    clauses, params = [], []
    if start:
        clauses.append("ts >= ?")
        params.append(start)
    if end:
        clauses.append("ts <= ?")
        params.append(end)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT * FROM events {where} ORDER BY id ASC;", params
    ).fetchall()
