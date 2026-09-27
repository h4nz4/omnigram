"""Tests for omnigram.mailing_dialogs — the pure helpers behind the mailing dialogs.

No Qt here: the dialogs themselves are exercised by hand (offscreen import check).
"""
from random import Random

from omnigram import broadcast
from omnigram.broadcast import Recipient
from omnigram.mailing_dialogs import (
    PREVIEW_CONTEXT,
    comment_texts,
    plan_summary,
    split_posts,
    targets_text,
    to_recipients,
)
from omnigram.parser import parse_filter
from omnigram.templates import render


def test_split_posts_separates_on_dash_lines():
    assert split_posts("first post\n---\nsecond post") == ["first post", "second post"]


def test_split_posts_trims_and_drops_empties():
    """Blank posts (double separators, leading/trailing ones) never reach Telegram."""
    assert split_posts("\n\n---\n\n  hello  \n\n---\n\n---\n") == ["hello"]


def test_split_posts_tolerates_whitespace_and_longer_rules():
    assert split_posts("a\n  ---  \nb\n-----\nc") == ["a", "b", "c"]


def test_split_posts_keeps_inner_dashes_and_newlines():
    """Only a whole line of dashes separates; a '--' inside a post is just text."""
    assert split_posts("one\n- two\n--\nthree") == ["one\n- two\n--\nthree"]


def test_split_posts_empty_input():
    assert split_posts("") == []
    assert split_posts(None) == []


def test_comment_texts_one_per_line_trimmed():
    assert comment_texts("  nice!  \n\nwow\n") == ["nice!", "wow"]


def test_comment_texts_empty_input():
    assert comment_texts("") == []
    assert comment_texts(None) == []


def test_targets_text_uses_at_username_and_plain_ids():
    rs = [Recipient(id=1, username="ann"), Recipient(id=42)]
    assert targets_text(rs) == "@ann\n42"


def test_targets_text_round_trips_through_parse_targets():
    rs = [Recipient(id=1, username="ann"), Recipient(id=42), Recipient(id=7, username="bob")]
    assert targets_text(rs) == "@ann\n42\n@bob"
    back = broadcast.parse_targets(targets_text(rs))
    assert [(r.username, r.id) for r in back] == [("ann", 0), ("", 42), ("bob", 0)]


def test_plan_summary_counts_and_estimates_time():
    rs = [Recipient(id=i) for i in range(3)]
    steps = broadcast.plan(rs, "hi", min_delay=30, max_delay=30, seed=Random(0))
    assert plan_summary(steps) == "3 recipients · about 1 min 0 s"


def test_plan_summary_singular_and_empty():
    assert plan_summary([]) == "no recipients"
    one = broadcast.plan([Recipient(id=1)], "hi", min_delay=5, max_delay=5)
    assert plan_summary(one) == "1 recipient · about 0 s"


def test_plan_summary_reflects_per_hour_cap():
    """A per-hour cap raises the floor, so the estimate must grow with it."""
    rs = [Recipient(id=i) for i in range(4)]
    slow = broadcast.plan(rs, "hi", min_delay=1, max_delay=1, per_hour=60, seed=Random(0))
    assert [s.delay for s in slow] == [0, 60, 60, 60]
    assert "3 min 0 s" in plan_summary(slow)


def test_to_recipients_maps_fields():
    users = [{"id": 5, "first_name": "Anna", "last_name": "Smith", "username": "ann", "phone": "1"}]
    assert to_recipients(users) == [Recipient(id=5, first_name="Anna", last_name="Smith",
                                              username="ann", phone="1")]


def test_to_recipients_tolerates_missing_keys():
    """parse_participants always sends the keys, but a partial dict must not crash the dialog."""
    rs = to_recipients([{"id": 9}])
    assert rs == [Recipient(id=9)]


def test_to_recipients_carries_bot_and_deleted_flags_into_parse_filter():
    users = [{"id": 1, "username": "ann"},
             {"id": 2, "username": "helper_bot", "bot": True},
             {"id": 3, "username": "gone", "deleted": True}]
    rs = to_recipients(users)
    assert [r.id for r in parse_filter(rs)] == [1]
    assert [r.id for r in parse_filter(rs, include_bots=True)] == [1, 2]


def test_preview_context_renders_like_a_real_recipient():
    assert PREVIEW_CONTEXT["first_name"] == "Anna"
    assert render("Hi {first_name} @{username} ({rand: a | b})", PREVIEW_CONTEXT, seed=Random(0)) \
        in ("Hi Anna @anna (a)", "Hi Anna @anna (b)")


def test_preview_context_has_every_sample_variable():
    for name in ("first_name", "last_name", "username", "phone"):
        assert PREVIEW_CONTEXT[name]


def test_preview_context_satisfies_the_snippet_placeholders():
    """Every snippet the dialog inserts must resolve in the preview context (no '{phone}' left raw)."""
    for snippet in ("{first_name}", "{last_name}", "{username}", "{phone}"):
        assert render(snippet, PREVIEW_CONTEXT) != snippet
