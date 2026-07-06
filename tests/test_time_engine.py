"""
test_time_engine.py -- regression tests for the time-accounting rules.

The time math is the most subtle, most important logic in the system, so it has
a dedicated test. Run it after touching schedule.py or state.py:

    python tests/test_time_engine.py

It uses a throwaway temp database and asserts the exact spec rules:
  * breaks and off-hours are non-counting time (both active AND delay pause),
  * parallel work in two bays counts the UNION of active periods, never the sum,
  * delay time is partitioned out of active time,
  * queue time accrues while a unit occupies zero bays,
  * shift attribution uses clean cutoffs and wraps past midnight.

No pytest required (keeps the dependency list minimal). Exits non-zero on any
failure so it can gate an update.
"""

import os
import shutil
import sys
import tempfile
import uuid as _uuid
from datetime import datetime

# Make the repo importable and point the app at a temp data dir BEFORE import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="bt_test_")
os.environ["BAYTRACKER_DATA"] = _TMP

from baytracker import db, bootstrap, events, state          # noqa: E402
from baytracker.schedule import Schedule                      # noqa: E402

DAY = "2026-06-10"   # a Wednesday
_FAILS = []


def T(hhmm):
    return datetime.strptime(f"{DAY} {hhmm}:00", "%Y-%m-%d %H:%M:%S")


def check(name, got, want):
    ok = abs(got - want) < 1e-6
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got}, want {want}")
    if not ok:
        _FAILS.append(name)


def fresh_conn():
    """An ISOLATED database per test (a unique file), like test_undo.py.

    pytest imports every test module into ONE process, so the default
    db.connect() path (frozen at first import of baytracker.config) would be
    shared across modules -- and test_notify.py replaces the seeded bays in
    its database. A unique file per test makes the isolation unconditional.
    """
    state.invalidate_cache()   # the replay cache is keyed by max event id; reset per DB
    conn = db.connect(os.path.join(_TMP, f"t_{_uuid.uuid4().hex}.db"))
    db.create_schema(conn)
    bootstrap.seed(conn)
    # 24/7 operating, no breaks, unless a test overrides it.
    db.set_setting(conn, "operating_calendar",
                   {k: [["00:00", "24:00"]] for k in
                    ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    db.set_setting(conn, "break_schedule", [])
    return conn


def bay(conn, n):
    return conn.execute("SELECT id FROM bays WHERE sort_order=?", (n,)).fetchone()["id"]


def unit_durs(conn, wo):
    r = state.replay(conn)
    sched = Schedule.from_settings(conn)
    return state.unit_durations(sched, r.units[wo], r.runs, datetime.now())


def test_schedule_breaks_and_offhours():
    print("== breaks + off-hours are non-counting ==")
    s = Schedule(
        operating_calendar={k: [["00:00", "24:00"]] for k in
                            ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        break_schedule=[{"start": "12:00", "minutes": 30, "label": "Lunch"}],
        shifts=[])
    check("11:00-13:00 spans 30m lunch -> 90m", s.counted_seconds(T("11:00"), T("13:00")) / 60, 90)
    check("12:10-12:20 inside lunch -> 0", s.counted_seconds(T("12:10"), T("12:20")) / 60, 0)
    check("is_counting during break = 0", float(s.is_counting(T("12:15"))), 0.0)

    s2 = Schedule(operating_calendar={"wed": [["06:00", "14:00"]]}, break_schedule=[], shifts=[])
    check("13:00-15:00 with 14:00 cutoff -> 60m", s2.counted_seconds(T("13:00"), T("15:00")) / 60, 60)
    check("02:00-05:00 off-hours -> 0", s2.counted_seconds(T("02:00"), T("05:00")) / 60, 0)


def test_shift_attribution():
    print("== shift attribution with midnight wrap ==")
    s = Schedule(None, [], [{"name": "S1", "start": "06:00"},
                            {"name": "S2", "start": "14:00"},
                            {"name": "S3", "start": "22:00"}])
    check("07:00 -> S1", float(s.shift_for(T("07:00")) == "S1"), 1.0)
    check("15:00 -> S2", float(s.shift_for(T("15:00")) == "S2"), 1.0)
    check("02:00 wraps -> S3", float(s.shift_for(T("02:00")) == "S3"), 1.0)


def test_parallel_union():
    print("== parallel work counts the UNION, not the sum ==")
    conn = fresh_conn()
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=bay(conn, 1), work_order="X", product_number="P")
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=bay(conn, 2), work_order="X", product_number="P")
    events.append(conn, "COMPLETE_BAY", ts=f"{DAY} 11:00:00", bay_id=bay(conn, 2), work_order="X")
    events.append(conn, "UNIT_COMPLETE", ts=f"{DAY} 11:30:00", work_order="X")
    d = unit_durs(conn, "X")
    check("active = 90m union (not 150m sum)", d["active"] / 60, 90)
    check("cycle = 90m", d["cycle"] / 60, 90)
    conn.close()


def test_delay_partition_and_break():
    print("== delay partitions out of active; break pauses delay ==")
    conn = fresh_conn()
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=bay(conn, 1), work_order="Y", product_number="P")
    events.append(conn, "DELAY_START", ts=f"{DAY} 10:20:00", bay_id=bay(conn, 1), work_order="Y", reason_label="R")
    events.append(conn, "DELAY_CLEAR", ts=f"{DAY} 10:50:00", bay_id=bay(conn, 1), work_order="Y")
    events.append(conn, "UNIT_COMPLETE", ts=f"{DAY} 11:00:00", work_order="Y")
    d = unit_durs(conn, "Y")
    check("active 30m", d["active"] / 60, 30)
    check("delay 30m", d["delay"] / 60, 30)
    conn.close()

    conn = fresh_conn()
    db.set_setting(conn, "break_schedule", [{"start": "12:00", "minutes": 30, "label": "Lunch"}])
    events.append(conn, "START", ts=f"{DAY} 11:00:00", bay_id=bay(conn, 1), work_order="Z", product_number="P")
    events.append(conn, "DELAY_START", ts=f"{DAY} 11:50:00", bay_id=bay(conn, 1), work_order="Z", reason_label="R")
    events.append(conn, "UNIT_COMPLETE", ts=f"{DAY} 12:40:00", work_order="Z")
    d = unit_durs(conn, "Z")
    check("active 50m before delay", d["active"] / 60, 50)
    check("delay 20m (break removed)", d["delay"] / 60, 20)
    conn.close()


