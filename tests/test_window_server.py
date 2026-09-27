"""The real MainWindow with a server: move an account there, run a job and its chat window through the server, see
the server's events, move it back. The server runs in-process with Telegram faked; only SSH is left out."""
import asyncio
import threading

import aiohttp
import pytest

from omnigram import ai, telegram

from test_remote import fakes, on_loop, wait_for  # noqa: F401 - fixtures and helpers
from test_server import start

PROXY = "socks5://h:1080"


def pump(qapp, condition, timeout=8.0):
    for _ in range(int(timeout * 50)):
        qapp.processEvents()
        if condition():
            return
        threading.Event().wait(0.02)
    raise AssertionError("timed out")


@pytest.fixture
def window(qapp, tmp_path, fakes, monkeypatch):  # noqa: F811 - the imported fixture
    from PySide6.QtCore import QStandardPaths
    from omnigram import window as window_module
    QStandardPaths.setTestModeEnabled(True)
    qapp.setOrganizationName("OmnigramTest")
    qapp.setApplicationName(f"win-{tmp_path.name}")
    root = __import__("pathlib").Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    (root / "sessions" / "acc.session").write_bytes(b"session-bytes")
    (root / "settings.json").write_text('{"api_id": "1", "api_hash": "h", "server_disclosed": true}', "utf-8")
    stop = threading.Event()
    thread, api = start(tmp_path / "server", stop)
    api("POST", "/settings", {"values": {"api_id": "1", "api_hash": "h"}})
    telegram.start()
    w = window_module.MainWindow()
    account = w.model.accounts[0]
    account.name, account.proxy = "Alice", PROXY
    w.changed()

    async def attach():  # what Remote._connect does after the tunnel is up
        r = w.remote
        r._http = aiohttp.ClientSession(headers={"Authorization": f"Bearer {api.token}"})
        r._url = api.url
        r._ws = await r._http.ws_connect(api.url + "/events")
        asyncio.ensure_future(r._listen())

    on_loop(attach())
    pump(qapp, lambda: w.remote.state == "online")
    w.api, w.server_data = api, tmp_path / "server"
    yield w
    for chat_window in list(w.chat_windows.values()):
        chat_window.app_closing = True
        chat_window.close()
    on_loop(w.remote._close())
    ai.WATCHERS.remove(w.remote.on_profile_saved)
    for job in list(w.engine.jobs.values()):
        job.future.cancel()
    w.close()
    stop.set()
    thread.join(15)
    import shutil
    shutil.rmtree(root, ignore_errors=True)


def test_an_account_moves_to_the_server_works_there_and_comes_back(qapp, window, fakes):  # noqa: F811
    w = window
    account = w.model.accounts[0]
    w.move_to_server([account])
    pump(qapp, lambda: account.placement == "server")
    assert (w.server_data / "sessions" / "acc.session").read_bytes() == b"session-bytes"
    assert w.model.data(w.model.index(0, 11)) == "Server"

    w.start_task("online/acc", w.call(account, telegram.online_keeper, 10, w.emitter(account)), "keeping online")
    pump(qapp, lambda: fakes["online"] == ["server"])
    assert w.task_running("online/acc") and w.busy(account) and not w.busy(account, own="online")
    pump(qapp, lambda: "☁ ◉ [Alice] online for 10 min" in w.log_view.toPlainText())

    w.local_only([account], "exported")  # the backup session here is never handed out
    assert "run on the server" in w.statusBar().currentMessage()

    w.open_chats(account)
    assert "acc" not in w.chat_windows  # the online keeper holds the session there: refused, as locally
    w.stop_task("online/acc")
    pump(qapp, lambda: not w.task_running("online/acc"))
    w.open_chats(account)
    chat_window = w.chat_windows["acc"]
    pump(qapp, lambda: len(chat_window.chats.chats) == 1)
    assert chat_window.chats.chats[0].title == "Bob"

    chat_window.close()
    w.move_back([account])
    pump(qapp, lambda: account.placement == "local")
    assert not (w.server_data / "sessions" / "acc.session").exists()
    w.start_task("online/acc", w.call(account, telegram.online_keeper, 10, w.emitter(account)), "keeping online")
    pump(qapp, lambda: len(fakes["online"]) == 2 and fakes["online"][-1] != "server")  # it runs here again
    assert w.engine.running("online/acc")


def test_a_server_account_without_the_server_can_be_forced_back(qapp, window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    w = window
    account = w.model.accounts[0]
    w.move_to_server([account])
    pump(qapp, lambda: account.placement == "server")
    on_loop(w.remote._close())
    w.remote._set_state("offline", "gone")
    pump(qapp, lambda: w.busy(account))  # offline: the locked backup isn't used
    monkeypatch.setattr(QMessageBox, "exec", lambda box: next(
        b for b in box.buttons() if b.text() == "Force back").click() or 0)
    w.move_back([account])
    assert account.placement == "local"
