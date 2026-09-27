"""The chat window, driven headlessly with a fake ChatClient: no network, no MainWindow, no app-data folder.
Requests complete synchronously, so each step's effect is visible right after it."""
import asyncio
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget

from omnigram import chat, chat_window
from omnigram.store import Account

NOW = datetime.now(timezone.utc)
ALICE, TEAM = 11, -22


def png(w=320, h=200) -> bytes:
    image = QImage(w, h, QImage.Format_RGB32)
    image.fill(0x335577)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


class FakeClient:
    """Stands in for telegram.ChatClient and records what the window asked for."""
    instances = []

    def __init__(self, session, api_id, api_hash, proxy, on_event):
        self.on_event, self.calls = on_event, []
        self.next_id = 100
        FakeClient.instances.append(self)

    async def run(self):  # never awaited: the fake start_task doesn't run it
        pass

    async def ready(self):
        pass

    async def dialogs(self, more=False, limit=100):
        self.calls.append(("dialogs", more))
        return [chat.Chat(ALICE, "Alice", "user", unread=2, last_text="hi", last_date=NOW),
                chat.Chat(TEAM, "Team", "group", last_text="lunch?", last_date=NOW - timedelta(hours=1))], False

    async def history(self, chat_id, before_id=0, limit=50):
        self.calls.append(("history", chat_id, before_id))
        if before_id:
            return []
        return [chat.Msg(1, chat_id, False, NOW - timedelta(days=1), "yesterday's", sender="Alice"),
                chat.Msg(2, chat_id, True, NOW, "mine", edited=True),
                chat.Msg(3, chat_id, False, NOW, "", sender="Alice", media="photo", media_label="Photo",
                         has_thumb=True),
                chat.Msg(4, chat_id, False, NOW, "a reply", sender="Alice", reply_to=2)]

    async def thumbnail(self, chat_id, msg_id):
        return png()

    async def mark_read(self, chat_id, max_id=0):
        self.calls.append(("mark_read", chat_id, max_id))

    async def send_text(self, chat_id, text, reply_to=None):
        self.calls.append(("send_text", chat_id, text, reply_to))
        self.next_id += 1
        return chat.Msg(self.next_id, chat_id, True, NOW, text, reply_to=reply_to)

    async def edit(self, chat_id, msg_id, text):
        self.calls.append(("edit", chat_id, msg_id, text))
        return chat.Msg(msg_id, chat_id, True, NOW, text, edited=True)


class Host(QWidget):
    """The bits of MainWindow the chat window uses."""
    _call = Signal(object)

    def __init__(self, tmp_path):
        super().__init__()
        self._call.connect(lambda fn: fn())
        self.store = type("S", (), {"sessions": tmp_path / "sessions", "path": lambda self, a: Path("x.session")})()
        self.chat_windows, self.stopped = {}, []

    def credentials(self, account=None):
        return 1, "hash"

    def run(self, coro, on_done):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except Exception as e:  # noqa: BLE001 - handed to on_done like the real run() does
            future.set_exception(e)
        on_done(future)
        return future

    def start_task(self, key, coro, verb, on_done=None, quiet=False):
        coro.close()
        return Future()

    def stop_task(self, key):
        self.stopped.append(key)


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(chat_window.telegram, "ChatClient", FakeClient)
    FakeClient.instances.clear()
    host = Host(tmp_path)
    w = chat_window.ChatWindow(host, Account("s1", name="Bob"))
    w.resize(900, 600)
    w.show()
    yield w
    w.close()


def open_chat(w, row=0):
    w.chat_list.setCurrentIndex(w.chat_filter.index(row, 0))


def test_connecting_loads_the_chat_list(win):
    assert [c.title for c in win.chats.chats] == ["Alice", "Team"]
    assert not win.overlay.isVisible()


def test_opening_a_chat_loads_history_and_previews_without_marking_read(win):
    client = FakeClient.instances[0]
    open_chat(win)
    assert [m.id for m in win.messages.msgs] == [1, 2, 3, 4]
    assert 3 in win.messages.thumbs  # the photo preview loaded
    assert not any(c[0] == "mark_read" for c in client.calls)  # the switch is off: Alice doesn't see "seen"
    assert win.chats.chats[0].unread == 2


def test_the_mark_as_read_switch_sends_the_receipt(win):
    client = FakeClient.instances[0]
    open_chat(win)
    win.mark_read.setChecked(True)
    assert ("mark_read", ALICE, 4) in client.calls
    assert win.chats.chats[0].unread == 0


