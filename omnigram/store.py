"""An account is a Telethon .session file plus a row in accounts.json holding what the UI shows."""
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import phonenumbers


@dataclass
class Account:
    session: str  # file stem in Store.sessions
    name: str = ""
    username: str = ""
    phone: str = ""
    status: str = "unknown"  # unknown | active | dead | error
    spam: str = ""  # "" (unchecked) | clean | limited; set by a spam checker once one exists
    proxy: str = ""  # scheme://[user:pass@]host:port, see telegram.parse_proxy
    folder: str = ""
    roles: str = ""
    api_id: int = 0  # the app this session was created with (session JSON / number login); 0 = use Settings
    api_hash: str = ""

    @property
    def geo(self) -> str:
        """Region of the phone's calling code. ponytail: shared codes (+1, +7) map to their main region (US, RU)."""
        try:
            region = phonenumbers.region_code_for_country_code(phonenumbers.parse("+" + self.phone).country_code)
        except phonenumbers.NumberParseException:
            return ""
        return "" if region == "ZZ" else region


_STORED = {f.name for f in fields(Account)} - {"session"}


@dataclass
class Proxy:
    """A pool entry. Accounts refer to it by `url` (Account.proxy), so the pool is just a catalogue."""
    url: str  # scheme://[user:pass@]host:port, see proxies.normalize
    name: str = ""
    ping: int | None = None  # ms; None = not tested, -1 = failed
    geo: str = ""  # exit IP country code


def _write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")  # write-then-rename so a crash never truncates the file
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


class Store:
    def __init__(self, root: Path):
        self.sessions, self.trash, self.meta = root / "sessions", root / "trash", root / "accounts.json"
        self.proxies = root / "proxies.json"
        self.templates = root / "templates.json"  # named messages, see template_store.TemplateStore
        self.funnels = root / "funnels"  # one <name>.json per funnel, see funnel.load/save
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.trash.mkdir(exist_ok=True)
        self.funnels.mkdir(exist_ok=True)

    def path(self, account: Account) -> Path:
        return self.sessions / f"{account.session}.session"

    def load(self) -> list[Account]:
        meta = json.loads(self.meta.read_text("utf-8")) if self.meta.exists() else {}
        return [
            Account(p.stem, **{k: v for k, v in meta.get(p.stem, {}).items() if k in _STORED})
            for p in sorted(self.sessions.glob("*.session"))
        ]

    def save(self, accounts: list[Account]):
        _write_json(self.meta, {a.session: {k: v for k, v in asdict(a).items() if k in _STORED} for a in accounts})

    def load_proxies(self) -> list[Proxy]:
        return [Proxy(**row) for row in json.loads(self.proxies.read_text("utf-8"))] if self.proxies.exists() else []

    def save_proxies(self, pool: list[Proxy]):
        _write_json(self.proxies, [asdict(p) for p in pool])

    def move_to_trash(self, account: Account):
        self.path(account).replace(self.trash / f"{account.session}.session")
