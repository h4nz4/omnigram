"""Account warm-up: a plan of harmless own-presence actions, ramped over days and placed in active hours.

A warm-up only touches the account's own presence: read the dialogs, view a channel, react to
a post it already reads, join a recommended channel the operator picked, keep the profile
current, sit online, pause. Nothing here messages a third party, so ``telegram.warmup_run``
only needs the account's own client.

`warmup_plan` decides how many actions of which kind land on each day; `schedule` gives each one
a wall-clock time inside that day's active-hours window, in the account's timezone (DST-aware).
The runner waits for those times, so "7 days" really takes a week.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, tzinfo
from pathlib import Path
from random import Random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Kinds ``telegram._warmup_step`` knows how to perform; keep in sync.
ACTIONS = ("read", "view", "react", "join", "online", "pause")
# Several accounts at once never share a channel: those kinds need the operator's channel list, and many
# accounts viewing/reacting/joining the same channel together is coordinated engagement, not warm-up.
BULK_ACTIONS = ("read", "online", "pause")

RAMP_FLOOR = 0.4  # day one runs at 40% of the target; the last day runs at 100%
MIN_GAP = 5  # seconds; the smallest allowed gap between two actions of one account
WINDOW = (time(9), time(23))  # default active hours, in the account's local time


@dataclass
class Action:
    """One warm-up action: `kind` to perform on day `day`, at `at` (UTC epoch seconds, set by `schedule`)."""
    kind: str
    day: int
    at: float = 0.0


def warmup_plan(days: int, per_day: int, *, ramp: bool = False, kinds: Sequence[str] | None = None,
                seed: Random | None = None) -> list[Action]:
    """`days` x `per_day` actions of random `kinds`, each tagged with its day.

    With `ramp`, day 1 gets ``RAMP_FLOOR`` of `per_day` and the last day gets all of it, so a fresh
    account does not act like a seasoned one on day one.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    if per_day < 1:
        raise ValueError("per_day must be >= 1")
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
        plan.extend(Action(kind=rng.choice(kinds), day=day) for _ in range(count))
    return plan


def schedule(plan: Sequence[Action], now: datetime, tz: tzinfo | None, *, window: tuple[time, time] = WINDOW,
             min_gap: int = 60, seed: Random | None = None) -> list[Action]:
    """Give every action a random time inside its day's `window`, in `tz` (None = this computer's local
    time). `now` must be timezone-aware. Returns new Actions, in time order.

    Day 0 is today while today's window is still open (starting no earlier than `now`), else tomorrow.
    Times within a day are at least `min_gap` seconds apart; a day too crowded for its window spills
    past the window's end rather than dropping actions.
    """
    start, end = window
    if end <= start:
        raise ValueError("the active-hours window must end after it starts")
    if min_gap < MIN_GAP:
        raise ValueError(f"min_gap must be >= {MIN_GAP} s")
    rng = seed or Random()
    local_now = now.astimezone(tz)
    first = local_now.date() + timedelta(days=local_now.time() >= end)
    out: list[Action] = []
    for day in sorted({a.day for a in plan}):
        todays = [a for a in plan if a.day == day]
        date = first + timedelta(days=day)
        lo = max(datetime.combine(date, start, tz).timestamp(), now.timestamp())
        hi = datetime.combine(date, end, tz).timestamp()
        times = sorted(rng.uniform(lo, hi) for _ in todays)
        for i in range(1, len(times)):
            times[i] = max(times[i], times[i - 1] + min_gap)
        out.extend(Action(a.kind, a.day, at) for a, at in zip(todays, times))
    return out


def zone(name: str) -> tzinfo | None:
    """The IANA zone `name`; None — this computer's local time, DST included — when empty or unknown."""
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def per_day(plan: Sequence[Action]) -> dict[int, int]:
    """Actions per day index — for the confirmation line ("day 1: 4 actions, day 2: 7, …")."""
    counts: dict[int, int] = {}
    for a in plan:
        counts[a.day] = counts.get(a.day, 0) + 1
    return counts


# ---- run state, so a multi-day warm-up survives an app restart ------------------------------------

def save(path: Path, actions: Sequence[Action], targets: Sequence[str], done: int):
    tmp = path.with_suffix(".tmp")  # write-then-rename: a crash never leaves a truncated file
    tmp.write_text(json.dumps({"actions": [asdict(a) for a in actions], "targets": list(targets), "done": done}),
                   "utf-8")
    tmp.replace(path)


def load(path: Path) -> tuple[list[Action], list[str], int]:
    data = json.loads(path.read_text("utf-8"))
    return [Action(**a) for a in data["actions"]], data["targets"], data["done"]
