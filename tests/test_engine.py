"""The engine (no Qt): settings file and migration, wire encoding, busy sessions, jobs that are saved, stopped and
resumed, and the shared per-account connection. Telegram is faked; jobs run on the real Telethon loop thread."""
import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from omnigram import ai, chat, engine, settings, telegram, warmup, wire
from omnigram.engine import AIConfig, Call, Emit, Engine, Progress
from omnigram.store import Account

telegram.start()
PROXIED = "socks5://h:1080"


def wait_for(condition, timeout=5.0):
    done = threading.Event()
    for _ in range(int(timeout * 100)):
        if condition():
            return
        done.wait(0.01)
    raise AssertionError("timed out")


@pytest.fixture
def eng(tmp_path):
    accounts = [Account("a", name="Alice", proxy=PROXIED, api_id=1, api_hash="h"), Account("b", api_id=1, api_hash="h")]
    for a in accounts:
        (tmp_path / "sessions").mkdir(exist_ok=True)
        (tmp_path / "sessions" / f"{a.session}.session").write_bytes(b"")
    e = Engine(tmp_path, accounts=lambda: accounts)
    e.events = []
    e.subscribe(lambda kind, payload: e.events.append((kind, payload)))
    yield e
    for job in list(e.jobs.values()):
        if job.future:
            job.future.cancel()


def ended(e, key):
    return [p for kind, p in e.events if kind == "job_ended" and p["key"] == key]


# ---- settings ----------------------------------------------------------------------------------------------

class FakeQSettings:
    def __init__(self, values):
        self.values = dict(values)

    def allKeys(self):
        return list(self.values)

    def value(self, key):
        return self.values[key]

    def remove(self, key):
        del self.values[key]


def test_settings_survive_a_restart_and_are_owner_only(tmp_path):
    s = settings.Settings(tmp_path / "settings.json")
    s.update({"ai_key": "sk-1", "favorites": ["chats"]})
    again = settings.Settings(tmp_path / "settings.json")
    assert again.get("ai_key") == "sk-1" and again.get("favorites") == ["chats"]
    assert again.get("missing", "default") == "default" and again.get("empty") == ""
    if os.name == "posix":
        assert (tmp_path / "settings.json").stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_migration_moves_qsettings_once_and_deletes_them_there(tmp_path):
    q = FakeQSettings({"api_id": "123", "api_hash": "abc", "ai_key": "sk", "bot_token": "t", "listen/x": "{}"})
    s = settings.Settings(tmp_path / "settings.json")
    assert settings.migrate(s, q) == 5
    assert s.get("api_id") == "123" and s.get("listen/x") == "{}"
    assert q.values == {}  # keys are not kept twice
    q.values["api_id"] = "999"  # something wrote QSettings again: settings.json exists, so nothing moves
    assert settings.migrate(settings.Settings(tmp_path / "settings.json"), q) == 0
    assert settings.Settings(tmp_path / "settings.json").get("api_id") == "123"


def test_a_fresh_install_migrates_nothing_but_never_again(tmp_path):
    s = settings.Settings(tmp_path / "settings.json")
    assert settings.migrate(s, FakeQSettings({})) == 0
    assert s.path.exists()


def test_the_server_gets_only_the_settings_it_uses(tmp_path):
    s = settings.Settings(tmp_path / "settings.json")
    s.update({"ai_model": "m", "api_id": "1", "favorites": ["x"], "server_host": "h", "listen/a": "{}"})
    assert s.for_server() == {"ai_model": "m", "api_id": "1"}


# ---- wire ---------------------------------------------------------------------------------------------------

def test_wire_round_trips_the_values_jobs_and_chats_use(tmp_path):
    when = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    value = {"msg": chat.Msg(1, -5, False, when, "hi", sender="A"), "at": when, "raw": b"\x00\xff",
             "tz": ZoneInfo("Europe/Belgrade"), "chats": (["x"], True), "ids": {1: "one"},
             "state": ai.ChatState(paused=True), "funnel": tmp_path / "funnels" / "f.json",
             "actions": [warmup.Action("read", 0, 1.5)]}
    encoded = json.loads(json.dumps(wire.encode(value, tmp_path)))
    assert encoded["funnel"] == {"$": "data", "v": "funnels/f.json"}
    back = wire.decode(encoded, tmp_path / "elsewhere")
    assert back["msg"] == value["msg"] and back["at"] == when and back["raw"] == b"\x00\xff"
    assert back["tz"].key == "Europe/Belgrade" and back["ids"] == {1: "one"} and back["state"].paused
    assert back["funnel"] == tmp_path / "elsewhere" / "funnels" / "f.json"  # found in the other side's data
    assert back["actions"] == value["actions"] and back["chats"] == [["x"], True]


