"""Tests for omnigram.warmup — account warm-up planning, scheduling into active hours, and run state.

A warm-up is a plan of harmless account actions (read, view, react, join, online, pause) spread
over days, ramping from a few actions a day to the target, each placed at a real time inside
that day's active-hours window in the account's timezone. It only ever touches the account's
own presence — no messages to third parties.
"""
from collections import Counter
from datetime import datetime, time, timezone
from random import Random
from zoneinfo import ZoneInfo

import pytest

from omnigram import warmup
from omnigram.warmup import ACTIONS, BULK_ACTIONS, Action, schedule, warmup_plan

BERLIN = ZoneInfo("Europe/Berlin")


def local(at: float, tz=BERLIN) -> datetime:
    return datetime.fromtimestamp(at, tz)


# ---- warmup_plan ----------------------------------------------------------------------------------

def test_plan_length_is_days_times_per_day():
    assert len(warmup_plan(days=3, per_day=4, seed=Random(0))) == 12


def test_plan_actions_are_known_kinds():
    assert all(a.kind in ACTIONS for a in warmup_plan(days=2, per_day=5, seed=Random(1)))


def test_plan_is_deterministic_with_seed():
    a = warmup_plan(days=4, per_day=3, seed=Random(7))
    b = warmup_plan(days=4, per_day=3, seed=Random(7))
    assert [(x.kind, x.day) for x in a] == [(x.kind, x.day) for x in b]


def test_plan_ramps_actions_per_day():
    """Day 1 is light, later days add more — a fresh account should not act 50 times on day one."""
    counts = Counter(a.day for a in warmup_plan(days=7, per_day=10, ramp=True, seed=Random(3)))
    assert [counts[d] for d in range(7)] == [4, 5, 6, 7, 8, 9, 10]


def test_plan_without_ramp_has_equal_days():
    assert Counter(a.day for a in warmup_plan(days=3, per_day=4, seed=Random(0))) == {0: 4, 1: 4, 2: 4}


@pytest.mark.parametrize("days, per_day", [(0, 3), (3, 0)])
def test_plan_rejects_empty_sizes(days, per_day):
    with pytest.raises(ValueError):
        warmup_plan(days=days, per_day=per_day)


def test_plan_can_exclude_kinds():
    assert {a.kind for a in warmup_plan(days=3, per_day=4, kinds=["read", "pause"], seed=Random(0))} <= {"read", "pause"}


def test_plan_empty_kinds_raises():
    with pytest.raises(ValueError):
        warmup_plan(days=1, per_day=2, kinds=[])


def test_bulk_kinds_never_need_a_shared_channel():
    """Several accounts viewing/reacting/joining one channel together is coordinated engagement."""
    assert set(BULK_ACTIONS) <= set(ACTIONS)
    assert not {"view", "react", "join"} & set(BULK_ACTIONS)


# ---- schedule -------------------------------------------------------------------------------------

def test_days_really_span_days():
    """The bug this replaces: a '7 day' warm-up used to finish in about 4.5 hours."""
    now = datetime(2026, 6, 1, 7, 0, tzinfo=BERLIN)
    plan = schedule(warmup_plan(days=7, per_day=10, ramp=True, seed=Random(0)), now, BERLIN, seed=Random(0))
    assert {local(a.at).date().day for a in plan} == {1, 2, 3, 4, 5, 6, 7}
    assert (plan[-1].at - plan[0].at) > 5 * 86400


def test_every_action_is_inside_active_hours():
    now = datetime(2026, 6, 1, 7, 0, tzinfo=BERLIN)
    plan = schedule(warmup_plan(days=5, per_day=8, seed=Random(1)), now, BERLIN,
                    window=(time(10), time(18)), min_gap=60, seed=Random(1))
    assert all(time(10) <= local(a.at).time() <= time(18) for a in plan)
    assert all(local(a.at).date().day == 1 + a.day for a in plan)


