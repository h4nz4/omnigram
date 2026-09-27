"""`omnigram serve` in-process: the API and its event socket, with Telegram faked. On Windows (no Unix sockets for
asyncio) it listens on 127.0.0.1 with its token; the Unix socket's permissions are checked where it exists."""
import asyncio
import base64
import io
import json
import os
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import aiohttp
import pytest

from omnigram import ai, chat, server, telegram, wire
from omnigram.store import Account

PROXY = "socks5://h:1080"


class Api:
    def __init__(self, url, token):
        self.url, self.token = url, token

    def __call__(self, method, path, body=None, token=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data, method=method, headers={
            "Authorization": f"Bearer {self.token if token is None else token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read()
                return payload if raw else json.loads(payload)
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read() or b"{}")}

    def result(self, method, path, body=None):
        reply = self(method, path, body)
        assert "error" not in reply, reply
        return wire.decode(reply.get("result"))


class Events:
    """The /events socket, read on its own thread."""

    def __init__(self, url, token):
        self.messages, self.url, self.token = [], url, token
        self.opened = threading.Event()
        self.outbox: asyncio.Queue | None = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)
        self.thread.start()
        assert self.opened.wait(5)

    async def _run(self):
        self.outbox = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {self.token}"}) as session:
            async with session.ws_connect(self.url + "/events") as ws:
                self.opened.set()

                async def send():
                    while True:
                        await ws.send_str(json.dumps(await self.outbox.get()))

                sender = asyncio.ensure_future(send())
                async for message in ws:
                    self.messages.append(json.loads(message.data))
                sender.cancel()

    def send(self, message):
        self.loop.call_soon_threadsafe(self.outbox.put_nowait, message)

    def wait(self, predicate, timeout=5):
        for _ in range(int(timeout * 50)):
            if found := [m for m in self.messages if predicate(m)]:
                return found[0]
            threading.Event().wait(0.02)
        raise AssertionError(f"no such event; got {[m['kind'] for m in self.messages]}")


@pytest.fixture
def fakes(monkeypatch):
    seen = {"checks": 0, "online": threading.Event()}

    async def check(session, api_id, api_hash, proxy):
        seen["checks"] += 1
        if Path(session).read_bytes() == b"dead":
            return {"status": "dead"}
        return {"status": "active", "name": "Alice", "user_id": 77}

    async def online_keeper(session, api_id, api_hash, proxy, minutes, emit):
        emit(f"online for {minutes} min")
        seen["online"].set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(telegram, "check", check)
    monkeypatch.setattr(telegram, "online_keeper", online_keeper)
    return seen


def start(data: Path, stop: threading.Event):
    ready = {}
    done = threading.Event()

    def started(address, token):
        ready.update(address=address, token=token)
        done.set()

    thread = threading.Thread(target=server.serve, args=(data, None, 0, stop, started), daemon=True)
    thread.start()
    assert done.wait(10)
    return thread, Api(ready["address"], ready["token"])


@pytest.fixture
def srv(tmp_path, fakes):
    stop = threading.Event()
    data = tmp_path / "server"
    thread, api = start(data, stop)
    api.data, api.stop, api.thread = data, stop, thread
    api.settings = lambda: json.loads((data / "settings.json").read_text("utf-8"))
    api("POST", "/settings", {"values": {"api_id": "1", "api_hash": "h"}})
    yield api
    stop.set()
    thread.join(15)


def upload(api, session="acc", proxy=PROXY, content=b"session-bytes", files=None):
    account = Account(session, name="Alice", proxy=proxy, placement="server")
    files = {f"sessions/{session}.session": content, **(files or {})}
    return api("PUT", f"/accounts/{session}", {
        "account": wire.encode(account), "files": {k: base64.b64encode(v).decode() for k, v in files.items()}})


def test_the_token_is_required_and_status_answers(srv):
    assert srv("GET", "/status", token="wrong")["status"] == 401
    status = srv("GET", "/status")
    assert status["api"] == server.API and status["accounts"] == 0
    if os.name == "posix":
        assert (srv.data / "token").stat().st_mode & 0o777 == 0o600


def test_the_server_takes_only_the_settings_it_uses(srv):
    srv("POST", "/settings", {"values": {"ai_key": "sk", "favorites": ["x"], "server_host": "h", "desktop_tz": "Europe/Belgrade"}})
    settings = srv.settings()
    assert settings["ai_key"] == "sk" and settings["desktop_tz"] == "Europe/Belgrade"
    assert "favorites" not in settings and "server_host" not in settings


