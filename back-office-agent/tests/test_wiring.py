"""The keyless check: the world, the agent's loop, the scorer and Prove's store, with no model.

    BACKOFFICE_WORLD=world pytest tests

Needs the world generated (``python -m backoffice_world ensure world``). Spends nothing.
"""

from __future__ import annotations

import pytest
from poisson_prove import Episode

from backoffice_agent import agent
from backoffice_agent.draw import ACTION_TYPES
from backoffice_agent.facts import case_facts

from scripted import OracleClient


@pytest.fixture(scope="module")
def world():
    return agent.open_world()


@pytest.mark.parametrize("action", ACTION_TYPES)
def test_every_fixed_case_scores_right_on_the_oracles_own_pick(world, action, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKOFFICE_LEDGER", str(tmp_path / "spend.jsonl"))
    monkeypatch.setattr(agent, "_spent", None)
    for case_id in agent.cases(world, action):
        client = OracleClient(case_facts(world, case_id))
        s, meta = agent.run_episode(world, case_id, client, agent.SONNET)
        assert s.outcome == "right", (case_id, s)
        steps = [sp["step_name"] for sp in meta["trace"]["spans"] if sp["step_name"] != "episode"]
        assert steps == meta["tool_calls"]
        assert meta["cost_usd"] == 0


def test_the_rollout_returns_an_episode_with_its_case(world, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKOFFICE_LEDGER", str(tmp_path / "spend.jsonl"))
    monkeypatch.setattr(agent, "_spent", None)
    monkeypatch.setattr(agent, "_client", lambda: None)
    real = agent.run_episode
    monkeypatch.setattr(agent, "run_episode", lambda w, c, _client, m, *tb: real(w, c, OracleClient(case_facts(w, c)), m, *tb))
    ep = agent.rollout_haiku({"action": "stuck", "case": "2"}, seed=0)
    assert isinstance(ep, Episode) and ep.outcome == "right"
    assert ep.meta["case_id"] == agent.cases(world, "stuck")[1] and ep.meta["model"] == agent.HAIKU


def test_the_cap_stops_before_a_call(world, tmp_path, monkeypatch):
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text('{"usd": 20.0, "model": "x", "case_id": "x", "input_tokens": 0, "output_tokens": 0, "t": 0}\n')
    monkeypatch.setenv("BACKOFFICE_LEDGER", str(ledger))
    monkeypatch.setattr(agent, "_spent", None)
    case_id = agent.cases(world, "rfi")[0]
    client = OracleClient(case_facts(world, case_id))
    with pytest.raises(agent.CapReached):
        agent.run_episode(world, case_id, client, agent.SONNET)
    assert client.n == 0


def test_the_planted_break_stops_an_rfi_at_the_identity_tool_and_leaves_a_break_alone(world, tmp_path, monkeypatch):
    from backoffice_agent.planted import BrokenIdentityToolBox

    monkeypatch.setenv("BACKOFFICE_LEDGER", str(tmp_path / "spend.jsonl"))
    monkeypatch.setattr(agent, "_spent", None)
    rfi = agent.cases(world, "rfi")[0]
    s, meta = agent.run_episode(world, rfi, OracleClient(case_facts(world, rfi)), agent.SONNET, BrokenIdentityToolBox)
    assert s.outcome == "no_action" and meta["stopped"] == "tool_timeout"
    assert meta["tool_calls"][-1] == "read_identity_record"
    brk = agent.cases(world, "break")[0]
    s, meta = agent.run_episode(world, brk, OracleClient(case_facts(world, brk)), agent.SONNET, BrokenIdentityToolBox)
    assert s.outcome == "right"
