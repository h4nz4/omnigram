"""Broadcast planning: who to send to, what text each gets, how long to wait between sends.

Everything here is pure (no network, no Qt) so it can be tested and previewed before a run.
The actual send loop lives in ``telegram.send_broadcast`` and consumes a ``plan``.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from random import Random

from omnigram.templates import render

MIN_PER_HOUR = 1  # sanity floor; a per_hour below this does not slow anything further


@dataclass
class Recipient:
    """A Telegram user we may write to. Only ids and the fields a template can use."""
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    phone: str = ""

    @property
    def context(self) -> dict:
        """Template variables for this recipient."""
        return {"id": self.id, "first_name": self.first_name, "last_name": self.last_name,
                "name": " ".join(filter(None, [self.first_name, self.last_name])),
                "username": self.username, "phone": self.phone}


@dataclass
class Step:
    """One send: recipient, rendered text, seconds to wait before sending it."""
    recipient: Recipient
    text: str
    delay: int


def recipients_from_users(users: Iterable) -> list[Recipient]:
    """Telethon User objects (or anything with the same attributes) -> Recipient list."""
    return [Recipient(id=u.id, first_name=u.first_name or "", last_name=u.last_name or "",
                      username=u.username or "", phone=u.phone or "") for u in users]


def parse_targets(text: str) -> list[Recipient]:
    """Paste-friendly recipient list: one target per line, '@name' or a numeric id.

    Blank lines and '#…' comments are ignored; duplicates collapse (same username or id).
    Lines that are neither a username nor a number are skipped silently — pasting a mixed list
    should not lose the valid half.
    """
    out: list[Recipient] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("@")
        if not line or line.startswith("#"):
            continue
        if line.lstrip("-").isdigit():
            out.append(Recipient(id=int(line)))
        elif all(part and (part.replace("_", "").isalnum()) for part in line.split()) and len(line) >= 3:
            out.append(Recipient(id=0, username=line))
    seen, unique = set(), []
    for r in out:
        key = r.username.lower() if r.username else r.id
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def peer(r: Recipient):
    """What telegram.send_message wants for this recipient: the @username, else the numeric id."""
    return r.username or r.id


def dedupe(recipients: Iterable[Recipient]) -> list[Recipient]:
    """Drop repeated ids, keeping the first occurrence (and therefore the input order)."""
    seen: set[int] = set()
    out: list[Recipient] = []
    for r in recipients:
        if r.id not in seen:
            seen.add(r.id)
            out.append(r)
    return out


def filter_blacklist(recipients: Iterable[Recipient], ids: set[int] = frozenset(),
                     usernames: set[str] = frozenset()) -> list[Recipient]:
    """Drop any recipient whose id or lowercased username is on the blacklist."""
    lower = {u.lower() for u in usernames}
    return [r for r in recipients if r.id not in ids and (not r.username or r.username.lower() not in lower)]


def plan(recipients: Sequence[Recipient], template: str, *, min_delay: int = 5, max_delay: int = 15,
         per_hour: int = 0, seed: Random | None = None) -> list[Step]:
    """Render `template` for each recipient and assign a wait before each send.

    The first step never waits (the user just pressed the button). Later steps wait a random
    `min_delay..max_delay` seconds; `per_hour` (if set) raises the floor to 3600/per_hour so the
    schedule physically cannot exceed the requested rate. `seed` makes a plan reproducible for
    previews.
    """
    if min_delay < 0 or max_delay < min_delay:
        raise ValueError("expected 0 <= min_delay <= max_delay")
    rng = seed or Random()
    floor = max(min_delay, (3600 // max(per_hour, MIN_PER_HOUR)) if per_hour else 0)
    high = max(max_delay, floor)
    steps = []
    for i, r in enumerate(recipients):
        steps.append(Step(r, render(template, r.context, seed=rng), 0 if i == 0 else rng.randint(floor, high)))
    return steps


def total_seconds(steps: Sequence[Step]) -> int:
    """Wall-clock estimate for the plan (sum of all delays)."""
    return sum(s.delay for s in steps)


def human_time(seconds: int) -> str:
    """"3 h 12 min" style duration, for the confirmation prompt."""
    minutes, seconds = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} s"
    return f"{seconds} s"


def batches(recipients: Sequence[Recipient], size: int) -> list[list[Recipient]]:
    """Split into consecutive chunks of `size`; the last chunk may be shorter."""
    if size < 1:
        raise ValueError("batch size must be >= 1")
    return [list(recipients[i:i + size]) for i in range(0, len(recipients), size)]
