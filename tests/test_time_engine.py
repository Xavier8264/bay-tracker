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
    conn = db.connect()
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


def main():
    try:
        test_schedule_breaks_and_offhours()
        test_shift_attribution()
        test_parallel_union()
        test_delay_partition_and_break()
        test_queue_time()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + ("ALL TIME-ENGINE TESTS PASSED" if not _FAILS else f"FAILURES: {_FAILS}"))
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    main()