def test_queue_time():
    print("== queue time accrues in the WIP pool ==")
    conn = fresh_conn()
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=bay(conn, 1), work_order="Q", product_number="P")
    events.append(conn, "COMPLETE_BAY", ts=f"{DAY} 10:30:00", bay_id=bay(conn, 1), work_order="Q")
    events.append(conn, "START", ts=f"{DAY} 11:00:00", bay_id=bay(conn, 2), work_order="Q", product_number="P")
    events.append(conn, "UNIT_COMPLETE", ts=f"{DAY} 11:15:00", work_order="Q")
    d = unit_durs(conn, "Q")
    check("active 45m", d["active"] / 60, 45)
    check("queue 30m", d["queue"] / 60, 30)
    check("cycle 75m", d["cycle"] / 60, 75)
    conn.close()


def test_shift_windows():
    print("== shift start/end windows (incl. midnight wrap) ==")
    s = Schedule(None, [], [{"name": "Day",     "start": "06:00", "end": "14:00"},
                            {"name": "Evening", "start": "14:00", "end": "22:00"},
                            {"name": "Night",   "start": "22:00", "end": "06:00"}])
    check("07:00 -> Day", float(s.shift_for(T("07:00")) == "Day"), 1.0)
    check("14:00 boundary -> Evening", float(s.shift_for(T("14:00")) == "Evening"), 1.0)
    check("23:00 -> Night", float(s.shift_for(T("23:00")) == "Night"), 1.0)
    check("02:00 wraps -> Night", float(s.shift_for(T("02:00")) == "Night"), 1.0)


