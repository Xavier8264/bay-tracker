"""
actions.py -- validate a requested action against current state, then log it.

This is the ONLY place that turns a user's button-press into an event. Every
function here follows the same shape:

    1. Replay the log to see the current state.
    2. Check the action is legal right now (raise ActionError with a friendly
       message if not -- the console shows it and nothing is logged).
    3. Snapshot any data that must stay audit-stable (e.g. a delay reason's
       division/control tag).
    4. Append the event (server stamps the time) and invalidate the state cache.

Nothing here ever invents a value (Appendix C2): if the user didn't provide
initials/note/etc., the action is rejected rather than auto-filled.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from . import events, state, config
from .schedule import Schedule


class ActionError(Exception):
    """A requested action is not valid in the current state (shown to the user)."""


# --- small validation helpers ----------------------------------------------

def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def _require(value: Optional[str], field: str) -> str:
    v = _clean(value)
    if not v:
        raise ActionError(f"{field} is required.")
    return v


def _bay(conn, bay_id) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM bays WHERE id = ?;", (bay_id,)).fetchone()
    if row is None or not row["active"]:
        raise ActionError("That bay does not exist.")
    return row


def _finish(conn, row):
    """Common tail: invalidate the derived-state cache and return the event."""
    state.invalidate_cache()
    return row


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

def start(conn, bay_id, work_order, product_number, initials, component_label=None):
    """Begin a unit in an idle bay."""
    _bay(conn, bay_id)
    wo = _require(work_order, "Work Order")
    pn = _require(product_number, "Product Number")
    who = _require(initials, "Initials")

    r = state.cached_replay(conn)
    if bay_id in r.bay_current:
        raise ActionError("That bay is already running. Complete or move it first.")

    unit = r.units.get(wo)
    if unit and not unit.is_open:
        raise ActionError(f"Work order {wo} was already {unit.outcome}. Use a new work order.")
    if unit and len(unit.occupied_bays) >= 2:
        raise ActionError(f"Work order {wo} is already in 2 bays (max). Mate or complete one first.")

    row = events.append(conn, "START", bay_id=bay_id, work_order=wo,
                        product_number=pn, component_label=_clean(component_label) or None,
                        initials=who)
    return _finish(conn, row)


def move(conn, bay_id, target_bay_id, initials):
    """Atomic handoff: complete the run in this bay and start it in the target bay."""
    _bay(conn, bay_id)
    _bay(conn, target_bay_id)
    who = _require(initials, "Initials")
    if bay_id == target_bay_id:
        raise ActionError("Choose a different target bay.")

    r = state.cached_replay(conn)
    src = r.bay_current.get(bay_id)
    if src is None:
        raise ActionError("That bay has nothing running to move.")
    if target_bay_id in r.bay_current:
        raise ActionError("The target bay is occupied. Pick an empty bay.")

    row = events.append(conn, "MOVE", bay_id=bay_id, target_bay_id=target_bay_id,
                        work_order=src.work_order, product_number=src.product_number,
                        component_label=src.component_label, initials=who)
    return _finish(conn, row)


def complete_bay(conn, bay_id, initials):
    """Mark work finished at this bay. The part STAYS in the bay (DONE state)
    until a later move/merge/unit-complete; the bay is not freed here."""
    _bay(conn, bay_id)
    who = _require(initials, "Initials")
    r = state.cached_replay(conn)
    run = r.bay_current.get(bay_id)
    if run is None:
        raise ActionError("That bay has nothing running to complete.")

    row = events.append(conn, "COMPLETE_BAY", bay_id=bay_id, work_order=run.work_order,
                        product_number=run.product_number,
                        component_label=run.component_label, initials=who)
    return _finish(conn, row)


def mate(conn, keep_bay_id, release_bay_id, initials):
    """Merge two occupied bays into one continuing unit.

    The unit continues in ``keep_bay_id`` and ``release_bay_id`` frees up. When
    both bays carry the SAME work order this simply joins the two parallel
    halves. When they carry DIFFERENT work orders, the kept bay's work order is
    the survivor and the released bay's unit is recorded as 'merged' (so it is
    not left as dangling WIP) -- see state.replay's MATE handling.
    """
    _bay(conn, keep_bay_id)
    _bay(conn, release_bay_id)
    who = _require(initials, "Initials")
    if keep_bay_id == release_bay_id:
        raise ActionError("Merge needs two different bays.")

    r = state.cached_replay(conn)
    keep = r.bay_current.get(keep_bay_id)
    rel = r.bay_current.get(release_bay_id)
    if keep is None or rel is None:
        raise ActionError("Both bays must be occupied to merge.")

    row = events.append(conn, "MATE", bay_id=keep_bay_id, target_bay_id=release_bay_id,
                        work_order=keep.work_order, product_number=keep.product_number,
                        component_label=keep.component_label, initials=who)
    return _finish(conn, row)


# ---------------------------------------------------------------------------
# Delays
# ---------------------------------------------------------------------------

def flag_delay(conn, bay_id, reason_id, note, initials):
    """Pause active, start delay, turn the bay red, fire the takeover."""
    _bay(conn, bay_id)
    who = _require(initials, "Initials")
    note = _require(note, "Note")          # a note is required on EVERY delay (>=1 char)

    reason = conn.execute(
        "SELECT * FROM delay_reasons WHERE id = ? AND active = 1;", (reason_id,)
    ).fetchone()
    if reason is None:
        raise ActionError("Pick a delay reason.")

    r = state.cached_replay(conn)
    run = r.bay_current.get(bay_id)
    if run is None:
        raise ActionError("That bay isn't running, so it can't be delayed.")
    if run.current_pause is not None:
        raise ActionError("That bay is paused. Resume it before flagging a delay.")
    if run.current_delay is not None:
        raise ActionError("That bay is already flagged as delayed.")

    # Snapshot the reason's division + control tag so renaming/retiring the
    # reason later can never rewrite this delay's history.
    division = None
    if reason["division_id"]:
        drow = conn.execute("SELECT name FROM divisions WHERE id = ?;",
                            (reason["division_id"],)).fetchone()
        division = drow["name"] if drow else None

    row = events.append(conn, "DELAY_START", bay_id=bay_id, work_order=run.work_order,
                        product_number=run.product_number,
                        delay_reason_id=reason["id"], reason_label=reason["label"],
                        division=division, in_out_of_control=reason["in_out_of_control"],
                        note=note, initials=who)
    return _finish(conn, row)


def clear_delay(conn, bay_id, initials):
    """Stop the delay, resume active."""
    _bay(conn, bay_id)
    who = _require(initials, "Initials")
    r = state.cached_replay(conn)
    run = r.bay_current.get(bay_id)
    if run is None or run.current_delay is None:
        raise ActionError("That bay isn't currently delayed.")

    row = events.append(conn, "DELAY_CLEAR", bay_id=bay_id, work_order=run.work_order,
                        product_number=run.product_number, initials=who)
    return _finish(conn, row)


# ---------------------------------------------------------------------------
# Shift staffing / parked bays
#
# At a short-staffed shift changeover, occupied bays that won't be worked are
# PAUSED ("parked"): their clocks freeze (active/cycle/elapsed/total stop, like a
# break) and they raise no delay alerts, so nobody is credited with work nobody
# did. Resuming returns the bay to normal monitoring where it left off.
# ---------------------------------------------------------------------------

def pause_bay(conn, bay_id, initials):
    """Park an occupied bay (freeze its clocks; suppress alerts)."""
    _bay(conn, bay_id)
    who = _require(initials, "Initials")
    r = state.cached_replay(conn)
    run = r.bay_current.get(bay_id)
    if run is None:
        raise ActionError("That bay is empty — there's nothing to pause.")
    if run.current_pause is not None:
        raise ActionError("That bay is already paused.")

    row = events.append(conn, "PAUSE", bay_id=bay_id, work_order=run.work_order,
                        product_number=run.product_number,
                        component_label=run.component_label, initials=who)
    return _finish(conn, row)


def resume_bay(conn, bay_id, initials):
    """Un-park a paused bay: it returns to normal idle/running/delayed."""
    _bay(conn, bay_id)
    who = _require(initials, "Initials")
    r = state.cached_replay(conn)
    run = r.bay_current.get(bay_id)
    if run is None or run.current_pause is None:
        raise ActionError("That bay isn't paused.")

    row = events.append(conn, "RESUME", bay_id=bay_id, work_order=run.work_order,
                        product_number=run.product_number, initials=who)
    return _finish(conn, row)


def shift_changeover(conn, pause_ids, resume_ids, initials):
    """Apply a whole shift's staffing in one save (the changeover pop-up).

    ``pause_ids`` are occupied bays to park; ``resume_ids`` are parked bays to
    re-staff. Each bay is idempotent (one already in the desired state is simply
    skipped), so confirming with no real change is harmless. Bays are evaluated
    against the state at entry, so the order of the batch doesn't matter.
    """
    who = _require(initials, "Initials")
    pause_ids = [int(x) for x in (pause_ids or [])]
    resume_ids = [int(x) for x in (resume_ids or [])]

    # One shared action_group across the whole batch so the console Undo reverses
    # the entire changeover (every parked/re-staffed bay) as a single action.
    group = uuid.uuid4().hex

    r = state.cached_replay(conn)
    changed = 0
    for bid in pause_ids:
        run = r.bay_current.get(bid)
        if run is not None and run.current_pause is None:
            events.append(conn, "PAUSE", action_group=group, bay_id=bid,
                          work_order=run.work_order,
                          product_number=run.product_number,
                          component_label=run.component_label, initials=who)
            changed += 1
    for bid in resume_ids:
        run = r.bay_current.get(bid)
        if run is not None and run.current_pause is not None:
            events.append(conn, "RESUME", action_group=group, bay_id=bid,
                          work_order=run.work_order,
                          product_number=run.product_number, initials=who)
            changed += 1

    state.invalidate_cache()
    return None   # a batch has no single representative event row


# ---------------------------------------------------------------------------
# Undo (console). Reverse the most recent floor action by appending a VOID row
# for each event in its action_group; state.replay then skips those rows. The
# original rows and their VOIDs both stay in the append-only log -- nothing is
# deleted, so the audit trail and exports' raw Events sheet stay complete.
# ---------------------------------------------------------------------------

def undo_last(conn, initials, expect_event_id=None, now=None):
    """Undo the most recent undoable action (see state.UNDOABLE_TYPES).

    Bounded to the current shift (state.undo_floor) so a new shift can't rewrite
    an earlier one. ``expect_event_id`` is a race token from the snapshot: if the
    floor has moved on since the operator opened Undo, we refuse rather than
    silently undo something else. Pressing Undo again walks one action further
    back. Returns None (an undo may void several rows; it has no single one)."""
    who = _require(initials, "Initials")
    now = now or datetime.now()

    r = state.cached_replay(conn)
    members = r.undo_members
    if not members:
        raise ActionError("There's nothing to undo.")

    sched = Schedule.from_settings(conn)
    earliest = min(events.parse_ts(m["ts"]) for m in members)
    if earliest < state.undo_floor(sched, now):
        raise ActionError("That action is from an earlier shift — undo only "
                          "reaches back to the start of this shift.")

    newest_id = max(m["id"] for m in members)
    if expect_event_id is not None and int(expect_event_id) != newest_id:
        raise ActionError("The floor changed since you opened Undo — "
                          "check the board and try again.")

    group = uuid.uuid4().hex
    for m in members:
        events.append(conn, "VOID", action_group=group,
                      supersedes_event_id=m["id"], bay_id=m["bay_id"],
                      work_order=m["work_order"], product_number=m["product_number"],
                      note=f"Undo {m['type']} (event #{m['id']})", initials=who)
    return _finish(conn, None)


# ---------------------------------------------------------------------------
# Terminal outcomes
# ---------------------------------------------------------------------------

def _terminal(conn, etype, work_order, initials):
    who = _require(initials, "Initials")
    wo = _require(work_order, "Work Order")
    r = state.cached_replay(conn)
    unit = r.units.get(wo)
    if unit is None:
        raise ActionError(f"No work order {wo} is active.")
    if not unit.is_open:
        raise ActionError(f"Work order {wo} is already {unit.outcome}.")
    row = events.append(conn, etype, work_order=wo,
                        product_number=unit.product_number, initials=who)
    return _finish(conn, row)


def unit_complete(conn, work_order, initials):
    """Terminal: the unit is done and leaves the area."""
    return _terminal(conn, "UNIT_COMPLETE", work_order, initials)


# NOTE: scrapping was removed as a loggable action (2026-06). The SCRAP event
# type still exists in events.py/state.py so any historical scrap rows in an
# existing log keep replaying correctly -- but nothing can create new ones.


# ---------------------------------------------------------------------------
# Corrections (Stats page). Each logs a CORRECTION event and leaves the
# original event intact -- the audit trail stays honest (spec section 6c).
# ---------------------------------------------------------------------------

def _parse_ts_or_error(value: str) -> datetime:
    # "ended now" is stamped server-side and rounded to the nearest minute (like
    # every logged event), never trusting a client clock. Rounding is monotonic,
    # so a rounded "now" is still always >= an already-rounded start time.
    if not value or _clean(value).lower() == "now":
        return events.round_to_minute(datetime.now())
    value = _clean(value)
    for fmt in (config.TS_FORMAT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ActionError(f"Could not understand the time '{value}'. Use YYYY-MM-DD HH:MM.")


def correct_event_time(conn, event_id, new_ts, initials, note=None):
    """Supersede an earlier event's effective TIME (e.g. a run logged late)."""
    who = _require(initials, "Initials")
    target = conn.execute("SELECT * FROM events WHERE id = ?;", (event_id,)).fetchone()
    if target is None:
        raise ActionError("That event no longer exists.")
    when = _parse_ts_or_error(new_ts)
    new_ts_str = when.strftime(config.TS_FORMAT)

    row = events.append(conn, "CORRECTION", supersedes_event_id=event_id,
                        corrected_ts=new_ts_str, bay_id=target["bay_id"],
                        work_order=target["work_order"],
                        product_number=target["product_number"],
                        note=_clean(note) or f"Retime event #{event_id}", initials=who)
    return _finish(conn, row)


