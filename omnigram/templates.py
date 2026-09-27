"""Templates: small text bodies used by mailing, funnels, auto-replies.

Supports:
  {first_name}, {last_name}, {name}, {username}, {phone}, {id}  → per-recipient substitution.
  Unknown variables are left intact (a typo never silently empties the message).
  {rand: a | b | c} (alias: {spin: ...}) → one alternative, picked fresh each call.
  Literal ``\\n`` becomes a real newline.

The parser/validator are separate from render so the UI can flag a broken template
without running it through a real recipient.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from random import Random

_KNOWN = {"first_name", "last_name", "name", "username", "phone", "id"}

# {name} — a plain variable.
_VAR = re.compile(r"\{([a-z_][a-z_0-9]*)\}")
# {rand: ...} or {spin: ...} — alternatives separated by '|'; whitespace around each is trimmed.
_RAND = re.compile(r"\{(rand|spin):\s*((?:[^|}]+\|)*[^|}]+)\}", re.IGNORECASE)


def parse(template: str) -> set[str]:
    """The set of variable names referenced in `template` (including inside {rand:...})."""
    names: set[str] = set()
    for m in _VAR.finditer(template):
        if m.group(1) in _KNOWN:
            names.add(m.group(1))
    for m in _RAND.finditer(template):
        inner = m.group(2)
        names.update(_VAR.findall(inner))
    return names


def validate(template: str) -> list[str]:
    """Human-readable problems; empty list means the template is safe to render."""
    errors: list[str] = []
    opens = template.count("{")
    closes = template.count("}")
    if opens != closes:
        errors.append(f"unbalanced braces: {opens} '{{' vs {closes} '}}'")
    for m in _RAND.finditer(template):
        alternatives = [a.strip() for a in m.group(2).split("|")]
        if not any(alternatives):
            errors.append(f"empty {{rand: ...}}: '{m.group(0)}'")
    return errors


def _replace_rand(template: str, rng: Random) -> str:
    def pick(m: re.Match) -> str:
        alternatives = [a.strip() for a in m.group(2).split("|")]
        return rng.choice(alternatives) if alternatives else m.group(0)
    return _RAND.sub(pick, template)


def _replace_vars(template: str, context: dict | None) -> str:
    if not context:
        return template
    def sub(m: re.Match) -> str:
        name = m.group(1)
        return str(context[name]) if name in context and context[name] not in (None, "") else m.group(0)
    return _VAR.sub(sub, template)


def render(template: str, context: dict | None = None, *, seed: Random | None = None) -> str:
    """Substitute variables, pick one alternative per {rand:...}, decode ``\\n``.

    `seed` makes the picks reproducible (used by retries and tests); default picks fresh each call.
    """
    rng = seed or Random()
    text = template.replace("\\n", "\n")
    text = _replace_rand(text, rng)
    text = _replace_vars(text, context)
    return text


def alternatives(template: str) -> Iterable[str]:
    """Yield each {rand: a | b | c} block's alternatives (whitespace-trimmed)."""
    for m in _RAND.finditer(template):
        yield from (a.strip() for a in m.group(2).split("|"))
