"""The agent's tools over one case: six read-only tools and the action tools.

Every read tool returns the world's data as of the case's own time and writes nothing (the world
is opened read-only). Exactly one action tool call ends the episode: the first; any call after it
is refused with a structured result naming that first action. No tool writes the world, moves
money or holds a key.

``ToolBox(world, case_id).call(name, args)`` returns a ``ToolResult``: the JSON-able content the
model reads, whether it is an error, and whether the call was the episode's action.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backoffice_world.clock import (
    CALENDAR_RAILS,
    ZONE_NAME,
    ZONE_OFFSET,
    clock_unit,
    elapsed_on_clock,
    from_iso_utc,
    local_date,
    next_cutoff,
)
from backoffice_world.rules import (
    CLARIFICATION_LABEL,
    MONEY_MOVING,
    RFI_ACTIONS,
    evaluate_degraded,
    resolutions_for,
)
from backoffice_world.sources import load_sources
from backoffice_world.world import World

from .facts import CURRENCY
from .scorer import Pick

READ_TOOLS: tuple[str, ...] = (
    "read_context",
    "read_case",
    "read_identity_record",
    "read_break",
    "read_payment",
    "read_rail_health",
)
ACTION_TOOLS: tuple[str, ...] = (
    "answer_rfi",
    "resolve_break",
    "flag_late_payout",
    "draft_reroute",
    "draft_hold",
)
TOOLS: tuple[str, ...] = READ_TOOLS + ACTION_TOOLS
#: The three standard RFI actions; the fourth, for a reason that cannot be read, is ours.
STANDARD_RFI_ACTIONS: tuple[str, ...] = (
    "answer_from_record",
    "request_from_customer",
    "escalate_contradiction",
)
OUR_ADDITION = "request_clarification_from_provider"
#: Tool calls an episode may make; at the limit with no action the episode is ``no_action``.
TOOL_CALL_LIMIT = 12


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _strings(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


#: What the model is offered, in this order, on every call.
TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "read_context",
        "description": "Read one of the back office's context documents: glossary, normalisation_table, resolution_playbook, routing_policy, rfi_item_map, ops_weekly_w39 or schema_notes.",
        "input_schema": _schema({"name": _string("the document's name")}, ["name"]),
    },
    {
        "name": "read_case",
        "description": "Read a provider's compliance request (RFI) case: provider, request type or the reason as sent, the end user, its age and state, follow-ups.",
        "input_schema": _schema({"case_id": _string("the RFI case id")}, ["case_id"]),
    },
    {
        "name": "read_identity_record",
        "description": "Read an end user's identity record: each item with its value where it has one, whether it is present, and its expiry.",
        "input_schema": _schema({"user_id": _string("the end user id")}, ["user_id"]),
    },
    {
        "name": "read_break",
        "description": "Read a reconciliation break: its kind, the evidence the pass recorded (our row and the statement entry), prior versions, the passes that saw it, and the resolutions its kind allows.",
        "input_schema": _schema({"break_id": _string("the break id")}, ["break_id"]),
    },
    {
        "name": "read_payment",
        "description": "Read a payout attempt: provider, rail, amounts, status, submitted time in operator and local time, elapsed time on the rail's clock, the pair's settlement profile, the calendar facts that apply, and whether the pair is degraded.",
        "input_schema": _schema({"attempt_id": _string("the attempt id")}, ["attempt_id"]),
    },
    {
        "name": "read_rail_health",
        "description": "Read the health of every provider and rail pair on a corridor over the trailing 24 hours (attempts, failures, share, baseline, degraded, open incident, p95), each with its payouts still created.",
        "input_schema": _schema({"corridor": _string("the destination country code, such as MX")}, ["corridor"]),
    },
    {
        "name": "answer_rfi",
        "description": "Answer an RFI case with one action. answer_from_record, request_from_customer and escalate_contradiction answer, request or escalate; request_clarification_from_provider is for requests whose reason cannot be read. List item ids only in items and fields; free text goes in note. This ends the episode.",
        "input_schema": _schema(
            {
                "case_id": _string("the RFI case id"),
                "action": {"type": "string", "enum": list(RFI_ACTIONS)},
                "items": _strings("item ids: the items that answer, or the items requested"),
                "fields": _strings("for escalate_contradiction, the two item ids that contradict"),
                "note": _string("free text for the provider"),
            },
            ["case_id", "action", "items", "fields", "note"],
        ),
    },
    {
        "name": "resolve_break",
        "description": "Pick the resolution for a break from its kind's list. This ends the episode.",
        "input_schema": _schema(
            {
                "break_id": _string("the break id"),
                "resolution": _string("one resolution from the break kind's list"),
                "note": _string("why"),
            },
            ["break_id", "resolution", "note"],
        ),
    },
    {
        "name": "flag_late_payout",
        "description": "Say whether a payout is late against its own pair's baseline. With stuck true, note is the note to the provider. This ends the episode.",
        "input_schema": _schema(
            {
                "attempt_id": _string("the attempt id"),
                "stuck": {"type": "boolean"},
                "note": _string("the note to the provider"),
            },
            ["attempt_id", "stuck", "note"],
        ),
    },
    {
        "name": "draft_reroute",
        "description": "Draft a reroute for a person to approve: from the degraded pair to a healthy one, listing the payouts still created. Nothing is sent. This ends the episode.",
        "input_schema": _schema(
            {
                "corridor": _string("the destination country code"),
                "from_provider": _string("the degraded provider"),
                "from_rail": _string("the degraded rail"),
                "to_provider": _string("the destination provider"),
                "to_rail": _string("the destination rail"),
                "payouts": _strings("the attempt ids to reroute"),
                "note": _string("the draft for the approver"),
            },
            ["corridor", "from_provider", "from_rail", "to_provider", "to_rail", "payouts", "note"],
        ),
    },
    {
        "name": "draft_hold",
        "description": "Draft a hold for a person to approve when no healthy provider exists on the corridor. Nothing is sent. This ends the episode.",
        "input_schema": _schema(
            {"corridor": _string("the destination country code"), "note": _string("the draft for the approver")},
            ["corridor", "note"],
        ),
    },
)


@dataclass(frozen=True)
class ToolResult:
    content: dict[str, Any]
    is_error: bool = False
    action: bool = False

    def text(self) -> str:
        return json.dumps(self.content, sort_keys=True, ensure_ascii=False)


def operator_time(t: int | None, seconds: bool = False) -> str | None:
    """A time in operator time (America/New_York, EDT throughout the window)."""
    if t is None:
        return None
    fmt = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    return dt.datetime.fromtimestamp(t + ZONE_OFFSET["US"], dt.UTC).strftime(fmt) + " ET"


def local_time(t: int, corridor: str) -> str:
    stamp = dt.datetime.fromtimestamp(t + ZONE_OFFSET[corridor], dt.UTC).strftime("%Y-%m-%d %H:%M")
    return f"{stamp} {ZONE_NAME[corridor]}"


def _render_times(doc: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in doc.items():
        if (k.endswith("_at") or k == "as_of") and isinstance(v, int) and not isinstance(v, bool):
            out[k] = operator_time(v, seconds=True)
        else:
            out[k] = v
    return out


class ToolBox:
    """The tools of one episode, bound to its case and the case's as-of time."""

    def __init__(self, world: World, case_id: str) -> None:
        self.world = world
        self.case_id = case_id
        self.case = world.oracle.raw["cases"][case_id]
        self.as_of = int(self.case["as_of"])
        self.pick: Pick | None = None
        self.calls = 0
        self._calendars = load_sources().calendars

    # ----- plumbing -----

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur = self.world.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def call(self, name: str, args: Mapping[str, Any] | None) -> ToolResult:
        """Run one tool call; the first action call is the episode's action."""
        self.calls += 1
        args = dict(args or {})
        if self.pick is not None:
            return ToolResult({"refused": True, "first_action": self.pick.tool}, is_error=True)
        if name in ACTION_TOOLS:
            self.pick = Pick(name, args)
            return ToolResult({"accepted": True, "action": name}, action=True)
        handler = getattr(self, name, None) if name in READ_TOOLS else None
        if handler is None:
            return ToolResult({"error": "unknown_tool", "tools": list(TOOLS)}, is_error=True)
        try:
            content = handler(**args)
        except TypeError:
            return ToolResult({"error": "bad_arguments", "tool": name}, is_error=True)
        if content is None:
            return ToolResult({"error": "not_found", "tool": name}, is_error=True)
        return ToolResult(content)

    # ----- the read tools -----

    def read_context(self, name: str) -> dict[str, Any] | None:
        path = self.world.context_dir / f"{name}.md"
        names = sorted(p.stem for p in self.world.context_dir.glob("*.md"))
        if name not in names:
            return {"error": "no_such_document", "documents": names}
        return {"name": name, "text": path.read_text(encoding="utf-8")}

    def read_case(self, case_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM rfis WHERE rfi_id = ?", (case_id,))
        if not rows or from_iso_utc(rows[0]["received_at"]) > self.as_of:
            return None
        r = rows[0]
        received = from_iso_utc(r["received_at"])
        return {
            "case_id": r["rfi_id"],
            "provider": r["provider"],
            "attempt_id": r["attempt_id"],
            "end_user": r["end_user"],
            "platform": r["platform"],
            "request_type": r["request_type"],
            "reason_as_sent": r["raw_reason"],
            "received_at": operator_time(received),
            "age_hours": round((self.as_of - received) / 3600, 1),
            "state": "in_review",
            "follow_ups": int(r["follow_ups"]),
        }

    def read_identity_record(self, user_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT user_id, platform, name, items FROM identity_records WHERE user_id = ?", (user_id,))
        if not rows:
            return None
        r = rows[0]
        return {"user_id": r["user_id"], "platform": r["platform"], "name": r["name"], "items": json.loads(r["items"])}

    def read_break(self, break_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM breaks WHERE break_id = ?", (break_id,))
        if not rows or from_iso_utc(rows[0]["found_at"]) > self.as_of:
            return None
        r = rows[0]
        passes = {
            p["pass_id"]: from_iso_utc(p["run_at"])
            for p in self._rows("SELECT pass_id, run_at FROM passes")
        }
        seen = [p for p in json.loads(r["seen_in_passes"]) if passes.get(p, 0) <= self.as_of]
        evidence = json.loads(r["evidence"])
        prior = [
            {"break_id": b["break_id"], "version": int(b["version"]), "evidence": _render_times(json.loads(b["evidence"]))}
            for b in self._rows(
                "SELECT break_id, version, evidence, found_at FROM breaks WHERE attempt_id = ? AND kind = ? AND version < ? ORDER BY version",
                (r["attempt_id"], r["kind"], r["version"]),
            )
            if from_iso_utc(b["found_at"]) <= self.as_of
        ]
        entry = None
        if r["statement_entry_id"]:
            e = self._rows("SELECT * FROM statements WHERE entry_id = ?", (r["statement_entry_id"],))
            if e and from_iso_utc(e[0]["entry_at"]) <= self.as_of:
                entry = {k: v for k, v in e[0].items()}
                entry["entry_at"] = operator_time(from_iso_utc(e[0]["entry_at"]), seconds=True)
        resolutions = list(resolutions_for(r["kind"]))
        return {
            "break_id": r["break_id"],
            "kind": r["kind"],
            "attempt_id": r["attempt_id"],
            "version": int(r["version"]),
            "found_at": operator_time(from_iso_utc(r["found_at"])),
            "first_pass": r["first_pass"],
            "seen_in_passes": seen,
            "evidence": _render_times(evidence),
            "statement_entry": entry,
            "prior_versions": prior,
            "resolutions": resolutions,
            "money_moving": [x for x in resolutions if x in MONEY_MOVING],
        }

    def _pair_health(self, provider: str, corridor: str, rail: str) -> dict[str, Any]:
        prof = self._rows("SELECT * FROM profiles WHERE provider = ? AND corridor = ? AND rail = ?", (provider, corridor, rail))
        snap = self.world.as_of(self.as_of)
        n, f = snap.trailing_counts(provider, corridor, rail)
        baseline = float(prof[0]["baseline_failure"]) if prof else None
        incident = [
            i["incident_id"]
            for i in self._rows("SELECT * FROM incidents WHERE provider = ? AND corridor = ? AND rail = ?", (provider, corridor, rail))
            if from_iso_utc(i["first_observed"]) <= self.as_of
            and (i["resolved_at"] is None or from_iso_utc(i["resolved_at"]) > self.as_of)
        ]
        return {
            "provider": provider,
            "rail": rail,
            "attempts_24h": n,
            "failures_24h": f,
            "share": round(f / n, 4) if n else 0.0,
            "baseline": baseline,
            "degraded": bool(baseline is not None and evaluate_degraded(n, f, baseline)),
            "open_incident": incident[0] if incident else None,
            "p95": int(prof[0]["p95"]) if prof else None,
            "unit": clock_unit(rail),
        }

    def read_payment(self, attempt_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,))
        if not rows or from_iso_utc(rows[0]["created_at"]) > self.as_of:
            return None
        a = rows[0]
        corridor, rail, provider = a["corridor"], a["rail"], a["provider"]
        cal = self._calendars[corridor]
        sub = from_iso_utc(a["submitted_at"]) if a["submitted_at"] else None
        if sub is not None and sub > self.as_of:
            sub = None
        own = self.case if self.case.get("attempt_id") == attempt_id and self.case["action_type"] == "stuck" else None
        if own is not None:
            status = own["status_at_as_of"]
            elapsed = int(own["elapsed"])
            p95 = own["p95"]
            profile_count = int(own["profile_count"])
            nxt = own["next_cutoff"]
            degraded = bool(own["degraded_pair"])
        else:
            status = self.world.as_of(self.as_of).attempt_status(attempt_id)[0] if sub is not None else "created"
            elapsed = elapsed_on_clock(rail, sub, self.as_of, cal) if sub is not None else 0
            health = self._pair_health(provider, corridor, rail)
            p95 = health["p95"]
            prof = self._rows("SELECT count FROM profiles WHERE provider = ? AND corridor = ? AND rail = ?", (provider, corridor, rail))
            profile_count = int(prof[0]["count"]) if prof else 0
            nxt = next_cutoff(rail, self.as_of, cal) if rail in CALENDAR_RAILS else None
            degraded = bool(health["degraded"])
        today = local_date(self.as_of, cal.offset)
        return {
            "attempt_id": a["attempt_id"],
            "payment_id": a["payment_id"],
            "corridor": corridor,
            "provider": provider,
            "rail": rail,
            "amount_usd": a["amount_usd_cents"] / 100.0,
            "amount_destination": a["amount_dst"] / 100.0,
            "currency": CURRENCY[corridor],
            "status": status,
            "submitted_at_operator": operator_time(sub),
            "submitted_at_local": local_time(sub, corridor) if sub is not None else None,
            "elapsed": elapsed,
            "unit": clock_unit(rail),
            "p95": p95,
            "profile_settled_attempts": profile_count,
            "next_cutoff": operator_time(nxt) if nxt is not None else None,
            "next_banking_day": cal.next_banking_day(today).isoformat(),
            "banking_day_today": cal.is_banking_day(today),
            "pair_degraded": degraded,
        }

    def _created(self, provider: str, corridor: str, rail: str) -> list[str]:
        out = []
        for r in self._rows(
            "SELECT attempt_id, created_at, submitted_at, terminal_at FROM attempts WHERE provider = ? AND corridor = ? AND rail = ? ORDER BY attempt_id",
            (provider, corridor, rail),
        ):
            if from_iso_utc(r["created_at"]) > self.as_of:
                continue
            if r["submitted_at"] and from_iso_utc(r["submitted_at"]) <= self.as_of:
                continue
            if r["terminal_at"] and from_iso_utc(r["terminal_at"]) <= self.as_of:
                continue
            out.append(r["attempt_id"])
        return out

    def read_rail_health(self, corridor: str) -> dict[str, Any] | None:
        if corridor not in CURRENCY:
            return None
        profs = self._rows("SELECT provider, rail FROM profiles WHERE corridor = ? ORDER BY provider, rail", (corridor,))
        own = self.case if self.case["action_type"] == "reroute" and self.case["corridor"] == corridor else None
        pairs = []
        if own is not None:
            for p in own["pairs"]:
                row = {k: p[k] for k in ("provider", "rail", "attempts_24h", "failures_24h", "share", "baseline", "degraded", "p95")}
                row["open_incident"] = self._pair_health(p["provider"], corridor, p["rail"])["open_incident"]
                row["unit"] = clock_unit(p["rail"])
                if (p["provider"], p["rail"]) == (own["from_provider"], own["from_rail"]):
                    row["created_payouts"] = list(own["created_payouts"])
                else:
                    row["created_payouts"] = self._created(p["provider"], corridor, p["rail"])
                pairs.append(row)
        else:
            for p in profs:
                row = self._pair_health(p["provider"], corridor, p["rail"])
                row["created_payouts"] = self._created(p["provider"], corridor, p["rail"])
                pairs.append(row)
        return {"corridor": corridor, "as_of": operator_time(self.as_of), "pairs": pairs}


__all__ = [
    "ACTION_TOOLS",
    "CLARIFICATION_LABEL",
    "OUR_ADDITION",
    "STANDARD_RFI_ACTIONS",
    "READ_TOOLS",
    "TOOLS",
    "TOOL_CALL_LIMIT",
    "TOOL_SPECS",
    "ToolBox",
    "ToolResult",
    "local_time",
    "operator_time",
]
