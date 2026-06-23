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
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import db, events
from .schedule import Schedule, Interval

# Event types the console Undo can reverse: the floor actions an operator logs.
# CORRECTION (stats page, PIN-gated) and VOID (an undo itself) are deliberately
# excluded -- undo is for fixing an accidental floor action.
UNDOABLE_TYPES = {
    "START", "MOVE", "COMPLETE_BAY", "MATE", "DELAY_START", "DELAY_CLEAR",
    "PAUSE", "RESUME", "UNIT_COMPLETE", "SCRAP",
}

# When no shifts are configured, Undo reaches back at most this many hours rather
# than into the entire log (a sane "this working session" bound).
UNDO_FALLBACK_HOURS = 12

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


class PauseEpisode:
    """One pause->resume interval on a bay run: the bay was parked/unstaffed
    (e.g. across a short-staffed shift). While paused the bay's clocks freeze,
    exactly like a scheduled break, so nobody is credited with work that wasn't
    done. It is NOT a delay -- it is never red and raises no alerts."""

    def __init__(self, started, paused_by, start_event_id):
        self.started: datetime = started
        self.resumed: Optional[datetime] = None
        self.paused_by = paused_by
        self.resumed_by: Optional[str] = None
        self.start_event_id = start_event_id

    @property
    def is_open(self) -> bool:
        return self.resumed is None


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
        # When the operator logged COMPLETE_BAY: work at this bay is finished
        # but the part PHYSICALLY STAYS in the bay (a distinct DONE state) until
        # it is moved, merged, or the unit completes. Active time stops here,
        # but the bay keeps showing the part (never a misleading "IDLE").
        self.work_done_at: Optional[datetime] = None
        self.started_by = started_by
        self.completed_by: Optional[str] = None
        self.end_kind: Optional[str] = None  # MOVE|COMPLETE_BAY|MATE|UNIT_COMPLETE|SCRAP
        self.start_event_id = start_event_id
        self.delays: List[DelayEpisode] = []
        self.current_delay: Optional[DelayEpisode] = None
        self.pauses: List[PauseEpisode] = []
        self.current_pause: Optional[PauseEpisode] = None

    @property
    def is_open(self) -> bool:
        return self.ended is None

    @property
    def status(self) -> str:
        if not self.is_open:
            return "CLOSED"
        if self.current_pause:
            return "PAUSED"        # parked/unstaffed; clocks frozen (not a delay)
        if self.current_delay:
            return "DELAYED"
        if self.work_done_at is not None:
            return "DONE"          # work finished; part still occupies the bay
        return "RUNNING"

    def _active_end(self, fallback_end: datetime) -> datetime:
        """When active time stops accruing: at work-done if set, else when the
        run ended, else now. (Work-done freezes active time even though the
        bay stays occupied until the part is physically moved out.)"""
        if self.work_done_at is not None:
            return self.work_done_at
        return self.ended or fallback_end

    def occupied_interval(self, fallback_end: datetime) -> Interval:
        # Occupancy runs until the part actually leaves the bay (ended), which
        # may be later than work-done.
        return (self.started, self.ended or fallback_end)

    def occupied_intervals(self, fallback_end: datetime) -> List[Interval]:
        """Occupancy with paused (frozen) time removed -- what the unit's TOTAL
        (summed bay occupancy) should count. A parked bay holds the part but its
        clock is frozen, so the paused span is not 'taken-up' time."""
        return _subtract([self.occupied_interval(fallback_end)],
                         self.paused_intervals(fallback_end))

    def delayed_intervals(self, fallback_end: datetime) -> List[Interval]:
        return [(d.started, d.cleared or fallback_end) for d in self.delays]

    def paused_intervals(self, fallback_end: datetime) -> List[Interval]:
        return [(p.started, p.resumed or fallback_end) for p in self.pauses]

    def running_intervals(self, fallback_end: datetime) -> List[Interval]:
        """Active (RUNNING) time = from start to work-done/end, minus delays and
        minus paused (parked) time -- a parked bay accrues no active time."""
        return _subtract([(self.started, self._active_end(fallback_end))],
                         self.delayed_intervals(fallback_end)
                         + self.paused_intervals(fallback_end))


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
        # When two DIFFERENT work orders merge, the released unit is absorbed
        # into the survivor. The survivor records the absorbed work order(s) here
        # so its live elapsed/total can include the absorbed cell's bay-time and
        # earliest start; the absorbed unit records who it merged into.
        self.absorbed_work_orders: List[str] = []
        self.merged_into: Optional[str] = None

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
        # event ids reversed by a VOID/undo (skipped during this replay).
        self.voided_event_ids: set = set()
        # The event row(s) the NEXT console Undo would reverse -- the most recent
        # not-yet-voided undoable action, expanded to its whole action_group so a
        # multi-row action (shift changeover) undoes as one. Empty if nothing.
        self.undo_members: List = []


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


