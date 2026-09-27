"""The application layer: everything between the UI and Telegram that isn't a widget. No Qt here, so the same
engine runs inside the desktop app and inside `omnigram serve` on a server.

It owns: the accounts' connection details (credentials, proxy, session file); which session is busy (a session
file can't be opened by two clients); long jobs (start, stop, list, their end); saving the long ones to `jobs/`
and resuming them after a restart; and an event stream (log lines, jobs starting and ending, account changes)
that the desktop window, or a server's connected desktops, subscribe to.

Work is described as a `Call`: a telegram.py function by name plus plain-data arguments. That is what makes a job
savable (resume after a restart) and sendable (run it on a server): see wire.py. Arguments that can't be data —
the log emitter, a progress callback, the AI config — are markers (Emit, Progress, AIConfig) the engine fills in
where the job runs.
"""
import asyncio
import functools
import json
from collections import Counter
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from omnigram import ai, telegram, warmup, wire
from omnigram.settings import Settings
from omnigram.store import Account, Store

# Long jobs that survive a restart (job kind = the key's part before "/"). One-shot jobs (a broadcast, invites,
# forwarding) are not resumed: re-running them would repeat what they already did.
PERSISTED = ("warmup", "autopilot", "funnel", "listen", "online", "bot")
# Kinds that share the account's one live connection (telegram.Link), so they may run together.
SHARED = ("chat", "autopilot")

# The telegram.py functions a Call may name. A saved job file or a desktop can only run these.
OPS = frozenset({
    "check", "check_spam", "password_state", "set_password", "authorizations", "terminate_authorization",
    "terminate_other_authorizations", "get_profile", "update_profile", "dialogs", "leave", "join_requests",
    "resolve_join_requests", "dump", "schedule_post", "create_chat", "search_public", "account_stats",
    "stars_and_gifts", "listen", "send_many", "schedule_series", "post_now", "react", "comment_latest", "watch_react",
    "parse_participants", "check_numbers", "invite_users", "join", "boost", "view_stories", "forward", "clone_chat",
    "report", "warmup_run", "dialogues", "online_keeper", "funnel_watch", "run_autopilot",
})


class Emit(wire.Marker):
    """A log emitter: emit(line) shows `line` in the log, prefixed with the symbol and the account's name."""
    kind = "emit"

    def __init__(self, symbol: str = "◉"):
        self.symbol = symbol


class Progress(wire.Marker):
    """A progress callback: progress(n) saves n as argument number `arg` of the job's saved Call, so a resumed
    job continues from there (warm-up's `done`)."""
    kind = "progress"

    def __init__(self, arg: int):
        self.arg = arg


class AIConfig(wire.Marker):
    """Settings → AI where the job runs (the key never goes into a job file)."""
    kind = "ai_config"


class Managed(wire.Marker):
    """The Telegram ids of every account this app manages (Engine.managed_ids), for the autopilot's loop guard."""
    kind = "managed"


wire.MARKERS.update({m.kind: m for m in (Emit, Progress, AIConfig, Managed)})


class Busy(Exception):
    pass


class Call:
    """One telegram.py function for one account: fn(session_path, api_id, api_hash, proxy, *args, **kwargs).
    Awaiting it runs it here; the desktop sends a server account's Call to the server instead (remote.py)."""

    def __init__(self, engine: "Engine", account: Account, fn, args=(), kwargs=None):
        if isinstance(fn, functools.partial):
            fn, kwargs = fn.func, {**fn.keywords, **(kwargs or {})}
        self.engine, self.account = engine, account
        self.name = fn if isinstance(fn, str) else fn.__name__
        self.args, self.kwargs = list(args), dict(kwargs or {})

    async def coroutine(self, key: str = ""):
        return await self.engine.prepare(self, key)  # prepare's errors (no credentials) end the job, not the caller

    def __await__(self):
        return self.coroutine().__await__()

    def record(self, root: Path | None = None, portable: bool = False) -> dict:
        return {"session": self.account.session, "fn": self.name,
                "args": wire.encode(self.args, root, portable), "kwargs": wire.encode(self.kwargs, root, portable)}


