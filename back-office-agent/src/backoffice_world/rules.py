"""The rule functions, each implemented once.

The oracle, the rendered context documents (playbook, routing policy, RFI item map, glossary)
and the tests all read these objects and call these functions. Nothing else decides a correct
answer. Every function is pure: it reads its arguments and writes nothing.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .clock import (
    CLOCK_RAILS,
    DAY,
    HOUR,
    OPERATOR_OFFSET,
    Calendar,
    elapsed_on_clock,
    local_date,
)
from .providers import TERMINAL

# ---------------------------------------------------------------------------------------------
# Breaks: kinds, resolutions, the evidence each reads, and the one correct resolution.
# ---------------------------------------------------------------------------------------------

MONEY_MOVING = frozenset(
    {"adopt_and_resubmit", "request_reversal_of_duplicate", "create_ledger_row"}
)

# kind -> ordered list of (resolution, plain-words condition). The playbook renders this table;
# `resolution_conditions` evaluates it. Exactly one condition holds for every break the world
# emits; the test pins that.
RESOLUTION_TABLE: dict[str, tuple[tuple[str, str], ...]] = {
    "status_mismatch": (
        (
            "adopt_provider_status",
            "our status was assumed at the poll deadline (status source timeout_assumed) and the statement entry carries a provider reference or a failure code",
        ),
        ("requery_provider", "the statement entry is older than our last applied event and carries neither a provider reference nor a failure code"),
        (
            "escalate",
            "both sides carry a final reference (ours from a webhook or a poll, theirs on the statement) and they disagree",
        ),
    ),
    "ours_behind": (
        ("adopt_provider_status", "the statement entry carries a provider reference"),
        ("requery_provider", "the statement entry carries no provider reference"),
    ),
    "amount_mismatch": (
        (
            "book_fee_difference",
            "the difference equals the provider's fee for that rail and the fee is not yet booked",
        ),
        (
            "book_fx_difference",
            "the fee is already booked, the difference is within that day's FX tolerance, and the difference does not equal the fee",
        ),
        (
            "escalate",
            "otherwise, including a difference that equals the fee when the fee is already booked",
        ),
    ),
    "missing_at_provider": (
        (
            "wait_for_statement",
            "our submission time plus the provider's statement lag is later than the pass time",
        ),
        ("requery_provider", "the lag has passed and our attempt is not terminal"),
        ("escalate", "the lag has passed and our attempt is settled with a provider reference"),
    ),
    "missing_in_ledger": (
        (
            "create_ledger_row",
            "the statement's client reference matches an attempt of ours still created, with no provider reference",
        ),
        ("escalate", "no attempt of ours matches"),
    ),
    "duplicate_at_provider": (
        (
            "request_reversal_of_duplicate",
            "both entries are settled, positive, with different references",
        ),
        (
            "mark_false_duplicate",
            "the second entry is a negative correction that nets the first to zero",
        ),
    ),
    "late_return": (
        ("book_return", "a return reference is present and the returned amount equals ours"),
        ("escalate", "the returned amount differs from ours"),
    ),
    "late_cancel": (
        ("adopt_and_resubmit", "no settlement reference was ever issued"),
        ("escalate", "a settlement reference exists; where the money is must be asked"),
    ),
}
BREAK_KINDS: tuple[str, ...] = tuple(RESOLUTION_TABLE)


def resolutions_for(kind: str) -> tuple[str, ...]:
    return tuple(r for r, _ in RESOLUTION_TABLE[kind])


def resolution_conditions(kind: str, ev: dict[str, Any]) -> dict[str, bool]:
    """Which resolutions' conditions hold against the evidence. Exactly one should."""
    if kind == "status_mismatch":
        theirs_final = bool(ev.get("theirs_reference")) or bool(ev.get("theirs_failure_code"))
        ours_final = ev.get("ours_status_source") != "timeout_assumed" and (
            bool(ev.get("ours_reference")) or bool(ev.get("ours_failure_code"))
        )
        return {
            "adopt_provider_status": ev.get("ours_status_source") == "timeout_assumed"
            and theirs_final,
            "requery_provider": int(ev["theirs_entry_at"]) < int(ev["ours_last_event_at"]) and not theirs_final,
            "escalate": ours_final and theirs_final,
        }
    if kind == "ours_behind":
        has_ref = bool(ev.get("theirs_reference"))
        return {"adopt_provider_status": has_ref, "requery_provider": not has_ref}
    if kind == "amount_mismatch":
        diff = abs(int(ev["ours_amount"]) - int(ev["theirs_amount"]))
        fee = int(ev["fee_for_rail"])
        booked = bool(ev["fee_booked"])
        tol = int(ev["fx_tolerance"])
        fee_c = diff == fee and not booked
        fx_c = booked and diff <= tol and diff != fee
        return {
            "book_fee_difference": fee_c,
            "book_fx_difference": fx_c,
            "escalate": not fee_c and not fx_c,
        }
    if kind == "missing_at_provider":
        wait = int(ev["submitted_at"]) + int(ev["provider_lag_s"]) > int(ev["as_of"])
        status = ev["ours_status"]
        return {
            "wait_for_statement": wait,
            "requery_provider": (not wait) and status in ("submitted", "processing"),
            "escalate": (not wait) and status == "settled" and bool(ev.get("ours_reference")),
        }
    if kind == "missing_in_ledger":
        match = bool(ev.get("matching_attempt"))
        return {"create_ledger_row": match, "escalate": not match}
    if kind == "duplicate_at_provider":
        first, second = ev["entries"][0], ev["entries"][1]
        reversal = (
            first["status"] == "settled"
            and second["status"] == "settled"
            and int(first["amount"]) > 0
            and int(second["amount"]) > 0
            and first["reference"] != second["reference"]
        )
        false_dup = int(second["amount"]) == -int(first["amount"]) and int(first["amount"]) > 0
        return {"request_reversal_of_duplicate": reversal, "mark_false_duplicate": false_dup}
    if kind == "late_return":
        equal = int(ev["theirs_return_amount"]) == int(ev["ours_amount"])
        return {"book_return": bool(ev.get("return_reference")) and equal, "escalate": not equal}
    if kind == "late_cancel":
        has_settlement = bool(ev.get("settlement_reference"))
        return {"adopt_and_resubmit": not has_settlement, "escalate": has_settlement}
    raise KeyError(kind)