def test_a_folder_on_this_computer_cant_be_sent_to_a_server(tmp_path):
    with pytest.raises(wire.NotPortable):
        wire.encode(Path("C:/Users/me/Desktop"), tmp_path / "data", portable=True)
    with pytest.raises(ValueError):
        wire.decode({"$": "data", "v": "../../etc/passwd"}, tmp_path)


def test_markers_travel_and_unknown_types_do_not(tmp_path):
    back = wire.decode(json.loads(json.dumps(wire.encode([Emit("✨"), Progress(3), AIConfig()]))))
    assert isinstance(back[0], Emit) and back[0].symbol == "✨" and back[1].arg == 3 and isinstance(back[2], AIConfig)
    with pytest.raises(KeyError):
        wire.decode({"$": "dc", "t": "Popen", "v": {}})


# ---- busy ---------------------------------------------------------------------------------------------------

def fake_job(e, key, verb="job"):
    e.jobs[key] = engine.Job(key, verb, None)


def test_a_running_job_holds_the_session(eng):
    """The bug once: only checks and listeners counted, so a second client opened a session a warm-up used."""
    fake_job(eng, "warmup/a", "warm-up")
    assert eng.busy("a") == "warm-up"
    fake_job(eng, "warmup/ab")
    assert not eng.busy("b")


def test_a_jobs_own_dialog_may_still_open_to_stop_it(eng):
    fake_job(eng, "online/a")
    assert not eng.busy("a", own="online")
    fake_job(eng, "broadcast/a", "broadcast")
    assert eng.busy("a", own="online") == "broadcast"


def test_chat_window_and_autopilot_share_the_connection(eng):
    fake_job(eng, "autopilot/a")
    assert not eng.busy("a", own="chat")
    fake_job(eng, "listen/a", "listener")
    assert eng.busy("a", own="chat") == "listener"


def test_checks_hold_the_session(eng):
    eng.pending.add("a")
    assert eng.busy("a") == "being checked"


# ---- jobs: run, save, stop, resume ---------------------------------------------------------------------------

@pytest.fixture
def fake_ops(monkeypatch):
    """Two fake telegram functions: a long runner and a warm-up that reports progress."""
    seen = {"started": threading.Event(), "calls": []}

    async def online_keeper(session, api_id, api_hash, proxy, minutes, emit):
        seen["calls"].append((Path(session).name, api_id, proxy, minutes))
        emit(f"online for {minutes} min")
        seen["started"].set()
        await asyncio.sleep(3600)

    async def warmup_run(session, api_id, api_hash, proxy, actions, targets, emit, done=0, progress=None):
        seen["calls"].append(("warmup", done))
        progress(done + 2)
        seen["started"].set()
        await asyncio.sleep(3600)

    async def check(session, api_id, api_hash, proxy):
        return {"status": "active", "user_id": 42}

    monkeypatch.setattr(telegram, "online_keeper", online_keeper)
    monkeypatch.setattr(telegram, "warmup_run", warmup_run)
    monkeypatch.setattr(telegram, "check", check)
    return seen


def test_a_long_job_is_saved_while_it_runs_and_removed_when_stopped(eng, fake_ops):
    a = eng.account("a")
    eng.start("online/a", eng.call(a, "online_keeper", 30, Emit()), "keeping online")
    assert fake_ops["started"].wait(5)
    saved = json.loads((eng.jobs_dir / "online__a.json").read_text("utf-8"))
    assert saved["fn"] == "online_keeper" and saved["session"] == "a" and saved["args"][0] == 30
    assert ("log", {"session": "a", "line": "◉ [Alice] online for 30 min"}) in eng.events
    assert eng.stop("online/a")
    wait_for(lambda: ended(eng, "online/a"))
    assert ended(eng, "online/a")[0]["outcome"] == "stopped"
    assert not (eng.jobs_dir / "online__a.json").exists()
    assert not eng.busy("a")


def test_one_shot_jobs_are_not_saved(eng, fake_ops):
    eng.start("forward/a", eng.call(eng.account("a"), "online_keeper", 1, Emit()), "forwarding")
    assert fake_ops["started"].wait(5)
    assert not list(eng.jobs_dir.glob("*.json"))


def test_shutting_down_keeps_saved_jobs_and_a_restart_resumes_them(tmp_path, eng, fake_ops):
    eng.start("online/a", eng.call(eng.account("a"), "online_keeper", 30, Emit()), "keeping online")
    assert fake_ops["started"].wait(5)
    eng.shutdown()
    wait_for(lambda: ended(eng, "online/a"))
    assert (eng.jobs_dir / "online__a.json").exists()

    fake_ops["started"].clear()
    restarted = Engine(tmp_path, accounts=eng._accounts)
    assert restarted.resume() == 1
    assert fake_ops["started"].wait(5)
    assert restarted.running("online/a") and fake_ops["calls"][-1] == ("a.session", 1, PROXIED, 30)
    restarted.stop("online/a")


