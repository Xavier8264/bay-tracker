"""
test_undo.py -- regression tests for the console Undo (VOID events).

Undo is an append-only reversal: pressing Undo writes a VOID row that points at
the most recent action's event(s); state.replay then skips those rows, so the
action is undone WITHOUT deleting anything. These tests assert:

  * a merge/move/etc. is structurally reversed by one Undo,
  * Undo walks back action-by-action and stops when nothing is left,
  * a multi-row action (shift changeover) is undone atomically,
  * Undo is bounded to the current shift (can't rewrite a prior shift),
  * the original rows AND their VOIDs both stay in the log (audit intact),
  * the race token and the initials requirement are enforced.

Same lightweight, pytest-free harness as test_time_engine.py:

    python tests/test_undo.py

Exits non-zero on any failure so it can gate an update.
"""

import os
import shutil
import sys
import tempfile
import uuid as _uuid
from datetime import datetime

# Make the repo importable and point the app at a temp data dir BEFORE import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="bt_undo_test_")
os.environ["BAYTRACKER_DATA"] = _TMP

from baytracker import db, bootstrap, events, state, actions      # noqa: E402
from baytracker.actions import ActionError                        # noqa: E402

DAY = "2026-06-10"   # a Wednesday
_FAILS = []


def T(hhmm):
    return datetime.strptime(f"{DAY} {hhmm}:00", "%Y-%m-%d %H:%M:%S")


def check(name, got, want):
    ok = (got == want) if isinstance(want, bool) else abs(got - want) < 1e-6
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got}, want {want}")
    if not ok:
        _FAILS.append(name)


