"""A planted regression: a known-bad change to the agent, made on purpose to show what catching
one looks like. It is not a change anyone should make.

The change: ``read_identity_record`` times out on every call, as if the identity service behind
it had gone down. Everything else is the agent as it is, on the same model. The loop stops an
episode at a tool that times out, so an RFI the agent works by reading the identity record now
stops at that step with no action. The other three action types never read the record and
should not move; they are the control.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from poisson_prove import Episode

from .agent import SONNET, ToolTimeout, _rollout
from .tools import ToolBox


class BrokenIdentityToolBox(ToolBox):
    """The tools, with the identity record's tool broken: every call times out."""

    def read_identity_record(self, user_id: str) -> dict[str, Any] | None:
        raise ToolTimeout("read_identity_record")


def rollout_planted(cfg: Mapping[str, Any], seed: int) -> Episode:
    """The agent on Claude Sonnet 5.5 with the planted break."""
    return _rollout(cfg, SONNET, BrokenIdentityToolBox)