def correct_resolution(kind: str, ev: dict[str, Any]) -> str:
    """The one resolution whose condition holds. Raises if none or several do."""
    conds = resolution_conditions(kind, ev)
    hits = [r for r, ok in conds.items() if ok]
    if len(hits) != 1:
        raise ValueError(f"{kind}: {len(hits)} resolutions satisfied ({hits}) by {ev}")
    return hits[0]


# ---------------------------------------------------------------------------------------------
# RFIs: the item map and the correct action.
# ---------------------------------------------------------------------------------------------

RFI_REQUEST_TYPES: tuple[str, ...] = (
    "source_of_funds",
    "employment_vs_source_of_funds",
    "proof_of_address",
    "id_document_expired",
    "beneficiary_name_mismatch",
    "purpose_of_payment",
    "date_of_birth_mismatch",
    "tax_id_missing",
    "transaction_invoice",
)
# request type -> the identity-record items that answer it. `transaction_invoice` names an item
# no onboarding record holds, so it is always requested from the customer.
RFI_ITEM_MAP: dict[str, tuple[str, ...]] = {
    "source_of_funds": ("item_source_of_funds",),
    "employment_vs_source_of_funds": ("item_employment", "item_source_of_funds"),
    "proof_of_address": ("item_address", "doc_proof_of_address"),
    "id_document_expired": ("doc_identity",),
    "beneficiary_name_mismatch": ("item_name", "doc_identity"),
    "purpose_of_payment": ("item_purpose",),
    "date_of_birth_mismatch": ("item_date_of_birth", "doc_identity"),
    "tax_id_missing": ("item_tax_id",),
    "transaction_invoice": ("transaction_invoice",),
}
RECORD_ITEMS: tuple[str, ...] = (
    "item_name",
    "item_date_of_birth",
    "item_address",
    "item_employment",
    "item_source_of_funds",
    "item_purpose",
    "item_tax_id",
    "doc_identity",
    "doc_proof_of_address",
)
RFI_ACTIONS: tuple[str, ...] = (
    "answer_from_record",
    "request_from_customer",
    "escalate_contradiction",
    "request_clarification_from_provider",
)
CLARIFICATION_LABEL = (
    "request_clarification_from_provider is our addition for requests whose reason cannot be "
    "read."
)
RFI_STATES: tuple[str, ...] = (
    "received",
    "in_review",
    "answered",
    "customer_requested",
    "escalated",
    "clarification_requested",
    "closed",
)


