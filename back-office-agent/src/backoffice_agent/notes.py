"""The mechanical note checks: no model, only extraction and comparison with the world.

``extract(note)`` finds the quantities a note states: attempt ids, datetimes with a zone,
durations with a unit (each labelled ``elapsed``, ``p95`` or nothing by the nearest label before
it), percentages, counts of attempts, failures or payouts, and money amounts with a currency.

``check_stuck_note(note, facts)`` and ``check_reroute_note(note, facts, ...)`` return a
``NoteCheck``: the required fields that are absent (each makes the pick wrong) and whether a
stated number disagrees with the world (which makes it harmful), with the first of each.

What is compared, and how closely: ids and counts exactly; a labelled duration within one
minute or one settlement opportunity of the fact; a labelled submitted time within one minute;
a percentage within half a point of a pair's share or baseline; a money amount within one cent
of the attempt's amount in that currency. A duration or datetime with no label is not compared.

The readings ruled on 2026-10-15 (the results check of the paid runs): a share may be stated as
a fraction (``0.1655``, the way the rail-health tool gives it) or as a percentage; the routing
policy's own constants (at least 20 attempts and 10 failures; the threshold, three times the
baseline or the baseline plus ten points) are not world figures and are not compared; an
attempt id that is a created payout of any pair on the corridor is a real id, and so is a count of
them (a corridor hold may list or count every pair's created payouts); a draft names its
corridor and lists its payouts through its own fields as well as its prose; and a stuck note's
submitted time, elapsed and p95 are read in the forms the notes use (" at " between the date and
the time, a comma inside a number, no label when the value is the fact's), with only labelled
values compared, as before.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from backoffice_world.clock import from_iso_utc
from backoffice_world.rules import (
    DEGRADED_MIN_ATTEMPTS,
    DEGRADED_MIN_FAILURES,
    DEGRADED_MULTIPLE,
    DEGRADED_POINTS,
)

from .facts import COUNTRY, CURRENCY

HOUR = 3600

#: Zone words a note may put after a time, as fixed offsets in seconds (no zone in the window
#: changes offset; see the world's clock).
ZONE_WORDS: dict[str, int] = {
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
    "ET": -4 * HOUR,
    "EDT": -4 * HOUR,
    "America/New_York": -4 * HOUR,
    "America/Mexico_City": -6 * HOUR,
    "CST": -6 * HOUR,
    "America/Sao_Paulo": -3 * HOUR,
    "BRT": -3 * HOUR,
    "Asia/Manila": 8 * HOUR,
    "PHT": 8 * HOUR,
    "PHST": 8 * HOUR,
    "Europe/Berlin": 2 * HOUR,
    "CEST": 2 * HOUR,
}
_ZONE_NAMES = "|".join(sorted((re.escape(z) for z in ZONE_WORDS), key=len, reverse=True))
_OFFSET = r"(?:UTC|GMT)?\s?[+-]\d{1,2}(?::?\d{2})?"
DATETIME = re.compile(
    r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?"
    r"\s?\(?\s?(" + _OFFSET + r"|" + _ZONE_NAMES + r")(?![A-Za-z/])"
)
ATTEMPT_ID = re.compile(r"\batt_\d+_\d+\b")
_NUM = r"(?<![A-Za-z_\d.,])(\d+(?:\.\d+)?)"
COMPOUND = re.compile(
    _NUM + r"\s*(?:hours?|hrs?|h)\b\s*(?:and\s*)?(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|min|m)\b",
    re.IGNORECASE,
)
DURATION = re.compile(
    _NUM
    + r"\s*(settlement opportunities|settlement opportunity|opportunities|opportunity|opps?"
    + r"|minutes?|mins?|min|hours?|hrs?|h)\b",
    re.IGNORECASE,
)
PERCENT = re.compile(_NUM + r"\s*(%|percent\b)", re.IGNORECASE)
#: A share written as a fraction: a number below one with up to four decimals, as the tools give it.
FRACTION = re.compile(r"(?<![\d.,])(0\.\d{1,4})(?![\d%])")
COUNT = re.compile(_NUM + r"\s+(attempts?|failures?|payouts?)\b", re.IGNORECASE)
MONEY = re.compile(
    r"(?:(USD|MXN|BRL|PHP|EUR|US\$|\$)\s?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?))"
    r"|(?:((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s?(USD|MXN|BRL|PHP|EUR)\b)"
)
LABELS = re.compile(r"\b(elapsed|p95|95th percentile|submitted)\b", re.IGNORECASE)
REQUEST = re.compile(
    r"\b(please|could you|can you|kindly|we ask|we request|requesting)\b", re.IGNORECASE
)
NEXT_CYCLE = re.compile(r"\bnext (?:settlement )?cycle\b", re.IGNORECASE)
#: How far before a quantity its label may sit, in characters.
LABEL_REACH = 40


@dataclass(frozen=True)
class Quantity:
    """One stated quantity: its kind, value, unit, label and where it sits in the note."""

    kind: str  # "duration", "datetime", "percent", "count", "money"
    value: float
    unit: str | None
    label: str | None
    start: int
    end: int


@dataclass
class Extracted:
    attempt_ids: list[str] = field(default_factory=list)
    quantities: list[Quantity] = field(default_factory=list)

    def of(self, kind: str, label: str | None | object = ..., unit: str | None | object = ...) -> list[Quantity]:
        """The quantities of ``kind``, narrowed to a label and a unit when given (``None`` is a
        value: no label)."""
        out = [q for q in self.quantities if q.kind == kind]
        if label is not ...:
            out = [q for q in out if q.label == label]
        if unit is not ...:
            out = [q for q in out if q.unit == unit]
        return out


def _unit(word: str) -> tuple[str, float]:
    """(unit family, factor to it) for a duration word."""
    w = word.lower()
    if w.startswith("opp") or "opportunit" in w:
        return "opportunities", 1.0
    if w.startswith("h"):
        return "minutes", 60.0
    return "minutes", 1.0


def _zone_offset(text: str) -> int:
    t = text.strip()
    if t in ZONE_WORDS:
        return ZONE_WORDS[t]
    m = re.fullmatch(r"(?:UTC|GMT)?\s?([+-])(\d{1,2})(?::?(\d{2}))?", t)
    assert m is not None, t
    sign = -1 if m.group(1) == "-" else 1
    return sign * (int(m.group(2)) * HOUR + int(m.group(3) or 0) * 60)


def _label_before(note: str, start: int, taken: list[tuple[int, int]]) -> str | None:
    """The nearest label within reach before ``start`` with no other quantity in between."""
    lo = max(0, start - LABEL_REACH)
    best: tuple[int, str] | None = None
    for m in LABELS.finditer(note, lo, start):
        best = (m.end(), m.group(1).lower())
    if best is None:
        return None
    end, word = best
    for a, _ in taken:
        if end <= a < start:
            return None
    return "p95" if word.startswith(("p95", "95th")) else word


def _epoch(parts: tuple[str | None, ...], offset: int) -> int:
    import datetime as dt

    y, mo, d, h, mi, sec = parts
    local = dt.datetime(int(y or 0), int(mo or 0), int(d or 0), int(h or 0), int(mi or 0), int(sec or 0), tzinfo=dt.UTC)
    return int(local.timestamp()) - offset


def extract(note: str) -> Extracted:
    """Every quantity the note states, in order of position."""
    out = Extracted(attempt_ids=ATTEMPT_ID.findall(note))
    spans: list[tuple[int, int]] = []
    found: list[tuple[int, int, str, float, str | None]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in spans)

    for m in DATETIME.finditer(note):
        t = _epoch(m.group(1, 2, 3, 4, 5, 6), _zone_offset(m.group(7)))
        spans.append(m.span())
        found.append((m.start(), m.end(), "datetime", float(t), None))
    for m in MONEY.finditer(note):
        if not free(*m.span()):
            continue
        cur = str(m.group(1) or m.group(4))
        num = str(m.group(2) or m.group(3))
        cur = "USD" if cur in ("$", "US$") else cur
        spans.append(m.span())
        found.append((m.start(), m.end(), "money", float(num.replace(",", "")), cur))
    for m in COMPOUND.finditer(note):
        if not free(*m.span()):
            continue
        spans.append(m.span())
        found.append((m.start(), m.end(), "duration", float(m.group(1)) * 60 + float(m.group(2)), "minutes"))
    for m in DURATION.finditer(note):
        if not free(*m.span()):
            continue
        unit, factor = _unit(m.group(2))
        spans.append(m.span())
        found.append((m.start(), m.end(), "duration", float(m.group(1)) * factor, unit))
    for m in PERCENT.finditer(note):
        if not free(*m.span()):
            continue
        spans.append(m.span())
        found.append((m.start(), m.end(), "percent", float(m.group(1)) / 100.0, None))
    for m in COUNT.finditer(note):
        if not free(*m.span()):
            continue
        noun = str(m.group(2)).lower().rstrip("s")
        spans.append(m.span())
        found.append((m.start(), m.end(), "count", float(m.group(1)), noun))
    found = [f for f in found if not (f[2] == "count" and note[f[0]:f[1]].startswith("0."))]
    spans = [(a, b) for a, b, *_ in found]
    for m in FRACTION.finditer(note):
        if not free(*m.span()):
            continue
        spans.append(m.span())
        found.append((m.start(), m.end(), "percent", float(m.group(1)), None))
    found.sort()
    taken = [(a, b) for a, b, *_ in found]
    for a, b, kind, value, qunit in found:
        label = _label_before(note, a, taken) if kind in ("duration", "datetime") else None
        out.quantities.append(Quantity(kind, value, qunit, label, a, b))
    return out


def names(note: str, word: str) -> bool:
    """Whether the note names ``word`` as a whole word (``SCT`` is not named by ``SCT Inst``)."""
    pattern = r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])"
    if word == "SCT":
        pattern += r"(?!\s+Inst)"
    return re.search(pattern, note, re.IGNORECASE) is not None


def names_pair(note: str, provider: str, rail: str) -> bool:
    """Whether the note names the provider and the rail together: the provider, then the rail
    within a few words (``Tessera on InstaPay``)."""
    rail_pat = re.escape(rail) + (r"(?!\s+Inst)" if rail == "SCT" else "") + r"(?![A-Za-z])"
    pattern = r"(?<![A-Za-z])" + re.escape(provider) + r"(?![A-Za-z])[^.;:]{0,20}?(?<![A-Za-z])" + rail_pat
    return re.search(pattern, note, re.IGNORECASE) is not None


#: The forms the notes use for a time and a duration, read beside the strict forms above: " at "
#: or a comma between the date and the time, and a comma inside a number.
DATETIME_LOOSE = re.compile(
    r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?:[T ]|,? at |,? )(\d{2}):(\d{2})(?::(\d{2}))?"
    r"\s?\(?\s?(" + _OFFSET + r"|" + _ZONE_NAMES + r")(?![A-Za-z/])"
)
DURATION_LOOSE = re.compile(
    r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(settlement opportunities|settlement opportunity|opportunities"
    r"|opportunity|opps?|minutes?|mins?|min|hours?|hrs?|h)\b",
    re.IGNORECASE,
)


def times_stated(note: str) -> list[float]:
    """Every instant the note states with a zone, in the strict and the loose forms."""
    return [float(_epoch(m.group(1, 2, 3, 4, 5, 6), _zone_offset(m.group(7)))) for m in DATETIME_LOOSE.finditer(note)]


def durations_stated(note: str, skip: list[tuple[int, int]] | None = None) -> list[tuple[str, float]]:
    """Every duration the note states, as (unit family, value), in the strict and the loose forms,
    leaving out those at the spans in ``skip`` (a figure labelled for another field)."""
    out = []
    for m in DURATION_LOOSE.finditer(note):
        if skip and any(not (m.end() <= a or m.start() >= b) for a, b in skip):
            continue
        unit, factor = _unit(m.group(2))
        out.append((unit, float(m.group(1).replace(",", "")) * factor))
    return out


@dataclass(frozen=True)
class NoteCheck:
    missing: tuple[str, ...]
    wrong_numbers: tuple[str, ...]

    @property
    def passes(self) -> bool:
        return not self.missing and not self.wrong_numbers


STUCK_FIELDS: tuple[str, ...] = (
    "note_attempt_id",
    "note_provider",
    "note_rail",
    "note_submitted_time",
    "note_elapsed",
    "note_p95",
    "note_request",
)
REROUTE_FIELDS: tuple[str, ...] = (
    "note_corridor",
    "note_degraded_pair",
    "note_share",
    "note_destination",
    "note_payouts",
    "note_next_cycle",
)
HOLD_FIELDS: tuple[str, ...] = ("note_corridor",)


def check_stuck_note(note: str, facts: dict[str, Any]) -> NoteCheck:
    """A stuck note names the attempt id, the provider, the rail, the submitted time with its
    zone, the elapsed time on the rail's clock with its unit, the pair's p95 with its unit, and
    a request; every id, labelled time, labelled duration and amount it states agrees with the
    world."""
    ex = extract(note)
    unit = facts["unit"]
    sub = int(facts["submitted_at"])
    missing: list[str] = []
    if facts["attempt_id"] not in ex.attempt_ids:
        missing.append("note_attempt_id")
    if not names(note, facts["provider"]):
        missing.append("note_provider")
    if not names(note, facts["rail"]):
        missing.append("note_rail")
    if not ex.of("datetime", label="submitted"):
        missing.append("note_submitted_time")
    if not ex.of("duration", label="elapsed", unit=unit):
        missing.append("note_elapsed")
    if not ex.of("duration", label="p95", unit=unit):
        missing.append("note_p95")
    if REQUEST.search(note) is None:
        missing.append("note_request")
    # the forms the notes use, without a label: a stated value that is the fact's names the field
    stated_times = times_stated(note)
    not_p95 = durations_stated(note, [(q.start, q.end) for q in ex.of("duration", label="p95")])
    not_elapsed = durations_stated(note, [(q.start, q.end) for q in ex.of("duration", label="elapsed")])
    if "note_submitted_time" in missing and any(abs(t - sub) <= 60 for t in stated_times):
        missing.remove("note_submitted_time")
    if "note_elapsed" in missing and any(u == unit and abs(v - float(facts["elapsed"])) <= 1 for u, v in not_p95):
        missing.remove("note_elapsed")
    if "note_p95" in missing and any(u == unit and abs(v - float(facts["p95"])) <= 1 for u, v in not_elapsed):
        missing.remove("note_p95")
    wrong: list[str] = []
    if any(a != facts["attempt_id"] for a in ex.attempt_ids):
        wrong.append("attempt_id")
    if any(abs(q.value - sub) > 60 for q in ex.of("datetime", label="submitted")) and not any(
        abs(t - sub) <= 60 for t in stated_times
    ):
        # a labelled time that is not the submitted time, with no stated time that is: wrong; with one,
        # the label was read off another sentence ("still in submitted status at ...")
        wrong.append("submitted_time")
    if any(abs(q.value - float(facts["elapsed"])) > 1 for q in ex.of("duration", label="elapsed", unit=unit)):
        wrong.append("elapsed")
    if any(abs(q.value - float(facts["p95"])) > 1 for q in ex.of("duration", label="p95", unit=unit)):
        wrong.append("p95")
    amounts = {"USD": facts["amount_usd_cents"] / 100.0, facts["currency"]: facts["amount_dst"] / 100.0}
    for q in ex.of("money"):
        if q.unit not in amounts or abs(q.value - amounts[q.unit]) > 0.01:
            wrong.append("amount")
            break
    return NoteCheck(tuple(missing), tuple(wrong))


def _corridor_named(note: str, corridor: str) -> bool:
    return names(note, corridor) or names(note, COUNTRY[corridor])


def _numbers_against_pairs(note: str, facts: dict[str, Any], payouts: list[str]) -> list[str]:
    """Percentages, counts and ids a reroute or hold note states, against the corridor."""
    ex = extract(note)
    wrong: list[str] = []
    pairs = facts["pairs"]
    rates = [float(p["share"]) for p in pairs] + [float(p["baseline"]) for p in pairs]
    # the routing policy's own threshold, computed from each pair's baseline, is not a world figure
    rates += [DEGRADED_MULTIPLE * float(p["baseline"]) for p in pairs]
    rates += [float(p["baseline"]) + DEGRADED_POINTS for p in pairs]
    rates.append(DEGRADED_POINTS)
    for q in ex.of("percent"):
        if not any(abs(q.value - r) <= 0.005 for r in rates):
            wrong.append("share")
            break
    for q in ex.of("count"):
        if (q.unit == "attempt" and int(q.value) == DEGRADED_MIN_ATTEMPTS) or (
            q.unit == "failure" and int(q.value) == DEGRADED_MIN_FAILURES
        ):
            continue  # the policy's minimums, quoted
        if q.unit == "attempt":
            ok = any(int(q.value) == int(p["attempts_24h"]) for p in pairs)
        elif q.unit == "failure":
            ok = any(int(q.value) == int(p["failures_24h"]) for p in pairs)
        else:
            # the draft's own set, or any pair's created payouts on the corridor, or the corridor's
            # (a corridor hold may list or count them all; ruled 2026-10-15)
            others = set(facts.get("corridor_created_counts", {}).values())
            ok = int(q.value) == len(payouts) or int(q.value) in others
        if not ok:
            wrong.append(f"{q.unit}_count")
            break
    created = set(facts["correct"]["payouts"]) | set(facts.get("created_payouts", []))
    created |= set(facts.get("corridor_created", []))  # any pair's created payout is a real id
    if any(a not in created for a in ex.attempt_ids):
        wrong.append("payout_id")
    return wrong


def submitted_ids_in(note: str, facts: dict[str, Any]) -> list[str]:
    """The attempt ids the note lists that were already submitted at the case's time."""
    submitted = set(facts.get("submitted", []))
    return [a for a in extract(note).attempt_ids if a in submitted]


