"""The chat window, driven headlessly with a fake chat handle: no network, no MainWindow, no app-data folder.
Requests complete synchronously, so each step's effect is visible right after it. Auto chats are answered by a
real telegram.Responder on the fake connection, as on the shared Link."""
import asyncio
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget

from omnigram import ai, chat, chat_window, telegram
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
    """Stands in for telegram.ChatHandle and records what the window asked for."""
    instances = []

    def __init__(self, host, account, on_event):
        self.host, self.account, self.on_event, self.calls = host, account, on_event, []
        self.next_id = 100
        FakeClient.instances.append(self)

    async def poke(self, chat_id):
        """What the shared connection's Responder does when Auto is switched on or resumed in a chat."""
        if self.host.config.ready:
            responder = telegram.Responder(self, self.host.ai_store_path(self.account), self.host.config, None,
                                           self.host.log, self.on_event)
            await responder.handle(chat_id)

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
        self.chat_windows, self.stopped, self.logged, self.handed_over = {}, [], [], []
        self.tmp_path = tmp_path
        self.config = ai.ProviderConfig()  # no provider: the AI stays out of the way unless a test sets one

    def credentials(self, account=None):
        return 1, "hash"

    def ai_config(self):
        return self.config

    def ai_store_path(self, account):
        return self.tmp_path / "ai" / f"{account.session}.json"

    def chat_client(self, account, on_event):
        return FakeClient(self, account, on_event)

    def task_running(self, key):
        return False

    def log(self, line):
        self.logged.append(line)

    def hand_over_to_autopilot(self, account):
        self.handed_over.append(account.session)

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
def win(qapp, tmp_path):
    FakeClient.instances.clear()
    host = Host(tmp_path)
    w = chat_window.ChatWindow(host, Account("s1", name="Bob"))
    w.resize(900, 600)
    w.show()
    yield w
    w.app_closing = True  # as when the app quits: no hand-over question (a modal would hang headless)
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


# ---- AI in the chat window ------------------------------------------------------------------------------

@pytest.fixture
def ai_win(win, monkeypatch):
    """The window with a provider and Jev configured, and both models faked."""
    win.window.config = ai.ProviderConfig(key="sk", jev_key="jv")
    answers = {"language": {"choice": "primary", "confidence": 0.9}, "sentiment": {"score": 3.0},
               "needs_reply": {"noul": 0.9}, "needs_owner": {"noul": 0.05}, "is_bot": {"noul": 0.01},
               "invents": {"noul": 0.02}, "commits": {"noul": 0.02}, "wrong_language": {"noul": 0.02}}
    win.jev_answers = answers
    monkeypatch.setattr(ai, "jev", lambda config, state, questions: {k: answers[k] for k in questions})
    monkeypatch.setattr(ai, "complete", lambda config, messages, model="", max_tokens=400, temperature=0.8:
                        "sounds good!")
    client = FakeClient.instances[0]

    async def typing(chat_id, seconds):
        client.calls.append(("typing", chat_id))

    async def latest_id(chat_id):
        return max([4] + [c[0] for c in client.calls if isinstance(c[0], int)])

    async def sleep(seconds):
        pass

    for name, fn in (("typing", typing), ("latest_id", latest_id), ("sleep", sleep)):
        monkeypatch.setattr(client, name, fn, raising=False)
    return win


def set_mode(w, mode):
    w.ai_mode.setCurrentIndex(w.ai_mode.findData(mode))


def test_draft_mode_puts_a_suggestion_in_the_composer(ai_win):
    open_chat(ai_win)
    set_mode(ai_win, "draft")
    assert ai_win.composer.toPlainText() == "sounds good!"  # the last message was Alice's: drafted at once
    assert ai_win.badges[ALICE] == ("DRAFT", "#6ab3f3")
    assert not any(c[0] == "send_text" for c in FakeClient.instances[0].calls)  # nothing sent by itself


def test_auto_mode_answers_a_live_message_and_you_can_take_over(ai_win):
    client = FakeClient.instances[0]
    open_chat(ai_win)
    set_mode(ai_win, "auto")  # Alice's last message is answered straight away
    assert ("send_text", ALICE, "sounds good!", None) in client.calls
    assert ai_win.badges[ALICE] == ("AUTO", "#22c55e")
    assert ai_win.store().state(ALICE).in_row == 1

    ai_win.composer.setPlainText("I'll take it from here")
    ai_win.send()  # you answered yourself
    assert ai_win.store().state(ALICE).paused
    assert ai_win.ai_flag.text() == "Paused — you took over" and not ai_win.ai_resume.isHidden()
    sent_before = len([c for c in client.calls if c[0] == "send_text"])
    ai_win.on_event("message", chat.Msg(60, ALICE, False, NOW, "you there?", sender="Alice"))
    assert len([c for c in client.calls if c[0] == "send_text"]) == sent_before  # paused: no AI reply

    ai_win.resume_auto()
    assert not ai_win.store().state(ALICE).paused


def test_a_handoff_flags_the_chat_for_you(ai_win):
    ai_win.jev_answers["needs_owner"] = {"noul": 0.95}
    open_chat(ai_win)
    set_mode(ai_win, "auto")
    assert ai_win.store().state(ALICE).flag == "this needs you personally"
    assert ai_win.badges[ALICE] == ("⚑", "#f59e0b") and "needs you" in ai_win.ai_flag.text()
    assert not any(c[0] == "send_text" for c in FakeClient.instances[0].calls)


def test_closing_hands_auto_chats_to_the_background_autopilot(ai_win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    open_chat(ai_win)
    set_mode(ai_win, "auto")
    monkeypatch.setattr(chat_window.QMessageBox, "question", lambda *a: QMessageBox.Yes)
    ai_win.close()
    assert ai_win.window.handed_over == ["s1"]