def fresh_conn():
    """An ISOLATED database per test (a unique file), so 'undo the last action'
    never reaches into another test's events. 24/7 operating, no breaks/shifts
    unless a test overrides it."""
    state.invalidate_cache()   # the replay cache is keyed by max-id; reset per DB
    conn = db.connect(os.path.join(_TMP, f"t_{_uuid.uuid4().hex}.db"))
    db.create_schema(conn)
    bootstrap.seed(conn)
    db.set_setting(conn, "operating_calendar",
                   {k: [["00:00", "24:00"]] for k in
                    ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    db.set_setting(conn, "break_schedule", [])
    return conn


def bay(conn, n):
    return conn.execute("SELECT id FROM bays WHERE sort_order=?", (n,)).fetchone()["id"]


def other_reason(conn):
    return conn.execute("SELECT id FROM delay_reasons WHERE is_other=1;").fetchone()["id"]


def raises(fn):
    try:
        fn()
        return False
    except ActionError:
        return True


# ---------------------------------------------------------------------------

def test_undo_reverses_merge():
    print("== one Undo reverses an accidental merge ==")
    conn = fresh_conn()
    b1, b2 = bay(conn, 1), bay(conn, 2)
    actions.start(conn, b1, "UA", "P1", "JP")
    actions.start(conn, b2, "UB", "P2", "JP")
    actions.mate(conn, b1, b2, "JP")            # keep b1, release b2; UB merged into UA

    r = state.replay(conn)
    check("merge freed bay 2", b2 not in r.bay_current, True)
    check("merge ended UB as 'merged'", r.units["UB"].outcome == "merged", True)

    snap = state.live_snapshot(conn)
    check("Undo offered after merge", bool(snap["undo"]["available"]), True)
    check("Undo label names the merge", "Merge" in (snap["undo"]["summary"] or ""), True)

    actions.undo_last(conn, "JP")
    r = state.replay(conn)
    check("Undo reopened bay 2", b2 in r.bay_current, True)
    check("Undo reopened UB as live WIP", r.units["UB"].is_open, True)
    check("Undo left UA running in bay 1", b1 in r.bay_current, True)
    conn.close()


def test_multi_level_walks_back_then_stops():
    print("== Undo walks back action-by-action, then reports nothing left ==")
    conn = fresh_conn()
    b1 = bay(conn, 1)
    actions.start(conn, b1, "ML", "P", "JP")
    actions.flag_delay(conn, b1, other_reason(conn), "stuck", "JP")

    run = state.replay(conn).bay_current.get(b1)
    check("bay is delayed before any undo", run is not None and run.current_delay is not None, True)

    actions.undo_last(conn, "JP")               # undo the delay
    run = state.replay(conn).bay_current.get(b1)
    check("undo #1: running, delay gone", run is not None and run.current_delay is None, True)

    actions.undo_last(conn, "JP")               # undo the start
    r = state.replay(conn)
    check("undo #2: bay idle again", b1 not in r.bay_current, True)

    check("undo #3 refuses (nothing left)", raises(lambda: actions.undo_last(conn, "JP")), True)
    conn.close()


def test_shift_changeover_undone_atomically():
    print("== one Undo reverses a whole multi-bay shift changeover ==")
    conn = fresh_conn()
    b1, b2, b3 = bay(conn, 1), bay(conn, 2), bay(conn, 3)
    for b, wo in ((b1, "S1"), (b2, "S2"), (b3, "S3")):
        actions.start(conn, b, wo, "P", "JP")
    actions.shift_changeover(conn, [b1, b2, b3], [], "JP")   # park all three at once

    r = state.replay(conn)
    check("all three parked", all(r.bay_current[b].current_pause is not None
                                  for b in (b1, b2, b3)), True)

    snap = state.live_snapshot(conn)
    check("Undo label counts the batch", "3 bays" in (snap["undo"]["summary"] or ""), True)

    actions.undo_last(conn, "JP")               # a single undo
    r = state.replay(conn)
    check("one Undo un-parked all three", all(r.bay_current[b].current_pause is None
                                              for b in (b1, b2, b3)), True)
    conn.close()


def test_shift_floor_bounds_undo():
    print("== Undo only reaches back to the start of the current shift ==")
    conn = fresh_conn()
    db.set_setting(conn, "shifts", [{"name": "Day", "start": "06:00", "end": "18:00"},
                                    {"name": "Night", "start": "18:00", "end": "06:00"}])
    b1, b2 = bay(conn, 1), bay(conn, 2)
    # One action from the prior (Night) shift, one in the current (Day) shift.
    events.append(conn, "START", ts=f"{DAY} 05:00:00", bay_id=b1,
                  work_order="OLD", product_number="P", initials="JP")
    events.append(conn, "START", ts=f"{DAY} 06:30:00", bay_id=b2,
                  work_order="NEW", product_number="P", initials="JP")
    now = T("07:00")

    snap = state.live_snapshot(conn, now=now)
    check("Undo available within the shift", bool(snap["undo"]["available"]), True)
    check("Undo targets the in-shift action", "NEW" in (snap["undo"]["summary"] or ""), True)

    actions.undo_last(conn, "JP", now=now)      # undo NEW (the in-shift action)

    snap = state.live_snapshot(conn, now=now)
    check("Undo now blocked (prior shift)", bool(snap["undo"]["available"]), False)
    check("undo of a prior-shift action refused",
          raises(lambda: actions.undo_last(conn, "JP", now=now)), True)
    conn.close()


def test_void_rows_persist_in_log():
    print("== Undo deletes nothing: original + VOID both remain in the log ==")
    conn = fresh_conn()
    b1 = bay(conn, 1)
    actions.start(conn, b1, "AUD", "P", "JP")
    before = len(events.all_events(conn))

    actions.undo_last(conn, "JP")
    rows = events.all_events(conn)
    check("original START still in the log",
          any(r["type"] == "START" and r["work_order"] == "AUD" for r in rows), True)
    check("a VOID row was appended", any(r["type"] == "VOID" for r in rows), True)
    check("log only grew (nothing deleted)", len(rows) == before + 1, True)
    conn.close()


def test_race_token_and_initials():
    print("== Undo enforces the race token and requires initials ==")
    conn = fresh_conn()
    b1 = bay(conn, 1)
    actions.start(conn, b1, "RT", "P", "JP")

    check("blank initials refused", raises(lambda: actions.undo_last(conn, "  ")), True)
    check("stale token refused",
          raises(lambda: actions.undo_last(conn, "JP", expect_event_id=999999)), True)

    tok = state.live_snapshot(conn)["undo"]["event_id"]
    actions.undo_last(conn, "JP", expect_event_id=tok)   # matching token works
    check("correct token undid the start", b1 not in state.replay(conn).bay_current, True)
    conn.close()


def main():
    try:
        test_undo_reverses_merge()
        test_multi_level_walks_back_then_stops()
        test_shift_changeover_undone_atomically()
        test_shift_floor_bounds_undo()
        test_void_rows_persist_in_log()
        test_race_token_and_initials()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + ("ALL UNDO TESTS PASSED" if not _FAILS else f"FAILURES: {_FAILS}"))
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    main()
