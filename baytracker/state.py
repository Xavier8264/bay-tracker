"""
state.py -- replay the append-only event log into current, derived state.

NOTHING about "what is happening right now" is stored in the database; it is all
*derived* here by replaying events in order. That is what makes a reboot or crash
lossless: on startup we just replay the log and we are exactly where we left off.

This module produces two things:

  1. replay(conn) -> ReplayResult
     The structural picture: every BayRun (open or closed), every DelayEpisode,
     every UnitJourney, and which bay currently holds what. No clocks here -- just
     start/end timestamps. Both the live UI and the exporter build on this.

  2. live_snapshot(conn) -> dict
     The JSON the dashboard/console render: each bay's status + live elapsed time
     (computed through the schedule so breaks/off-hours freeze it), plus break/
     off-hours banners, the WIP/queue pool, and open-item counts.

Time accounting rules implemented here (spec section 4):
  * Unit cycle counts the UNION of parallel active periods, never the sum.
  * active = bay RUNNING (minus delay, minus non-counting time)
  * delay  = bay DELAYED (minus non-counting time), and at the unit level only
             when no other bay of that unit is simultaneously running.
  * queue  = unit sits in the WIP pool occupying zero bays (minus non-counting).
  * cycle  = active + delay + queue.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import db, events
from .schedule import Schedule, Interval

# ---------------------------------------------------------------------------
# Episode containers. Plain classes with attributes -- readable over clever.
# ---------------------------------------------------------------------------


class DelayEpisode:
    """One flag->clear delay on a particular bay run."""

    def __init__(self, work_order, product_number, bay_id, reason_label,
                 division, in_out_of_control, note, started, flagged_by,
                 start_event_id):
        self.work_order = work_order
        self.product_number = product_number
        self.bay_id = bay_id
        self.reason_label = reason_label
        self.division = division
        self.in_out_of_control = in_out_of_control
        self.note = note
        self.started: datetime = started
        self.cleared: Optional[datetime] = None
        self.flagged_by = flagged_by
        self.cleared_by: Optional[str] = None
        self.start_event_id = start_event_id

    @property
    def is_open(self) -> bool:
        return self.cleared is None


class BayRun:
    """One period during which a unit occupied one bay."""

    def __init__(self, work_order, product_number, component_label, bay_id,
                 started, started_by, start_event_id):
        self.work_order = work_order
        self.product_number = product_number
        self.component_label = component_label
        self.bay_id = bay_id
        self.started: datetime = started
        self.ended: Optional[datetime] = None
        self.started_by = started_by
        self.completed_by: Optional[str] = None
        self.end_kind: Optional[str] = None  # MOVE|COMPLETE_BAY|MATE|UNIT_COMPLETE|SCRAP
        self.start_event_id = start_event_id
        self.delays: List[DelayEpisode] = []
        self.current_delay: Optional[DelayEpisode] = None

    @property
    def is_open(self) -> bool:
        return self.ended is None

    @property
    def status(self) -> str:
        if self.is_open:
            return "DELAYED" if self.current_delay else "RUNNING"
        return "CLOSED"

    def occupied_interval(self, fallback_end: datetime) -> Interval:
        return (self.started, self.ended or fallback_end)

    def delayed_intervals(self, fallback_end: datetime) -> List[Interval]:
        return [(d.started, d.cleared or fallback_end) for d in self.delays]

    def running_intervals(self, fallback_end: datetime) -> List[Interval]:
        """Occupied time minus delayed time = time the bay was actually RUNNING."""
        return _subtract([self.occupied_interval(fallback_end)],
                         self.delayed_intervals(fallback_end))


class UnitJourney:
    """The whole life of one work order (one physical unit)."""

    def __init__(self, work_order, product_number, first_started):
        self.work_order = work_order
        self.product_number = product_number
        self.first_started: datetime = first_started
        self.completed: Optional[datetime] = None
        self.outcome: Optional[str] = None       # 'complete' | 'scrap' | None(open)
        self.bays_visited: List[int] = []         # bay_ids in the order entered
        self.occupied_bays: set = set()           # bay_ids occupied right now
        self.delay_count = 0
        self.mated = False

    @property
    def is_open(self) -> bool:
        return self.outcome is None


class ReplayResult:
    def __init__(self):
        self.runs: List[BayRun] = []
        self.delays: List[DelayEpisode] = []
        self.units: Dict[str, UnitJourney] = {}
        self.bay_current: Dict[int, BayRun] = {}   # bay_id -> open run
        self.last_event_id: int = 0


# ---------------------------------------------------------------------------
# Interval algebra (re-used by exports). Kept here next to its only producer.
# ---------------------------------------------------------------------------

def _subtract(intervals: List[Interval], holes: List[Interval]) -> List[Interval]:
    result = list(intervals)
    for hs, he in holes:
        nxt: List[Interval] = []
        for s, e in result:
            if he <= s or hs >= e:
                nxt.append((s, e))
                continue
            if hs > s:
                nxt.append((s, hs))
            if he < e:
                nxt.append((he, e))
        result = nxt
    return result


def merge_intervals(intervals: List[Interval]) -> List[Interval]:
    """Merge overlapping/adjacent intervals into a disjoint, sorted list."""
    ivs = sorted((s, e) for s, e in intervals if e > s)
    if not ivs:
        return []
    merged = [ivs[0]]
    for s, e in ivs[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def counted_over(sched: Schedule, intervals: List[Interval]) -> float:
    """Counted seconds over a set of intervals (merged first to avoid double count)."""
    return sum(sched.counted_seconds(s, e) for s, e in merge_intervals(intervals))


# ---------------------------------------------------------------------------
# Effective-timestamp handling for corrections.
# ---------------------------------------------------------------------------

def _effective_ts_map(rows) -> Dict[int, str]:
    """Map event_id -> corrected timestamp for retime corrections (latest wins)."""
    out: Dict[int, str] = {}
    for r in rows:
        if r["type"] == "CORRECTION" and r["supersedes_event_id"] and r["corrected_ts"]:
            out[r["supersedes_event_id"]] = r["corrected_ts"]  # later rows overwrite earlier
    return out


# ---------------------------------------------------------------------------
# The replay itself.
# ---------------------------------------------------------------------------

def replay(conn) -> ReplayResult:
    rows = events.all_events(conn)
    result = ReplayResult()
    if not rows:
        return result
    result.last_event_id = rows[-1]["id"]

    retime = _effective_ts_map(rows)

    def eff(row) -> datetime:
        """Effective timestamp of an event (a retime correction can move it)."""
        ts = retime.get(row["id"], row["ts"])
        return events.parse_ts(ts)

    def get_unit(wo, pn, when) -> UnitJourney:
        u = result.units.get(wo)
        if u is None:
            u = UnitJourney(wo, pn, when)
            result.units[wo] = u
        if (not u.product_number) and pn:
            u.product_number = pn
        return u

    def close_run(run: BayRun, when: datetime, kind: str, by):
        run.ended = when
        run.end_kind = kind
        run.completed_by = by
        if run.current_delay:                 # closing a bay also closes its open delay
            run.current_delay.cleared = when
            run.current_delay.cleared_by = by
            run.current_delay = None
        result.bay_current.pop(run.bay_id, None)
        u = result.units.get(run.work_order)
        if u:
            u.occupied_bays.discard(run.bay_id)

    for r in rows:
        etype = r["type"]
        bay = r["bay_id"]
        wo = r["work_order"]

        # Retime corrections carry no transition -- their only effect (moving a
        # superseded event's time) is already baked into eff() above. Only
        # "acts_as" corrections perform a real close.
        if etype == "CORRECTION" and not r["acts_as"]:
            continue

        when = eff(r) if not (etype == "CORRECTION" and r["acts_as"]) else events.parse_ts(r["corrected_ts"])

        if etype == "START":
            run = BayRun(wo, r["product_number"], r["component_label"], bay,
                         when, r["initials"], r["id"])
            result.runs.append(run)
            result.bay_current[bay] = run
            u = get_unit(wo, r["product_number"], when)
            u.occupied_bays.add(bay)
            u.bays_visited.append(bay)

        elif etype == "MOVE":
            src = result.bay_current.get(bay)
            if src:
                close_run(src, when, "MOVE", r["initials"])
            dst_bay = r["target_bay_id"]
            run = BayRun(wo, r["product_number"], r["component_label"], dst_bay,
                         when, r["initials"], r["id"])
            result.runs.append(run)
            result.bay_current[dst_bay] = run
            u = get_unit(wo, r["product_number"], when)
            u.occupied_bays.add(dst_bay)
            u.bays_visited.append(dst_bay)

        elif etype == "COMPLETE_BAY":
            run = result.bay_current.get(bay)
            if run:
                close_run(run, when, "COMPLETE_BAY", r["initials"])

        elif etype == "MATE":
            releasing = r["target_bay_id"]
            rel_run = result.bay_current.get(releasing)
            if rel_run:
                close_run(rel_run, when, "MATE", r["initials"])
            u = result.units.get(wo)
            if u:
                u.mated = True

        elif etype == "DELAY_START":
            run = result.bay_current.get(bay)
            if run and run.current_delay is None:
                ep = DelayEpisode(wo, r["product_number"], bay, r["reason_label"],
                                  r["division"], r["in_out_of_control"], r["note"],
                                  when, r["initials"], r["id"])
                run.delays.append(ep)
                run.current_delay = ep
                result.delays.append(ep)
                u = result.units.get(wo)
                if u:
                    u.delay_count += 1

        elif etype == "DELAY_CLEAR":
            run = result.bay_current.get(bay)
            if run and run.current_delay:
                run.current_delay.cleared = when
                run.current_delay.cleared_by = r["initials"]
                run.current_delay = None

        elif etype in ("UNIT_COMPLETE", "SCRAP"):
            # Terminal: close every bay this unit still occupies, then mark outcome.
            u = result.units.get(wo)
            for b in list(u.occupied_bays) if u else []:
                run = result.bay_current.get(b)
                if run and run.work_order == wo:
                    close_run(run, when, etype, r["initials"])
            if u:
                u.completed = when
                u.outcome = "complete" if etype == "UNIT_COMPLETE" else "scrap"

        elif etype == "CORRECTION" and r["acts_as"]:
            acts = r["acts_as"]
            if acts == "DELAY_CLEAR":
                run = result.bay_current.get(bay)
                if run and run.current_delay:
                    run.current_delay.cleared = when
                    run.current_delay.cleared_by = r["initials"]
                    run.current_delay = None
            elif acts == "COMPLETE_BAY":
                run = result.bay_current.get(bay)
                if run:
                    close_run(run, when, "COMPLETE_BAY", r["initials"])
            elif acts == "UNIT_COMPLETE":
                u = result.units.get(wo)
                for b in list(u.occupied_bays) if u else []:
                    run = result.bay_current.get(b)
                    if run and run.work_order == wo:
                        close_run(run, when, "UNIT_COMPLETE", r["initials"])
                if u:
                    u.completed = when
                    u.outcome = "complete"

    return result


# ---------------------------------------------------------------------------
# Per-unit duration breakdown (active/delay/queue/cycle). Used by exports + stats.
# ---------------------------------------------------------------------------

def unit_durations(sched: Schedule, unit: UnitJourney, runs: List[BayRun],
                   now: datetime) -> Dict[str, Optional[float]]:
    """Return active/delay/queue/cycle SECONDS for a unit (None when still open)."""
    if unit.is_open:
        return {"active": None, "delay": None, "queue": None, "cycle": None}
    end = unit.completed or now
    unit_runs = [r for r in runs if r.work_order == unit.work_order]

    running_all: List[Interval] = []
    delayed_all: List[Interval] = []
    occupied_all: List[Interval] = []
    for r in unit_runs:
        running_all += r.running_intervals(end)
        delayed_all += r.delayed_intervals(end)
        occupied_all.append(r.occupied_interval(end))

    active = counted_over(sched, running_all)
    # Delay at unit level: delayed time when NOT simultaneously running elsewhere.
    delay = counted_over(sched, _subtract(merge_intervals(delayed_all),
                                          merge_intervals(running_all)))
    # Queue: span minus any bay occupancy.
    queue_iv = _subtract([(unit.first_started, end)], merge_intervals(occupied_all))
    queue = counted_over(sched, queue_iv)
    cycle = active + delay + queue
    return {"active": active, "delay": delay, "queue": queue, "cycle": cycle}


# ---------------------------------------------------------------------------
# A tiny cache so frequent heartbeats don't re-replay the whole log every tick.
# The structural replay only changes when a new event is appended; elapsed time
# (which depends on "now") is recomputed cheaply on top of the cached structure.
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache: Dict[str, object] = {"last_id": None, "result": None}


def _max_event_id(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events;").fetchone()
    return row["m"]


def cached_replay(conn) -> ReplayResult:
    """replay(), but reuse the previous result if no new events were appended."""
    current_max = _max_event_id(conn)
    with _cache_lock:
        if _cache["last_id"] == current_max and _cache["result"] is not None:
            return _cache["result"]  # nothing changed structurally
    result = replay(conn)
    with _cache_lock:
        _cache["last_id"] = current_max
        _cache["result"] = result
    return result


def invalidate_cache() -> None:
    with _cache_lock:
        _cache["last_id"] = None
        _cache["result"] = None


# ---------------------------------------------------------------------------
# The live snapshot the browsers render.
# ---------------------------------------------------------------------------

def _fmt(dt: Optional[datetime]) -> Optional[str]:
    from . import config
    return dt.strftime(config.TS_FORMAT) if dt else None


def live_snapshot(conn, now: Optional[datetime] = None) -> dict:
    """Build the JSON payload for dashboards/console: per-bay status + elapsed."""
    now = now or datetime.now()
    sched = Schedule.from_settings(conn)
    result = cached_replay(conn)

    on_break = sched.active_break(now)
    off_hours = sched.is_off_hours(now)
    counting = (on_break is None) and (not off_hours)

    bays = conn.execute(
        "SELECT * FROM bays WHERE active = 1 ORDER BY is_extra ASC, sort_order ASC;"
    ).fetchall()
    bay_number = {b["id"]: b["sort_order"] for b in bays}

    tiles = []
    for b in bays:
        run = result.bay_current.get(b["id"])
        tile = {
            "bay_id": b["id"],
            "name": b["name"],
            "is_extra": bool(b["is_extra"]),
            "grid_col": b["grid_col"],
            "status": "IDLE",
            "work_order": None,
            "product_number": None,
            "component_label": None,
            "elapsed_seconds": 0,
            "started_by": None,
            "delay": None,
            "paused_status": None,   # what the bay would be if not for the break
            "occupies_two": False,
        }
        if run is not None:
            underlying = run.status  # RUNNING or DELAYED
            tile["work_order"] = run.work_order
            tile["product_number"] = run.product_number
            tile["component_label"] = run.component_label
            tile["started_by"] = run.started_by
            # 2-bay indicator (a unit may occupy up to 2 bays).
            u = result.units.get(run.work_order)
            tile["occupies_two"] = bool(u and len(u.occupied_bays) >= 2)

            if underlying == "DELAYED":
                d = run.current_delay
                tile["elapsed_seconds"] = int(sched.counted_seconds(d.started, now))
                tile["delay"] = {
                    "reason": d.reason_label,
                    "division": d.division,
                    "in_out_of_control": d.in_out_of_control,
                    "note": d.note,
                    "since": _fmt(d.started),
                    "flagged_by": d.flagged_by,
                }
            else:  # RUNNING -> show active time so far
                tile["elapsed_seconds"] = int(
                    counted_over(sched, run.running_intervals(now)))

            # During a break window, every occupied bay shows the distinct ON
            # BREAK state (timers frozen). We keep the underlying status so the
            # tile can hint what it will return to, but ON BREAK is never red.
            if on_break is not None:
                tile["status"] = "ON_BREAK"
                tile["paused_status"] = underlying
            else:
                tile["status"] = underlying

        tiles.append(tile)

    # WIP / queue pool: units that currently occupy zero bays and aren't terminal.
    queue = []
    for wo, u in result.units.items():
        if u.is_open and not u.occupied_bays:
            queue.append({
                "work_order": wo,
                "product_number": u.product_number,
                "since": _fmt(u.first_started),   # informational; queue clock is real
            })

    open_runs = sum(1 for r in result.runs if r.is_open)
    open_delays = sum(1 for d in result.delays if d.is_open)

    return {
        "server_time": _fmt(now),
        "is_counting": counting,
        "off_hours": off_hours,
        "on_break": ({"label": on_break["label"], "ends_at": _fmt(on_break["ends_at"])}
                     if on_break else None),
        "tiles": tiles,
        "queue": queue,
        "open_runs": open_runs,
        "open_delays": open_delays,
        "bay_number": bay_number,
    }
