"""Account warm-up: a jittered plan of harmless own-presence actions, ramped over days.

A warm-up only touches the account's own presence: read the dialogs, view a channel, react to
a post it already reads, join a recommended channel the operator picked, keep the profile
current, sit online, pause. Nothing here messages a third party, so ``telegram.warmup_run``
only needs the account's own client.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from random import Random

# Kinds ``telegram._warmup_step`` knows how to perform; keep in sync.
ACTIONS = ("read", "view", "react", "join", "online", "pause")

RAMP_FLOOR = 0.4  # day one runs at 40% of the target; the last day runs at 100%


@dataclass
class Action:
    """One warm-up action: `kind` to perform, `delay` seconds to wait before it, `day` it belongs to."""
    kind: str
    delay: int
    day: int


def warmup_plan(days: int, per_day: int, *, ramp: bool = False, min_delay: int = 60, max_delay: int = 600,
                kinds: Sequence[str] | None = None, seed: Random | None = None) -> list[Action]:
    """Build the action plan.

    `days` x `per_day` actions (rounded up), each carrying a random `min_delay..max_delay` wait and
    the day it belongs to. With `ramp`, day 1 gets ``RAMP_FLOOR`` of `per_day` and the last day gets
    all of it, so a fresh account does not act like a seasoned one on day one.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    if per_day < 1:
        raise ValueError("per_day must be >= 1")
    if min_delay < 0 or max_delay < min_delay:
        raise ValueError("expected 0 <= min_delay <= max_delay")
    kinds = list(kinds) if kinds is not None else list(ACTIONS)
    if not kinds:
        raise ValueError("kinds must not be empty")
    rng = seed or Random()
    plan: list[Action] = []
    for day in range(days):
        count = per_day
        if ramp and days > 1:
            fraction = RAMP_FLOOR + (1 - RAMP_FLOOR) * day / (days - 1)
            count = max(1, round(per_day * fraction))
        for _ in range(count):
            plan.append(Action(kind=rng.choice(kinds), delay=rng.randint(min_delay, max_delay), day=day))
    return plan


def total_seconds(plan: Sequence[Action]) -> int:
    return sum(a.delay for a in plan)


def per_day(plan: Sequence[Action]) -> dict[int, int]:
    """Actions per day index — for the confirmation line ("day 1: 4 actions, day 2: 7, …")."""
    counts: dict[int, int] = {}
    for a in plan:
        counts[a.day] = counts.get(a.day, 0) + 1
    return counts