def _unit_frozen_intervals(unit_runs: List["BayRun"], end: datetime) -> List[Interval]:
    """The spans during which the WHOLE unit was parked (paused) and so its
    linear clock (elapsed / cycle) must freeze. A unit is frozen only while it
    has no bay actively running -- so if one of two parallel bays is paused but
    the other still runs, the unit keeps ticking. For the common single-bay case
    this is simply that bay's paused intervals."""
    paused_all: List[Interval] = []
    running_all: List[Interval] = []
    for r in unit_runs:
        paused_all += r.paused_intervals(end)
        running_all += r.running_intervals(end)
    return _subtract(merge_intervals(paused_all), merge_intervals(running_all))


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


def _next_undo_group(rows, voided: set) -> List:
    """The event row(s) the next console Undo would reverse.

    Find the most recent undoable, not-yet-voided action (scanning newest first),
    then expand it to every still-live row sharing its ``action_group`` so a
    multi-row action (a shift changeover) is undone as one unit. Rows predating
    the action_group column (NULL) stand alone. Returns [] when nothing remains
    to undo. The time-window bound (this shift) is applied by the caller, since
    it depends on "now"; this picks the structural target only.
    """
    target = None
    for r in reversed(rows):
        if r["type"] in UNDOABLE_TYPES and r["id"] not in voided:
            target = r
            break
    if target is None:
        return []
    grp = target["action_group"]
    if grp is None:
        return [target]
    return [r for r in rows
            if r["action_group"] == grp and r["type"] in UNDOABLE_TYPES
            and r["id"] not in voided]


def undo_floor(sched: Schedule, now: datetime) -> datetime:
    """The earliest action-time the console Undo may reach: the start of the
    current shift, or a fixed recent window when no shifts are configured."""
    return sched.current_shift_start(now) or (now - timedelta(hours=UNDO_FALLBACK_HOURS))


def describe_undo(members: List, bay_name: Dict[int, str]) -> str:
    """A short human label for what the next Undo reverses (shown on the button
    and the confirm dialog), e.g. 'Merge Bay 5 into Bay 3 (WO 1042)'."""
    if not members:
        return ""
    if len(members) > 1:
        # The only multi-row action today is a shift changeover (PAUSE/RESUME).
        n = len(members)
        return f"Shift changeover ({n} {'bay' if n == 1 else 'bays'})"

    m = members[0]
    et = m["type"]
    wo = m["work_order"] or ""
    bay = bay_name.get(m["bay_id"], f"Bay {m['bay_id']}") if m["bay_id"] else ""
    tgt = bay_name.get(m["target_bay_id"], f"Bay {m['target_bay_id']}") if m["target_bay_id"] else ""
    wo_tag = f" ({wo})" if wo else ""
    if et == "START":
        return f"Start at {bay}{wo_tag}"
    if et == "MOVE":
        return f"Move {wo}: {bay} → {tgt}".strip()
    if et == "COMPLETE_BAY":
        return f"Work done at {bay}{wo_tag}"
    if et == "MATE":
        return f"Merge {tgt} into {bay}{wo_tag}"
    if et == "DELAY_START":
        reason = m["reason_label"] or "delay"
        return f"Flag delay at {bay} — {reason}"
    if et == "DELAY_CLEAR":
        return f"Clear delay at {bay}{wo_tag}"
    if et == "PAUSE":
        return f"Pause {bay}{wo_tag}"
    if et == "RESUME":
        return f"Resume {bay}{wo_tag}"
    if et == "UNIT_COMPLETE":
        return f"Complete unit {wo}"
    if et == "SCRAP":
        return f"Scrap {wo}"
    return et