@dataclass
class Job:
    key: str
    verb: str
    future: Future
    record: dict | None = None  # the saved Call, for persisted kinds
    stopping: bool = False
    keep: bool = False  # stopped to move the account elsewhere: keep its saved Call for the other side


def job_kind(key: str) -> str:
    return key.split("/", 1)[0]


def job_session(key: str) -> str:
    return key.split("/", 1)[1] if "/" in key else ""


class Engine:
    """`accounts` returns the live Account objects (the desktop's table model, or the store on a server).
    `on_main(fn)` runs fn where account objects may be changed: the GUI thread on the desktop; on a server, right
    away. Event subscribers are called on whichever thread the event happens (usually the Telethon loop)."""

    def __init__(self, root: Path, settings: Settings | None = None, accounts=None, on_main=None, local_tz=None,
                 require_proxy: bool = False):
        self.root = root
        # A server: nobody is there to confirm the own-IP warning, and a proxy-less account would connect from a
        # datacenter IP. Every connection then needs a proxy (the own-IP rule without a user present).
        self.require_proxy = require_proxy
        self.store = Store(root)
        self.settings = settings or Settings(root / "settings.json")
        self.jobs_dir = root / "jobs"
        self.jobs_dir.mkdir(exist_ok=True)
        self._accounts = accounts or self._own_accounts
        self._own: list[Account] = []
        self.on_main = on_main or (lambda fn: fn())
        self.local_tz = local_tz  # IANA name of this computer's zone (the desktop's, for jobs it starts)
        self.jobs: dict[str, Job] = {}
        self.pending: set[str] = set()  # sessions in a one-shot check right now
        self.work = Counter()  # "verb ok|failed" -> count, for the dashboard and /stats
        self.subscribers: list = []
        self.closing = False

    # ---- accounts ------------------------------------------------------------------------------------------

    def _own_accounts(self) -> list[Account]:
        return self._own

    def reload(self):
        """Server: re-read accounts from the store (the desktop's list is its table model instead)."""
        current = {a.session: a for a in self._own}
        self._own = [current.get(a.session, a) for a in self.store.load()]

    def save(self):
        self.store.save(self.accounts())

    def accounts(self) -> list[Account]:
        return self._accounts()

    def account(self, session: str) -> Account | None:
        return next((a for a in self.accounts() if a.session == session), None)

    def credentials(self, account: Account | None = None) -> tuple[int, str] | None:
        """api_id/api_hash: the account's own (session JSON / number login), else Settings, else any imported
        account's; None when there are none at all."""
        if account and account.api_id and account.api_hash:
            return account.api_id, account.api_hash
        api_id, api_hash = self.settings.get("api_id"), self.settings.get("api_hash")
        if api_id and api_hash:
            return int(api_id), str(api_hash)
        for a in self.accounts():
            if a.api_id and a.api_hash:
                return a.api_id, a.api_hash
        return None

    def ai_config(self) -> ai.ProviderConfig:
        s = self.settings
        return ai.ProviderConfig(str(s.get("ai_provider", "openrouter")), str(s.get("ai_base_url")),
                                 str(s.get("ai_key")), str(s.get("ai_model") or ai.DEFAULT_MODEL),
                                 str(s.get("jev_key")), str(s.get("jev_model") or "jev-latest"),
                                 str(s.get("jev_via", "openrouter")))

    def ai_store_path(self, account: Account) -> Path:
        return self.root / "ai" / f"{account.session}.json"

    def managed_ids(self) -> set[int]:
        """Telegram ids of every account this app manages: its own (filled by Check) plus, on a server, the
        desktop's that it was told about (setting `managed_ids`). The autopilot never answers them."""
        return {a.user_id for a in self.accounts() if a.user_id} | {int(i) for i in self.settings.get("managed_ids", [])}

    def group_auto_refusal(self, session: str, chat_id: int, admin: bool) -> str:
        """Why `session` may not switch Auto on in group `chat_id` ('' = it may). Several managed accounts may
        answer on their own in the same group only where one of them runs it (creator or admin; recorded when
        Auto was switched on). Elsewhere one is the limit: more would look like independent people talking,
        which is manufactured engagement. Draft is never limited: the owner sends every message."""
        if chat_id >= 0 or admin:
            return ""
        others = False
        for other in self.accounts():
            if other.session == session:
                continue
            entry = ai.ProfileStore.load(self.ai_store_path(other)).chats.get(str(chat_id), {})
            if entry.get("profile", {}).get("mode") == "auto":
                if entry.get("admin"):
                    return ""
                others = True
        return ("Another of your accounts already answers in this group. Use Draft to reply from this one "
                "yourself." if others else "")

    def zone(self, account: Account):
        """The account's time zone for active hours: its proxy's exit-IP zone, else the zone of the computer that
        runs the engine (on a server, the desktop's zone it was told; never the server's own, usually UTC)."""
        zones = {p.url: p.tz for p in self.store.load_proxies() if p.tz}
        return (warmup.zone(zones.get(account.proxy, ""))
                or warmup.zone(self.local_tz or str(self.settings.get("desktop_tz"))))

    def check_proxy(self, account: Account):
        if self.require_proxy and not account.proxy:
            raise ValueError(f"{account.name or account.session} has no proxy: accounts on a server need one "
                             "(without it Telegram would see the server's IP)")

    # ---- an account's files (moving it between a desktop and a server) -------------------------------------

    def account_files(self, session: str) -> dict[str, bytes]:
        """Everything that belongs to one account, by data-relative path: its session, AI profile, saved jobs, and
        the funnel files those jobs read."""
        paths = [f"sessions/{session}.session", f"ai/{session}.json"]
        for job in self.jobs_dir.glob(f"*__{session}.json"):
            paths.append(f"jobs/{job.name}")
            record = json.loads(job.read_text("utf-8"))
            paths += [v["v"] for v in _data_paths(record.get("args", []))]
        return {rel: (self.root / rel).read_bytes() for rel in dict.fromkeys(paths) if (self.root / rel).is_file()}

    def receive_files(self, session: str, files: dict[str, bytes]):
        """Write an account's files (account_files from the other side). Only its own kinds of file, by name."""
        for rel, data in files.items():
            (self.root / account_file_path(session, rel)).parent.mkdir(parents=True, exist_ok=True)
            (self.root / account_file_path(session, rel)).write_bytes(data)

    def remove_files(self, session: str):
        for rel in self.account_files(session):
            if not rel.startswith("funnels/"):  # a funnel may be shared by several accounts
                (self.root / rel).unlink(missing_ok=True)

    # ---- events --------------------------------------------------------------------------------------------

    def subscribe(self, callback):
        self.subscribers.append(callback)
        return lambda: self.subscribers.remove(callback)

    def publish(self, kind: str, payload: dict):
        for callback in list(self.subscribers):
            callback(kind, payload)

    def log(self, line: str, session: str = ""):
        self.publish("log", {"session": session, "line": line})

    def emitter(self, account: Account | None, symbol: str = "◉"):
        name = (account.name or account.session) if account else ""
        prefix = f"{symbol} [{name}] " if name else f"{symbol} "
        session = account.session if account else ""
        return lambda line: self.log(prefix + line, session)

    # ---- calls ---------------------------------------------------------------------------------------------

    def call(self, account: Account, fn, *args, **kwargs) -> Call:
        return Call(self, account, fn, args, kwargs)

    def prepare(self, call: Call, key: str = ""):
        """The coroutine for a Call, with its markers filled in. Raises when the account can't connect here."""
        if call.name not in OPS:
            raise ValueError(f"unknown operation {call.name!r}")
        account = call.account
        self.check_proxy(account)
        credentials = self.credentials(account)
        if credentials is None:
            raise ValueError("no api_id/api_hash: set them in Settings or import a session with its JSON")

        def fill(value):
            if isinstance(value, Emit):
                return self.emitter(account, value.symbol)
            if isinstance(value, Progress):
                return lambda n, arg=value.arg: self._progress(key, arg, n)
            if isinstance(value, AIConfig):
                return self.ai_config()
            if isinstance(value, Managed):
                return self.managed_ids
            return value

        args = [fill(a) for a in call.args]
        kwargs = {k: fill(v) for k, v in call.kwargs.items()}
        fn = getattr(telegram, call.name)
        return fn(self.store.path(account), *credentials, account.proxy, *args, **kwargs)

    def _progress(self, key: str, arg: int, value):
        job = self.jobs.get(key)
        if job is None or job.stopping or job.record is None:
            return  # a stopped run can still finish its current step: don't recreate its file
        job.record["args"][arg] = wire.encode(value, self.root)
        self._save_record(job)

    # ---- jobs ----------------------------------------------------------------------------------------------

    def busy(self, session: str, own: str = "") -> str:
        """Why `session` can't open another client now ('' = free): a check, or a job other than kind `own` (the
        job whose dialog is opening; it must still open, to show Stop). Chat and autopilot share a connection."""
        if session in self.pending:
            return "being checked"
        for key, job in self.jobs.items():
            if job_session(key) != session or key == f"{own}/{session}":
                continue
            if own in SHARED and job_kind(key) in SHARED:
                continue
            return job.verb
        return ""

    def start(self, key: str, work, verb: str, persist: bool | None = None, record: dict | None = None) -> Future:
        """Run `work` (a Call, or any coroutine) as job `key` ('kind/session'). Its end is published as job_ended.
        Persisted kinds (PERSISTED) are saved to jobs/ until they end, and resumed by resume()."""
        if key in self.jobs:
            raise Busy(f"{self.jobs[key].verb} is already running")
        if persist is None:
            persist = isinstance(work, Call) and job_kind(key) in PERSISTED
        if persist and record is None:
            record = work.record(self.root)
        coroutine = work.coroutine(key) if isinstance(work, Call) else work
        job = Job(key, verb, None, {"key": key, "verb": verb, **record} if persist else None)
        self.jobs[key] = job
        if job.record:
            self._save_record(job)
        job.future = asyncio.run_coroutine_threadsafe(coroutine, telegram.LOOP)
        job.future.add_done_callback(lambda f: self._ended(job))
        self.publish("job_started", {"key": key, "verb": verb})
        return job.future

    def stop(self, key: str) -> bool:
        job = self.jobs.get(key)
        if job is None:
            return False
        job.stopping = True
        job.future.cancel()
        return True

    def running(self, key: str) -> bool:
        return key in self.jobs

    def _ended(self, job: Job):
        if self.jobs.get(job.key) is job:
            del self.jobs[job.key]
        future = job.future
        if future.cancelled():
            outcome, detail = "stopped", ""
        elif future.exception() is not None:
            error = future.exception()
            outcome, detail = "failed", f"{type(error).__name__}: {error}"
        else:
            outcome, detail = "finished", "" if future.result() is None else str(future.result())
        if job.record and not ((self.closing or job.keep) and outcome == "stopped"):  # resumed next time/there
            self._record_path(job.key).unlink(missing_ok=True)
        self.publish("job_ended", {"key": job.key, "verb": job.verb, "outcome": outcome, "detail": detail})

    def _record_path(self, key: str) -> Path:
        return self.jobs_dir / (key.replace("/", "__") + ".json")

    def _save_record(self, job: Job):
        path = self._record_path(job.key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job.record, ensure_ascii=False), "utf-8")
        tmp.replace(path)

    def job_list(self) -> list[dict]:
        return [{"key": key, "verb": job.verb} for key, job in self.jobs.items()]

    def shutdown(self):
        """Stop everything but keep the saved jobs, so the next start resumes them."""
        self.closing = True
        for job in list(self.jobs.values()):
            job.future.cancel()

    # ---- resume after a restart ----------------------------------------------------------------------------

    def migrate_warmups(self):
        """Warm-ups used to be saved as warmup/<session>.json; they are jobs like the others now."""
        for path in sorted(self.store.warmup.glob("*.json")):
            try:
                actions, targets, done = warmup.load(path)
            except (OSError, ValueError, KeyError, TypeError):
                path.unlink(missing_ok=True)
                continue
            account = self.account(path.stem) or Account(path.stem)
            call = Call(self, account, "warmup_run", [actions, targets, Emit(), done, Progress(3)])
            key = f"warmup/{path.stem}"
            record = {"key": key, "verb": f"warm-up [{account.name or account.session}]", **call.record(self.root)}
            self._record_path(key).write_text(json.dumps(record, ensure_ascii=False), "utf-8")
            path.unlink()

    def saved_jobs(self) -> list[dict]:
        records = []
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text("utf-8")))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
        return records

    def resume(self) -> int:
        """Start every saved job again. Nobody may be at the screen, so a proxy-less account is skipped with a log
        line (the own-IP rule) and its job stays saved for when it gets a proxy."""
        self.migrate_warmups()
        resumed = 0
        for record in self.saved_jobs():
            key = record["key"]
            if key in self.jobs:
                continue
            if record["fn"] == "bot":
                resumed += self._resume_bot(record)
                continue
            account = self.account(record["session"])
            if account is None:  # the session is gone
                self._record_path(key).unlink(missing_ok=True)
                continue
            if not self.may_resume(account):
                continue
            if not account.proxy:
                self.log(f"◉ [{account.name or account.session}] {record['verb']} not resumed: no proxy "
                         f"(assign one and restart, or start it again)", account.session)
                continue
            try:
                call = Call(self, account, record["fn"], wire.decode(record["args"], self.root),
                            wire.decode(record["kwargs"], self.root))
                self.start(key, call, record["verb"], persist=True,
                           record={k: record[k] for k in ("session", "fn", "args", "kwargs")})
                resumed += 1
            except Exception as e:
                self.log(f"✗ {record['verb']} not resumed: {type(e).__name__}: {e}", account.session)
        if resumed:
            self.log(f"→ resumed {resumed} job(s)")
        return resumed

    def may_resume(self, account: Account) -> bool:
        """Overridden by the desktop: a server account's jobs run on the server, not here."""
        return getattr(account, "placement", "local") == "local"

    # ---- the status bot ------------------------------------------------------------------------------------

    def bot_ready(self) -> str:
        """'' when the bot can start, else why not."""
        if self.credentials() is None:
            return "no api_id/api_hash"
        if not self.settings.get("bot_token") or not str(self.settings.get("bot_owner")).isdigit():
            return "set the status bot token and your user id first"
        return ""

    def start_bot(self) -> Future:
        api_id, api_hash = self.credentials()
        s = self.settings
        if self.require_proxy and not s.get("bot_proxy"):
            raise ValueError("the status bot needs a proxy on a server (Settings → Status bot proxy)")
        coroutine = telegram.status_bot(api_id, api_hash, str(s.get("bot_token")), int(s.get("bot_owner")),
                                        self.bot_answer, str(s.get("bot_proxy")))
        return self.start("bot", coroutine, "status bot", persist=True,
                          record={"session": "", "fn": "bot", "args": [], "kwargs": {}})

    def _resume_bot(self, record: dict) -> int:
        if self.bot_ready():
            self._record_path(record["key"]).unlink(missing_ok=True)
            return 0
        if not self.settings.get("bot_proxy"):
            self.log("◉ status bot not resumed: no proxy (Settings → Status bot proxy)")
            return 0
        self.start_bot()
        return 1

    async def bot_answer(self, command: str) -> str:
        done = asyncio.get_running_loop().create_future()

        def compute():
            try:
                result = self.bot_command(command)
                telegram.LOOP.call_soon_threadsafe(done.set_result, result)
            except Exception as e:
                telegram.LOOP.call_soon_threadsafe(done.set_exception, e)

        self.on_main(compute)
        return await done

    def bot_command(self, command: str) -> str:
        if command == "stats":
            return self.summary_text()
        if command == "check":
            # Nobody may be at the screen to answer the own-IP warning, so proxy-less accounts are skipped.
            accounts = self.accounts()
            proxied = [a for a in accounts if a.proxy and not self.busy(a.session)
                       and getattr(a, "placement", "local") == "local"]
            self.check(proxied)
            direct = sum(not a.proxy for a in accounts)
            skipped = f" Skipped {direct} without a proxy (check those from the app)." if direct else ""
            return f"Checking {len(proxied)} account(s).{skipped} Send /stats in a minute."
        return "Omnigram status bot. Commands: /stats, /check"

    # ---- one-shot checks (the bot's /check, and a server's Check) ------------------------------------------

    def check(self, accounts: list[Account], fn: str = "check", verb: str = "checking") -> int:
        """Run `fn` on each account (bounded by telegram._LIMIT); results are applied where accounts may change
        (on_main) and published as `account` events. Returns how many started."""
        for account in accounts:
            self.pending.add(account.session)
            future = asyncio.run_coroutine_threadsafe(Call(self, account, fn).coroutine(), telegram.LOOP)
            future.add_done_callback(lambda f, a=account: self.on_main(lambda: self._checked(a, f, fn, verb)))
        return len(accounts)

    def _checked(self, account: Account, future: Future, fn: str, verb: str):
        self.pending.discard(account.session)
        try:
            result = future.result()
        except Exception as e:
            result = e
        line = apply_result(account, fn, result)
        failed = isinstance(result, Exception)
        self.work[f"{verb} {'failed' if failed else 'ok'}"] += 1
        self.log(f"{'✗' if failed else '✓'} [{account.name or account.session}] {line}", account.session)
        self.publish("account", {"account": account})

    # ---- summary (dashboard, /stats) -----------------------------------------------------------------------

    def summary(self, accounts: list[Account] | None = None) -> list[tuple[str, list[tuple[str, object]]]]:
        accounts = self.accounts() if accounts is None else accounts
        breakdown = lambda values: Counter(values).most_common()
        kinds = Counter(job_kind(key) for key in self.jobs)
        return [
            ("Status", breakdown(a.status for a in accounts)),
            ("Spam", breakdown(a.spam or "unchecked" for a in accounts)),
            ("Geo", breakdown(a.geo or "unknown" for a in accounts)),
            ("Folder", breakdown(a.folder or "none" for a in accounts)),
            ("Proxy", breakdown("with proxy" if a.proxy else "without proxy" for a in accounts)),
            ("Work this session", sorted(self.work.items()) + [
                ("listeners running", kinds["listen"]),
                ("long jobs running", len(self.jobs) - kinds["listen"] - kinds["bot"]),
                ("status bot", "on" if "bot" in self.jobs else "off")]),
        ]

    def summary_text(self, accounts: list[Account] | None = None) -> str:
        accounts = self.accounts() if accounts is None else accounts
        return f"{len(accounts)} account(s)\n\n" + "\n\n".join(
            title + "\n" + "\n".join(f"  {label}: {value}" for label, value in rows)
            for title, rows in self.summary(accounts))