def test_multi_bay_parallel_journey():
    print("== one unit: parallel start in 2 bays, mate, then 2 more bays (4 total) ==")
    conn = fresh_conn()
    b1, b2, b3, b4 = bay(conn, 1), bay(conn, 2), bay(conn, 3), bay(conn, 4)
    # Two halves assembled simultaneously in two bays...
    events.append(conn, "START", ts=f"{DAY} 08:00:00", bay_id=b1, work_order="M",
                  product_number="P", component_label="Half A")
    events.append(conn, "START", ts=f"{DAY} 08:00:00", bay_id=b2, work_order="M",
                  product_number="P", component_label="Half B")
    # ...mated into one unit (continues in bay 1, bay 2 frees up)...
    events.append(conn, "MATE", ts=f"{DAY} 10:00:00", bay_id=b1, target_bay_id=b2,
                  work_order="M")
    # ...then travels through two more bays before completing (4 bays total).
    events.append(conn, "MOVE", ts=f"{DAY} 11:00:00", bay_id=b1, target_bay_id=b3,
                  work_order="M", product_number="P")
    events.append(conn, "MOVE", ts=f"{DAY} 12:00:00", bay_id=b3, target_bay_id=b4,
                  work_order="M", product_number="P")
    events.append(conn, "UNIT_COMPLETE", ts=f"{DAY} 13:00:00", work_order="M")

    r = state.replay(conn)
    u = r.units["M"]
    check("visited 4 distinct bays", float(len(set(u.bays_visited))), 4.0)
    check("unit was mated", float(u.mated), 1.0)
    check("journey closed as complete", float(u.outcome == "complete"), 1.0)
    check("no bay still occupied", float(len(u.occupied_bays)), 0.0)
    d = unit_durs(conn, "M")
    check("active = 300m union (parallel not double-counted)", d["active"] / 60, 300)
    check("queue 0m (always in a bay)", d["queue"] / 60, 0)
    check("cycle = 300m", d["cycle"] / 60, 300)
    conn.close()


def test_complete_bay_keeps_part_in_bay():
    print("== complete-at-bay keeps the part in the bay (DONE), waiting counts as queue ==")
    conn = fresh_conn()
    b1 = bay(conn, 1)
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=b1, work_order="D", product_number="P")
    events.append(conn, "COMPLETE_BAY", ts=f"{DAY} 10:30:00", bay_id=b1, work_order="D")
    # 10:30 onward: work done, part still sits in bay 1 (DONE), not freed.
    snap = state.live_snapshot(conn, now=T("11:00"))
    tile = next(t for t in snap["tiles"] if t["bay_id"] == b1)
    check("bay still occupied (not IDLE)", float(tile["status"] == "DONE"), 1.0)
    check("bay active time frozen at 30m", tile["elapsed_seconds"] / 60.0, 30)
    check("unit elapsed (linear) 60m", tile["unit_elapsed_seconds"] / 60.0, 60)
    check("unit total (occupancy, 1 bay) 60m", tile["unit_total_seconds"] / 60.0, 60)
    # Now finish the unit at 11:00; the 10:30->11:00 wait is queue, active stays 30.
    events.append(conn, "UNIT_COMPLETE", ts=f"{DAY} 11:00:00", work_order="D")
    d = unit_durs(conn, "D")
    check("active 30m (stopped at work-done)", d["active"] / 60, 30)
    check("queue 30m (waited in the bay)", d["queue"] / 60, 30)
    check("cycle 60m", d["cycle"] / 60, 60)
    conn.close()


def test_pause_freezes_clock():
    print("== a paused (parked) bay freezes active/cycle/elapsed/total, like a break ==")
    conn = fresh_conn()
    b1 = bay(conn, 1)
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=b1, work_order="PZ", product_number="P")
    events.append(conn, "PAUSE", ts=f"{DAY} 10:30:00", bay_id=b1, work_order="PZ")
    # During the pause (at 11:00) every clock is frozen at the 30m mark.
    snap = state.live_snapshot(conn, now=T("11:00"))
    tile = next(t for t in snap["tiles"] if t["bay_id"] == b1)
    check("tile reads PAUSED", float(tile["status"] == "PAUSED"), 1.0)
    check("active frozen at 30m", tile["elapsed_seconds"] / 60.0, 30)
    check("unit elapsed frozen at 30m", tile["unit_elapsed_seconds"] / 60.0, 30)
    check("unit total frozen at 30m", tile["unit_total_seconds"] / 60.0, 30)
    # Resume at 12:00 (90m parked), finish at 12:30.
    events.append(conn, "RESUME", ts=f"{DAY} 12:00:00", bay_id=b1, work_order="PZ")
    events.append(conn, "UNIT_COMPLETE", ts=f"{DAY} 12:30:00", work_order="PZ")
    d = unit_durs(conn, "PZ")
    check("active 60m (10:00-10:30 + 12:00-12:30; pause excluded)", d["active"] / 60, 60)
    check("cycle 60m (90m pause is non-counting, not queue)", d["cycle"] / 60, 60)
    check("queue 0m (pause is not waiting)", d["queue"] / 60, 0)
    conn.close()


