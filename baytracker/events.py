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
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from . import config


# The complete set of columns an event row may carry, in a stable order. Keeping
# this in one place means append() and the raw export never drift apart.
#   action_group (2026-06): a tag shared by every row written for ONE operator
#   action, so the console Undo can reverse a multi-row action (a shift
#   changeover parks several bays at once) as a single unit. A standalone action
#   gets its own unique group; older rows have NULL and are undone individually.
EVENT_COLUMNS = [
    "ts", "type", "bay_id", "target_bay_id", "work_order", "product_number",
    "component_label", "delay_reason_id", "reason_label", "division",
    "in_out_of_control", "note", "initials",
    "supersedes_event_id", "corrected_ts", "acts_as", "action_group",
]

# Every recognised event type (spec section 3).
#   PAUSE / RESUME (2026-06): park an occupied-but-unstaffed bay at a shift
#   changeover. While paused, the bay's clocks freeze (active/cycle/elapsed/total
#   all stop accruing, like a break) and no delay can be flagged on it.
#   VOID (2026-06): the console Undo. A VOID row points (via supersedes_event_id)
#   at an earlier action's row; state.replay then skips that row entirely, so the
#   action is reversed WITHOUT rewriting history -- both the original row and its
#   VOID stay in the append-only log for the audit trail.
EVENT_TYPES = {
    "START", "MOVE", "COMPLETE_BAY", "MATE",
    "DELAY_START", "DELAY_CLEAR", "UNIT_COMPLETE", "SCRAP", "CORRECTION",
    "PAUSE", "RESUME", "VOID",
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
    """Parse one of our stored timestamps back into a datetime.

    fromisoformat accepts our exact TS_FORMAT and is ~35x faster than strptime
    -- timestamp parsing was about HALF the cost of a full event-log replay at
    multi-year size. strptime stays as the fallback for any odd legacy value.
    """
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s, config.TS_FORMAT)


def append(conn: sqlite3.Connection, type: str, **fields) -> sqlite3.Row:
    """Append one event row and return it (including its new id).

    The timestamp is stamped here, server-side, unless the caller passes an
    explicit ``ts`` (used only when replaying tests). Unknown fields are ignored
    so callers can pass a superset comfortably.

    Every row is tagged with an ``action_group``: callers that write several rows
    for one logical action (e.g. a shift changeover) pass a shared group so Undo
    can reverse them together; everyone else gets a fresh unique group here.
    """
    if type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {type!r}")

    values = {col: None for col in EVENT_COLUMNS}
    values["type"] = type
    values["ts"] = fields.get("ts") or now_ts()
    values["action_group"] = fields.get("action_group") or uuid.uuid4().hex
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