# ---------------------------------------------------------------------------
# The replay itself.
# ---------------------------------------------------------------------------

def replay(conn) -> ReplayResult:
    rows = events.all_events(conn)
    result = ReplayResult()
    if not rows:
        return result
    result.last_event_id = rows[-1]["id"]

    # An undo is a VOID row pointing at the event it reverses; that target is
    # skipped below so the action is undone WITHOUT mutating the log.
    voided = {r["supersedes_event_id"] for r in rows
              if r["type"] == "VOID" and r["supersedes_event_id"] is not None}
    result.voided_event_ids = voided
    result.undo_members = _next_undo_group(rows, voided)

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
        if run.current_pause:                 # ...and any open pause
            run.current_pause.resumed = when
            run.current_pause.resumed_by = by
            run.current_pause = None
        result.bay_current.pop(run.bay_id, None)
        u = result.units.get(run.work_order)
        if u:
            u.occupied_bays.discard(run.bay_id)

    for r in rows:
        etype = r["type"]
        bay = r["bay_id"]
        wo = r["work_order"]

        # A VOID row carries no transition of its own -- its only effect (skipping
        # the event it reverses) is handled by the `voided` set below.
        if etype == "VOID":
            continue
        # An undone event replays as if it never happened.
        if r["id"] in voided:
            continue

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
            # New model: completing work at a bay does NOT free it. The part
            # stays in the bay (DONE state) until a later MOVE / MATE /
            # UNIT_COMPLETE physically moves it out. We just stamp work_done_at
            # (idempotent) and, if a delay was somehow still open, clear it.
            run = result.bay_current.get(bay)
            if run and run.is_open:
                if run.work_done_at is None:
                    run.work_done_at = when
                if run.current_delay:
                    run.current_delay.cleared = when
                    run.current_delay.cleared_by = r["initials"]
                    run.current_delay = None
                run.completed_by = r["initials"]

        elif etype == "MATE":
            releasing = r["target_bay_id"]
            rel_run = result.bay_current.get(releasing)
            rel_wo = rel_run.work_order if rel_run else None
            if rel_run:
                close_run(rel_run, when, "MATE", r["initials"])
            u = result.units.get(wo)
            if u:
                u.mated = True
            # If the released bay held a DIFFERENT work order, that unit has
            # been absorbed into this one. End its journey as 'merged' so it
            # isn't left as perpetual open WIP (it keeps its real recorded
            # bay-time and delays; it just doesn't count as completed output).
            if rel_wo and rel_wo != wo:
                ru = result.units.get(rel_wo)
                if ru and ru.is_open and not ru.occupied_bays:
                    ru.completed = when
                    ru.outcome = "merged"
                    ru.mated = True
                    ru.merged_into = wo
                    # The survivor records the absorbed work order(s) so its LIVE
                    # elapsed/total can fold in the absorbed cell's bay-time and
                    # earliest start (see unit_live_times). We do NOT touch the
                    # survivor's own first_started or per-unit duration breakdown,
                    # so historical exports/stats stay honest -- the absorbed unit
                    # keeps its own 'merged' row. A chain of merges carries fwd.
                    if u is not None:
                        u.absorbed_work_orders.append(rel_wo)
                        u.absorbed_work_orders.extend(ru.absorbed_work_orders)

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

        elif etype == "PAUSE":
            run = result.bay_current.get(bay)
            if run and run.is_open and run.current_pause is None:
                # Parking a bay also ends any open delay -- it's no longer being
                # worked, so it can't still be "delayed". Active stops here.
                if run.current_delay:
                    run.current_delay.cleared = when
                    run.current_delay.cleared_by = r["initials"]
                    run.current_delay = None
                ep = PauseEpisode(when, r["initials"], r["id"])
                run.pauses.append(ep)
                run.current_pause = ep

        elif etype == "RESUME":
            run = result.bay_current.get(bay)
            if run and run.current_pause:
                run.current_pause.resumed = when
                run.current_pause.resumed_by = r["initials"]
                run.current_pause = None

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
    """Return active/delay/queue/cycle SECONDS for a unit (None when still open).

    * active = counted UNION of the unit's running intervals (parallel work in
      two bays is counted once, never doubled).
    * delay  = counted delayed time when NO bay of the unit is simultaneously
      running.
    * cycle  = total counted time from the unit's first start to its end.
    * queue  = the remainder (cycle - active - delay): everything the unit spent
      neither actively worked nor delayed -- i.e. WAITING. In the new occupancy
      model that waiting happens while the part sits DONE in a bay (or, in older
      logs, between bays); either way this captures it.
    """
    if unit.is_open:
        return {"active": None, "delay": None, "queue": None, "cycle": None}
    end = unit.completed or now
    unit_runs = [r for r in runs if r.work_order == unit.work_order]

    running_all: List[Interval] = []
    delayed_all: List[Interval] = []
    for r in unit_runs:
        running_all += r.running_intervals(end)
        delayed_all += r.delayed_intervals(end)

    active = counted_over(sched, running_all)
    delay = counted_over(sched, _subtract(merge_intervals(delayed_all),
                                          merge_intervals(running_all)))
    # Cycle is the linear span from first start to end, MINUS the time the unit
    # was parked (frozen) -- otherwise that frozen time would be miscounted as
    # queue. Parked time is non-counting, exactly like a break.
    frozen = _unit_frozen_intervals(unit_runs, end)
    cycle = counted_over(sched, _subtract([(unit.first_started, end)], frozen))
    queue = max(0.0, cycle - active - delay)
    return {"active": active, "delay": delay, "queue": queue, "cycle": cycle}


