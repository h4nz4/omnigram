"""DM funnel and drip funnel: an ordered list of messages, each due N hours after the previous.

Same machinery serves both: the DM funnel is started by a contact's first message
(telegram.listen raises it), the drip funnel by adding a recipient by hand or from the parser.
State is JSON-serializable so a run survives a restart: ``funnel.json`` per account.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from omnigram.templates import render


@dataclass
class Step:
    """One message: wait `hours` after the previous step (or after opt-in for the first)."""
    hours: float
    template: str


@dataclass
class Funnel:
    """A named sequence of steps. `kind` distinguishes the sidebar entries: 'dm' | 'drip'."""
    name: str
    steps: list[Step] = field(default_factory=list)
    kind: str = "drip"


def new_entry(now: datetime, funnel: Funnel) -> dict:
    """A fresh subscription at step 0, due `steps[0].hours` from `now`."""
    delay = funnel.steps[0].hours if funnel.steps else 0
    return {"step": 0, "due_at": now + timedelta(hours=delay), "done": not funnel.steps, "started": now}


def due_entries(entries: dict, funnel: Funnel, now: datetime) -> list[int]:
    """Ids whose current step is due (at or before `now`) and not already sent."""
    return [rid for rid, e in entries.items() if not e["done"] and e["due_at"] <= now]


def enroll(entries: dict, funnel: Funnel, key, now: datetime) -> bool:
    """Add `key` as a fresh subscription unless it is already in, or the funnel has no steps.

    Used by ``telegram.funnel_watch`` for a DM funnel: the first private message from someone new
    enrolls them. Returns True when a subscriber was added.
    """
    if not funnel.steps or key in entries:
        return False
    entries[key] = new_entry(now, funnel)
    return True


def advance(entry: dict, funnel: Funnel, now: datetime) -> dict:
    """Move the entry past the step just sent; marks it done when no steps remain."""
    step = entry["step"] + 1
    if step >= len(funnel.steps):
        entry.update(step=step, done=True)
        return entry
    entry.update(step=step, due_at=now + timedelta(hours=funnel.steps[step].hours))
    return entry


def progress(entries: dict, funnel: Funnel) -> tuple[int, int, float]:
    """(messages sent, messages planned, percent) across all subscriptions."""
    sent = sum(e["step"] for e in entries.values())
    total = len(entries) * len(funnel.steps)
    return sent, total, (100 * sent / total if total else 0)


def render_step(funnel: Funnel, step: int, context: dict | None = None) -> str:
    return render(funnel.steps[step].template, context or {})


# ---- persistence ----------------------------------------------------------------------------------

def serialize(funnel: Funnel, entries: dict) -> dict:
    """JSON-safe snapshot: the funnel itself plus per-recipient state (ids become strings)."""
    return {"funnel": {"name": funnel.name, "kind": funnel.kind, "steps": [asdict(s) for s in funnel.steps]},
            "entries": {str(rid): {"step": e["step"], "due_at": e["due_at"].isoformat(),
                                   "done": e["done"], "started": e["started"].isoformat()}
                        for rid, e in entries.items()}}


def deserialize(data: dict) -> tuple[Funnel, dict]:
    """Keys come back as ints when they were numeric ids, else as usernames (funnels may address
    either; see broadcast.parse_targets)."""
    raw = data.get("funnel", {})
    funnel = Funnel(name=raw.get("name", ""), kind=raw.get("kind", "drip"),
                    steps=[Step(**s) for s in raw.get("steps", [])])
    entries = {int(rid) if rid.lstrip("-").isdigit() else rid:
               {"step": e["step"], "due_at": datetime.fromisoformat(e["due_at"]),
                "done": e["done"], "started": datetime.fromisoformat(e["started"])}
               for rid, e in data.get("entries", {}).items()}
    return funnel, entries


def load(path: Path) -> tuple[Funnel, dict] | None:
    return deserialize(json.loads(path.read_text("utf-8"))) if path.exists() else None


def save(path: Path, funnel: Funnel, entries: dict):
    tmp = path.with_suffix(".tmp")  # write-then-rename, same as store._write_json
    tmp.write_text(json.dumps(serialize(funnel, entries), ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)