def test_moving_an_account_in_checks_the_session_and_keeps_it(srv, fakes):
    reply = upload(srv, files={"ai/acc.json": b'{"defaults": {"about_me": "me"}, "chats": {}}'})
    assert reply["ok"] and wire.decode(reply["result"]).status == "active"
    accounts = srv.result("GET", "/accounts")
    assert [(a.session, a.placement, a.user_id) for a in accounts] == [("acc", "local", 77)]
    assert (srv.data / "sessions" / "acc.session").read_bytes() == b"session-bytes"
    assert json.loads((srv.data / "ai" / "acc.json").read_text())["defaults"]["about_me"] == "me"
    assert "already on this server" in upload(srv)["error"]


def test_a_session_the_server_cant_open_leaves_nothing_behind(srv):
    assert "couldn't open this session" in upload(srv, content=b"dead")["error"]
    assert not (srv.data / "sessions" / "acc.session").exists()
    assert srv.result("GET", "/accounts") == []


def test_server_accounts_need_a_proxy(srv):
    assert "needs one" in upload(srv, proxy="")["error"] or "need one" in upload(srv, proxy="")["error"]


def test_files_can_only_go_where_an_account_keeps_them(srv):
    reply = upload(srv, files={"../evil.py": b"x"})
    assert "not a file of account" in reply["error"]
    assert not (srv.data.parent / "evil.py").exists()


def test_a_call_runs_on_the_server_and_its_account_learns_the_result(srv, fakes):
    upload(srv)
    assert srv.result("POST", "/call", {"session": "acc", "fn": "check"})["status"] == "active"
    assert "unknown operation" in srv("POST", "/call", {"session": "acc", "fn": "shutil_rmtree"})["error"]
    assert srv("POST", "/call", {"session": "nope", "fn": "check"})["status"] == 404


def test_jobs_run_stream_their_log_and_stop(srv, fakes):
    upload(srv)
    events = Events(srv.url, srv.token)
    assert events.wait(lambda m: m["kind"] == "snapshot")["jobs"] == []
    srv("POST", "/jobs", {"key": "online/acc", "verb": "keeping online", "session": "acc", "fn": "online_keeper",
                          "args": wire.encode([15, wire.MARKERS["emit"]()]), "kwargs": {}})
    assert fakes["online"].wait(5)
    events.wait(lambda m: m["kind"] == "job_started" and m["key"] == "online/acc")
    events.wait(lambda m: m["kind"] == "log" and m["line"] == "◉ [Alice] online for 15 min")
    assert (srv.data / "jobs" / "online__acc.json").exists()
    srv("DELETE", "/jobs/online/acc")
    ended = events.wait(lambda m: m["kind"] == "job_ended")
    assert ended["outcome"] == "stopped" and not (srv.data / "jobs" / "online__acc.json").exists()


def test_moving_an_account_back_hands_over_its_running_jobs(srv, fakes):
    upload(srv)
    srv("POST", "/jobs", {"key": "online/acc", "verb": "keeping online", "session": "acc", "fn": "online_keeper",
                          "args": wire.encode([15, wire.MARKERS["emit"]()]), "kwargs": {}})
    assert fakes["online"].wait(5)
    reply = srv("POST", "/accounts/acc/release")
    files = {k: base64.b64decode(v) for k, v in reply["files"].items()}
    assert set(files) == {"sessions/acc.session", "jobs/online__acc.json"}  # the saved job travels back
    assert srv("GET", "/status")["jobs"] == [] and srv.result("GET", "/accounts") == []
    assert srv("POST", "/call", {"session": "acc", "fn": "check"})["status"] == 404  # never connected again
    srv("DELETE", "/accounts/acc")
    assert not (srv.data / "sessions" / "acc.session").exists() and not list((srv.data / "jobs").iterdir())


def test_a_restart_resumes_the_servers_jobs(srv, fakes, tmp_path):
    upload(srv)
    srv("POST", "/jobs", {"key": "online/acc", "verb": "keeping online", "session": "acc", "fn": "online_keeper",
                          "args": wire.encode([15, wire.MARKERS["emit"]()]), "kwargs": {}})
    assert fakes["online"].wait(5)
    srv.stop.set()
    srv.thread.join(15)
    assert (srv.data / "jobs" / "online__acc.json").exists()  # shutting down keeps it
    fakes["online"].clear()
    stop = threading.Event()
    thread, api = start(srv.data, stop)
    try:
        assert fakes["online"].wait(5)
        assert [j["key"] for j in api("GET", "/status")["jobs"]] == ["online/acc"]
    finally:
        stop.set()
        thread.join(15)
    srv.stop = threading.Event()  # the fixture's teardown: already stopped


