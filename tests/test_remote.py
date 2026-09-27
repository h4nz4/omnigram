"""The desktop's side of a server (remote.py) against a real in-process server (server.py) — only the SSH tunnel
is left out: the client talks to the server's 127.0.0.1 port directly. Telegram is faked on both sides."""
import asyncio
import json
import threading
from pathlib import Path

import aiohttp
import pytest

from omnigram import ai, chat, remote, telegram, wire
from omnigram.engine import Emit, Engine
from omnigram.store import Account

from test_server import FakeChatClient, start  # noqa: F401 - the server helpers

PROXY = "socks5://h:1080"


def on_loop(coro, timeout=15):
    return asyncio.run_coroutine_threadsafe(coro, telegram.LOOP).result(timeout)


def wait_for(condition, timeout=5.0):
    for _ in range(int(timeout * 50)):
        if condition():
            return
        threading.Event().wait(0.02)
    raise AssertionError("timed out")


@pytest.fixture
def fakes(monkeypatch):
    seen = {"online": [], "uploads": []}

    async def check(session, api_id, api_hash, proxy):
        return {"status": "active", "name": "Alice", "user_id": 77}

    async def online_keeper(session, api_id, api_hash, proxy, minutes, emit):
        seen["online"].append(Path(session).parent.parent.name)  # which data folder ran it: desktop or server
        emit(f"online for {minutes} min")
        await asyncio.sleep(3600)

    class ChatClient(FakeChatClient):
        async def send_file(self, chat_id, path, caption="", compress=True, reply_to=None):
            seen["uploads"].append((Path(path).name, Path(path).read_bytes(), caption))
            return chat.Msg(50, chat_id, True, None, caption)

        async def download(self, chat_id, msg_id, folder):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "photo.jpg").write_bytes(b"jpeg" * 1000)
            return str(folder / "photo.jpg")

    monkeypatch.setattr(telegram, "check", check)
    monkeypatch.setattr(telegram, "online_keeper", online_keeper)
    monkeypatch.setattr(telegram, "ChatClient", ChatClient)
    return seen


@pytest.fixture
def pair(tmp_path, fakes):
    """(desktop engine, its Remote, the server's Api) — connected."""
    stop = threading.Event()
    thread, api = start(tmp_path / "server", stop)
    api.data = tmp_path / "server"
    api("POST", "/settings", {"values": {"api_id": "1", "api_hash": "h"}})
    accounts = [Account("acc", name="Alice", proxy=PROXY, api_id=1, api_hash="h")]
    desktop = tmp_path / "desktop"
    (desktop / "sessions").mkdir(parents=True)
    (desktop / "sessions" / "acc.session").write_bytes(b"session-bytes")
    engine = Engine(desktop, accounts=lambda: accounts, local_tz="Europe/Belgrade")
    events = []
    client = remote.Remote(engine, lambda kind, payload: events.append((kind, payload)))
    client.events = events

    async def attach():
        client._http = aiohttp.ClientSession(headers={"Authorization": f"Bearer {api.token}"})
        client._url = api.url
        client._ws = await client._http.ws_connect(api.url + "/events")
        client._listener = asyncio.ensure_future(client._listen())

    on_loop(attach())
    wait_for(lambda: client.state == "online")
    yield engine, client, api
    on_loop(client._close())
    ai.WATCHERS.remove(client.on_profile_saved)
    for job in list(engine.jobs.values()):
        job.future.cancel()
    stop.set()
    thread.join(15)


def move_in(engine, client):
    account = engine.account("acc")
    on_loop(engine.release("acc"))
    on_loop(client.move_in(account, engine.account_files("acc")))
    account.placement = "server"
    for rel in engine.account_files("acc"):
        if rel.startswith("jobs/"):
            (engine.root / rel).unlink()


def test_the_desktop_learns_the_servers_state(pair):
    engine, client, api = pair
    assert client.state == "online" and client.jobs == {} and client.accounts == {}
    wait_for(lambda: json.loads((api.data / "settings.json").read_text("utf-8")).get("desktop_tz"))
    pushed = json.loads((api.data / "settings.json").read_text("utf-8"))
    assert pushed["desktop_tz"] == "Europe/Belgrade" and pushed["api_id"] == "1"