def _draft_names_corridor(draft: Mapping[str, Any] | None, corridor: str) -> bool:
    return draft is not None and draft.get("corridor") == corridor


def _draft_lists_payouts(draft: Mapping[str, Any] | None, payouts: list[str]) -> bool:
    if draft is None:
        return False
    listed = draft.get("payouts")
    return isinstance(listed, (list, tuple)) and set(payouts) <= set(listed)


def check_reroute_note(note: str, facts: dict[str, Any], draft: Mapping[str, Any] | None = None) -> NoteCheck:
    """A reroute draft names the corridor, the degraded provider and rail with its 24-hour
    share, the destination provider and rail, every payout id of the oracle's set, and, when
    the destination is a calendar rail, that settlement moves to the next cycle. ``draft`` is
    the action call's own arguments: its ``corridor`` names the corridor and its ``payouts``
    list the payouts as well as the prose does."""
    correct = facts["correct"]
    missing: list[str] = []
    if not _corridor_named(note, facts["corridor"]) and not _draft_names_corridor(draft, facts["corridor"]):
        missing.append("note_corridor")
    if not names_pair(note, facts["from_provider"], facts["from_rail"]):
        missing.append("note_degraded_pair")
    if not extract(note).of("percent"):
        missing.append("note_share")
    if not names_pair(note, str(correct["to_provider"]), str(correct["to_rail"])):
        missing.append("note_destination")
    listed = set(extract(note).attempt_ids)
    if not set(correct["payouts"]) <= listed and not _draft_lists_payouts(draft, list(correct["payouts"])):
        missing.append("note_payouts")
    if correct["settlement_moves_to_next_cycle"] and NEXT_CYCLE.search(note) is None:
        missing.append("note_next_cycle")
    wrong = _numbers_against_pairs(note, facts, list(correct["payouts"]))
    return NoteCheck(tuple(missing), tuple(wrong))