def test_profile_edits_merge_and_are_announced(srv):
    upload(srv)
    events = Events(srv.url, srv.token)
    path = srv.data / "ai" / "acc.json"
    ai.ProfileStore.update(path, lambda s: s.set_state(5, ai.ChatState(in_row=3)))  # the autopilot wrote this
    srv("PATCH", "/ai/acc", {"defaults": {"about_me": "new"}, "chats": {"5": {"profile": {"mode": "auto"}}}})
    store = ai.ProfileStore.load(path)
    assert store.defaults == {"about_me": "new"} and store.profile(5).mode == "auto" and store.state(5).in_row == 3
    announced = events.wait(lambda m: m["kind"] == "ai" and m["data"]["defaults"] == {"about_me": "new"})
    assert announced["session"] == "acc"


def test_a_backup_holds_sessions_profiles_and_jobs(srv):
    upload(srv, files={"ai/acc.json": b'{"defaults": {}, "chats": {}}'})
    raw = srv("GET", "/backup", raw=True)
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    assert set(names) == {"accounts.json", "acc.session", "data/ai/acc.json"}
    assert not list((srv.data / "tmp").iterdir())


class FakeChatClient:
    def __init__(self, session, api_id, api_hash, proxy, on_event):
        self.on_event, self.client, self.self_id = on_event, None, 1
        self._ready = asyncio.Event()

    async def run(self):
        self.client = object()
        self._ready.set()
        await asyncio.sleep(3600)

    async def ready(self):
        await self._ready.wait()

    async def chat_titles(self, limit=200):
        return {}

    async def dialogs(self, more=False, limit=100):
        self.on_event("message", chat.Msg(9, 5, False, None, "live"))
        return [chat.Chat(5, "Bob", "user")], False


def test_a_desktop_chat_window_runs_on_the_servers_connection(srv, monkeypatch):
    monkeypatch.setattr(telegram, "ChatClient", FakeChatClient)
    upload(srv)
    events = Events(srv.url, srv.token)
    events.send({"op": "chat_open", "session": "acc"})
    events.wait(lambda m: m["kind"] == "chat_ready")
    chats, more = srv.result("POST", "/chat/acc/dialogs", {"args": [], "kwargs": {}})
    assert chats[0].title == "Bob" and more is False
    live = events.wait(lambda m: m["kind"] == "chat" and m["event"] == "message")
    assert wire.decode(live["payload"]).text == "live"
    events.send({"op": "chat_close", "session": "acc"})
    for _ in range(100):
        if str(srv.data / "sessions" / "acc.session") not in telegram.LINKS:
            break
        threading.Event().wait(0.02)
    assert str(srv.data / "sessions" / "acc.session") not in telegram.LINKS  # the connection let go


@pytest.mark.skipif(os.name != "posix", reason="Unix sockets")
def test_the_unix_socket_is_owner_only(tmp_path, fakes):
    stop, done = threading.Event(), threading.Event()
    thread = threading.Thread(target=server.serve, args=(tmp_path / "d", None, None, stop,
                                                         lambda a, t: done.set()), daemon=True)
    thread.start()
    assert done.wait(10)
    try:
        sock = tmp_path / "d" / "omnigram.sock"
        assert sock.stat().st_mode & 0o777 == 0o600 and (tmp_path / "d").stat().st_mode & 0o777 == 0o700
    finally:
        stop.set()
        thread.join(15)


def test_two_desktops_see_the_same_jobs(srv, fakes):
    upload(srv)
    laptop, desktop = Events(srv.url, srv.token), Events(srv.url, srv.token)
    srv("POST", "/jobs", {"key": "online/acc", "verb": "keeping online", "session": "acc", "fn": "online_keeper",
                          "args": wire.encode([15, wire.MARKERS["emit"]()]), "kwargs": {}})
    desktop.wait(lambda m: m["kind"] == "job_started")
    srv("DELETE", "/jobs/online/acc")  # the laptop stops it
    assert desktop.wait(lambda m: m["kind"] == "job_ended")["outcome"] == "stopped"
    assert laptop.wait(lambda m: m["kind"] == "job_ended")["outcome"] == "stopped"
    late = Events(srv.url, srv.token)
    assert late.wait(lambda m: m["kind"] == "snapshot")["jobs"] == []
