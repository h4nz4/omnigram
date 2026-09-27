"""Tests for omnigram.parser - audience collection filters.

The network part (iterating a chat's participants) is in telegram.parse_participants;
the decisions a user makes about what to keep are pure and tested here.
"""
from omnigram.broadcast import Recipient
from omnigram.parser import parse_filter


def user(id: int, first: str = "", last: str = "", username: str = "", phone: str = "",
         bot: bool = False, deleted: bool = False):
    """A Recipient carrying the two extra flags telegram users expose."""
    r = Recipient(id=id, first_name=first, last_name=last, username=username, phone=phone)
    r.bot, r.deleted = bot, deleted
    return r


def test_parse_filter_drops_bots_and_deleted_by_default():
    rs = [user(1, "Ann"), user(2, "Bot", bot=True), user(3, "Gone", deleted=True)]
    assert [x.id for x in parse_filter(rs)] == [1]


def test_parse_filter_keeps_bots_when_asked():
    rs = [user(1, "Bot", bot=True), user(2, "Ann")]
    assert sorted(x.id for x in parse_filter(rs, include_bots=True)) == [1, 2]


def test_parse_filter_requires_username_when_asked():
    rs = [user(1, "Ann"), user(2, "Bob", username="bob")]
    assert [x.id for x in parse_filter(rs, require_username=True)] == [2]


def test_parse_filter_keyword_matches_name_and_username_case_insensitively():
    rs = [user(1, "Maria"), user(2, username="maria_x"), user(3, "Eve")]
    assert sorted(x.id for x in parse_filter(rs, keyword="MARIA")) == [1, 2]


def test_parse_filter_dedupes_repeated_ids():
    assert [x.id for x in parse_filter([user(1, "Ann"), user(1, "Ann"), user(2)])] == [1, 2]


def test_parse_filter_applies_blacklist():
    rs = [user(1, username="ann"), user(2)]
    out = parse_filter(rs, blacklist_ids={2}, blacklist_usernames={"ann"})
    assert out == []


def test_parse_filter_empty_input():
    assert parse_filter([]) == []


def test_parse_filter_combines_rules():
    rs = [user(1, "Ann", username="ann"), user(2, "Ann"),
          user(3, "Ann", username="ann3", bot=True), user(4, "Bob", username="bob")]
    assert [x.id for x in parse_filter(rs, keyword="ann", require_username=True)] == [1]
