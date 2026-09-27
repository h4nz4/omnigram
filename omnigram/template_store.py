"""Named message templates on disk (templates.json next to accounts.json).

A template is a name -> text pair; the text uses omnigram.templates syntax. Both the mailing
dialogs and the listener's first-DM link read from here.
"""
from __future__ import annotations

import json
from pathlib import Path


class TemplateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, str]:
        """{name: text}, sorted by name; an unreadable/corrupt file loads as empty."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (ValueError, OSError):
            return {}
        return {str(k): str(v) for k, v in sorted(data.items())} if isinstance(data, dict) else {}

    def save(self, templates: dict[str, str]):
        tmp = self.path.with_suffix(".tmp")  # write-then-rename, same as store._write_json
        tmp.write_text(json.dumps(dict(sorted(templates.items())), ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.path)

    def upsert(self, name: str, text: str):
        templates = self.load()
        templates[name] = text
        self.save(templates)

    def remove(self, name: str):
        templates = self.load()
        if name in templates:
            del templates[name]
            self.save(templates)