def _data_paths(value):
    """The {"$": "data"} paths inside an encoded value (a saved job's arguments)."""
    if isinstance(value, list):
        for v in value:
            yield from _data_paths(v)
    elif isinstance(value, dict):
        if value.get("$") == "data":
            yield value
        for v in value.values():
            yield from _data_paths(v)


def account_file_path(session: str, rel: str) -> str:
    """`rel` if it is one of `session`'s own files (see Engine.account_files); raises otherwise, so a peer can't
    write anywhere else in the data folder."""
    parts = rel.split("/")
    ok = (len(parts) == 2 and ".." not in parts and "\\" not in rel and (
        rel in (f"sessions/{session}.session", f"ai/{session}.json")
        or (parts[0] == "jobs" and parts[1].endswith(f"__{session}.json"))
        or (parts[0] == "funnels" and parts[1].endswith(".json") and not parts[1].startswith("."))))
    if not ok:
        raise ValueError(f"not a file of account {session}: {rel!r}")
    return rel


def apply_result(account: Account, fn: str, result) -> str:
    """Fold a check / spam check result (or its exception) into the account; returns the log line."""
    if fn == "check_spam":
        if isinstance(result, Exception):
            return f"{type(result).__name__}: {result}"
        account.spam = result["spam"]
        return f"{result['spam']} — {result.get('spam_detail', '')}"
    if isinstance(result, telegram.NotAuthorized) or getattr(result, "kind", "") == "NotAuthorized":
        account.status = "dead"
        return "not authorized"
    if isinstance(result, Exception):
        account.status = "error"
        return f"{type(result).__name__}: {result}"
    for key, value in result.items():
        setattr(account, key, value)
    return f"→ {account.status}"
