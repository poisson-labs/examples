"""A scripted model client that plays the world's own answer: the keyless check of the wiring.

``OracleClient`` answers ``messages.create`` with the reads an agent would make on the case and
then the oracle's pick, with a note written to the scorer's fields. Run through the agent, every
case scores ``right``, and through Prove every episode is stored with one step per tool call.
Nothing here calls a model or reads a key.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

UNIT_WORD = {"minutes": "minutes", "opportunities": "settlement opportunities"}


def et(t: int) -> str:
    """A time as ``YYYY-MM-DD HH:MM ET`` (EDT, UTC-4, throughout the window)."""
    return dt.datetime.fromtimestamp(t - 4 * 3600, dt.UTC).strftime("%Y-%m-%d %H:%M") + " ET"


# ---------- the scripted model client ----------


def tool_use(name: str, args: Mapping[str, Any], ident: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=ident, name=name, input=dict(args))


def text(s: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=s)


def response(*blocks: SimpleNamespace) -> SimpleNamespace:
    stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(type="message", role="assistant", content=list(blocks), stop_reason=stop)


@dataclass
class ScriptedClient:
    """A model client that answers each ``messages.create`` with the next planned response and
    keeps a deep copy of every request it was sent. Past the plan it stops the turn."""

    plan: list[SimpleNamespace]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.messages = self

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(copy.deepcopy(kwargs))
        i = len(self.requests) - 1
        if i < len(self.plan):
            return self.plan[i]
        return response(text("done"))


def calls(*pairs: tuple[str, Mapping[str, Any]], start: int = 0) -> list[SimpleNamespace]:
    """One response per call, each with one tool use."""
    return [response(tool_use(n, a, f"tu_{start + i:03d}")) for i, (n, a) in enumerate(pairs)]


def tool_results(client: ScriptedClient) -> list[dict[str, Any]]:
    """Every tool result the agent sent back, in order, with its content parsed."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for req in client.requests:
        for msg in req["messages"]:
            if msg["role"] != "user" or not isinstance(msg["content"], list):
                continue
            for block in msg["content"]:
                if block.get("type") == "tool_result" and block["tool_use_id"] not in seen:
                    seen.add(block["tool_use_id"])
                    out.append({**block, "parsed": json.loads(block["content"])})
    return out


# ---------- notes written to the spec's fields, independently of the package ----------


def stuck_note(f: Mapping[str, Any], **override: Any) -> str:
    v = {
        "attempt_id": f["attempt_id"],
        "provider": f["provider"],
        "rail": f["rail"],
        "submitted": et(int(f["submitted_at"])),
        "elapsed": f["elapsed"],
        "p95": f["p95"],
        "unit": UNIT_WORD[f["unit"]],
        "request": "Please confirm its status and send its settlement reference.",
    }
    v.update(override)
    return (
        f"Payout {v['attempt_id']} with {v['provider']} on {v['rail']}, submitted {v['submitted']}, "
        f"has elapsed {v['elapsed']} {v['unit']} on the rail's clock against the pair's p95 of "
        f"{v['p95']} {v['unit']}. {v['request']}"
    )


def degraded_pair(f: Mapping[str, Any]) -> Mapping[str, Any]:
    for p in f["pairs"]:
        if p["provider"] == f["from_provider"] and p["rail"] == f["from_rail"]:
            return p
    raise AssertionError(f"the case's pair is not among its pairs: {f['case_id']}")


def pct(share: float) -> str:
    s = f"{share * 100:.1f}".rstrip("0").rstrip(".")
    return f"{s}%"


def reroute_note(f: Mapping[str, Any], **override: Any) -> str:
    c = f["correct"]
    p = degraded_pair(f)
    v = {
        "corridor": f["corridor"],
        "from_provider": f["from_provider"],
        "from_rail": f["from_rail"],
        "share": pct(float(p["share"])),
        "counts": f"({p['failures_24h']} failures of {p['attempts_24h']} attempts)",
        "to_provider": c["to_provider"],
        "to_rail": c["to_rail"],
        "payouts": ", ".join(c["payouts"]),
        "cycle": " Settlement moves to the next cycle." if c["settlement_moves_to_next_cycle"] else "",
    }
    v.update(override)
    return (
        f"Reroute draft for {v['corridor']}: {v['from_provider']} on {v['from_rail']} is degraded, "
        f"failing {v['share']} of its attempts over the last 24 hours {v['counts']}. Proposed "
        f"destination: {v['to_provider']} on {v['to_rail']}. Payouts still created: {v['payouts']}.{v['cycle']}"
    )


def hold_note(f: Mapping[str, Any], **override: Any) -> str:
    v = {"corridor": f["corridor"]}
    v.update(override)
    return (
        f"Hold draft for {v['corridor']}: no healthy provider remains on the corridor. Hold the "
        f"payouts still created until a pair recovers."
    )


# ---------- the oracle's own pick, and each case's expected reads ----------


def oracle_pick(f: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The action call the oracle's answer is, with a note that passes the spec's checks."""
    c, kind = f["correct"], f["action_type"]
    if kind == "rfi":
        return "answer_rfi", {
            "case_id": f["case_id"], "action": c["action"], "items": list(c["items"]),
            "fields": list(c["fields"]), "note": "Answered from the case's own evidence.",
        }
    if kind == "break":
        return "resolve_break", {"break_id": f["case_id"], "resolution": c["resolution"], "note": "Per the playbook."}
    if kind == "stuck":
        note = stuck_note(f) if c["stuck"] else "Within its pair's profile; no note to the provider."
        return "flag_late_payout", {"attempt_id": f["attempt_id"], "stuck": bool(c["stuck"]), "note": note}
    if c["hold"]:
        return "draft_hold", {"corridor": f["corridor"], "note": hold_note(f)}
    return "draft_reroute", {
        "corridor": f["corridor"], "from_provider": f["from_provider"], "from_rail": f["from_rail"],
        "to_provider": c["to_provider"], "to_rail": c["to_rail"], "payouts": list(c["payouts"]),
        "note": reroute_note(f),
    }


def reads_for(f: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The read calls an agent working the case would make, in order."""
    kind = f["action_type"]
    if kind == "rfi":
        return [("read_context", {"name": "rfi_item_map"}), ("read_case", {"case_id": f["case_id"]}),
                ("read_identity_record", {"user_id": f["end_user"]})]
    if kind == "break":
        return [("read_context", {"name": "resolution_playbook"}), ("read_break", {"break_id": f["case_id"]})]
    if kind == "stuck":
        return [("read_payment", {"attempt_id": f["attempt_id"]}), ("read_rail_health", {"corridor": f["corridor"]})]
    return [("read_context", {"name": "routing_policy"}), ("read_rail_health", {"corridor": f["corridor"]})]


def oracle_plan(f: Mapping[str, Any]) -> list[SimpleNamespace]:
    reads = reads_for(f)
    return calls(*reads, oracle_pick(f))


class OracleClient:
    """A client whose ``messages.create`` plays ``oracle_plan`` for the case it was made for,
    with usage figures of zero so the spend ledger records $0."""

    def __init__(self, facts: Mapping[str, Any]) -> None:
        self.plan = oracle_plan(facts)
        self.n = 0
        self.messages = self

    def create(self, **kwargs: Any) -> SimpleNamespace:
        r = self.plan[self.n] if self.n < len(self.plan) else response(text("done"))
        self.n += 1
        r.usage = SimpleNamespace(input_tokens=0, output_tokens=0)
        return r