def test_resume_skips_an_account_without_a_proxy_and_keeps_its_job(tmp_path, eng, fake_ops):
    record = {"key": "online/b", "verb": "keeping online", **eng.call(eng.account("b"), "online_keeper", 5,
                                                                     Emit()).record(tmp_path)}
    (eng.jobs_dir / "online__b.json").write_text(json.dumps(record), "utf-8")
    assert eng.resume() == 0
    assert not eng.running("online/b") and (eng.jobs_dir / "online__b.json").exists()
    assert any("not resumed: no proxy" in p["line"] for kind, p in eng.events if kind == "log")


def test_a_job_for_a_session_that_is_gone_is_dropped(eng):
    (eng.jobs_dir / "online__zzz.json").write_text(json.dumps(
        {"key": "online/zzz", "verb": "x", "session": "zzz", "fn": "online_keeper", "args": [], "kwargs": {}}))
    assert eng.resume() == 0 and not (eng.jobs_dir / "online__zzz.json").exists()


def test_warmup_progress_is_saved_so_a_resume_continues_from_there(tmp_path, eng, fake_ops):
    a = eng.account("a")
    eng.start("warmup/a", eng.call(a, "warmup_run", [], [], Emit(), 3, Progress(3)), "warm-up")
    assert fake_ops["started"].wait(5)
    wait_for(lambda: json.loads((eng.jobs_dir / "warmup__a.json").read_text("utf-8"))["args"][3] == 5)
    eng.shutdown()
    wait_for(lambda: ended(eng, "warmup/a"))
    fake_ops["started"].clear()
    Engine(tmp_path, accounts=eng._accounts).resume()
    assert fake_ops["started"].wait(5)
    assert fake_ops["calls"][-1] == ("warmup", 5)


def test_old_warmup_files_become_jobs(tmp_path, eng, fake_ops):
    warmup.save(eng.store.warmup / "a.json", [], ["@chan"], 4)
    assert eng.resume() == 1
    assert fake_ops["started"].wait(5)
    assert fake_ops["calls"][-1] == ("warmup", 4) and not (eng.store.warmup / "a.json").exists()
    eng.stop("warmup/a")


def test_a_call_only_runs_known_operations(eng):
    with pytest.raises(ValueError, match="unknown operation"):
        asyncio.run(eng.prepare(Call(eng, eng.account("a"), "os_system", ["rm -rf /"])))


def test_the_ai_config_comes_from_settings_where_the_job_runs(eng):
    eng.settings.update({"ai_key": "sk-x", "ai_model": ""})
    config = eng.ai_config()
    assert config.key == "sk-x" and config.model == ai.DEFAULT_MODEL and config.jev_model == "jev-latest"


def test_the_status_bot_check_skips_accounts_without_a_proxy(eng, fake_ops):
    reply = eng.bot_command("check")
    assert reply.startswith("Checking 1 account(s). Skipped 1 without a proxy")
    wait_for(lambda: eng.account("a").status == "active")
    assert eng.account("a").user_id == 42 and eng.account("b").status == "unknown"
    assert "Status" in eng.bot_command("stats")


def test_time_zone_is_the_proxys_else_the_desktops(eng):
    eng.store.save_proxies([])
    eng.local_tz = "Europe/Belgrade"
    assert eng.zone(eng.account("a")).key == "Europe/Belgrade"
    from omnigram.store import Proxy
    eng.store.save_proxies([Proxy(PROXIED, tz="Asia/Tokyo")])
    assert eng.zone(eng.account("a")).key == "Asia/Tokyo"


# ---- the shared connection ------------------------------------------------------------------------------------

class FakeChatClient:
    connections = 0

    def __init__(self, session, api_id, api_hash, proxy, on_event):
        self.on_event, self.client, self.self_id = on_event, None, 1
        self._ready = asyncio.Event()

    async def run(self):
        FakeChatClient.connections += 1
        self.client = object()
        self._ready.set()
        await asyncio.sleep(3600)

    async def ready(self):
        await self._ready.wait()

    async def chat_titles(self, limit=200):
        return {}


