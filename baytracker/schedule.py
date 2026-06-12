"""
schedule.py -- the time engine: operating hours, breaks, shifts, and the
all-important "non-counting time" rule.

Two kinds of time never count toward any timer (spec section 4):

  1. Scheduled breaks (lunch + short breaks) -- the same windows every day.
  2. Off-hours -- any time outside the operating calendar.

During non-counting time BOTH the active and delay timers pause and resume
afterward, automatically and clock-driven (there is no manual break button).

Everything in this module is pure arithmetic over the configured schedule, so it
is easy to unit-test and reason about. The single primitive that the rest of the
system relies on is:

    Schedule.counted_seconds(t0, t1)
        -> the number of seconds between t0 and t1 that ARE operating time AND
           are NOT inside a break window. (Breaks + off-hours are subtracted.)

Elapsed/active/delay/queue durations are ALL computed by feeding the relevant
start/end timestamps through that one function, so every screen agrees and a
page refresh never resets or desyncs a timer.

----------------------------------------------------------------------------
Configuration shapes (all editable in /admin, all start empty -- Appendix C4):

  operating_calendar :  None  => treat ALL time as counting (the safe default
                                 before any hours are entered; nothing freezes).
                        OR a dict keyed by weekday short name with a list of
                        within-day [start, end] windows, e.g.:
                            {"mon": [["00:00","24:00"]],   # runs 24h
                             "sat": [["06:00","22:00"]],   # two shifts only
                             "sun": []}                    # closed
                        A missing or empty weekday = closed that day.
                        "24:00" means end-of-day (midnight that night).

  break_schedule :      list of {"start":"HH:MM", "minutes":int, "label":str},
                        applied every operating day. e.g.
                            [{"start":"11:30","minutes":30,"label":"Lunch"}]

  shifts :              list of {"name":str, "start":"HH:MM"} cutoffs, sorted by
                        time of day. The shift covering a timestamp is the one
                        whose start is the latest start at or before it (wrapping
                        past midnight). Empty => no shift attribution (None).
----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime, timedelta, date
from typing import List, Optional, Tuple

from . import db


# Python's date.weekday(): Monday=0 .. Sunday=6. Map to the keys used in config.
_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# A closed-form interval is a (start_datetime, end_datetime) pair.
Interval = Tuple[datetime, datetime]


def _parse_hhmm_on(d: date, hhmm: str) -> datetime:
    """Combine a 'HH:MM' string with a date. '24:00' => 00:00 the next day."""
    hh, mm = hhmm.split(":")
    h, m = int(hh), int(mm)
    if h == 24 and m == 0:
        return datetime(d.year, d.month, d.day) + timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, m)


# --- small interval algebra -------------------------------------------------

def _clip(intervals: List[Interval], lo: datetime, hi: datetime) -> List[Interval]:
    """Clip every interval to the window [lo, hi]; drop empty results."""
    out: List[Interval] = []
    for s, e in intervals:
        cs, ce = max(s, lo), min(e, hi)
        if ce > cs:
            out.append((cs, ce))
    return out


def _subtract(intervals: List[Interval], holes: List[Interval]) -> List[Interval]:
    """Return ``intervals`` with every ``hole`` removed (set difference)."""
    result = list(intervals)
    for hs, he in holes:
        nxt: List[Interval] = []
        for s, e in result:
            if he <= s or hs >= e:
                nxt.append((s, e))            # no overlap
                continue
            if hs > s:
                nxt.append((s, hs))           # piece before the hole
            if he < e:
                nxt.append((he, e))           # piece after the hole
        result = nxt
    return result


def _measure(intervals: List[Interval]) -> float:
    """Total seconds covered by a list of intervals."""
    return sum((e - s).total_seconds() for s, e in intervals)


class Schedule:
    """An immutable snapshot of the operating/break/shift configuration."""

    def __init__(self, operating_calendar, break_schedule, shifts):
        self.operating_calendar = operating_calendar  # None or dict
        self.break_schedule = break_schedule or []     # list of dicts
        # Sort shift cutoffs by their HH:MM so "latest start <= time" is easy.
        self.shifts = sorted(
            (shifts or []),
            key=lambda s: _minutes_of_day(s.get("start", "00:00")),
        )

    # ---- construction ----
    @classmethod
    def from_settings(cls, conn) -> "Schedule":
        return cls(
            operating_calendar=db.get_setting(conn, "operating_calendar", None),
            break_schedule=db.get_setting(conn, "break_schedule", []),
            shifts=db.get_setting(conn, "shifts", []),
        )

    # ---- per-day window builders ----
    def _operating_on(self, d: date) -> List[Interval]:
        """Operating intervals on a single calendar day."""
        if self.operating_calendar is None:
            # No calendar entered yet: the whole day counts (nothing freezes).
            return [(_parse_hhmm_on(d, "00:00"), _parse_hhmm_on(d, "24:00"))]
        windows = self.operating_calendar.get(_WEEKDAY_KEYS[d.weekday()], [])
        return [(_parse_hhmm_on(d, w[0]), _parse_hhmm_on(d, w[1])) for w in windows]

    def _breaks_on(self, d: date) -> List[Interval]:
        """Break intervals on a single calendar day."""
        out: List[Interval] = []
        for b in self.break_schedule:
            start = _parse_hhmm_on(d, b["start"])
            out.append((start, start + timedelta(minutes=int(b["minutes"]))))
        return out

    # ---- the core primitive ----
    def counted_seconds(self, t0: datetime, t1: datetime) -> float:
        """Seconds between t0 and t1 that are operating AND not in a break.

        This is THE function the whole system leans on for every duration. It
        subtracts both off-hours and breaks. Returns 0 for a zero/negative span
        (e.g. a bad correction) rather than a negative number.
        """
        if t1 <= t0:
            return 0.0
        total = 0.0
        # Walk day by day. Episodes are hours/days long, so this is cheap; we
        # start one day early so an operating window that began before t0 (it
        # can't cross midnight in our model, but be safe) is still captured.
        d = t0.date() - timedelta(days=1)
        last = t1.date()
        while d <= last:
            operating = _clip(self._operating_on(d), t0, t1)
            if operating:
                counted = _subtract(operating, self._breaks_on(d))
                total += _measure(counted)
            d += timedelta(days=1)
        return total

    # ---- "right now" helpers used by the live state/UI ----
    def is_off_hours(self, now: datetime) -> bool:
        """True if ``now`` is outside operating hours."""
        operating = self._operating_on(now.date())
        return not any(s <= now < e for s, e in operating)

    def active_break(self, now: datetime) -> Optional[dict]:
        """If ``now`` is inside a break window, return {label, ends_at}; else None."""
        for b in self.break_schedule:
            start = _parse_hhmm_on(now.date(), b["start"])
            end = start + timedelta(minutes=int(b["minutes"]))
            if start <= now < end:
                return {"label": b.get("label") or "Break", "ends_at": end}
        return None

    def is_counting(self, now: datetime) -> bool:
        """True if timers should be ticking right now (operating, not on break)."""
        return (not self.is_off_hours(now)) and (self.active_break(now) is None)

    def shift_for(self, when: datetime) -> Optional[str]:
        """Attribute a timestamp to a shift name using the configured cutoffs.

        Uses clean cutoffs only -- real-world handoff overlaps (a 2:00/2:30
        changeover) intentionally don't affect attribution (spec section 4).
        Returns None when no shifts are configured.
        """
        if not self.shifts:
            return None
        mins = when.hour * 60 + when.minute
        chosen = None
        for s in self.shifts:
            if _minutes_of_day(s.get("start", "00:00")) <= mins:
                chosen = s
        # If the time is before the first cutoff of the day, it belongs to the
        # last shift (which started the previous evening and wraps past midnight).
        if chosen is None:
            chosen = self.shifts[-1]
        return chosen.get("name")


def _minutes_of_day(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)
