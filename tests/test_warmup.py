"""Tests for omnigram.warmup — account warm-up scheduling.

A warm-up is a jittered plan of harmless account actions (read, view, react, join, pause)
spread over days, ramping from a few actions a day to the target. It only ever touches the
account's own presence — no messages to third parties.
"""
from random import Random

import pytest

from omnigram.warmup import ACTIONS, warmup_plan


def test_plan_length_is_days_times_per_day():
    plan = warmup_plan(days=3, per_day=4, seed=Random(0))
    assert len(plan) == 12


def test_plan_actions_are_known_kinds():
    plan = warmup_plan(days=2, per_day=5, seed=Random(1))
    assert all(a.kind in ACTIONS for a in plan)


def test_plan_delays_within_bounds():
    plan = warmup_plan(days=2, per_day=6, min_delay=30, max_delay=120, seed=Random(2))
    assert all(30 <= a.delay <= 120 for a in plan)


def test_plan_is_deterministic_with_seed():
    a = warmup_plan(days=4, per_day=3, seed=Random(7))
    b = warmup_plan(days=4, per_day=3, seed=Random(7))
    assert [(x.kind, x.delay) for x in a] == [(x.kind, x.delay) for x in b]


def test_plan_ramps_actions_per_day():
    """Day 1 is light, later days add more — a fresh account should not act 50 times on day one."""
    plan = warmup_plan(days=5, per_day=10, ramp=True, seed=Random(3))
    first_day = [a for a in plan if a.day == 0]
    last_day = [a for a in plan if a.day == 4]
    assert len(first_day) < len(last_day)


def test_plan_without_ramp_has_equal_days():
    plan = warmup_plan(days=3, per_day=4, ramp=False, seed=Random(0))
    from collections import Counter
    assert Counter(a.day for a in plan) == {0: 4, 1: 4, 2: 4}


def test_plan_rejects_zero_per_day():
    with pytest.raises(ValueError):
        warmup_plan(days=3, per_day=0)


def test_plan_rejects_zero_days():
    with pytest.raises(ValueError):
        warmup_plan(days=0, per_day=3)


def test_plan_days_are_in_order():
    plan = warmup_plan(days=4, per_day=5, seed=Random(9))
    assert [a.day for a in plan] == sorted(a.day for a in plan)


def test_plan_total_seconds_sums_delays():
    plan = warmup_plan(days=1, per_day=3, min_delay=10, max_delay=10, seed=Random(0))
    assert sum(a.delay for a in plan) == 30


def test_plan_can_exclude_kinds():
    plan = warmup_plan(days=3, per_day=4, kinds=["read", "pause"], seed=Random(0))
    assert {a.kind for a in plan} <= {"read", "pause"}


def test_plan_empty_kinds_raises():
    with pytest.raises(ValueError):
        warmup_plan(days=1, per_day=2, kinds=[], seed=Random(0))
