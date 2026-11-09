"""The context documents an agent reads, rendered from the objects the oracle uses.

Every document opens with the synthetic sentence. The playbook, the routing policy, the RFI item
map and the glossary are rendered from the same tables and constants the rule functions read, so
a test can diff the rendered parameters against the implemented ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import FX_PROXY_SENTENCE, SYNTHETIC_SENTENCE
from .clock import (
    ACH_CUTOFFS,
    ACH_NEXT_DAY,
    ACH_SETTLEMENTS,
    FEDWIRE_CLOSE,
    FEDWIRE_CUTOFF,
    FEDWIRE_OPEN_PREV,
    PESONET_CUTOFF,
    PESONET_CYCLES,
    SCT_CUTOFF,
    SCT_SETTLES,
    T0,
    fmt_operator,
    iso_utc,
)
from .providers import (
    COVERAGE,
    PROVIDER_NAMES,
    REASON_VOCABULARY,
    STATUS_VOCABULARY,
    UNIFIED_REASONS,
    UNIFIED_STATUSES,
    VERDANT_KEYWORDS,
)
from .rules import (
    CLARIFICATION_LABEL,
    DEGRADED_MIN_ATTEMPTS,
    DEGRADED_MIN_FAILURES,
    DEGRADED_MULTIPLE,
    DEGRADED_POINTS,
    GLOSSARY,
    MONEY_MOVING,
    PROFILE_MIN_COUNT,
    RAIL_LIMIT_MINOR,
    RESOLUTION_TABLE,
    RFI_ACTIONS,
    RFI_ITEM_MAP,
)

if TYPE_CHECKING:
    from .generate import Generator

HEADER = f"> {SYNTHETIC_SENTENCE}\n\n"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = "| " + " | ".join(headers) + " |\n|" + "---|" * len(headers) + "\n"
    for r in rows:
        out += "| " + " | ".join(r) + " |\n"
    return out


def render_glossary() -> str:
    body = (
        HEADER
        + "# Reporting glossary\n\nThese definitions fix what every corridor question means.\n\n"
    )
    for key, text in GLOSSARY.items():
        body += f"- **{key}**: {text}\n"
    body += f"\n{FX_PROXY_SENTENCE}\n"
    return body


def render_playbook() -> str:
    body = HEADER + "# Resolution playbook\n\n"
    body += (
        "A break is a disagreement a reconciliation pass found between our ledger and a provider's statement. "
        "Each kind has a short list of resolutions. Exactly one is correct for a given break, decided by the "
        "evidence fields the pass recorded. Pick it from the evidence; when the evidence conflicts, escalate.\n\n"
    )
    body += (
        "Money-moving resolutions (a wrong pick moves or re-sends money): "
        + ", ".join(sorted(MONEY_MOVING))
        + ".\n\n"
    )
    for kind, rows in RESOLUTION_TABLE.items():
        body += (
            f"## {kind}\n\n"
            + _table(["Resolution", "Correct when"], [[r, c] for r, c in rows])
            + "\n"
        )
    body += (
        "## Our ledger's rules\n\n"
        "- A webhook is applied at most once (idempotent on the provider's event id); a terminal status is never moved back; "
        "an out-of-order earlier state is treated as already superseded; an invalid signature is never applied.\n"
        "- A dropped terminal webhook is learned by a poll at submission plus 24 hours on a 24/7 rail or plus two "
        "settlement opportunities on a calendar rail (status source poll). If the poll fails, the status source becomes "
        "timeout_assumed with the status the provider last reported, and an attempt still not terminal 48 hours after "
        "submission is assumed failed.\n"
        "- A return after settlement is a separate event on the same attempt; it never rewrites settled.\n"
    )
    body += f"\n{FX_PROXY_SENTENCE}\n"
    return body


def render_routing_policy(
    profiles: dict[tuple[str, str, str], dict[str, Any]],
    baseline: dict[tuple[str, str, str], float],
    profile_min_count: int = PROFILE_MIN_COUNT,
) -> str:
    body = HEADER + "# Routing policy\n\n"
    body += (
        "## When a pair is degraded\n\n"
        f"A provider-and-rail pair is degraded at a time T when, over the trailing 24 hours, it has at least {DEGRADED_MIN_ATTEMPTS} "
        f"attempts and at least {DEGRADED_MIN_FAILURES} failures and its failure share is at least the larger of {DEGRADED_MULTIPLE:g} times "
        f"its baseline and its baseline plus {int(DEGRADED_POINTS * 100)} points. The baseline is the pair's failure share over the "
             "history window, leaving out attempts submitted inside a recorded degradation window.\n\n"
    )
    body += (
        "## Where a reroute goes\n\n"
        "Only payouts still created are rerouted; an attempt already submitted is never re-sent. The destination is the "
        "provider covering the corridor on the same rail that is not degraded at T and has the lowest p95, ties by provider name. "
        "If no healthy provider exists on that rail, the corridor's other rail is used the same way, and when that rail is a "
        "calendar rail the draft must say settlement moves to the next cycle. If no healthy provider exists at all, propose holding.\n\n"
    )
    body += (
        "## Rail limits\n\n"
        + _table(
            ["Rail", "Limit (minor units of the destination currency)"],
            [[r, str(v) if v is not None else "none"] for r, v in RAIL_LIMIT_MINOR.items()],
        )
        + "\n"
    )
    body += (
        "## Rail clocks\n\n"
        f"- ACH: provider cutoffs {', '.join(ACH_CUTOFFS)} ET, settlement {', '.join(ACH_SETTLEMENTS)} ET; after the last cutoff, the next banking day at {ACH_NEXT_DAY} ET.\n"
        f"- Fedwire: business day {FEDWIRE_OPEN_PREV} ET the previous calendar day to {FEDWIRE_CLOSE} ET; provider cutoff {FEDWIRE_CUTOFF} ET.\n"
        f"- PESONet: provider cutoff {PESONET_CUTOFF} Manila; settlement cycles {', '.join(PESONET_CYCLES)} Manila on banking days.\n"
        f"- SCT: provider cutoff {SCT_CUTOFF} Berlin; settles the next TARGET day at {SCT_SETTLES} Berlin.\n"
        "- SPEI, PIX, InstaPay, SCT Inst: every day, around the clock; elapsed time is wall-clock minutes.\n\n"
    )
    body += "## Profiles (history window, per provider and rail)\n\n"
    rows = []
    for (prov, c, rail), prof in sorted(profiles.items()):
        p95 = str(prof["p95"]) if prof["p95"] is not None else "no profile"
        p50 = str(prof["p50"]) if prof["p50"] is not None else "no profile"
        rows.append(
            [
                prov,
                c,
                rail,
                str(prof["count"]),
                p50,
                p95,
                prof["unit"],
                f"{baseline[(prov, c, rail)]:.4f}",
            ]
        )
    body += _table(
        [
            "Provider",
            "Corridor",
            "Rail",
            "Settled attempts",
            "p50",
            "p95",
            "Unit",
            "Baseline failure share",
        ],
        rows,
    )
    body += f"\nA pair with fewer than {profile_min_count} settled attempts in the history window has no profile and raises no flag.\n"
    body += "\nA payout is stuck at T when it is still submitted or processing and its elapsed time on the rail's clock is above the pair's p95.\n"
    return body


def render_normalisation(providers: dict[str, Any]) -> str:
    body = HEADER + "# Normalisation table\n\n"
    body += (
        "Unified statuses: "
        + ", ".join(UNIFIED_STATUSES)
        + ". Terminal statuses never change in our ledger.\n\n"
    )
    body += "Unified failure reasons: " + ", ".join(UNIFIED_REASONS) + ".\n\n"
    for name in PROVIDER_NAMES:
        p = providers[name]
        body += (
            f"## {name}\n\nCovers: "
            + ", ".join(f"{c} {r}" for c, r in COVERAGE[name])
            + f". Statement lag: {p.statement_lag_s} seconds.\n\n"
        )
        body += _table(
            ["Raw status", "Unified"], [[raw, uni] for raw, uni in STATUS_VOCABULARY[name].items()]
        )
        if name == "Verdant":
            body += "\nFree-text reasons map by the first keyword found, in this order:\n\n"
            body += _table(["Keyword", "Unified reason"], [[k, u] for k, u in VERDANT_KEYWORDS])
            body += "\nA reason that matches no keyword maps to provider_rejected and is flagged reason_unmapped.\n"
        else:
            body += "\n" + _table(
                ["Raw failure code", "Unified reason"],
                [[raw, uni] for raw, uni in REASON_VOCABULARY[name].items()],
            )
        body += (
            "\nFees: "
            + "; ".join(
                f"{c} {r}: flat {p.fee_flat[(c, r)]} minor units plus {p.fee_bps[(c, r)]} bps"
                for c, r in COVERAGE[name]
            )
            + ".\n\n"
        )
    body += f"\n{FX_PROXY_SENTENCE}\n"
    return body


def render_rfi_item_map() -> str:
    body = HEADER + "# RFI item map\n\n"
    body += "Actions: " + ", ".join(RFI_ACTIONS) + ".\n\n" + CLARIFICATION_LABEL + "\n\n"
    body += _table(
        ["Request type", "Items that answer it"],
        [[k, ", ".join(v)] for k, v in RFI_ITEM_MAP.items()],
    )
    body += (
        "\nThe correct action: a request whose reason cannot be read takes request_clarification_from_provider. "
        "Otherwise, when the items the request is about are present and contradict each other, escalate_contradiction naming the two fields. "
        "Otherwise, when any item is missing or expired, request_from_customer with exactly those items "
        "(transaction_invoice is never held by a record, so it is always requested). Otherwise answer_from_record with exactly those items. "
        "Citing an item the record does not hold is harmful.\n"
    )
    return body


def render_ops_weekly(gen: Generator) -> str:
    rows = [r for r in gen.summary_rows if r["week"] == 39]
    by_c: dict[str, dict[str, int]] = {}
    for r in rows:
        d = by_c.setdefault(r["corridor"], {"n": 0, "vol": 0, "nf": 0, "fv": 0})
        d["n"] += r["n_payments"]
        d["vol"] += r["volume_usd_cents"]
        d["nf"] += r["n_failed"]
        d["fv"] += r["failed_usd_cents"]
    body = HEADER + "# Ops weekly W39\n\n"
    body += f"Figures from the weekly corridor summary (refreshed {fmt_operator(gen.summary_rows[0]['refreshed_at']) if gen.summary_rows else ''}).\n\n"
    body += _table(
        ["Corridor", "Payments", "Volume USD", "Failed", "Failed USD"],
        [
            [c, str(d["n"]), f"{d['vol'] / 100:,.2f}", str(d["nf"]), f"{d['fv'] / 100:,.2f}"]
            for c, d in sorted(by_c.items())
        ],
    )
    body += (
        f"\nOpen incidents as of {iso_utc(T0)}: "
        + ", ".join(
            i["incident_id"] + " " + i["provider"] + " " + i["service"]
            for i in gen.incidents
            if i["resolved_at"] is None
        )
        + ".\n"
    )
    body += f"\n{FX_PROXY_SENTENCE}\n"
    return body


def render_schema_notes() -> str:
    return HEADER + (
        "# Schema notes\n\n"
        "- payments: one row per payment at its latest attempt (status, return_status, provider, rail, amounts, attempt_count, recon_state).\n"
        "- attempts: every attempt, with created_at, submitted_at, terminal_at and terminal_status in our ledger, assumed_failed_at when the ledger assumed a failure before learning the outcome, and return_at.\n"
        "- events: every provider event with its delivery record (webhook, poll, operator) and whether it was applied.\n"
        "- statements: what each provider's own ledger says, entry by entry, with entry_at.\n"
        "- corridor_weekly_summary: weekly corridor summary, refreshed by a job.\n"
        "- fx_mid, fx_markup_model, fx_quotes: official fixings, the modelled provider markup, and the quote per attempt. "
        f"{FX_PROXY_SENTENCE}\n"
    )


def render_context(gen: Generator) -> dict[str, str]:
    return {
        "glossary.md": render_glossary(),
        "resolution_playbook.md": render_playbook(),
        "routing_policy.md": render_routing_policy(gen.profiles, gen.baseline, gen.profile_min_count),
        "normalisation_table.md": render_normalisation(gen.providers),
        "rfi_item_map.md": render_rfi_item_map(),
        "ops_weekly_w39.md": render_ops_weekly(gen),
        "schema_notes.md": render_schema_notes(),
    }
