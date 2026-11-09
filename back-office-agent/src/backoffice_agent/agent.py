"""The operations agent and the rollouts Prove runs.

``rollout_sonnet(cfg, seed)`` and ``rollout_haiku(cfg, seed)`` are Prove rollouts: each runs one
case on one model and returns a ``poisson_prove.Episode``. ``cfg`` names the case by its action
type and its slot (``{"action": "stuck", "case": "3"}``), so a condition always runs the same case,
and the case's id goes in ``meta`` where Prove checks it.

One episode is a loop: the model is offered the six read tools and the action tools, reads what
it wants (twelve tool calls at most), and ends with one action. The scorer then compares the
pick with the answer the world's generator knows, so every outcome is checked, never judged by a
model: ``right``, ``wrong``, ``harmful`` (a pick that would pay twice, cite an item the record
does not hold, carry a wrong number to a provider, or reroute onto a degraded rail), or
``no_action``.

Each tool call is recorded as a step with Prove's ``record()``, named after the tool, inside one
``agent`` span for the episode, so the stored trace has one leaf per tool call. That is what Trace
counts as a step.

A tool that times out (``ToolTimeout``) stops the episode at that step with no action.

Every model call is priced from the usage the API returns and appended to a spend ledger
(``spend.jsonl`` in the working folder, or ``BACKOFFICE_LEDGER``). Before each call the ledger's
total is checked against the cap (``BACKOFFICE_CAP_USD``, $20 by default); at the cap the
episode raises ``CapReached`` and makes no call, and Prove stores it as an errored episode.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from poisson_prove import Episode, record
from poisson_prove.trace import collect

from backoffice_world.world import World

from .draw import ACTION_TYPES, draw_case
from .facts import case_facts
from .scorer import Score, score
from .tools import TOOL_CALL_LIMIT, TOOL_SPECS, ToolBox, operator_time

PROMPTS = Path(__file__).resolve().parent / "prompts"
MAX_TOKENS = 2048
CASES_PER_ACTION = 5

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

#: Dollars per million tokens, from Anthropic's published prices (read 2026-11-09). Haiku 5.5's
#: price is for prompts up to 100,000 tokens; an episode's prompts here are far below that.
PRICES: dict[str, tuple[float, float]] = {
    SONNET: (2.00, 10.00),
    HAIKU: (0.10, 0.50),
}

DEFAULT_CAP_USD = 20.0


class CapReached(RuntimeError):
    """The spend ledger has reached the cap; no further model call is made."""


class ToolTimeout(RuntimeError):
    """A tool call that did not return in time. The loop stops the episode at that step, with no
    action, as a harness stops an agent whose tool hangs."""


# ---------- the world and the cases ----------

_world_lock = threading.Lock()
_world: World | None = None


def world_dir() -> Path:
    """The world folder: ``BACKOFFICE_WORLD``, else ``./world``."""
    path = Path(os.environ.get("BACKOFFICE_WORLD", "world"))
    if not (path / "world.sqlite").is_file():
        raise RuntimeError(
            f"no world at {path}: generate it first with `python -m backoffice_world ensure {path}`"
        )
    return path


def open_world() -> World:
    global _world
    with _world_lock:
        if _world is None:
            _world = World.open(world_dir())
        return _world


def cases(world: World, action: str) -> list[str]:
    """The fixed cases of an action type: ``CASES_PER_ACTION`` distinct cases drawn once from the
    world's pool for it with a seed named after the type, about 30% of them hard cases."""
    rng = random.Random(f"backoffice-cases:{action}")
    picked: list[str] = []
    while len(picked) < CASES_PER_ACTION:
        c = draw_case(world, action, rng)
        if c not in picked:
            picked.append(c)
    return picked


def case_for(cfg: Mapping[str, Any]) -> str:
    action = str(cfg["action"])
    if action not in ACTION_TYPES:
        raise ValueError(f"no action type {action!r}; the four are {', '.join(ACTION_TYPES)}")
    slot = int(str(cfg["case"]))
    if not 1 <= slot <= CASES_PER_ACTION:
        raise ValueError(f"case must be 1 to {CASES_PER_ACTION}, got {slot}")
    return cases(open_world(), action)[slot - 1]


# ---------- the prompt and the task ----------


def prompt_text() -> str:
    return (PROMPTS / "v1.md").read_text(encoding="utf-8")


def task_message(facts: Mapping[str, Any]) -> str:
    """The opening message of an episode: the case and the time, nothing the oracle knows."""
    templates: dict[str, str] = json.loads((PROMPTS / "tasks.json").read_text(encoding="utf-8"))
    return templates[facts["action_type"]].format(
        case_id=facts["case_id"],
        as_of=operator_time(int(facts["as_of"])),
        attempt_id=facts.get("attempt_id", ""),
        corridor=facts.get("corridor", ""),
        from_provider=facts.get("from_provider", ""),
        from_rail=facts.get("from_rail", ""),
    )


# ---------- spend ----------

_ledger_lock = threading.Lock()
_spent: float | None = None


def ledger_path() -> Path:
    return Path(os.environ.get("BACKOFFICE_LEDGER", "spend.jsonl"))


def cap_usd() -> float:
    return float(os.environ.get("BACKOFFICE_CAP_USD", DEFAULT_CAP_USD))


def ledger_total(path: Path | None = None) -> float:
    p = path or ledger_path()
    if not p.is_file():
        return 0.0
    return sum(json.loads(line)["usd"] for line in p.read_text(encoding="utf-8").splitlines() if line.strip())


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES[model]
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def _check_cap() -> None:
    global _spent
    with _ledger_lock:
        if _spent is None:
            _spent = ledger_total()
        if _spent >= cap_usd():
            raise CapReached(f"spend ledger at ${_spent:.4f}, cap ${cap_usd():.2f}: no further model call")


