"""
metrics.py -- compute the Stats-page numbers from the derived tables.

Everything here is built from exports.derive_rows() (the same derivation the CSV/
XLSX export uses), so what you see on the Stats charts is exactly what you get in
an export of the same range. Per Appendix C2 we only ever report REAL recorded
data: a grouping with no data returns an empty series (the UI shows "no data"),
and nothing is interpolated, projected, or averaged-in to fill a gap.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from . import db, events, exports, state
from .schedule import Schedule


def _effective_range(conn, filters: dict, now: datetime):
    """Resolve the [start, end] datetimes the metrics cover."""
    start = filters.get("start")
    end = filters.get("end")
    start_dt = exports._parse_filter_dt(start, end_of_day=False) if start else None
    end_dt = exports._parse_filter_dt(end, end_of_day=True) if end else None
    if start_dt is None:
        first = conn.execute("SELECT MIN(ts) AS m FROM events;").fetchone()["m"]
        start_dt = events.parse_ts(first) if first else now
    if end_dt is None:
        end_dt = now
    return start_dt, end_dt


def compute(conn, filters: dict, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now()
    sched = Schedule.from_settings(conn)
    derived = exports.derive_rows(conn, now)

    delays = exports.apply_filters(derived["delays"], filters, "delays")
    runs = exports.apply_filters(derived["bay_runs"], filters, "bay_runs")
    journeys = exports.apply_filters(derived["unit_journeys"], filters, "unit_journeys")

    start_dt, end_dt = _effective_range(conn, filters, now)
    labor_rate = db.get_setting(conn, "labor_rate", None)

    return {
        "range": {"start": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                  "end": end_dt.strftime("%Y-%m-%d %H:%M:%S")},
        "delay_pareto_reason": _delay_pareto(delays, key="reason"),
        "delay_pareto_division": _delay_pareto(delays, key="division"),
        "bay_utilization": _bay_utilization(conn, runs, sched, start_dt, end_dt),
        "avg_cycle_by_product": _avg_cycle_by_product(journeys),
        "throughput": _throughput(journeys),
        "wip": _wip(journeys),
        "queue": _queue(journeys),
        "parallel": _parallel(conn, now),
        "cost": _cost(delays, labor_rate),
        "counts": {
            "delays": len([d for d in delays if d.get("cleared_at")]),
            "open_delays": len([d for d in delays if not d.get("cleared_at")]),
            "runs": len(runs),
            "units": len(journeys),
        },
    }


def _delay_pareto(delays, key: str):
    """Count + total delay minutes by reason or division, sorted descending."""
    count = defaultdict(int)
    minutes = defaultdict(float)
    for d in delays:
        if d.get("delay_minutes") is None:   # only closed delays have a total
            continue
        label = d.get(key) or "(none)"
        count[label] += 1
        minutes[label] += d["delay_minutes"]
    rows = [{"label": k, "count": count[k], "minutes": round(minutes[k], 1)}
            for k in minutes]
    rows.sort(key=lambda x: x["minutes"], reverse=True)
    return rows


def _bay_utilization(conn, runs, sched: Schedule, start_dt, end_dt):
    """Occupied counted minutes / available operating minutes, per bay.

    Occupancy is the wall-clock a bay physically held a unit (start -> end),
    CLAMPED to the reporting window and measured through the operating calendar.
    Clamping matters: without it, a long run reported over a short window (e.g.
    the "this shift" preset) contributes its whole length against a sliver of
    available time and the bay reads as >100% -- which is nonsense, since a bay
    holds at most one unit at a time. Clamped occupancy can never exceed the
    window's available minutes, so utilization is a true 0-100% figure (a final
    cap guards against any legacy overlapping runs in an old log)."""
    available = sched.counted_seconds(start_dt, end_dt) / 60.0
    occupied = defaultdict(float)
    for run in runs:
        started = events.parse_ts(run["started_at"])
        # An open run is still occupying the bay right now -> count up to the
        # window end; a closed run ends when it ended.
        ended = events.parse_ts(run["ended_at"]) if run.get("ended_at") else end_dt
        s, e = max(started, start_dt), min(ended, end_dt)
        if e > s:
            occupied[run["bay"]] += sched.counted_seconds(s, e) / 60.0
    bays = conn.execute("SELECT name FROM bays WHERE active = 1 ORDER BY is_extra, sort_order;").fetchall()
    rows = []
    for b in bays:
        used = round(occupied.get(b["name"], 0.0), 1)
        pct = round(min((used / available) * 100, 100.0), 1) if available > 0 else None
        rows.append({"bay": b["name"], "used_minutes": used, "utilization_pct": pct})
    return rows


def _avg_cycle_by_product(journeys):
    """Average cycle minutes by product number (completed units only)."""
    totals = defaultdict(float)
    counts = defaultdict(int)
    for j in journeys:
        if j.get("outcome") == "complete" and j.get("cycle_minutes") is not None:
            pn = j.get("product_number") or "(none)"
            totals[pn] += j["cycle_minutes"]
            counts[pn] += 1
    rows = [{"product_number": pn, "avg_cycle_minutes": round(totals[pn] / counts[pn], 1),
             "units": counts[pn]} for pn in totals]
    rows.sort(key=lambda x: x["avg_cycle_minutes"], reverse=True)
    return rows


def _throughput(journeys):
    """Units completed per calendar day (real points only)."""
    by_day = defaultdict(int)
    for j in journeys:
        if j.get("outcome") == "complete" and j.get("completed_at"):
            day = j["completed_at"][:10]
            by_day[day] += 1
    return [{"day": d, "units": by_day[d]} for d in sorted(by_day)]


def _wip(journeys):
    """Units currently in the area (open journeys)."""
    open_units = [j for j in journeys if j.get("outcome") == "open"]
    return {"current_wip": len(open_units),
            "work_orders": [j["work_order"] for j in open_units]}


def _queue(journeys):
    """Average queue minutes between bays (completed units that queued)."""
    vals = [j["queue_minutes"] for j in journeys
            if j.get("outcome") == "complete" and j.get("queue_minutes")]
    avg = round(sum(vals) / len(vals), 1) if vals else None
    return {"avg_queue_minutes": avg, "units_with_queue": len(vals)}


def _parallel(conn, now):
    """How often a work order ran in two bays at the same time."""
    r = state.replay(conn)
    parallel_units = 0
    for wo, u in r.units.items():
        runs = [run for run in r.runs if run.work_order == wo]
        if _has_overlap(runs, now):
            parallel_units += 1
    total = len(r.units)
    pct = round((parallel_units / total) * 100, 1) if total else None
    return {"parallel_units": parallel_units, "total_units": total, "pct": pct}


def _has_overlap(runs, now) -> bool:
    intervals = sorted((run.started, run.ended or now) for run in runs)
    for i in range(1, len(intervals)):
        if intervals[i][0] < intervals[i - 1][1]:
            return True
    return False


def _cost(delays, labor_rate):
    """Estimated delay cost per reason + total. Empty if no rate is set."""
    if labor_rate in (None, "", 0):
        return {"rate": None, "total": None, "by_reason": []}
    by_reason = defaultdict(float)
    total = 0.0
    for d in delays:
        if d.get("est_cost") is not None:
            by_reason[d.get("reason") or "(none)"] += d["est_cost"]
            total += d["est_cost"]
    rows = [{"reason": k, "cost": round(v, 2)} for k, v in by_reason.items()]
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return {"rate": labor_rate, "total": round(total, 2), "by_reason": rows}


# ---------------------------------------------------------------------------
# "Open & Recent / corrections" feed for the Stats page (stale items first).
# ---------------------------------------------------------------------------

def open_and_recent(conn, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now()
    sched = Schedule.from_settings(conn)
    r = state.replay(conn)
    stale_delay_min = db.get_setting(conn, "stale_delay_minutes", 120)
    stale_run_min = db.get_setting(conn, "stale_run_minutes", 720)
    bay_name = {b["id"]: b["name"] for b in conn.execute("SELECT id, name FROM bays;").fetchall()}

    open_delays, open_runs = [], []
    for d in r.delays:
        if d.is_open:
            mins = sched.counted_seconds(d.started, now) / 60.0
            open_delays.append({
                "bay_id": d.bay_id, "bay": bay_name.get(d.bay_id),
                "work_order": d.work_order, "reason": d.reason_label,
                "division": d.division, "note": d.note,
                "started_at": d.started.strftime("%Y-%m-%d %H:%M:%S"),
                "minutes": round(mins, 1), "flagged_by": d.flagged_by,
                "stale": mins >= float(stale_delay_min),
            })
    for run in r.runs:
        if run.is_open:
            mins = state.counted_over(sched, run.running_intervals(now)) / 60.0
            open_runs.append({
                "bay_id": run.bay_id, "bay": bay_name.get(run.bay_id),
                "work_order": run.work_order, "product_number": run.product_number,
                "started_at": run.started.strftime("%Y-%m-%d %H:%M:%S"),
                "minutes": round(mins, 1), "started_by": run.started_by,
                "delayed": run.current_delay is not None,
                "stale": mins >= float(stale_run_min),
            })

    # Recently closed (last 20 of each), newest first.
    recent_delays = sorted([d for d in r.delays if not d.is_open],
                           key=lambda d: d.cleared, reverse=True)[:20]
    recent_runs = sorted([run for run in r.runs if not run.is_open],
                         key=lambda run: run.ended, reverse=True)[:20]

    # Stale items float to the top of the open lists.
    open_delays.sort(key=lambda x: (not x["stale"], -x["minutes"]))
    open_runs.sort(key=lambda x: (not x["stale"], -x["minutes"]))

    return {
        "open_delays": open_delays,
        "open_runs": open_runs,
        "recent_delays": [{
            "bay": bay_name.get(d.bay_id), "work_order": d.work_order,
            "reason": d.reason_label,
            "started_at": d.started.strftime("%Y-%m-%d %H:%M:%S"),
            "cleared_at": d.cleared.strftime("%Y-%m-%d %H:%M:%S"),
            "start_event_id": d.start_event_id,
        } for d in recent_delays],
        "recent_runs": [{
            "bay": bay_name.get(run.bay_id), "work_order": run.work_order,
            "started_at": run.started.strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": run.ended.strftime("%Y-%m-%d %H:%M:%S"),
            "start_event_id": run.start_event_id,
        } for run in recent_runs],
        "thresholds": {"stale_delay_minutes": stale_delay_min,
                       "stale_run_minutes": stale_run_min},
    }
