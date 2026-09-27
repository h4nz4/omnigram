"""omnigram.chat — the chat window's plain data and display rules (no Qt, no Telegram)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from omnigram import chat

T = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def msg(id, text="", **kw):
    return chat.Msg(id=id, chat_id=1, out=False, date=kw.pop("date", T), text=text, **kw)


def test_sizes_and_durations():
    assert chat.human_size(512) == "512 B"
    assert chat.human_size(1536) == "1.5 KB"
    assert chat.human_size(3 * 1024 ** 2) == "3.0 MB"
    assert chat.duration(12) == "0:12"
    assert chat.duration(3725) == "1:02:05"
    assert chat.duration(None) == ""


def test_media_labels():
    assert chat.media_label("photo") == "Photo"
    assert chat.media_label("voice", seconds=12) == "Voice 0:12"
    assert chat.media_label("document", name="report.pdf", size=1536) == "report.pdf · 1.5 KB"
    assert chat.media_label("audio", title="Song – Band", seconds=201) == "Song – Band · 3:21"
    assert chat.media_label("sticker", emoji="😀") == "Sticker 😀"


def test_preview_is_one_short_line():
    assert chat.preview(msg(1, "hello\nsecond line")) == "hello"
    assert chat.preview(msg(1, media="photo", media_label="Photo")) == "Photo"
    assert chat.preview(msg(1, "look", media="photo", media_label="Photo")) == "Photo, look"
    long = chat.preview(msg(1, "x" * 100))
    assert len(long) == 60 and long.endswith("…")


def test_merge_keeps_one_copy_per_id_oldest_first_and_edits_win():
    merged = chat.merge([msg(3, "c"), msg(1, "a")], [msg(2, "b"), msg(3, "c, edited")])
    assert [(m.id, m.text) for m in merged] == [(1, "a"), (2, "b"), (3, "c, edited")]


def test_day_labels_and_headers():
    today = date(2026, 9, 27)
    assert chat.day_label(today, today) == "Today"
    assert chat.day_label(today - timedelta(days=1), today) == "Yesterday"
    assert chat.day_label(date(2026, 3, 12), today) == "12 March"
    assert chat.day_label(date(2025, 3, 12), today) == "12 March 2025"
    first, same_day, next_day = msg(1), msg(2, date=T + timedelta(minutes=5)), msg(3, date=T + timedelta(days=1))
    assert chat.starts_day(None, first)
    assert not chat.starts_day(first, same_day)
    assert chat.starts_day(same_day, next_day)


def test_read_receipts_only_when_asked_except_after_sending():
    """Looking at a chat must not tell the other side "seen" unless the window's switch is on."""
    assert not chat.mark_read_now(False, "open")
    assert not chat.mark_read_now(False, "incoming")
    assert chat.mark_read_now(True, "open") and chat.mark_read_now(True, "incoming")
    assert chat.mark_read_now(False, "sent")
    with pytest.raises(ValueError):
        chat.mark_read_now(True, "typo")