def test_pause_ends_open_delay():
    print("== pausing a delayed bay ends the delay at pause time ==")
    conn = fresh_conn()
    b1 = bay(conn, 1)
    events.append(conn, "START", ts=f"{DAY} 09:00:00", bay_id=b1, work_order="PD", product_number="P")
    events.append(conn, "DELAY_START", ts=f"{DAY} 09:20:00", bay_id=b1, work_order="PD", reason_label="R")
    events.append(conn, "PAUSE", ts=f"{DAY} 09:50:00", bay_id=b1, work_order="PD")
    r = state.replay(conn)
    run = r.bay_current[b1]
    check("bay is paused", float(run.current_pause is not None), 1.0)
    check("no delay left open", float(run.current_delay is None), 1.0)
    check("the delay closed at pause time (30m)",
          state.counted_over(Schedule.from_settings(conn), run.delayed_intervals(T("11:00"))) / 60, 30)
    conn.close()


def test_merge_total_vs_elapsed():
    print("== merged unit: total (summed bays) exceeds elapsed (union) ==")
    conn = fresh_conn()
    b1, b2 = bay(conn, 1), bay(conn, 2)
    # Two halves run in parallel for 60m, then merge into bay 1 and keep going.
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=b1, work_order="G", product_number="P", component_label="A")
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=b2, work_order="G", product_number="P", component_label="B")
    events.append(conn, "MATE", ts=f"{DAY} 11:00:00", bay_id=b1, target_bay_id=b2, work_order="G")
    snap = state.live_snapshot(conn, now=T("11:30"))
    tile = next(t for t in snap["tiles"] if t["bay_id"] == b1)
    check("bay 2 freed after merge", float(any(t["bay_id"] == b2 and t["status"] == "IDLE" for t in snap["tiles"])), 1.0)
    # elapsed (linear) = 10:00->11:30 = 90m; total (occupancy) = bay1(90) + bay2(60) = 150m.
    check("elapsed 90m (linear)", tile["unit_elapsed_seconds"] / 60.0, 90)
    check("total 150m (summed bay occupancy)", tile["unit_total_seconds"] / 60.0, 150)
    conn.close()


def test_merge_different_work_orders():
    print("== merging two different work orders: target survives, source 'merged' ==")
    conn = fresh_conn()
    b1, b2 = bay(conn, 1), bay(conn, 2)
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=b1, work_order="A", product_number="P")
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=b2, work_order="B", product_number="P")
    # merge B (bay 2) into A (bay 1): A continues, B ends as 'merged'.
    events.append(conn, "MATE", ts=f"{DAY} 11:00:00", bay_id=b1, target_bay_id=b2, work_order="A")
    r = state.replay(conn)
    check("A still open in bay 1", float(r.units["A"].is_open and b1 in r.units["A"].occupied_bays), 1.0)
    check("B ended as 'merged'", float(r.units["B"].outcome == "merged"), 1.0)
    check("B no longer open WIP", float(not r.units["B"].is_open), 1.0)
    conn.close()


