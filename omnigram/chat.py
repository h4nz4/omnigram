"""Plain data for the chat window (no Qt, no Telethon): chats, messages, and how they are shown.

telegram.ChatClient turns Telethon objects into these; chat_window.py only ever sees these.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Chat:
    id: int  # Telethon's marked peer id (channels are -100…)
    title: str
    kind: str  # user | bot | group | channel
    unread: int = 0
    last_text: str = ""
    last_date: datetime | None = None
    can_send: bool = True
    admin: bool = False  # this account created or administers the group/channel


@dataclass
class Msg:
    id: int
    chat_id: int
    out: bool
    date: datetime
    text: str = ""
    sender: str = ""  # shown above incoming messages in groups
    reply_to: int | None = None
    edited: bool = False
    forwarded: str = ""  # "Forwarded from …", "" if not forwarded
    media: str = ""  # photo | sticker | gif | video | voice | audio | document | location | contact | poll | other
    media_label: str = ""  # "Photo", "Voice 0:12", "report.pdf · 1.2 MB"
    has_thumb: bool = False
    mentioned: bool = False  # it @-mentions this account or replies to one of its messages (Telegram's flag)
    sender_id: int = 0
    extra: dict = field(default_factory=dict)  # room for later fields without breaking callers


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02}:{seconds % 60:02}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02}"


def media_label(kind: str, *, name: str = "", size: int = 0, seconds: float | None = None, title: str = "",
                emoji: str = "") -> str:
    """What a message's media is called in its bubble and in the chat list."""
    length = duration(seconds)
    if kind == "photo":
        return "Photo"
    if kind == "sticker":
        return f"Sticker {emoji}".strip()
    if kind == "gif":
        return "GIF"
    if kind in ("video", "voice"):
        return f"{kind.capitalize()} {length}".strip()
    if kind == "audio":
        return " · ".join(filter(None, [title or name or "Audio", length]))
    if kind == "document":
        return " · ".join(filter(None, [name or "File", human_size(size) if size else ""]))
    return {"location": "Location", "contact": "Contact", "poll": "Poll"}.get(kind, "Message")


def preview(msg: Msg, limit: int = 60) -> str:
    """One line for the chat list: the text's first line, else the media label."""
    text = (msg.text or "").strip().splitlines()
    line = text[0] if text else msg.media_label
    if msg.media and text:
        line = f"{msg.media_label}, {line}"
    return line if len(line) <= limit else line[: limit - 1] + "…"


def merge(existing: list[Msg], incoming: list[Msg]) -> list[Msg]:
    """Older pages, live messages and edits all land here: one list, oldest first, one entry per id (the
    incoming copy wins, so an edit replaces the old text)."""
    by_id = {m.id: m for m in existing}
    by_id.update({m.id: m for m in incoming})
    return sorted(by_id.values(), key=lambda m: m.id)


def day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if (today - day).days == 1:
        return "Yesterday"
    return f"{day.day} {day:%B}" + ("" if day.year == today.year else f" {day.year}")


def starts_day(previous: Msg | None, msg: Msg) -> bool:
    """Whether a date header goes above `msg` (first message, or a new local day)."""
    return previous is None or previous.date.astimezone().date() != msg.date.astimezone().date()


def mark_read_now(toggle: bool, reason: str) -> bool:
    """Whether to send a read receipt. Sending always marks the chat read (as Telegram does); opening a chat
    or receiving a message in it only does when the window's "Mark as read" switch is on, so you can look
    without the other side seeing "seen"."""
    if reason not in ("open", "incoming", "sent"):
        raise ValueError(reason)
    return reason == "sent" or toggle
