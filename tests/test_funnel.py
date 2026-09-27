"""Tests for omnigram.funnel — DM funnel and drip funnel scheduling.

A funnel is an ordered list of messages, each due a number of hours after the previous one.
The DM funnel starts when a contact writes first; the drip funnel starts when a user is added
to it. Both share the same schedule/state machine tested here; telegram.funnel_run does the
sending.
"""
from datetime import datetime, timedelta

from omnigram.funnel import (
    Funnel,
    Step,
    advance,
    deserialize,
    due_entries,
    new_entry,
    progress,
    serialize,
)

T0 = datetime(2026, 1, 1, 12, 0)


def funnel(*delays, name="test"):
    return Funnel(name=name, steps=[Step(hours=h, template=f"msg{h}") for h in delays])


def test_new_entry_starts_due_after_first_delay():
    f = funnel(2, 24)
    entry = new_entry(T0, f)
    assert entry["step"] == 0
    assert entry["due_at"] == T0 + timedelta(hours=2)
    assert entry["done"] is False


def test_new_entry_with_zero_first_delay_is_due_now():
    entry = new_entry(T0, funnel(0, 24))
    assert entry["due_at"] == T0


def test_due_entries_returns_entries_whose_time_has_come():
    f = funnel(2, 24)
    entries = {1: new_entry(T0, f), 2: new_entry(T0 - timedelta(hours=5), f)}
    assert due_entries(entries, f, T0) == [2]  # started 5 h ago, first step needs 2 h: overdue
    assert sorted(due_entries(entries, f, T0 + timedelta(hours=5))) == [1, 2]


def test_advance_moves_to_next_step_with_its_delay():
    f = funnel(2, 24)
    entry = new_entry(T0, f)
    entry = advance(entry, f, T0 + timedelta(hours=2))
    assert entry["step"] == 1
    assert entry["due_at"] == T0 + timedelta(hours=2) + timedelta(hours=24)
    assert entry["done"] is False


def test_advance_past_last_step_marks_done():
    f = funnel(2)
    entry = new_entry(T0, f)
    entry = advance(entry, f, T0 + timedelta(hours=2))
    assert entry["done"] is True
    assert due_entries({1: entry}, f, T0 + timedelta(days=30)) == []


def test_done_entries_are_never_due():
    f = funnel(0)
    entry = new_entry(T0, f)
    entry["done"] = True
    assert due_entries({1: entry}, f, T0 + timedelta(days=1)) == []


def test_progress_counts_sent_steps():
    f = funnel(1, 1, 1)
    entries = {1: new_entry(T0, f), 2: new_entry(T0, f)}
    entries[1] = advance(entries[1], f, T0)
    sent, total, percent = progress(entries, f)
    assert (sent, total) == (1, 6)  # 2 subscriptions x 3 steps
    assert 10 <= percent <= 20


def test_progress_ignores_done_entries():
    f = funnel(1, 1)
    e = new_entry(T0, f)
    e = advance(e, f, T0)
    e = advance(e, f, T0)
    assert progress({1: e}, f)[0] == 2


def test_serialize_roundtrip():
    f = funnel(2, 24)
    entries = {1: new_entry(T0, f)}
    data = serialize(f, entries)
    f2, entries2 = deserialize(data)
    assert f2 == f
    assert entries2 == entries


def test_serialize_is_json_safe():
    import json
    f = funnel(0.5, 25)
    json.dumps(serialize(f, {42: new_entry(T0, f)}))  # must not raise


def test_empty_funnel_has_no_steps():
    f = Funnel(name="empty", steps=[])
    assert f.steps == []
    assert due_entries({1: new_entry(T0, f)}, f, T0) == []


def test_username_keys_survive_roundtrip():
    from omnigram.funnel import deserialize, serialize
    f = funnel(1)
    entries = {"@ann": new_entry(T0, f), 42: new_entry(T0, f)}
    _, back = deserialize(serialize(f, entries))
    assert sorted(map(str, back)) == ["42", "@ann"]
    assert isinstance(next(k for k in back if k == 42), int)


def test_enroll_adds_a_new_key_once():
    from omnigram.funnel import enroll
    f = funnel(2, 24)
    entries = {}
    assert enroll(entries, f, 7, T0) is True
    assert entries[7]["step"] == 0
    assert enroll(entries, f, 7, T0) is False  # already in
    assert len(entries) == 1


def test_enroll_refuses_a_funnel_without_steps():
    from omnigram.funnel import enroll
    f = Funnel(name="empty", steps=[])
    entries = {}
    assert enroll(entries, f, 7, T0) is False
    assert entries == {}


def test_enroll_keeps_existing_progress():
    from omnigram.funnel import advance, enroll
    f = funnel(1, 1)
    entries = {}
    enroll(entries, f, 7, T0)
    advance(entries[7], f, T0)
    enroll(entries, f, 7, T0 + timedelta(hours=5))
    assert entries[7]["step"] == 1