def test_merge_different_wo_live_times():
    print("== merging two different cells: survivor's total adds up, elapsed ties to earliest ==")
    conn = fresh_conn()
    b1, b2 = bay(conn, 1), bay(conn, 2)
    # An EXISTING cell (EX) has been running a while; a NEW cell (NW) just started.
    # (Distinct work orders, since the test DB is shared across the whole run.)
    events.append(conn, "START", ts=f"{DAY} 10:00:00", bay_id=b1, work_order="EX", product_number="P")
    events.append(conn, "START", ts=f"{DAY} 10:30:00", bay_id=b2, work_order="NW", product_number="P")
    # Merge the existing cell (EX, bay 1) INTO the new cell (NW, bay 2): NW survives.
    events.append(conn, "MATE", ts=f"{DAY} 10:45:00", bay_id=b2, target_bay_id=b1, work_order="NW")
    snap = state.live_snapshot(conn, now=T("11:00"))
    tile = next(t for t in snap["tiles"] if t["bay_id"] == b2)
    check("released cell (bay 1) freed to IDLE",
          float(any(t["bay_id"] == b1 and t["status"] == "IDLE" for t in snap["tiles"])), 1.0)
    # total = B bay2 occupancy (10:30->11:00 = 30m) + absorbed A bay1 (10:00->10:45 = 45m).
    check("total 75m (absorbed cell's bay-time added)", tile["unit_total_seconds"] / 60.0, 75)
    # elapsed = linear from the EARLIEST cell's start (A at 10:00) to now (11:00).
    check("elapsed 60m (ties to earliest cell's start)", tile["unit_elapsed_seconds"] / 60.0, 60)
    conn.close()


def test_bay_utilization_never_exceeds_100():
    print("== bay utilization is a true percentage (never > 100%) ==")
    from baytracker import metrics
    conn = fresh_conn()
    b1 = bay(conn, 1)
    # A 4-hour run reported over a 1-hour window inside it. The OLD math summed
    # the whole run's 240 min against 60 available -> 400%. Clamped occupancy is
    # the 60 in-window minutes -> exactly 100%, the physical ceiling for one bay.
    D2 = "2026-06-15"   # a clean day no other test touches (shared test DB)
    events.append(conn, "START", ts=f"{D2} 10:00:00", bay_id=b1, work_order="UTIL", product_number="P")
    events.append(conn, "UNIT_COMPLETE", ts=f"{D2} 14:00:00", work_order="UTIL")
    stats = metrics.compute(conn, {"start": f"{D2} 10:00:00", "end": f"{D2} 11:00:00"})
    util = {r["bay"]: r["utilization_pct"] for r in stats["bay_utilization"]}
    check("bay 1 == 100% (full window, clamped)", util.get("Bay 1"), 100.0)
    check("no bay exceeds 100%", float(max((v or 0) for v in util.values()) <= 100.0), 1.0)
    conn.close()


def test_event_timestamps_round_to_minute():
    print("== newly-logged events round to the nearest minute ==")
    from baytracker.events import round_to_minute
    r = round_to_minute
    check(":29 rounds down",
          float(r(datetime(2026, 6, 10, 10, 0, 29)) == datetime(2026, 6, 10, 10, 0, 0)), 1.0)
    check(":30 rounds up (tie)",
          float(r(datetime(2026, 6, 10, 10, 0, 30)) == datetime(2026, 6, 10, 10, 1, 0)), 1.0)
    check(":59 carries across the hour",
          float(r(datetime(2026, 6, 10, 10, 59, 59)) == datetime(2026, 6, 10, 11, 0, 0)), 1.0)
    check("microseconds dropped",
          float(r(datetime(2026, 6, 10, 10, 0, 10, 500000)) == datetime(2026, 6, 10, 10, 0, 0)), 1.0)


def main():
    try:
        test_event_timestamps_round_to_minute()
        test_bay_utilization_never_exceeds_100()
        test_schedule_breaks_and_offhours()
        test_shift_attribution()
        test_shift_windows()
        test_parallel_union()
        test_delay_partition_and_break()
        test_queue_time()
        test_multi_bay_parallel_journey()
        test_complete_bay_keeps_part_in_bay()
        test_pause_freezes_clock()
        test_pause_ends_open_delay()
        test_merge_total_vs_elapsed()
        test_merge_different_work_orders()
        test_merge_different_wo_live_times()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + ("ALL TIME-ENGINE TESTS PASSED" if not _FAILS else f"FAILURES: {_FAILS}"))
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    main()
