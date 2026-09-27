"""JSON for the values that cross a process boundary: a job's arguments saved to `jobs/` (to resume it after a
restart) and the calls, results and events between the desktop and a server.

Plain JSON passes through; the rest is tagged {"$": kind, ...}. Only the dataclasses listed in TYPES decode, so a
file or a peer can't make this build arbitrary objects. Paths inside the data folder travel as data-relative
("sessions/x.session"), so the same job runs on a server whose data folder is elsewhere; a path outside it
(a folder on this computer) has no meaning elsewhere and is refused when sending (`portable=True`).
"""
import base64
import dataclasses
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from omnigram import ai, autopilot, broadcast, chat, funnel, store, warmup

TYPES = {cls.__name__: cls for cls in (
    chat.Chat, chat.Msg, warmup.Action, broadcast.Recipient, broadcast.Step, funnel.Step, funnel.Funnel,
    ai.ProviderConfig, ai.Profile, ai.ChatState, autopilot.Decision, autopilot.Outcome, store.Account, store.Proxy)}


class Marker:
    """An argument the engine fills in where the job runs (a log emitter, a progress callback, the AI config),
    because a function can't be saved to a file or sent to a server."""
    kind = ""

    def to_wire(self) -> dict:
        return {"$": "marker", "kind": self.kind, **{k: v for k, v in vars(self).items() if not k.startswith("_")}}


MARKERS: dict[str, type] = {}  # kind -> Marker subclass, filled by engine.py


class NotPortable(ValueError):
    pass


def encode(value, root: Path | None = None, portable: bool = False):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [encode(v, root, portable) for v in value]
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {k: encode(v, root, portable) for k, v in value.items()} if "$" not in value else \
                {"$": "dict", "items": [[k, encode(v, root, portable)] for k, v in value.items()]}
        return {"$": "dict", "items": [[encode(k, root, portable), encode(v, root, portable)] for k, v in value.items()]}
    if isinstance(value, Marker):
        return value.to_wire()
    if isinstance(value, datetime):
        return {"$": "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {"$": "date", "v": value.isoformat()}
    if isinstance(value, bytes):
        return {"$": "bytes", "v": base64.b64encode(value).decode()}
    if isinstance(value, ZoneInfo):
        return {"$": "tz", "v": value.key}
    if isinstance(value, Path):
        if root is not None:
            try:
                return {"$": "data", "v": value.resolve().relative_to(root.resolve()).as_posix()}
            except ValueError:
                pass
        if portable:
            raise NotPortable(f"{value} is a folder on this computer; this doesn't work for a server account")
        return {"$": "path", "v": str(value)}
    if dataclasses.is_dataclass(value) and type(value).__name__ in TYPES:
        return {"$": "dc", "t": type(value).__name__,
                "v": {f.name: encode(getattr(value, f.name), root, portable) for f in dataclasses.fields(value)}}
    if isinstance(value, BaseException):
        return {"$": "error", "t": type(value).__name__, "v": str(value)}
    raise TypeError(f"can't send a {type(value).__name__}")


def decode(value, root: Path | None = None):
    if isinstance(value, list):
        return [decode(v, root) for v in value]
    if not isinstance(value, dict):
        return value
    tag = value.get("$")
    if tag is None:
        return {k: decode(v, root) for k, v in value.items()}
    if tag == "dict":
        return {_hashable(decode(k, root)): decode(v, root) for k, v in value["items"]}
    if tag == "marker":
        fields = {k: v for k, v in value.items() if k not in ("$", "kind")}
        return MARKERS[value["kind"]](**fields)
    if tag == "datetime":
        return datetime.fromisoformat(value["v"])
    if tag == "date":
        return date.fromisoformat(value["v"])
    if tag == "bytes":
        return base64.b64decode(value["v"])
    if tag == "tz":
        return ZoneInfo(value["v"])
    if tag == "data":
        relative = Path(value["v"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"bad data path {value['v']!r}")
        return (root or Path()) / relative
    if tag == "path":
        return Path(value["v"])
    if tag == "dc":
        cls = TYPES[value["t"]]
        return cls(**{k: decode(v, root) for k, v in value["v"].items()})
    if tag == "error":
        return RemoteError(value["t"], value["v"])
    raise ValueError(f"unknown tag {tag!r}")


def _hashable(key):
    return tuple(key) if isinstance(key, list) else key


class RemoteError(Exception):
    """An exception raised where the call ran (on a server), carried back by name and message."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind

    def __str__(self):
        return super().__str__() or self.kind