def test_the_chat_window_and_the_autopilot_share_one_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "ChatClient", FakeChatClient)
    FakeChatClient.connections = 0
    session = tmp_path / "a.session"
    seen = []

    async def scenario():
        handle = telegram.ChatHandle(session, 1, "h", "", lambda kind, payload: seen.append(kind))
        window = asyncio.ensure_future(handle.run())
        await handle.ready()
        autopilot = asyncio.ensure_future(telegram.run_autopilot(session, 1, "h", "", tmp_path / "ai.json",
                                                                 ai.ProviderConfig(), None, lambda line: None))
        await asyncio.sleep(0.05)
        link = telegram.LINKS[str(session)]
        assert link.holders == 2 and link.responder is not None and FakeChatClient.connections == 1
        window.cancel()  # the window closes: the autopilot keeps the connection
        await asyncio.gather(window, return_exceptions=True)
        assert telegram.LINKS[str(session)] is link and link.holders == 1
        link.publish("message", "x")
        autopilot.cancel()
        await asyncio.gather(autopilot, return_exceptions=True)
        assert str(session) not in telegram.LINKS and link.runner.done()

    asyncio.run_coroutine_threadsafe(scenario(), telegram.LOOP).result(5)
    assert seen == []  # the closed window no longer gets events


def test_the_responder_pauses_a_chat_the_owner_answered_from_another_device(tmp_path):
    store_path = tmp_path / "ai.json"
    ai.ProfileStore.update(store_path, lambda s: s.set_override(7, {"mode": "auto"}))
    published = []
    r = telegram.Responder(None, store_path, ai.ProviderConfig(), None, lambda line: None,
                           lambda kind, payload: published.append(payload))
    r.on_event("message", chat.Msg(5, 7, True, datetime.now(timezone.utc), "me"))
    assert ai.ProfileStore.load(store_path).state(7).paused
    assert published == [{"chat_id": 7, "outcome": None}]
    r.sent_by_ai.add(6)
    ai.ProfileStore.update(store_path, lambda s: s.set_state(7, ai.ChatState()))
    r.on_event("message", chat.Msg(6, 7, True, datetime.now(timezone.utc), "ai"))
    assert not ai.ProfileStore.load(store_path).state(7).paused  # its own message isn't you taking over


# ---- groups: several managed accounts ------------------------------------------------------------------------

def set_auto(e, session, chat_id, admin=False, mode="auto"):
    def change(s):
        s.set_override(chat_id, {"mode": mode})
        s.chats[str(chat_id)]["admin"] = admin
    ai.ProfileStore.update(e.ai_store_path(e.account(session)), change)


def test_one_auto_account_per_group_the_owner_doesnt_run(eng):
    assert eng.group_auto_refusal("b", -100, admin=False) == ""  # the first one may
    set_auto(eng, "a", -100)
    assert "Another of your accounts already answers" in eng.group_auto_refusal("b", -100, admin=False)
    assert "Use Draft" in eng.group_auto_refusal("b", -100, admin=False)


def test_several_auto_accounts_where_one_of_them_runs_the_group(eng):
    set_auto(eng, "a", -100)
    assert eng.group_auto_refusal("b", -100, admin=True) == ""  # b administers it
    set_auto(eng, "a", -200, admin=True)
    assert eng.group_auto_refusal("b", -200, admin=False) == ""  # a administers it


def test_drafts_and_private_chats_are_never_limited(eng):
    set_auto(eng, "a", -100, mode="draft")
    assert eng.group_auto_refusal("b", -100, admin=False) == ""
    set_auto(eng, "a", 555)
    assert eng.group_auto_refusal("b", 555, admin=False) == ""


def test_managed_ids_are_the_accounts_and_the_desktops(eng):
    eng.account("a").user_id = 11
    eng.settings.set("managed_ids", [22])
    assert eng.managed_ids() == {11, 22}


def test_managed_accounts_never_answer_each_other(tmp_path):
    store_path = tmp_path / "ai.json"
    ai.ProfileStore.update(store_path, lambda s: (s.set_override(-100, {"mode": "auto"}),
                                                  s.set_override(9, {"mode": "auto"})))
    r = telegram.Responder(None, store_path, ai.ProviderConfig(key="k"), None, lambda line: None,
                           lambda kind, payload: None, managed=lambda: {42}, owner_name="Ivan")
    poked = []
    r.poke = poked.append
    now = datetime.now(timezone.utc)
    r.on_event("message", chat.Msg(1, -100, False, now, "@ivan hi", sender_id=42, mentioned=True))
    r.on_event("message", chat.Msg(2, 9, False, now, "hi", sender_id=42))
    assert poked == []  # another managed account, in a group and in private
    r.on_event("message", chat.Msg(3, -100, False, now, "lunch?", sender_id=7))
    assert poked == []  # a group message that isn't for it: not even looked at
    r.on_event("message", chat.Msg(4, -100, False, now, "@ivan hi", sender_id=7, mentioned=True))
    r.on_event("message", chat.Msg(5, 9, False, now, "hi", sender_id=9))
    assert poked == [-100, 9]