def close_open_delay(conn, bay_id, ended_at, initials, note=None):
    """Fix a forgotten clear: close a still-open delay at the time it really ended."""
    _bay(conn, bay_id)
    who = _require(initials, "Initials")
    r = state.cached_replay(conn)
    run = r.bay_current.get(bay_id)
    if run is None or run.current_delay is None:
        raise ActionError("That bay has no open delay to close.")
    when = _parse_ts_or_error(ended_at)
    if when < run.current_delay.started:
        raise ActionError("The end time is before the delay started.")
    new_ts_str = when.strftime(config.TS_FORMAT)

    row = events.append(conn, "CORRECTION", acts_as="DELAY_CLEAR", bay_id=bay_id,
                        work_order=run.work_order, product_number=run.product_number,
                        supersedes_event_id=run.current_delay.start_event_id,
                        corrected_ts=new_ts_str,
                        note=_clean(note) or "Closed a forgotten-open delay",
                        initials=who)
    return _finish(conn, row)


def close_open_run(conn, bay_id, ended_at, initials, terminal=False, note=None):
    """Close a still-open run at the time it really ended.

    ``terminal=False`` sends the unit to the queue (COMPLETE_BAY); ``True`` ends
    the unit's whole journey (UNIT_COMPLETE).
    """
    _bay(conn, bay_id)
    who = _require(initials, "Initials")
    r = state.cached_replay(conn)
    run = r.bay_current.get(bay_id)
    if run is None:
        raise ActionError("That bay has no open run to close.")
    when = _parse_ts_or_error(ended_at)
    if when < run.started:
        raise ActionError("The end time is before the run started.")
    new_ts_str = when.strftime(config.TS_FORMAT)
    acts_as = "UNIT_COMPLETE" if terminal else "COMPLETE_BAY"

    row = events.append(conn, "CORRECTION", acts_as=acts_as, bay_id=bay_id,
                        work_order=run.work_order, product_number=run.product_number,
                        supersedes_event_id=run.start_event_id, corrected_ts=new_ts_str,
                        note=_clean(note) or f"Closed a forgotten-open run ({acts_as})",
                        initials=who)
    return _finish(conn, row)