@dataclass(frozen=True)
class RfiAction:
    action: str
    items: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "items": list(self.items), "fields": list(self.fields)}


def correct_rfi_action(case: dict[str, Any], record: dict[str, Any], as_of: int) -> RfiAction:
    """The correct action for an RFI case against the end user's identity record at `as_of`.

    `case`: readable (bool), request_type (str or None).
    `record`: items: {item_id: {present: bool, expires: 'YYYY-MM-DD' or None}},
              contradictions: [[item_a, item_b], ...] (pairs of present items that contradict).
    """
    if not case.get("readable"):
        return RfiAction("request_clarification_from_provider")
    items = RFI_ITEM_MAP[case["request_type"]]
    rec_items = record.get("items", {})
    for pair in record.get("contradictions", []):
        a, b = pair[0], pair[1]
        if (
            a in items
            and b in items
            and rec_items.get(a, {}).get("present")
            and rec_items.get(b, {}).get("present")
        ):
            return RfiAction("escalate_contradiction", fields=(a, b))
    today = local_date(as_of, OPERATOR_OFFSET)
    missing: list[str] = []
    for item in items:
        info = rec_items.get(item)
        if not info or not info.get("present"):
            missing.append(item)
            continue
        exp = info.get("expires")
        if exp and dt.date.fromisoformat(exp) < today:
            missing.append(item)
    if missing:
        return RfiAction("request_from_customer", items=tuple(missing))
    return RfiAction("answer_from_record", items=tuple(items))


# ---------------------------------------------------------------------------------------------
# Profiles, the stuck clock, degradation and routing.
# ---------------------------------------------------------------------------------------------

PROFILE_MIN_COUNT = 200
DEGRADED_MIN_ATTEMPTS = 20
DEGRADED_MIN_FAILURES = 10
DEGRADED_MULTIPLE = 3.0
DEGRADED_POINTS = 0.10
TRAILING_WINDOW_S = 24 * HOUR
RAIL_LIMIT_MINOR: dict[str, int | None] = {
    "SPEI": None,
    "PIX": None,
    "InstaPay": 50_000 * 100,
    "PESONet": None,
    "SCT": None,
    "SCT Inst": 100_000 * 100,
    "ACH": 1_000_000 * 100,
    "Fedwire": None,
}


