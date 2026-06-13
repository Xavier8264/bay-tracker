"""
make_demo_data.py -- build a SEPARATE, disposable demo database full of
realistic example data, for pitching the system to management.

    python make_demo_data.py                 # creates C:\\BayTrackerData_demo
    python make_demo_data.py --fresh         # regenerate (replaces demo DB only)

Then launch the server against it with:   .\\start.ps1 -Demo
...and return to the real log by simply restarting without -Demo. Deleting the
demo afterwards = deleting the demo folder. Nothing else changes.

WHY A SEPARATE DATABASE (read this before "improving" it):
The production log is append-only and must never contain fabricated rows
(spec Appendix C) -- mixing example data into the real file and deleting it
later would mean UPDATE/DELETE on the source of truth, exactly what the design
forbids. So demo data lives in a physically different folder/file and the real
log is never opened, never written, never at risk. This script enforces that:

  * it only writes into a folder whose name ends with "_demo";
  * it refuses to run against the live data folder (BAYTRACKER_DATA/default);
  * it refuses to touch any existing database unless that database says
    demo_mode=true (i.e. it was created by this script);
  * the generated DB carries demo_mode=true, which the UI shows as a
    "DEMO DATA" badge on every screen.

The events are generated through the same events.append() used in production
(with explicit backdated timestamps, which append() supports for exactly this
kind of use), so replay/stats/exports treat them like any real history.
"""

import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from baytracker import bootstrap, config, db, events, metrics, state

RNG = random.Random(20260612)   # deterministic: re-running yields the same demo

# ---------------------------------------------------------------------------
# Demo configuration (what /admin would normally be filled with)
# ---------------------------------------------------------------------------
DIVISIONS = ["Assembly", "Fabrication", "Paint", "Quality", "Materials"]

# (label, division, in/out of control)
REASONS = [
    ("Waiting on parts",      "Materials",   "out"),
    ("Machine down",          "Fabrication", "out"),
    ("Rework required",       "Quality",     "in"),
    ("Missing paperwork",     "Materials",   "in"),
    ("Paint queue full",      "Paint",       "out"),
    ("Waiting on engineering","Assembly",    "in"),
]

PRODUCTS = [
    ("0347",  "Control cabinet, 60A"),
    ("0512",  "Control cabinet, 100A"),
    ("1108",  "Operator pedestal"),
    ("2204",  "Conveyor drive unit"),
    ("7741",  "Hydraulic power pack"),
    ("8830",  "Custom skid frame"),
]

ROSTER = [("JP", "Jordan P."), ("MT", "Mike T."), ("DK", "Dana K."),
          ("RS", "Rachel S."), ("AL", "Aaron L."), ("CW", "Chris W."),
          ("BH", "Beth H."), ("TN", "Tom N.")]

BREAKS = [
    {"start": "09:00", "minutes": 15, "label": "Morning break"},
    {"start": "12:00", "minutes": 30, "label": "Lunch"},
    {"start": "14:30", "minutes": 15, "label": "Afternoon break"},
]
SHIFTS = [{"name": "Day", "start": "06:00", "end": "14:30"},
          {"name": "Evening", "start": "14:30", "end": "22:30"}]
OPERATING = {d: [["06:00", "22:30"]] for d in ("mon", "tue", "wed", "thu", "fri")}
OPERATING.update({"sat": [], "sun": []})

DELAY_NOTES = {
    "Waiting on parts":       ["Backordered relays, ETA after lunch", "Hardware kit short 4 bolts"],
    "Machine down":           ["Press fault code 17, maintenance called", "Crane out of service"],
    "Rework required":        ["Failed hipot, rechecking terminations", "Paint run on door panel"],
    "Missing paperwork":      ["Traveler not released", "Waiting on signed deviation"],
    "Paint queue full":       ["Booth backed up, 2 ahead of us", "Waiting on cure oven"],
    "Waiting on engineering": ["Print conflict on layout sheet", "Awaiting ECO approval"],
}


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