def test_a_running_job_moves_to_the_server_and_continues_there(pair, fakes):
    engine, client, api = pair
    engine.start("online/acc", engine.call(engine.account("acc"), "online_keeper", 30, Emit()), "keeping online")
    wait_for(lambda: fakes["online"] == ["desktop"])
    move_in(engine, client)
    wait_for(lambda: fakes["online"] == ["desktop", "server"])  # resumed on the server from its saved job
    wait_for(lambda: "online/acc" in client.jobs)
    assert not engine.jobs and not list(engine.jobs_dir.iterdir())  # nothing runs here any more
    assert (engine.root / "sessions" / "acc.session").exists()  # the locked backup
    assert client.busy("acc") == "keeping online" and client.busy("acc", own="online") == ""
    wait_for(lambda: any(kind == "log" and "☁ ◉ [Alice] online for 30 min" in p["line"] for kind, p in client.events))


def test_calls_and_jobs_for_a_server_account_run_there(pair, fakes):
    engine, client, api = pair
    move_in(engine, client)
    account = engine.account("acc")
    assert on_loop(_await(client.call(account, "check")))["user_id"] == 77
    on_loop(client.start_job("online/acc", client.call(account, "online_keeper", 5, Emit()), "keeping online"))
    wait_for(lambda: "online/acc" in client.jobs)
    on_loop(client.stop_job("online/acc"))
    wait_for(lambda: "online/acc" not in client.jobs)
    ended = [p for kind, p in client.events if kind == "job_ended"]
    assert ended[-1]["outcome"] == "stopped"


async def _await(awaitable):
    return await awaitable


def test_a_folder_on_this_computer_isnt_sent(pair):
    engine, client, api = pair
    move_in(engine, client)
    with pytest.raises(wire.NotPortable):
        client.call(engine.account("acc"), "dump", [1], Path("C:/Users/me/dumps")).body()


def test_ai_settings_edited_here_merge_into_the_server_and_back(pair):
    engine, client, api = pair
    move_in(engine, client)
    wait_for(lambda: "acc" in client.mirrors or client.state == "online")
    local = engine.root / "ai" / "acc.json"
    server_file = api.data / "ai" / "acc.json"
    ai.ProfileStore.update(local, lambda s: s.set_override(5, {"mode": "auto"}))  # the chat window, here
    wait_for(lambda: server_file.exists() and ai.ProfileStore.load(server_file).profile(5).mode == "auto")
    # the server's autopilot writes a state; the desktop's mirror follows
    ai.ProfileStore.update(server_file, lambda s: s.set_state(5, ai.ChatState(in_row=2)))
    wait_for(lambda: ai.ProfileStore.load(local).state(5).in_row == 2)
    ai.ProfileStore.update(local, lambda s: setattr(s, "defaults", {"about_me": "me"}))
    wait_for(lambda: ai.ProfileStore.load(server_file).defaults == {"about_me": "me"})
    assert ai.ProfileStore.load(server_file).state(5).in_row == 2  # the desktop's edit didn't undo it


def test_moving_back_brings_the_session_and_jobs_home(pair, fakes):
    engine, client, api = pair
    move_in(engine, client)
    on_loop(client.start_job("online/acc", client.call(engine.account("acc"), "online_keeper", 5, Emit()),
                             "keeping online"))
    wait_for(lambda: fakes["online"][-1:] == ["server"])
    (api.data / "sessions" / "acc.session").write_bytes(b"updated-by-telegram")
    _copy, files = on_loop(client.release("acc"))
    engine.receive_files("acc", files)
    on_loop(client.remove("acc"))
    engine.account("acc").placement = "local"
    assert (engine.root / "sessions" / "acc.session").read_bytes() == b"updated-by-telegram"
    assert engine.resume() == 1
    wait_for(lambda: fakes["online"][-1:] == ["desktop"])
    assert not (api.data / "sessions" / "acc.session").exists()


def test_the_chat_window_works_through_the_server(pair, fakes, tmp_path):
    engine, client, api = pair
    move_in(engine, client)
    seen = []
    chats = client.chat_client(engine.account("acc"), lambda kind, payload: seen.append((kind, payload)))

    async def scenario():
        window = asyncio.ensure_future(chats.run())
        await chats.ready()
        page, more = await chats.dialogs()
        photo = tmp_path / "pic.png"
        photo.write_bytes(b"x" * 200_000)
        sent = await chats.send_file(5, str(photo), "look", compress=True)
        saved = await chats.download(5, 9, tmp_path / "downloads")
        window.cancel()
        await asyncio.gather(window, return_exceptions=True)
        return page, sent, saved

    page, sent, saved = on_loop(scenario())
    assert page[0].title == "Bob" and sent.text == "look"
    assert fakes["uploads"] == [("pic.png", b"x" * 200_000, "look")]
    assert Path(saved).read_bytes() == b"jpeg" * 1000 and not list((api.data / "tmp").iterdir())
    wait_for(lambda: any(kind == "message" for kind, _ in seen))  # the server's live event reached the window
    assert any(kind == "progress" and payload[0] == "Uploading pic.png" for kind, payload in seen)


