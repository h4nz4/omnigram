"""Phone-number list handling for the Number checker.

Pure parsing/normalizing only: the checker itself (does Telegram know this number) runs over
Telegram in ``telegram.check_numbers``. Here we make sure a pasted list becomes clean E.164
numbers, deduplicated, without inventing ones that could never be dialed.
"""
from __future__ import annotations

from collections.abc import Iterable

import phonenumbers

# Default region for numbers pasted without a country code; the checker lets the user override.
DEFAULT_REGION = "US"


def normalize_number(text: str) -> str | None:
    """'+1 (202) 555-0143' -> '+12025550143'; None when there are no digits at all."""
    digits = "".join(c for c in (text or "") if c.isdigit())
    return f"+{digits}" if digits else None


def to_phone(text: str, region: str = DEFAULT_REGION) -> str | None:
    """Normalize and validate; E.164 string or None. Numbers without '+' are parsed in `region`."""
    has_plus = "+" in (text or "")
    number = normalize_number(text)
    if not number:
        return None
    try:
        parsed = phonenumbers.parse(number if has_plus else number.lstrip("+"), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def check_list(items: Iterable[str], region: str = DEFAULT_REGION) -> list[str]:
    """Normalize, validate and dedupe a pasted list, keeping the original order."""
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        number = to_phone(item, region)
        if number and number not in seen:
            seen.add(number)
            out.append(number)
    return out