def unit_live_times(sched: Schedule, unit: UnitJourney, runs: List[BayRun],
                    now: datetime) -> Tuple[float, float]:
    """Return (elapsed, total) counted SECONDS for a unit, live.

    * elapsed = LINEAR time: counted wall-clock from the EARLIEST start of any
      cell now part of this unit to ``now`` (or completion). It does not
      double-count parallel work. When two units started near the same time and
      were merged, this ties to whichever cell started first.
    * total   = the time the unit has TAKEN UP across every bay it has touched,
      i.e. the SUM of each bay's occupied time. Two bays held in parallel add
      together here.
    They are equal for a unit that was only ever in one bay at a time; once it
    runs in two bays at once, total pulls ahead of elapsed by the overlap.

    A unit absorbed from a different work order (a merge of two distinct cells)
    folds its bay-time and start into the survivor here, so the surviving tile's
    elapsed/total reflect BOTH cells -- the merge makes them add up and the clock
    reaches back to the earliest cell.
    """
    end = unit.completed or now
    # The survivor's own work order plus any work orders merged into it.
    work_orders = {unit.work_order, *unit.absorbed_work_orders}
    unit_runs = [r for r in runs if r.work_order in work_orders]
    total = 0.0
    earliest = unit.first_started
    for r in unit_runs:
        # Occupancy with parked (frozen) spans removed -- a paused bay holds the
        # part but stops adding to the unit's taken-up time.
        total += counted_over(sched, r.occupied_intervals(end))
        if r.started < earliest:
            earliest = r.started
    # Linear elapsed, minus the spans the whole unit was parked, so a paused bay
    # freezes the tile's elapsed clock just like its active clock.
    frozen = _unit_frozen_intervals(unit_runs, end)
    elapsed = counted_over(sched, _subtract([(earliest, end)], frozen))
    return elapsed, total


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
    bay_name = {b["id"]: b["name"] for b in bays}

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
            "paused": None,          # {since, paused_by} when this bay is parked
            "paused_status": None,   # what the bay would be if not for the break
            "occupies_two": False,
            # Unit-level live times (counted seconds). elapsed = parallel-aware
            # union; total = sum across the unit's bays. Equal unless the unit
            # ran in two bays at once (then total stays ahead after they merge).
            "unit_elapsed_seconds": 0,
            "unit_total_seconds": 0,
        }
        if run is not None:
            underlying = run.status  # RUNNING | DELAYED | DONE
            tile["work_order"] = run.work_order
            tile["product_number"] = run.product_number
            tile["component_label"] = run.component_label
            tile["started_by"] = run.started_by
            # 2-bay indicator (a unit may occupy up to 2 bays).
            u = result.units.get(run.work_order)
            tile["occupies_two"] = bool(u and len(u.occupied_bays) >= 2)
            if u is not None:
                elapsed_u, total_u = unit_live_times(sched, u, result.runs, now)
                tile["unit_elapsed_seconds"] = int(elapsed_u)
                tile["unit_total_seconds"] = int(total_u)

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
            else:  # RUNNING / DONE / PAUSED -> show this bay's active time so far
                # For a parked bay this is the active time accrued BEFORE it was
                # paused (running_intervals already excludes the paused span), so
                # the tile shows how far the work got before it was set aside.
                tile["elapsed_seconds"] = int(
                    counted_over(sched, run.running_intervals(now)))

            if underlying == "PAUSED":
                p = run.current_pause
                tile["paused"] = {"since": _fmt(p.started), "paused_by": p.paused_by}

            # A parked bay always reads PAUSED -- it is a deliberate per-bay state
            # that outranks the floor-wide ON BREAK. Otherwise, during a break
            # window every occupied bay shows the distinct ON BREAK state (timers
            # frozen); we keep the underlying status as a hint of what it returns
            # to. Neither ON BREAK nor PAUSED is ever red.
            if on_break is not None and underlying != "PAUSED":
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

    # What the console Undo button offers right now: the most recent undoable
    # action, but only while it falls within the current shift (so a fresh shift
    # can't reach back and rewrite the last one). event_id is a race token the
    # POST echoes back, so two operators can't undo different things by surprise.
    undo = {"available": False, "summary": None, "kind": None,
            "count": 0, "at": None, "event_id": None}
    members = result.undo_members
    if members:
        at = min(events.parse_ts(m["ts"]) for m in members)
        if at >= undo_floor(sched, now):
            undo = {
                "available": True,
                "summary": describe_undo(members, bay_name),
                "kind": "shift_changeover" if len(members) > 1 else members[0]["type"],
                "count": len(members),
                "at": _fmt(at),
                "event_id": max(m["id"] for m in members),
            }

    return {
        "server_time": _fmt(now),
        # True only in a database created by make_demo_data.py. The UI shows a
        # "DEMO DATA" badge so example data can never pass for the real log.
        "demo_mode": bool(db.get_setting(conn, "demo_mode", False)),
        "is_counting": counting,
        "off_hours": off_hours,
        # The shift the current moment falls in (None if no shifts configured) --
        # the console's shift-changeover pop-up shows it as a badge.
        "shift": sched.shift_for(now),
        "on_break": ({"label": on_break["label"], "ends_at": _fmt(on_break["ends_at"])}
                     if on_break else None),
        "tiles": tiles,
        "queue": queue,
        "open_runs": open_runs,
        "open_delays": open_delays,
        "bay_number": bay_number,
        "undo": undo,
    }
