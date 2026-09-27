"""App settings: `settings.json` in the data folder. Plain JSON so the headless server (no Qt) reads the same file,
and so the desktop can push the server the part it needs (SERVER_KEYS)."""
import json
import os
import threading
from pathlib import Path

# What a server gets from the desktop: Telegram app credentials, AI, the status bot. Never the desktop-only rest
# (favorites, the server connection itself, window preferences).
SERVER_KEYS = ("api_id", "api_hash", "bot_token", "bot_owner", "bot_proxy", "ai_provider", "ai_base_url", "ai_key",
               "ai_model", "jev_key", "jev_model", "jev_via")


def write_private(path: Path, text: str):
    """Write-then-rename (a crash never leaves half a file), owner-only on POSIX (it holds keys)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()  # the Telethon thread reads it too (AI config, bot)
        self.data: dict = json.loads(path.read_text("utf-8")) if path.exists() else {}

    def get(self, key: str, default=""):
        value = self.data.get(key)
        return default if value in (None, "") else value

    def set(self, key: str, value):
        self.update({key: value})

    def update(self, values: dict):
        with self._lock:
            self.data.update(values)
            write_private(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    def remove(self, key: str):
        with self._lock:
            if self.data.pop(key, None) is not None:
                write_private(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    def for_server(self) -> dict:
        return {k: self.data[k] for k in SERVER_KEYS if k in self.data}


def migrate(settings: Settings, qsettings) -> int:
    """First launch after settings moved out of QSettings: copy every key over once, then delete them there so
    keys and tokens are not kept twice. Runs only while settings.json doesn't exist. Returns how many moved."""
    if settings.path.exists():
        return 0
    keys = qsettings.allKeys()
    values = {key: qsettings.value(key) for key in keys}
    settings.update(values)  # creates the file even when there was nothing: migration never repeats
    for key in keys:
        qsettings.remove(key)
    return len(keys)
