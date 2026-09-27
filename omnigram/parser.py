"""Audience collection filters.

``telegram.parse_participants`` fetches users from a chat; ``parse_filter`` decides which
of them are worth keeping, so the rule set is previewable and testable without Telegram.
The bot/deleted flags come from the Telethon User object and are carried on the Recipient.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from omnigram.broadcast import Recipient, dedupe


def parse_filter(recipients: Iterable[Recipient], *, keyword: str = "", require_username: bool = False,
                 include_bots: bool = False, include_deleted: bool = False,
                 blacklist_ids: set[int] = frozenset(),
                 blacklist_usernames: set[str] = frozenset()) -> list[Recipient]:
    """Apply the audience rules and drop duplicates.

    Bots and deleted accounts are removed unless asked for; `keyword` matches a substring of
    the full name or username, case-insensitively; `require_username` keeps only users a
    broadcast can address by @handle. Blacklists win over everything.
    """
    banned = {u.lower().lstrip("@") for u in blacklist_usernames}
    keyword = keyword.strip().lower()
    kept: list[Recipient] = []
    for r in recipients:
        if getattr(r, "bot", False) and not include_bots:
            continue
        if getattr(r, "deleted", False) and not include_deleted:
            continue
        if require_username and not r.username:
            continue
        if keyword and keyword not in f"{r.first_name} {r.last_name} {r.username}".lower():
            continue
        if r.id in blacklist_ids or (r.username and r.username.lower().lstrip("@") in banned):
            continue
        kept.append(r)
    return dedupe(kept)


def format_recipients(recipients: Sequence[Recipient]) -> list[str]:
    """One display line per recipient, for the parser's result list and CSV export."""
    return [f"{r.id}  {r.first_name} {r.last_name}".rstrip() + (f"  @{r.username}" if r.username else "")
            + (f"  {r.phone}" if r.phone else "") for r in recipients]