def _charge(model: str, case_id: str, input_tokens: int, output_tokens: int) -> float:
    global _spent
    usd = price(model, input_tokens, output_tokens)
    line = {"t": time.time(), "model": model, "case_id": case_id, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "usd": usd}
    with _ledger_lock:
        with ledger_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
        _spent = (_spent or 0.0) + usd
    return usd


# ---------- one episode ----------


def _get(block: Any, name: str) -> Any:
    if isinstance(block, Mapping):
        return block.get(name)
    return getattr(block, name, None)


def _as_param(block: Any) -> dict[str, Any]:
    """A response content block as a request message block."""
    kind = _get(block, "type")
    if kind == "tool_use":
        return {"type": "tool_use", "id": _get(block, "id"), "name": _get(block, "name"), "input": _get(block, "input")}
    if kind == "text":
        return {"type": "text", "text": _get(block, "text")}
    dump = getattr(block, "model_dump", None)
    return dict(dump(exclude_none=True)) if callable(dump) else dict(block)


def run_episode(
    world: World, case_id: str, client: Any, model: str, toolbox: type[ToolBox] = ToolBox
) -> tuple[Score, dict[str, Any]]:
    """One episode on ``case_id`` with ``model``: the loop, the pick and its score, and the meta
    Prove stores (with the trace under ``trace``). ``toolbox`` is the tools' class."""
    facts = case_facts(world, case_id)
    box = toolbox(world, case_id)
    tool_calls: list[str] = []
    model_calls = 0
    tokens_in = tokens_out = 0
    usd = 0.0
    stopped = ""
    with collect() as collected:
        with record("agent", "episode", input=case_id, step_name="episode"):
            messages: list[dict[str, Any]] = [{"role": "user", "content": task_message(facts)}]
            while True:
                _check_cap()
                response = client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=prompt_text(),
                    tools=[dict(t) for t in TOOL_SPECS],
                    messages=messages,
                )
                model_calls += 1
                usage = _get(response, "usage")
                i, o = int(_get(usage, "input_tokens") or 0), int(_get(usage, "output_tokens") or 0)
                tokens_in += i
                tokens_out += o
                usd += _charge(model, case_id, i, o)
                content: Sequence[Any] = _get(response, "content") or []
                uses = [b for b in content if _get(b, "type") == "tool_use"]
                if not uses:
                    stopped = "model_stopped"
                    break
                results: list[dict[str, Any]] = []
                for use in uses:
                    if len(tool_calls) >= TOOL_CALL_LIMIT:
                        # the model asked for more calls in one turn than the episode has left:
                        # the rest are not run, and the episode stops at the limit
                        stopped = "tool_call_limit"
                        break
                    name = str(_get(use, "name"))
                    args = _get(use, "input")
                    with record("tool", name, input=args, step_name=name) as span:
                        try:
                            res = box.call(name, args if isinstance(args, Mapping) else {})
                            out: Any = res.content
                            is_error = res.is_error
                        except ToolTimeout:
                            out, is_error = {"error": "timed_out", "tool": name}, True
                            stopped = "tool_timeout"
                        span.output = out
                        if is_error:
                            span.error = json.dumps(out, sort_keys=True, ensure_ascii=False)[:500]
                    tool_calls.append(name)
                    if stopped == "tool_timeout":
                        break
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": _get(use, "id"),
                        "content": json.dumps(out, sort_keys=True, ensure_ascii=False),
                        "is_error": is_error,
                    })
                if stopped in ("tool_timeout", "tool_call_limit"):
                    break
                if box.pick is not None:
                    stopped = "action"
                    break
                if len(tool_calls) >= TOOL_CALL_LIMIT:
                    stopped = "tool_call_limit"
                    break
                messages.append({"role": "assistant", "content": [_as_param(b) for b in content]})
                messages.append({"role": "user", "content": results})
    s = score(facts, box.pick, facts["correct"])
    meta: dict[str, Any] = {
        "case_id": case_id,
        "action": facts["action_type"],
        "hard": bool(facts.get("hard")),
        "model": model,
        "pick": None if box.pick is None else box.pick.as_dict(),
        "failed_check": s.failed_check,
        "harmful_class": s.harmful_class,
        "tool_calls": tool_calls,
        "model_calls": model_calls,
        "stopped": stopped,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": round(usd, 6),
        "trace": collected.trace.to_dict(),
    }
    return s, meta


def _client() -> Any:
    import anthropic

    return anthropic.Anthropic()


def _rollout(cfg: Mapping[str, Any], model: str, toolbox: type[ToolBox] = ToolBox) -> Episode:
    world = open_world()
    case_id = case_for(cfg)
    s, meta = run_episode(world, case_id, _client(), model, toolbox)
    return Episode(outcome=s.outcome, t=len(meta["tool_calls"]), horizon=TOOL_CALL_LIMIT, meta=meta)


def rollout_sonnet(cfg: Mapping[str, Any], seed: int) -> Episode:
    """The agent on Claude Sonnet 5.5. The API takes no seed, so ``seed`` pairs the episode with
    its twin in the other run and changes nothing in the call."""
    return _rollout(cfg, SONNET)


def rollout_haiku(cfg: Mapping[str, Any], seed: int) -> Episode:
    """The same agent, prompt and tools on Claude Haiku 5.5: the change under test."""
    return _rollout(cfg, HAIKU)
