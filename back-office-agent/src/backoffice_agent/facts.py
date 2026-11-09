"""What the scorer and the note checks read about one case: the oracle's case record, enriched
with the few world facts the harmful classes need, all as of the case's own time.

``case_facts(world, case_id)`` is a pure read of the world's emitted tables and its oracle; it
never writes and never reads anything later than the case's as-of time, except the oracle's own
labels, which only the scorer sees.
"""

from __future__ import annotations

import json
from typing import Any

from backoffice_world.clock import from_iso_utc
from backoffice_world.rules import RECORD_ITEMS
from backoffice_world.world import World

#: Every item id an RFI answer may cite: what a record can hold, plus the invoice no record holds.
ITEM_IDS: tuple[str, ...] = (*RECORD_ITEMS, "transaction_invoice")

#: Destination currency per corridor.
CURRENCY: dict[str, str] = {"MX": "MXN", "BR": "BRL", "PH": "PHP", "DE": "EUR", "US": "USD"}

#: Country names a note may use for a corridor, beside its code.
COUNTRY: dict[str, str] = {
    "MX": "Mexico",
    "BR": "Brazil",
    "PH": "Philippines",
    "DE": "Germany",
    "US": "United States",
}


def _row(world: World, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    cur = world.conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    r = cur.fetchone()
    return None if r is None else dict(zip(cols, r, strict=True))


def held_items(record_items: dict[str, Any]) -> frozenset[str]:
    """The item ids a record holds: present, expired or not."""
    return frozenset(k for k, v in record_items.items() if isinstance(v, dict) and v.get("present"))


def submitted_on_corridor(world: World, corridor: str, as_of: int) -> frozenset[str]:
    """Our attempts on the corridor already submitted at ``as_of``: never to be re-sent."""
    cur = world.conn.execute(
        "SELECT attempt_id, submitted_at FROM attempts WHERE corridor = ? AND submitted_at IS NOT NULL",
        (corridor,),
    )
    return frozenset(a for a, s in cur.fetchall() if from_iso_utc(s) <= as_of)


def created_on_corridor(world: World, corridor: str, as_of: int) -> dict[str, list[str]]:
    """Every attempt on the corridor still created at ``as_of`` (created by then, not yet
    submitted and not terminal, the rail-health tool's own reading), by pair: "provider on rail"
    to its sorted ids."""
    cur = world.conn.execute(
        "SELECT attempt_id, provider, rail, created_at, submitted_at, terminal_at FROM attempts WHERE corridor = ?",
        (corridor,),
    )
    out: dict[str, list[str]] = {}
    for attempt_id, provider, rail, created_at, submitted_at, terminal_at in cur.fetchall():
        if from_iso_utc(created_at) > as_of:
            continue
        if submitted_at and from_iso_utc(submitted_at) <= as_of:
            continue
        if terminal_at and from_iso_utc(terminal_at) <= as_of:
            continue
        out.setdefault(f"{provider} on {rail}", []).append(attempt_id)
    return {k: sorted(v) for k, v in sorted(out.items())}


def case_facts(world: World, case_id: str) -> dict[str, Any]:
    """The oracle's record of the case plus what the harmful classes read:

    - rfi: ``record_items`` (the identity record's items) and ``held`` (sorted ids it holds);
    - stuck: ``amount_usd_cents``, ``amount_dst``, ``currency`` of the attempt;
    - reroute: ``submitted`` (sorted ids of the corridor's attempts submitted at the as-of time)
      and ``corridor_created`` (sorted ids of every pair's payouts still created at that time,
      the failing pair's included) with ``corridor_created_counts`` (how many per pair, as
      "provider on rail", and the corridor's total under "corridor").
    """
    raw = world.oracle.raw["cases"][case_id]
    facts: dict[str, Any] = dict(raw)
    facts["case_id"] = case_id
    kind = raw["action_type"]
    as_of = int(raw["as_of"])
    if kind == "rfi":
        rec = _row(world, "SELECT items FROM identity_records WHERE user_id = ?", (raw["end_user"],))
        items = json.loads(rec["items"]) if rec else {}
        facts["record_items"] = items
        facts["held"] = sorted(held_items(items))
    elif kind == "stuck":
        att = _row(
            world,
            "SELECT amount_usd_cents, amount_dst FROM attempts WHERE attempt_id = ?",
            (raw["attempt_id"],),
        )
        assert att is not None
        facts["amount_usd_cents"] = int(att["amount_usd_cents"])
        facts["amount_dst"] = int(att["amount_dst"])
        facts["currency"] = CURRENCY[raw["corridor"]]
    elif kind == "reroute":
        facts["submitted"] = sorted(submitted_on_corridor(world, raw["corridor"], as_of))
        by_pair = created_on_corridor(world, raw["corridor"], as_of)
        facts["corridor_created"] = sorted(a for ids in by_pair.values() for a in ids)
        facts["corridor_created_counts"] = {**{k: len(v) for k, v in by_pair.items()}, "corridor": len(facts["corridor_created"])}
    return facts