def test_the_installer_stops_before_changing_anything_without_systemd():
    class FakeSSH:
        config = remote.ServerConfig("h", 22, "u", "k")

        def __init__(self, facts):
            self.facts, self.scripts = facts, []

        async def run(self, script, stdin=None, timeout=300):
            self.scripts.append(script)
            return self.facts if "uname" in script else ""

        async def copy(self, local, remote_path):
            self.scripts.append(f"scp {Path(local).name} {remote_path}")

    ssh = FakeSSH("/home/u\nLinux x86_64\nnone\n")
    with pytest.raises(remote.RemoteError, match="systemd is required"):
        asyncio.run(remote.install(ssh, Path("omnigram-1.0-py3-none-any.whl"), lambda text: None))
    assert len(ssh.scripts) == 1

    ssh, steps = FakeSSH("/home/u\nLinux x86_64\nsystemd\n"), []
    socket = asyncio.run(remote.install(ssh, Path("omnigram-1.0-py3-none-any.whl"), steps.append))
    assert socket == "/home/u/.local/share/omnigram/omnigram.sock"
    assert any("tool install --force" in s and "omnigram-1.0-py3-none-any.whl" in s for s in ssh.scripts)
    assert any("enable-linger" in s for s in ssh.scripts) and "systemctl --user restart omnigram" in ssh.scripts[-1]


def test_ssh_trusts_only_confirmed_host_keys_and_never_asks_for_a_password(tmp_path):
    ssh = remote.SSH(remote.ServerConfig("vps.example", 2222, "me", "C:/keys/id"), tmp_path / "known_hosts")
    command = " ".join(ssh.command("true"))
    assert "BatchMode=yes" in command and "StrictHostKeyChecking=yes" in command
    assert f"UserKnownHostsFile={tmp_path / 'known_hosts'}" in command and "-p 2222 me@vps.example" in command
    assert ssh.config.known_name == "[vps.example]:2222" and not ssh.trusted()


def test_cancelling_an_upload_stops_both_legs(pair, fakes, monkeypatch, tmp_path):
    engine, client, api = pair
    move_in(engine, client)
    started, cancelled = threading.Event(), threading.Event()

    async def slow_send_file(self, chat_id, path, caption="", compress=True, reply_to=None):
        started.set()
        try:
            await asyncio.sleep(3600)  # Telegram taking its time
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(telegram.ChatClient, "send_file", slow_send_file)
    chats = client.chat_client(engine.account("acc"), lambda kind, payload: None)
    big = tmp_path / "clip.mp4"
    big.write_bytes(b"v" * 300_000)

    async def scenario():
        window = asyncio.ensure_future(chats.run())
        await chats.ready()
        upload = asyncio.ensure_future(chats.send_file(5, str(big)))
        while not started.is_set():
            await asyncio.sleep(0.02)
        upload.cancel()  # the window's Cancel
        await asyncio.gather(upload, return_exceptions=True)
        window.cancel()
        await asyncio.gather(window, return_exceptions=True)

    on_loop(scenario())
    assert cancelled.wait(5)  # the server stopped sending to Telegram
    wait_for(lambda: not list((api.data / "tmp").iterdir()))  # and dropped its copy


def test_a_server_backup_restores_here_as_local_accounts(pair, tmp_path):
    from omnigram import backup
    from omnigram.store import Store
    engine, client, api = pair
    move_in(engine, client)
    ai.ProfileStore.update(api.data / "ai" / "acc.json", lambda s: setattr(s, "defaults", {"about_me": "me"}))
    zip_path = tmp_path / "server-backup.zip"
    on_loop(client.download_backup(zip_path))
    fresh = Store(tmp_path / "fresh")
    imported, skipped = backup.import_backup(fresh, zip_path)
    assert imported == ["acc"] and not skipped
    restored = fresh.load()[0]
    assert restored.placement == "local" and restored.proxy == PROXY
    assert ai.ProfileStore.load(tmp_path / "fresh" / "ai" / "acc.json").defaults == {"about_me": "me"}