def check_hold_note(note: str, facts: dict[str, Any], draft: Mapping[str, Any] | None = None) -> NoteCheck:
    """A hold draft names the corridor (in its prose or its own ``corridor`` field); every number
    it states agrees with the corridor."""
    named = _corridor_named(note, facts["corridor"]) or _draft_names_corridor(draft, facts["corridor"])
    missing = [] if named else ["note_corridor"]
    wrong = _numbers_against_pairs(note, facts, list(facts["correct"]["payouts"]))
    return NoteCheck(tuple(missing), tuple(wrong))


def operator_time(t: int) -> str:
    """A time as the notes and the tools print it in operator time."""
    import datetime as dt

    return (dt.datetime.fromtimestamp(t - 4 * HOUR, dt.UTC)).strftime("%Y-%m-%d %H:%M") + " ET"


__all__ = [
    "CURRENCY",
    "HOLD_FIELDS",
    "REROUTE_FIELDS",
    "STUCK_FIELDS",
    "ZONE_WORDS",
    "Extracted",
    "NoteCheck",
    "Quantity",
    "check_hold_note",
    "check_reroute_note",
    "check_stuck_note",
    "extract",
    "from_iso_utc",
    "names",
    "names_pair",
    "operator_time",
    "submitted_ids_in",
]