def test_active_hours_follow_the_timezone_across_dst():
    """Europe/Berlin leaves summer time on 2026-10-25: the window stays 09–23 local on both sides."""
    now = datetime(2026, 10, 23, 8, 0, tzinfo=BERLIN)
    plan = schedule(warmup_plan(days=5, per_day=20, seed=Random(2)), now, BERLIN, seed=Random(2))
    assert {local(a.at).date().day for a in plan} == {23, 24, 25, 26, 27}
    assert all(time(9) <= local(a.at).time() <= time(23) for a in plan)
    before = [a.at for a in plan if local(a.at).day == 24]
    after = [a.at for a in plan if local(a.at).day == 26]
    # the same local window is an hour later in UTC once the clocks go back
    assert min(datetime.fromtimestamp(t, timezone.utc).hour for t in after) >= 8
    assert min(datetime.fromtimestamp(t, timezone.utc).hour for t in before) >= 7


def test_timezones_shift_the_same_window():
    now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    tokyo = ZoneInfo("Asia/Tokyo")
    plan = schedule(warmup_plan(days=2, per_day=10, seed=Random(3)), now, tokyo, seed=Random(3))
    assert all(time(9) <= local(a.at, tokyo).time() <= time(23) for a in plan)


def test_day_zero_starts_now_when_the_window_is_open():
    now = datetime(2026, 6, 1, 15, 0, tzinfo=BERLIN)
    plan = schedule(warmup_plan(days=2, per_day=10, seed=Random(4)), now, BERLIN, seed=Random(4))
    first_day = [a for a in plan if a.day == 0]
    assert all(a.at >= now.timestamp() and local(a.at).day == 1 for a in first_day)


def test_day_zero_moves_to_tomorrow_after_the_window_closed():
    now = datetime(2026, 6, 1, 23, 30, tzinfo=BERLIN)
    plan = schedule(warmup_plan(days=1, per_day=5, seed=Random(5)), now, BERLIN, seed=Random(5))
    assert {local(a.at).day for a in plan} == {2}


def test_actions_keep_the_min_gap_and_time_order():
    now = datetime(2026, 6, 1, 8, 0, tzinfo=BERLIN)
    plan = schedule(warmup_plan(days=2, per_day=50, seed=Random(6)), now, BERLIN,
                    window=(time(9), time(9, 5)), min_gap=30, seed=Random(6))
    gaps = [b.at - a.at for a, b in zip(plan, plan[1:]) if a.day == b.day]
    assert min(gaps) >= 30
    assert [a.at for a in plan] == sorted(a.at for a in plan)


def test_local_time_when_no_timezone():
    now = datetime.now(timezone.utc)
    plan = schedule(warmup_plan(days=2, per_day=5, seed=Random(7)), now, None, seed=Random(7))
    assert all(time(9) <= datetime.fromtimestamp(a.at).time() <= time(23) for a in plan)


@pytest.mark.parametrize("kwargs", [{"min_gap": warmup.MIN_GAP - 1}, {"window": (time(23), time(9))},
                                    {"window": (time(9), time(9))}])
def test_schedule_rejects_bad_settings(kwargs):
    with pytest.raises(ValueError):
        schedule(warmup_plan(days=1, per_day=1), datetime.now(timezone.utc), None, **kwargs)


def test_zone_falls_back_to_local_time():
    assert warmup.zone("Europe/Berlin") == BERLIN
    assert warmup.zone("") is None
    assert warmup.zone("Not/AZone") is None
    assert warmup.zone("../etc") is None


# ---- run state ------------------------------------------------------------------------------------

def test_state_round_trips(tmp_path):
    path = tmp_path / "acc.json"
    actions = [Action("read", 0, 1.5), Action("online", 1, 90000.0)]
    warmup.save(path, actions, ["@chan"], 1)
    assert warmup.load(path) == (actions, ["@chan"], 1)
    assert not path.with_suffix(".tmp").exists()