def test_sending_a_reply_appends_it_and_marks_the_chat_read(win):
    client = FakeClient.instances[0]
    open_chat(win)
    win.start_reply(win.messages.find(4))
    win.composer.setPlainText("  sure  ")
    win.send()
    assert ("send_text", ALICE, "sure", 4) in client.calls
    assert win.messages.msgs[-1].text == "sure"
    assert win.composer.toPlainText() == "" and win.reply_to is None
    assert any(c[0] == "mark_read" for c in client.calls)  # sending marks read, as Telegram does


def test_editing_replaces_the_message_in_place(win):
    client = FakeClient.instances[0]
    open_chat(win)
    win.start_edit(win.messages.find(2))
    assert win.composer.toPlainText() == "mine"
    win.composer.setPlainText("mine, fixed")
    win.send()
    assert ("edit", ALICE, 2, "mine, fixed") in client.calls
    assert win.messages.find(2).text == "mine, fixed" and len(win.messages.msgs) == 4


def test_a_live_message_in_another_chat_moves_it_up_as_unread(win):
    open_chat(win)  # Alice is open
    win.on_event("message", chat.Msg(50, TEAM, False, NOW, "lunch now", sender="Carol"))
    assert [c.title for c in win.chats.chats] == ["Team", "Alice"]
    assert win.chats.chats[0].unread == 1 and win.chats.chats[0].last_text == "lunch now"
    assert win.current.id == ALICE and len(win.messages.msgs) == 4  # the open chat is untouched


def test_live_messages_edits_and_deletes_in_the_open_chat(win):
    open_chat(win)
    win.on_event("message", chat.Msg(5, ALICE, False, NOW, "new", sender="Alice"))
    assert win.messages.msgs[-1].text == "new"
    win.on_event("edited", chat.Msg(5, ALICE, False, NOW, "new!", sender="Alice", edited=True))
    assert win.messages.find(5).text == "new!"
    win.on_event("deleted", (None, [5, 1]))  # private chats: Telegram doesn't say which chat
    assert [m.id for m in win.messages.msgs] == [2, 3, 4]


def test_closing_frees_the_session(win):
    host = win.window
    host.chat_windows["s1"] = win
    win.close()
    assert host.stopped == ["chat/s1"] and "s1" not in host.chat_windows


def test_message_model_inserts_instead_of_resetting(qapp):
    """Appends and older pages must not reset the view (that jumps the scroll position)."""
    model = chat_window.MessageModel()
    model.reset([chat.Msg(i, 1, False, NOW, str(i)) for i in (5, 6)])
    resets, inserts = [], []
    model.modelReset.connect(lambda: resets.append(1))
    model.rowsInserted.connect(lambda parent, first, last: inserts.append((first, last)))
    model.merge([chat.Msg(7, 1, False, NOW, "7")])
    model.merge([chat.Msg(3, 1, False, NOW, "3"), chat.Msg(4, 1, False, NOW, "4")])
    assert inserts == [(2, 2), (0, 1)] and not resets
    assert [m.id for m in model.msgs] == [3, 4, 5, 6, 7]


def test_an_upload_can_be_cancelled(win, monkeypatch):
    """A big video must not lock the window: Cancel stops the upload and nothing is added to the chat."""
    open_chat(win)
    pending: list[Future] = []
    real_run = win.window.run

    def run(coro, on_done):
        if coro.__qualname__.endswith("send_file"):  # an upload still in progress
            coro.close()
            future = Future()
            future.add_done_callback(on_done)
            pending.append(future)
            return future
        return real_run(coro, on_done)

    async def send_file(chat_id, path, caption="", compress=True, reply_to=None):
        raise AssertionError("replaced by the pending future above")

    monkeypatch.setattr(win.window, "run", run)
    monkeypatch.setattr(FakeClient.instances[0], "send_file", send_file, raising=False)
    win.send_attachment("C:/videos/clip.mp4", compress=False)
    win.on_event("progress", ("Uploading clip.mp4", 0.01))
    assert win.status.text() == "Uploading clip.mp4 1%"
    assert win.cancel_transfer.isVisible() and not win.attach.isEnabled()

    win.cancel_transfer.click()
    assert pending[0].cancelled()
    assert not win.cancel_transfer.isVisible() and win.attach.isEnabled()
    assert "cancelled" in win.status.text().lower()
    assert len(win.messages.msgs) == 4
    win.on_event("progress", ("Uploading clip.mp4", 0.02))  # a straggler from the cancelled upload
    assert "cancelled" in win.status.text().lower()
