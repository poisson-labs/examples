"""The seed-to-case draw: the episode's seeded ``random`` picks the case, so the same seed is the
same case in every arm, and a comparison paired on the baseline's salt sees the same cases.

The pool for an action type is the world's whole pool for it (``World.cases``): RFI and break
pools span the whole window, stuck and reroute pools the monitoring window. A draw takes one
number to decide whether the case is a hard one (``HARD_SHARE`` of draws) and one to pick a
case, uniformly, from the sorted hard or ordinary part of the pool. Repeats are allowed; they are
counted where the runs are read, not removed here.
"""

from __future__ import annotations

import random as _random
from typing import Protocol

from backoffice_world.world import World

ACTION_TYPES: tuple[str, ...] = ("rfi", "break", "stuck", "reroute")
HARD_SHARE = 0.30


class Rng(Protocol):
    def random(self) -> float: ...

    def randrange(self, stop: int) -> int: ...


def pools(world: World, action_type: str) -> tuple[list[str], list[str]]:
    """(hard, ordinary): the sorted case ids of the pool split by whether the case is hard."""
    if action_type not in ACTION_TYPES:
        raise ValueError(f"no action type {action_type!r}; the four are {', '.join(ACTION_TYPES)}")
    every = sorted(world.cases(action_type))
    hard = set(world.cases(action_type, hard_only=True))
    return [c for c in every if c in hard], [c for c in every if c not in hard]


def draw_case(world: World, action_type: str, rng: Rng | None = None) -> str:
    """The case for an episode, drawn with ``rng`` (the process's seeded ``random`` by default)."""
    r = _random if rng is None else rng
    hard, ordinary = pools(world, action_type)
    part = hard if r.random() < HARD_SHARE else ordinary
    return part[r.randrange(len(part))]
