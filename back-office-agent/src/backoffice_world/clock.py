"""Clocks, zones, weeks and the rails' settlement arithmetic.

Timestamps are integers: seconds since the Unix epoch, UTC. Every destination's local time is a
fixed UTC offset for the whole window (no zone changes its offset inside it), so local time is
plain arithmetic. Operator time is America/New_York on EDT, UTC-4, for the whole window.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

HOUR = 3600
DAY = 86400

OPERATOR_OFFSET = -4 * HOUR
ZONE_OFFSET: dict[str, int] = {
    "US": -4 * HOUR,  # America/New_York, EDT
    "MX": -6 * HOUR,  # America/Mexico_City, no DST since 2022
    "BR": -3 * HOUR,  # America/Sao_Paulo, no DST since 2019
    "PH": 8 * HOUR,  # Asia/Manila
    "DE": 2 * HOUR,  # Europe/Berlin, CEST
}
ZONE_NAME: dict[str, str] = {
    "US": "America/New_York",
    "MX": "America/Mexico_City",
    "BR": "America/Sao_Paulo",
    "PH": "Asia/Manila",
    "DE": "Europe/Berlin",
}

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def ts(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    offset: int = OPERATOR_OFFSET,
) -> int:
    """A timestamp from a local wall-clock time at a fixed UTC offset."""
    local = dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.UTC)
    return int((local - _EPOCH).total_seconds()) - offset


def to_local(t: int, offset: int) -> dt.datetime:
    """The naive local wall-clock time at `offset` for timestamp `t`."""
    return _EPOCH.replace(tzinfo=None) + dt.timedelta(seconds=t + offset)


def iso_utc(t: int | None) -> str | None:
    if t is None:
        return None
    return to_local(t, 0).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso_utc(s: str) -> int:
    d = dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    return int((d - _EPOCH).total_seconds())


def local_date(t: int, offset: int) -> dt.date:
    return to_local(t, offset).date()


def local_midnight(d: dt.date, offset: int) -> int:
    return ts(d.year, d.month, d.day, offset=offset)


def operator_week(t: int) -> int:
    """The operator-time ISO week number of timestamp `t` (the glossary's week)."""
    return local_date(t, OPERATOR_OFFSET).isocalendar()[1]


def week_bounds(week: int, year: int = 2026) -> tuple[int, int]:
    """[start, end) of an operator-time ISO week, as timestamps."""
    monday = dt.date.fromisocalendar(year, week, 1)
    start = local_midnight(monday, OPERATOR_OFFSET)
    return start, start + 7 * DAY


# The window and the demo's clock (spec section 3).
WINDOW_START = ts(2026, 6, 29, 0, 0, 0)
WINDOW_END = ts(2026, 10, 4, 23, 59, 59)
T0 = ts(2026, 10, 5, 9, 0, 0)
HISTORY_WEEKS = tuple(range(27, 37))
MONITORING_WEEKS = tuple(range(37, 41))
HISTORY_END = week_bounds(36)[1]
MONITORING_START = week_bounds(37)[0]


class Calendar:
    """A destination's banking calendar: weekends plus the frozen non-banking weekdays."""

    def __init__(self, destination: str, non_banking_weekdays: Iterable[dt.date]) -> None:
        self.destination = destination
        self.offset = ZONE_OFFSET[destination]
        self.holidays: frozenset[dt.date] = frozenset(non_banking_weekdays)

    def is_banking_day(self, d: dt.date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def next_banking_day(self, d: dt.date) -> dt.date:
        d = d + dt.timedelta(days=1)
        while not self.is_banking_day(d):
            d = d + dt.timedelta(days=1)
        return d

    def banking_days_between(self, start: dt.date, end: dt.date) -> int:
        """Banking days strictly after `start` up to and including `end`."""
        n = 0
        d = start
        while d < end:
            d = d + dt.timedelta(days=1)
            if self.is_banking_day(d):
                n += 1
        return n

    def add_banking_days(self, d: dt.date, n: int) -> dt.date:
        while n > 0:
            d = self.next_banking_day(d)
            n -= 1
        return d


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _at(d: dt.date, hm: str, offset: int) -> int:
    h, m = _hm(hm)
    return ts(d.year, d.month, d.day, h, m, 0, offset=offset)


# Rail clocks (spec section 4). A rail is either 24/7 (elapsed time is wall-clock) or a calendar
# rail (elapsed time is counted in settlement opportunities: cutoffs on banking days).

CLOCK_RAILS = frozenset({"SPEI", "PIX", "InstaPay", "SCT Inst"})
CALENDAR_RAILS = frozenset({"PESONet", "SCT", "ACH", "Fedwire"})
RAIL_DESTINATION: dict[str, str] = {
    "SPEI": "MX",
    "PIX": "BR",
    "InstaPay": "PH",
    "PESONet": "PH",
    "SCT": "DE",
    "SCT Inst": "DE",
    "ACH": "US",
    "Fedwire": "US",
}
CORRIDOR_RAILS: dict[str, tuple[str, ...]] = {
    "MX": ("SPEI",),
    "BR": ("PIX",),
    "PH": ("InstaPay", "PESONet"),
    "DE": ("SCT Inst", "SCT"),
    "US": ("ACH", "Fedwire"),
}
# Provider cutoffs (local), which sit ahead of the rail's own.
ACH_CUTOFFS = ("10:00", "14:15", "16:15")
ACH_SETTLEMENTS = ("13:00", "17:00", "18:00")
ACH_NEXT_DAY = "13:00"
PESONET_CUTOFF = "14:00"
PESONET_CYCLES = ("12:00", "16:00")
SCT_CUTOFF = "16:00"
SCT_SETTLES = "09:00"
FEDWIRE_CUTOFF = "17:00"
FEDWIRE_OPEN_PREV = "21:00"
FEDWIRE_CLOSE = "19:00"


def cutoffs_of(rail: str) -> tuple[str, ...]:
    if rail == "ACH":
        return ACH_CUTOFFS
    if rail == "PESONet":
        return (PESONET_CUTOFF,)
    if rail == "SCT":
        return (SCT_CUTOFF,)
    if rail == "Fedwire":
        return (FEDWIRE_CUTOFF,)
    raise ValueError(rail)


def settlement_time(rail: str, submitted_at: int, calendar: Calendar) -> int:
    """When a calendar-rail payout submitted at `submitted_at` settles, by the rail's rule."""
    off = calendar.offset
    d = local_date(submitted_at, off)
    if rail == "ACH":
        if calendar.is_banking_day(d):
            for cut, settle in zip(ACH_CUTOFFS, ACH_SETTLEMENTS, strict=True):
                if submitted_at <= _at(d, cut, off):
                    return _at(d, settle, off)
        return _at(calendar.next_banking_day(d), ACH_NEXT_DAY, off)
    if rail == "PESONet":
        if calendar.is_banking_day(d):
            if submitted_at <= _at(d, PESONET_CYCLES[0], off):
                return _at(d, PESONET_CYCLES[0], off)
            if submitted_at <= _at(d, PESONET_CUTOFF, off):
                return _at(d, PESONET_CYCLES[1], off)
        return _at(calendar.next_banking_day(d), PESONET_CYCLES[0], off)
    if rail == "SCT":
        if calendar.is_banking_day(d) and submitted_at <= _at(d, SCT_CUTOFF, off):
            return _at(calendar.next_banking_day(d), SCT_SETTLES, off)
        nxt = calendar.next_banking_day(d)
        return _at(calendar.next_banking_day(nxt), SCT_SETTLES, off)
    raise ValueError(rail)


def fedwire_settlement(submitted_at: int, calendar: Calendar, minutes: int) -> int:
    """A Fedwire payout settles `minutes` after submission inside the business day it belongs
    to (21:00 ET the previous calendar day to 19:00 ET), or in the next business day's morning
    when submitted after the provider's 17:00 cutoff or on a non-business day."""
    off = calendar.offset
    d = local_date(submitted_at, off)
    if calendar.is_banking_day(d) and submitted_at <= _at(d, FEDWIRE_CUTOFF, off):
        t = submitted_at + minutes * 60
        close = _at(d, FEDWIRE_CLOSE, off)
        return min(t, close - 60)
    nxt = calendar.next_banking_day(d)
    # The next business day opened at 21:00 ET the evening before; the payout is processed in
    # its morning hours.
    return _at(nxt, "08:00", off) + minutes * 60


def opportunities_elapsed(rail: str, submitted_at: int, at: int, calendar: Calendar) -> int:
    """Settlement opportunities (cutoffs on banking days) passed between submission and `at`.

    After the last cutoff of a banking day, or on a non-banking day, a payout counts as
    submitted at the next banking day's first cutoff, so no opportunity passes until then.
    """
    if at <= submitted_at:
        return 0
    off = calendar.offset
    cuts = cutoffs_of(rail)
    d = local_date(submitted_at, off)
    n = 0
    # Walk cutoffs from the submission day forward until `at`.
    first = True
    while True:
        if calendar.is_banking_day(d):
            for cut in cuts:
                c = _at(d, cut, off)
                if first and c < submitted_at:
                    continue
                if c > at:
                    return n
                n += 1
        first = False
        d = d + dt.timedelta(days=1)
        if local_midnight(d, off) > at:
            return n


def elapsed_on_clock(rail: str, submitted_at: int, at: int, calendar: Calendar) -> int:
    """Elapsed time on the rail's clock: minutes on a 24/7 rail, opportunities on a calendar rail."""
    if rail in CLOCK_RAILS:
        return max(0, (at - submitted_at) // 60)
    return opportunities_elapsed(rail, submitted_at, at, calendar)


def clock_unit(rail: str) -> str:
    return "minutes" if rail in CLOCK_RAILS else "opportunities"


def next_cutoff(rail: str, at: int, calendar: Calendar) -> int | None:
    """The next settlement opportunity at or after `at` on a calendar rail."""
    if rail in CLOCK_RAILS:
        return None
    off = calendar.offset
    d = local_date(at, off)
    for _ in range(40):
        if calendar.is_banking_day(d):
            for cut in cutoffs_of(rail):
                c = _at(d, cut, off)
                if c >= at:
                    return c
        d = d + dt.timedelta(days=1)
    return None


def pix_night_queue(created_at: int) -> int | None:
    """If `created_at` falls in PIX's night window (20:00 to 06:00 BRT), the 06:00 it queues to."""
    off = ZONE_OFFSET["BR"]
    local = to_local(created_at, off)
    if local.hour >= 20:
        d = local.date() + dt.timedelta(days=1)
        return _at(d, "06:00", off)
    if local.hour < 6:
        return _at(local.date(), "06:00", off)
    return None


def fmt_local(t: int, destination: str) -> str:
    return (
        to_local(t, ZONE_OFFSET[destination]).strftime("%Y-%m-%d %H:%M")
        + " "
        + ZONE_NAME[destination]
    )


def fmt_operator(t: int) -> str:
    return to_local(t, OPERATOR_OFFSET).strftime("%Y-%m-%d %H:%M") + " America/New_York"
