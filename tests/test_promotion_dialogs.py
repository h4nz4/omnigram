"""Tests for omnigram.promotion_dialogs — the pure helpers behind the promotion dialogs.

parse_links / peer_targets decide what a pasted box means, pick_by_username resolves an @name
against a chat's participant list, and collect_lines turns fetched participants into box lines.
The dialogs themselves only wire these to widgets and telegram coroutines.
"""
from omnigram.promotion_dialogs import (
    collect_lines,
    parse_links,
    peer_targets,
    pick_by_username,
)


def test_links_trim_and_drop_blanks_and_comments():
    assert parse_links("  @chan \n\n# a note\n t.me/other \n   \n") == ["@chan", "t.me/other"]


def test_links_keep_order_and_dedupe():
    assert parse_links("@a\n@b\n@a\nhttps://t.me/+AbC\nhttps://t.me/+AbC") == ["@a", "@b", "https://t.me/+AbC"]


def test_links_empty():
    assert parse_links("") == []
    assert parse_links("  \n # only a comment \n") == []


def test_pick_by_username_is_case_insensitive_and_strips_at():
    rows = [{"id": 5, "username": "Anna"}, {"id": 9, "username": "bob"}]
    assert pick_by_username(rows, "@anna") == 5
    assert pick_by_username(rows, "BOB") == 9


def test_pick_by_username_missing():
    assert pick_by_username([{"id": 1, "username": "ann"}], "bob") is None
    assert pick_by_username([], "ann") is None
    assert pick_by_username([{"id": 1, "username": ""}], "ann") is None


def test_peer_targets_numeric_ids_and_usernames():
    assert peer_targets("123\n@ann\n456") == [123, "@ann", 456]


def test_peer_targets_dedupe_and_order():
    assert peer_targets("@ann\n123\n@ann\n123") == ["@ann", 123]


def test_collect_lines_prefers_username_then_id():
    assert collect_lines([{"id": 1, "username": "ann"}, {"id": 2, "username": ""}]) == ["@ann", "2"]


def test_collect_lines_drops_bots_and_deleted():
    rows = [{"id": 1, "username": "ann"}, {"id": 2, "username": "botty", "bot": True},
            {"id": 3, "username": "gone", "deleted": True}]
    assert collect_lines(rows) == ["@ann"]