def _resolve_target(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    base = str(config._default_data_dir())
    return Path(base + "_demo")


def _guard(target: Path, fresh: bool) -> Path:
    """Return the demo DB path, or exit loudly if anything is unsafe."""
    norm = lambda p: str(Path(p).resolve()).lower().rstrip("\\/")

    if not target.name.lower().endswith("_demo"):
        sys.exit(f"REFUSING: demo data folder must end with '_demo' (got: {target}). "
                 "This keeps demo data physically separate from the real log.")
    if norm(target) == norm(config.DATA_DIR):
        sys.exit(f"REFUSING: {target} is the LIVE data folder (BAYTRACKER_DATA). "
                 "Demo data must go in a separate folder, e.g. C:\\BayTrackerData_demo.")

    db_path = target / "baytracker.db"
    if db_path.exists():
        conn = db.connect(db_path)
        try:
            is_demo = bool(db.get_setting(conn, "demo_mode", False))
        except Exception:
            is_demo = False
        finally:
            conn.close()
        if not is_demo:
            sys.exit(f"REFUSING: {db_path} exists and is NOT marked demo_mode. "
                     "It might be a real log; this script will not touch it.")
        if not fresh:
            sys.exit(f"A demo database already exists at {db_path}. "
                     "Re-run with --fresh to regenerate it (only the demo file is replaced).")
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
    return db_path


# ---------------------------------------------------------------------------
# Event generation
# ---------------------------------------------------------------------------

def _workdays_back(now: datetime, n: int):
    """The last n weekdays, oldest first (includes today if it's a weekday)."""
    days, d = [], now.date()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


class Sim:
    """Tracks bay availability while we lay out a realistic history."""

    def __init__(self, bay_ids, reasons):
        self.free_at = {b: datetime.min for b in bay_ids}
        self.reasons = reasons          # list of sqlite rows (with division name)
        self.events = []                # (ts, seq, type, fields)
        self.seq = 0
        self.wo_counter = 1021

    def emit(self, ts, etype, **fields):
        self.seq += 1
        self.events.append((ts, self.seq, etype, fields))

    def next_wo(self):
        self.wo_counter += 1
        return f"WO-{self.wo_counter}"

    def grab_bay(self, when, exclude=()):
        avail = [b for b, t in self.free_at.items() if t <= when and b not in exclude]
        if not avail:
            return None
        b = RNG.choice(avail)
        self.free_at[b] = datetime.max     # held until release()
        return b

    def release(self, bay, when):
        self.free_at[bay] = when + timedelta(minutes=RNG.randint(5, 40))

    def maybe_delay(self, bay, wo, pn, start, end):
        """Insert one delay episode inside [start, end] ~40% of the time."""
        if RNG.random() > 0.40 or (end - start) < timedelta(minutes=45):
            return
        r = RNG.choice(self.reasons)
        d_start = start + (end - start) * RNG.uniform(0.25, 0.55)
        d_len = timedelta(minutes=RNG.randint(15, 75))
        d_end = min(d_start + d_len, end - timedelta(minutes=5))
        if d_end <= d_start:
            return
        who, who2 = RNG.choice(ROSTER)[0], RNG.choice(ROSTER)[0]
        note = RNG.choice(DELAY_NOTES[r["label"]])
        self.emit(d_start, "DELAY_START", bay_id=bay, work_order=wo, product_number=pn,
                  delay_reason_id=r["id"], reason_label=r["label"], division=r["division"],
                  in_out_of_control=r["in_out_of_control"], note=note, initials=who)
        self.emit(d_end, "DELAY_CLEAR", bay_id=bay, work_order=wo, product_number=pn,
                  initials=who2)


def _simulate_day(sim: Sim, day, is_today: bool, now: datetime):
    """Lay out one workday of activity. On 'today', leave open runs/queue/delay."""
    day_start = datetime(day.year, day.month, day.day, 6, 0)
    day_end = datetime(day.year, day.month, day.day, 16, 30)

    for _ in range(RNG.randint(3, 5)):
        wo = sim.next_wo()
        pn = RNG.choice(PRODUCTS)[0]
        who = RNG.choice(ROSTER)[0]
        start = day_start + timedelta(minutes=RNG.randint(15, 300))
        bay = sim.grab_bay(start)
        if bay is None:
            continue
        run_len = timedelta(minutes=RNG.randint(90, 330))
        end = min(start + run_len, day_end - timedelta(minutes=10))
        if end <= start:
            sim.release(bay, start)
            continue

        # On the current day, runs that would still be going stay OPEN.
        if is_today and end >= now:
            sim.emit(start, "START", bay_id=bay, work_order=wo, product_number=pn,
                     initials=who)
            continue  # bay stays held -> open run on the live screen

        sim.emit(start, "START", bay_id=bay, work_order=wo, product_number=pn,
                 initials=who)
        sim.maybe_delay(bay, wo, pn, start, end)

        roll = RNG.random()
        if roll < 0.35:                                   # two-step via queue
            sim.emit(end, "COMPLETE_BAY", bay_id=bay, work_order=wo,
                     product_number=pn, initials=RNG.choice(ROSTER)[0])
            sim.release(bay, end)
            q_end = end + timedelta(minutes=RNG.randint(20, 90))
            bay2 = sim.grab_bay(q_end, exclude=(bay,))
            if bay2 is None:
                return
            fin = min(q_end + timedelta(minutes=RNG.randint(45, 150)),
                      day_end - timedelta(minutes=5))
            if is_today and fin >= now:
                sim.emit(q_end, "START", bay_id=bay2, work_order=wo,
                         product_number=pn, initials=RNG.choice(ROSTER)[0])
                return
            sim.emit(q_end, "START", bay_id=bay2, work_order=wo, product_number=pn,
                     initials=RNG.choice(ROSTER)[0])
            sim.emit(fin, "UNIT_COMPLETE", work_order=wo, product_number=pn,
                     initials=RNG.choice(ROSTER)[0])
            sim.release(bay2, fin)
        elif roll < 0.55:                                 # move bays mid-run
            mid = start + (end - start) * 0.5
            bay2 = sim.grab_bay(mid, exclude=(bay,))
            if bay2 is not None:
                sim.emit(mid, "MOVE", bay_id=bay, target_bay_id=bay2, work_order=wo,
                         product_number=pn, initials=RNG.choice(ROSTER)[0])
                sim.release(bay, mid)
                bay = bay2
            sim.emit(end, "UNIT_COMPLETE", work_order=wo, product_number=pn,
                     initials=RNG.choice(ROSTER)[0])
            sim.release(bay, end)
        else:                                             # complete from the bay
            sim.emit(end, "UNIT_COMPLETE", work_order=wo, product_number=pn,
                     initials=RNG.choice(ROSTER)[0])
            sim.release(bay, end)


def _today_extras(sim: Sim, day, now: datetime):
    """Make the live screen look mid-shift: several bays running, one delayed,
    units in queue. Anchored to the afternoon of the most recent workday so the
    demo looks busy no matter what time it is generated."""
    anchor = min(now, datetime(day.year, day.month, day.day, 15, 0))

    # five bays currently RUNNING (open runs at different elapsed times)
    for _ in range(5):
        wo, pn = sim.next_wo(), RNG.choice(PRODUCTS)[0]
        start = anchor - timedelta(minutes=RNG.randint(45, 280))
        bay = sim.grab_bay(start)
        if bay is None:
            continue
        sim.emit(start, "START", bay_id=bay, work_order=wo, product_number=pn,
                 initials=RNG.choice(ROSTER)[0])

    # one bay currently DELAYED (open delay on an open run)
    wo, pn = sim.next_wo(), RNG.choice(PRODUCTS)[0]
    start = anchor - timedelta(minutes=RNG.randint(150, 240))
    bay = sim.grab_bay(start)
    if bay is not None:
        r = RNG.choice(sim.reasons)
        sim.emit(start, "START", bay_id=bay, work_order=wo, product_number=pn,
                 initials=RNG.choice(ROSTER)[0])
        d_start = anchor - timedelta(minutes=RNG.randint(20, 50))
        sim.emit(d_start, "DELAY_START", bay_id=bay, work_order=wo, product_number=pn,
                 delay_reason_id=r["id"], reason_label=r["label"], division=r["division"],
                 in_out_of_control=r["in_out_of_control"],
                 note=RNG.choice(DELAY_NOTES[r["label"]]),
                 initials=RNG.choice(ROSTER)[0])

    # two units waiting in the WIP/queue pool
    for _ in range(2):
        wo, pn = sim.next_wo(), RNG.choice(PRODUCTS)[0]
        start = anchor - timedelta(minutes=RNG.randint(180, 300))
        bay = sim.grab_bay(start)
        if bay is None:
            continue
        comp = start + timedelta(minutes=RNG.randint(60, 120))
        sim.emit(start, "START", bay_id=bay, work_order=wo, product_number=pn,
                 initials=RNG.choice(ROSTER)[0])
        sim.emit(comp, "COMPLETE_BAY", bay_id=bay, work_order=wo, product_number=pn,
                 initials=RNG.choice(ROSTER)[0])
        sim.release(bay, comp)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--data-dir", help="Demo data folder (must end with _demo). "
                                       "Default: <default data dir>_demo")
    ap.add_argument("--days", type=int, default=15,
                    help="How many workdays of history to generate (default 15)")
    ap.add_argument("--fresh", action="store_true",
                    help="Replace an existing demo database (demo files only)")
    args = ap.parse_args()

    target = _resolve_target(args.data_dir)
    db_path = _guard(target, args.fresh)
    target.mkdir(parents=True, exist_ok=True)

    conn = db.connect(db_path)
    try:
        db.create_schema(conn)
        bootstrap.seed(conn)

        # --- demo flag FIRST, so a half-built file is still recognisably demo ---
        db.set_setting(conn, "demo_mode", True)

        # --- configuration (what /admin would hold) ---
        for name in DIVISIONS:
            conn.execute("INSERT OR IGNORE INTO divisions (name, active) VALUES (?, 1);", (name,))
        for i, (label, division, ctrl) in enumerate(REASONS, start=1):
            div_id = conn.execute("SELECT id FROM divisions WHERE name = ?;",
                                  (division,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO delay_reasons (label, division_id, in_out_of_control, "
                "active, is_other, sort_order) VALUES (?, ?, ?, 1, 0, ?);",
                (label, div_id, ctrl, i))
        for number, desc in PRODUCTS:
            conn.execute("INSERT OR IGNORE INTO product_numbers (number, description, active) "
                         "VALUES (?, ?, 1);", (number, desc))
        for ini, name in ROSTER:
            conn.execute("INSERT OR IGNORE INTO initials_roster (initials, name, active) "
                         "VALUES (?, ?, 1);", (ini, name))
        conn.commit()
        db.set_setting(conn, "break_schedule", BREAKS)
        db.set_setting(conn, "shifts", SHIFTS)
        db.set_setting(conn, "operating_calendar", OPERATING)
        db.set_setting(conn, "labor_rate", 85.0)

        reasons = conn.execute(
            "SELECT r.id, r.label, r.in_out_of_control, d.name AS division "
            "FROM delay_reasons r JOIN divisions d ON r.division_id = d.id "
            "WHERE r.is_other = 0;").fetchall()
        bay_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM bays WHERE active = 1;").fetchall()]

        # --- the history itself ---
        now = datetime.now().replace(microsecond=0)
        days = _workdays_back(now, args.days)
        sim = Sim(bay_ids, reasons)
        for day in days:
            is_today = (day == days[-1])
            _simulate_day(sim, day, is_today, now)
            if is_today:
                _today_extras(sim, day, now)

        # Append in strict chronological order so replay (which walks insertion
        # order) sees a causally valid log, exactly like real life.
        sim.events.sort(key=lambda e: (e[0], e[1]))
        for ts, _seq, etype, fields in sim.events:
            events.append(conn, etype, ts=ts.strftime(config.TS_FORMAT), **fields)

        # --- prove the result is coherent before declaring success ---
        snap = state.live_snapshot(conn)
        stats = metrics.compute(conn, {})
        n_events = conn.execute("SELECT COUNT(*) AS n FROM events;").fetchone()["n"]
        running = sum(1 for t in snap["tiles"] if t["status"] != "IDLE")
    finally:
        conn.close()

    print(f"[demo] Demo database created: {db_path}")
    print(f"[demo] {n_events} events over {args.days} workdays · "
          f"{running} bays occupied right now · {len(snap['queue'])} in queue · "
          f"{snap['open_delays']} open delay(s)")
    print("[demo] Launch it:      powershell -ExecutionPolicy Bypass -File .\\start.ps1 -Demo")
    print("[demo] Back to live:   restart without -Demo (the real log was never touched)")
    print(f"[demo] Delete demo:    remove the folder {target}")


if __name__ == "__main__":
    main()
