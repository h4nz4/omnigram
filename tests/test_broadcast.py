"""Tests for omnigram.broadcast — recipient handling, message planning, pacing.

The broadcast itself (the Telethon send loop) lives in telegram.send_broadcast; everything
that can be decided without a network connection lives here so it is testable and cannot
surprise the user mid-run: dedupe, blacklist, per-recipient rendering, pacing between sends.
"""
from random import Random

import pytest

from omnigram.broadcast import (
    Recipient,
    batches,
    dedupe,
    filter_blacklist,
    plan,
    recipients_from_users,
    total_seconds,
)


def user(id, first_name="", last_name="", username="", phone=""):
    """Stand-in for a telethon User — only the attributes we read."""
    class U:
        pass
    u = U()
    u.id, u.first_name, u.last_name, u.username, u.phone = id, first_name, last_name, username, phone
    return u


def test_recipients_from_users_maps_fields():
    rs = recipients_from_users([user(1, "Anna", "Smith", "ann", "79990000001")])
    assert rs == [Recipient(id=1, first_name="Anna", last_name="Smith", username="ann", phone="79990000001")]


def test_recipients_from_users_handles_none_fields():
    """Telegram users can have None for username/phone/last_name."""
    rs = recipients_from_users([user(2, "Bob", None, None, None)])
    assert rs[0].first_name == "Bob"
    assert rs[0].last_name == ""
    assert rs[0].username == ""
    assert rs[0].phone == ""


def test_dedupe_keeps_first_seen_order():
    rs = [Recipient(id=1), Recipient(id=2), Recipient(id=1), Recipient(id=3)]
    assert [r.id for r in dedupe(rs)] == [1, 2, 3]


def test_filter_blacklist_by_id_and_username():
    rs = [Recipient(id=1, username="ann"), Recipient(id=2, username="bob"), Recipient(id=3)]
    out = filter_blacklist(rs, ids={2}, usernames={"ann"})
    assert [r.id for r in out] == [3]


def test_filter_blacklist_empty_is_noop():
    rs = [Recipient(id=1), Recipient(id=2)]
    assert filter_blacklist(rs, ids=set(), usernames=set()) == rs


def test_plan_renders_template_per_recipient():
    rs = [Recipient(id=1, first_name="Anna"), Recipient(id=2, first_name="Bob")]
    steps = plan(rs, "Hi {first_name}!", min_delay=1, max_delay=1, seed=Random(0))
    assert [s.text for s in steps] == ["Hi Anna!", "Hi Bob!"]


def test_plan_includes_recipient_with_each_step():
    rs = [Recipient(id=7, first_name="Zoe")]
    steps = plan(rs, "hey", min_delay=0, max_delay=0)
    assert steps[0].recipient.id == 7


def test_plan_delays_within_bounds():
    rs = [Recipient(id=i) for i in range(20)]
    steps = plan(rs, "hi", min_delay=2, max_delay=5, seed=Random(1))
    assert steps[0].delay == 0  # first send is immediate
    assert all(2 <= s.delay <= 5 for s in steps[1:])


def test_plan_first_delay_is_zero():
    """The first send should not wait — the user just pressed the button."""
    rs = [Recipient(id=1), Recipient(id=2)]
    steps = plan(rs, "hi", min_delay=3, max_delay=3, seed=Random(0))
    assert steps[0].delay == 0
    assert steps[1].delay == 3


def test_plan_is_deterministic_with_seed():
    rs = [Recipient(id=i) for i in range(5)]
    a = plan(rs, "hi", min_delay=1, max_delay=9, seed=Random(42))
    b = plan(rs, "hi", min_delay=1, max_delay=9, seed=Random(42))
    assert [s.delay for s in a] == [s.delay for s in b]


def test_total_seconds_sums_delays():
    rs = [Recipient(id=i) for i in range(4)]
    steps = plan(rs, "hi", min_delay=1, max_delay=1, seed=Random(0))
    assert total_seconds(steps) == 3


def test_batches_splits_evenly_and_keeps_order():
    rs = [Recipient(id=i) for i in range(5)]
    out = batches(rs, 2)
    assert [[r.id for r in b] for b in out] == [[0, 1], [2, 3], [4]]


def test_batches_rejects_zero_size():
    with pytest.raises(ValueError):
        batches([Recipient(id=1)], 0)


def test_plan_respects_max_per_hour_by_setting_min_delay():
    """When the caller caps sends/hour, the plan's floor delay must honor that cap."""
    rs = [Recipient(id=i) for i in range(3)]
    steps = plan(rs, "hi", min_delay=0, max_delay=0, per_hour=60, seed=Random(0))
    # 60/hour = 60s between sends minimum, enforced even though min/max were 0
    assert all(s.delay >= 60 for s in steps[1:])


def test_parse_targets_handles_usernames_ids_and_noise():
    from omnigram.broadcast import parse_targets
    rs = parse_targets("@ann\n123\n# comment\n\nann\nbob\nx\n")
    assert [(r.username, r.id) for r in rs] == [("ann", 0), ("", 123), ("bob", 0)]


def test_parse_targets_dedupes_case_insensitively():
    from omnigram.broadcast import parse_targets
    assert [r.username for r in parse_targets("@Ann\n@ann\n")] == ["Ann"]


def test_peer_prefers_username():
    from omnigram.broadcast import peer
    assert peer(Recipient(id=5, username="ann")) == "ann"
    assert peer(Recipient(id=5)) == 5