def percentile(values: list[int], q: float) -> int:
    """Nearest-rank percentile of integer values (q in (0, 1])."""
    if not values:
        raise ValueError("no values")
    s = sorted(values)
    k = max(1, int(-(-q * len(s) // 1)))  # ceil
    return s[k - 1]


def evaluate_stuck(
    status: str, rail: str, submitted_at: int | None, at: int, p95: int | None, calendar: Calendar
) -> bool | None:
    """Stuck at `at`: in flight and past the pair's p95 on the rail's clock. None when the pair
    has no profile (no flag is raised)."""
    if p95 is None:
        return None
    if status not in ("submitted", "processing") or submitted_at is None:
        return False
    return elapsed_on_clock(rail, submitted_at, at, calendar) > p95


def evaluate_degraded(attempts_24h: int, failures_24h: int, baseline: float) -> bool:
    if attempts_24h < DEGRADED_MIN_ATTEMPTS or failures_24h < DEGRADED_MIN_FAILURES:
        return False
    share = failures_24h / attempts_24h
    return share + 1e-9 >= max(DEGRADED_MULTIPLE * baseline, baseline + DEGRADED_POINTS)


@dataclass(frozen=True)
class PairState:
    provider: str
    rail: str
    degraded: bool
    p95: int | None


def route(
    corridor: str, amount_minor: int, pairs: list[PairState], exclude: tuple[str, str] | None = None
) -> tuple[str, str] | None:
    """The destination of a reroute: the provider covering the corridor on a rail the amount is
    allowed on, not degraded, with the lowest p95 on that rail; ties on provider name. None when
    no healthy provider exists."""
    best: tuple[int, str, str] | None = None
    for p in pairs:
        if exclude is not None and (p.provider, p.rail) == exclude:
            continue
        if p.degraded:
            continue
        limit = RAIL_LIMIT_MINOR.get(p.rail)
        if limit is not None and amount_minor > limit:
            continue
        p95 = p.p95 if p.p95 is not None else 10**9
        key = (p95, p.provider, p.rail)
        if best is None or key < best:
            best = key
    if best is None:
        return None
    return best[1], best[2]


# ---------------------------------------------------------------------------------------------
# The glossary, and its answers by SQL over the emitted tables (the oracle's second path).
# ---------------------------------------------------------------------------------------------

GLOSSARY: dict[str, str] = {
    "week": "A week is an operator-time ISO week (America/New_York). A payment belongs to a window by its created_at.",
    "how_much": '"How much" is amount_usd summed over payments at their latest attempt, never over attempts.',
    "failed": '"Failed" is a latest attempt that is failed or cancelled as of the as-of time (cancelled_by_operator counts as failed).',
    "returned": '"Returned" is a return after settlement. It is its own line, never counted under failed.',
    "in_flight": '"In flight" is a latest attempt that is not terminal (created, submitted or processing). It is its own line.',
    "success_rate": '"Success rate" is settled over settled plus failed plus cancelled; in flight is excluded.',
    "corridor": "A corridor is a destination country; provider and rail breakdowns are by the latest attempt.",
    "unmapped": "A failure reason flagged reason_unmapped is reported as provider_rejected (unmapped), never dropped.",
    "status_counts_window": 'Status counts "as of now" are over payments created in the 30 days up to and including the as-of time.',
    "failure_share": "A failure share is failed over settled plus failed plus cancelled, among payments whose latest attempt is terminal; the highest share wins, ties by name.",
}
QUESTION_FAMILIES: tuple[str, ...] = ("corridor_volume_failed", "failure_share_by", "status_counts")
STATUS_COUNTS_LOOKBACK_S = 30 * DAY

# ISO week of created_at in operator time, computed in SQL from the Monday that starts ISO week
# 1 of 2026 (2025-12-29), independently of Python's calendar.
_SQL_WEEK = "(CAST((julianday(datetime(p.created_at, '-4 hours')) - julianday('2025-12-29')) / 7 AS INTEGER) + 1)"

_SQL_LATEST = """
WITH latest AS (
    SELECT a.*
    FROM attempts a
    JOIN (
        SELECT payment_id, MAX(n) AS n FROM attempts WHERE created_at <= :as_of GROUP BY payment_id
    ) m ON m.payment_id = a.payment_id AND m.n = a.n
),
rows AS (
    SELECT p.payment_id, p.corridor, p.amount_usd_cents, p.created_at,
           l.provider, l.rail, l.attempt_id,
           CASE
             WHEN l.terminal_at IS NOT NULL AND l.terminal_at <= :as_of THEN l.terminal_status
             WHEN l.assumed_failed_at IS NOT NULL AND l.assumed_failed_at <= :as_of THEN 'failed'
             ELSE 'in_flight'
           END AS status,
           CASE WHEN l.return_at IS NOT NULL AND l.return_at <= :as_of THEN 1 ELSE 0 END AS returned
    FROM payments p JOIN latest l ON l.payment_id = p.payment_id
    WHERE p.created_at <= :as_of
)
"""


def _window_clause(window: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if window["kind"] == "week":
        return f"{_SQL_WEEK} = :week", {"week": int(window["week"])}
    if window["kind"] == "range":
        return "p.created_at >= :ws AND p.created_at < :we", {
            "ws": window["start"],
            "we": window["end"],
        }
    raise KeyError(window["kind"])


def glossary_answer(
    conn: sqlite3.Connection, family: str, params: dict[str, Any]
) -> dict[str, Any]:
    """The correct answer to a question instance, by SQL over the emitted tables under the glossary.

    params: country, as_of (ISO UTC string), window ({kind: week, week} or {kind: range, start, end}),
    by ('provider' or 'rail') for failure_share_by.
    """
    as_of = params["as_of"]
    country = params["country"]
    if family == "corridor_volume_failed":
        clause, extra = _window_clause(params["window"])
        sql = (
            _SQL_LATEST.replace("FROM payments p JOIN latest l", "FROM payments p JOIN latest l")
            + f"""
        SELECT COALESCE(SUM(amount_usd_cents), 0),
               COUNT(*),
               COALESCE(SUM(CASE WHEN status IN ('failed', 'cancelled') THEN amount_usd_cents ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN status IN ('failed', 'cancelled') THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN status = 'settled' AND returned = 1 THEN amount_usd_cents ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN status = 'in_flight' THEN amount_usd_cents ELSE 0 END), 0)
        FROM rows p WHERE p.corridor = :country AND {clause}
        """
        )
        row = conn.execute(sql, {"as_of": as_of, "country": country, **extra}).fetchone()
        return {
            "volume_usd": row[0] / 100,
            "n_payments": row[1],
            "failed_usd": row[2] / 100,
            "n_failed": row[3],
            "returned_usd": row[4] / 100,
            "in_flight_usd": row[5] / 100,
        }
    if family == "failure_share_by":
        clause, extra = _window_clause(params["window"])
        col = "provider" if params["by"] == "provider" else "rail"
        sql = (
            _SQL_LATEST
            + f"""
        SELECT {col},
               SUM(CASE WHEN status IN ('failed', 'cancelled') THEN 1 ELSE 0 END) AS n_failed,
               SUM(CASE WHEN status IN ('settled', 'failed', 'cancelled') THEN 1 ELSE 0 END) AS n_terminal
        FROM rows p WHERE p.corridor = :country AND {clause}
        GROUP BY {col} HAVING n_terminal > 0
        """
        )
        rows = conn.execute(sql, {"as_of": as_of, "country": country, **extra}).fetchall()
        best = None
        for name, n_failed, n_terminal in rows:
            share = round(n_failed / n_terminal, 4)
            key = (-share, name)
            if best is None or key < best[0]:
                best = (key, name, share, n_failed, n_terminal)
        if best is None:
            return {"by": None, "share": None, "n_failed": 0, "n_terminal": 0}
        return {"by": best[1], "share": best[2], "n_failed": best[3], "n_terminal": best[4]}
    if family == "status_counts":
        sql = (
            _SQL_LATEST
            + """
        SELECT
          SUM(CASE WHEN status = 'in_flight' THEN 1 ELSE 0 END),
          COALESCE(SUM(CASE WHEN status = 'in_flight' THEN amount_usd_cents ELSE 0 END), 0),
          SUM(CASE WHEN status = 'settled' AND returned = 1 THEN 1 ELSE 0 END),
          COALESCE(SUM(CASE WHEN status = 'settled' AND returned = 1 THEN amount_usd_cents ELSE 0 END), 0),
          SUM(CASE WHEN status IN ('failed', 'cancelled') THEN 1 ELSE 0 END),
          COALESCE(SUM(CASE WHEN status IN ('failed', 'cancelled') THEN amount_usd_cents ELSE 0 END), 0)
        FROM rows p WHERE p.corridor = :country AND p.created_at >= :ws AND p.created_at <= :as_of
        """
        )
        row = conn.execute(
            sql, {"as_of": as_of, "country": country, "ws": params["window"]["start"]}
        ).fetchone()
        return {
            "n_in_flight": row[0] or 0,
            "in_flight_usd": row[1] / 100,
            "n_returned": row[2] or 0,
            "returned_usd": row[3] / 100,
            "n_failed": row[4] or 0,
            "failed_usd": row[5] / 100,
        }
    raise KeyError(family)


__all__ = [
    "BREAK_KINDS",
    "CLARIFICATION_LABEL",
    "GLOSSARY",
    "MONEY_MOVING",
    "PROFILE_MIN_COUNT",
    "QUESTION_FAMILIES",
    "RECORD_ITEMS",
    "RESOLUTION_TABLE",
    "RFI_ACTIONS",
    "RFI_ITEM_MAP",
    "RFI_REQUEST_TYPES",
    "RFI_STATES",
    "RAIL_LIMIT_MINOR",
    "PairState",
    "RfiAction",
    "correct_resolution",
    "correct_rfi_action",
    "evaluate_degraded",
    "evaluate_stuck",
    "glossary_answer",
    "percentile",
    "resolution_conditions",
    "resolutions_for",
    "route",
    "CLOCK_RAILS",
    "TERMINAL",
    "field",
]
