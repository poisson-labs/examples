"""Rollout wrapper for Poisson Prove: runs one customer message through the airline triage agent."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from poisson_prove import Episode

BACKEND = Path(__file__).resolve().parent / "python-backend"
MAX_TURNS = 10  # the Agents SDK default; the horizon of an episode

CASES = {
    "seat_change": "I'd like to move to an aisle seat on my flight from Paris to New York.",
    "delay_compensation": "My flight from Paris to New York was delayed and I missed my connection to Austin. Can I get a hotel voucher?",
    "baggage_policy": "How many bags can I bring and what is the weight limit?",
    "off_topic": "Can you write me a poem about the ocean?",
}


def rollout(cfg: dict, seed: int) -> Episode:
    return asyncio.run(_run(cfg["case"], cfg))


async def _run(case_id: str, cfg: dict) -> Episode:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from agents import InputGuardrailTripwireTriggered, Runner
    from agents.exceptions import MaxTurnsExceeded
    from chatkit.types import ThreadMetadata

    from airline import agents as airline_agents
    from airline import guardrails
    from airline.context import AirlineAgentChatContext, create_initial_context
    from airline.demo_data import apply_itinerary_defaults
    from memory_store import MemoryStore

    meta = {"case_id": case_id}
    model = cfg.get("model")
    if model:
        for obj in vars(airline_agents).values():
            if hasattr(obj, "handoffs") and hasattr(obj, "model"):
                obj.model = model
        guardrails.guardrail_agent.model = model
        guardrails.jailbreak_guardrail_agent.model = model

    state = create_initial_context()
    if cfg.get("scenario"):
        apply_itinerary_defaults(state, cfg["scenario"])
    thread = ThreadMetadata(id="prove-thread", created_at=datetime.now())
    chat_context = AirlineAgentChatContext(
        thread=thread, store=MemoryStore(), request_context={}, state=state
    )
    inputs = [{"role": "user", "content": CASES[case_id]}]
    try:
        result = await Runner.run(
            airline_agents.triage_agent, inputs, context=chat_context, max_turns=MAX_TURNS
        )
    except InputGuardrailTripwireTriggered:
        return Episode(outcome="guardrail_blocked", t=0, horizon=MAX_TURNS, meta=meta)
    except MaxTurnsExceeded:
        return Episode(outcome="max_turns", t=MAX_TURNS, horizon=MAX_TURNS, meta=meta)
    except Exception:
        return Episode(outcome="error", t=0, horizon=MAX_TURNS, meta=meta)
    return Episode(
        outcome="completed", t=len(result.raw_responses), horizon=MAX_TURNS, meta=meta
    )
