"""The loading overlay on a real (headless) dialog, with MainWindow.run replaced by a future we complete by hand."""
from concurrent.futures import Future

import pytest

from omnigram import loading


@pytest.fixture
def dialog(qapp):
    from PySide6.QtWidgets import QDialog, QLineEdit, QPushButton, QVBoxLayout

    d = QDialog()
    d.field, d.save, d.off = QLineEdit(), QPushButton("Save"), QPushButton("Off")
    d.off.setEnabled(False)
    layout = QVBoxLayout(d)
    for w in (d.field, d.save, d.off):
        layout.addWidget(w)
    d.show()
    requests: list[Future] = []

    def run(coro, on_done):
        future = Future()
        future.add_done_callback(on_done)
        requests.append(future)
        return future

    d.overlay, d.requests = loading.LoadingOverlay(d, run), requests
    yield d
    d.close()


def test_the_dialog_is_locked_until_the_request_finishes(dialog):
    results = []
    dialog.overlay.run(None, "Saving the profile…", results.append)
    assert dialog.overlay.isVisible() and dialog.overlay.busy
    assert not dialog.field.isEnabled() and not dialog.save.isEnabled()
    assert dialog.overlay.status.text() == "Saving the profile…"

    dialog.requests[0].set_result("ok")
    assert not dialog.overlay.isVisible() and not dialog.overlay.busy
    assert dialog.field.isEnabled() and dialog.save.isEnabled()
    assert not dialog.off.isEnabled()  # what was disabled before stays disabled
    assert [f.result() for f in results] == ["ok"]


def test_the_counter_and_the_slow_proxy_hint(dialog, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(loading.time, "monotonic", lambda: now[0])
    dialog.overlay.run(None, "Loading chats…", lambda f: None)
    assert dialog.overlay.hint.isHidden()
    now[0] += 7
    dialog.overlay.tick()
    assert dialog.overlay.status.text() == "Loading chats… 7 s"
    now[0] += loading.SLOW_AFTER
    dialog.overlay.tick()
    assert not dialog.overlay.hint.isHidden()


def test_cancelling_a_change_keeps_the_dialog_and_says_it_may_have_applied(dialog, monkeypatch):
    shown, results = [], []
    monkeypatch.setattr(loading.QMessageBox, "information", lambda parent, title, text: shown.append(text))
    dialog.overlay.run(None, "Saving the profile…", results.append, change=True)
    dialog.overlay.cancel()
    assert shown == [loading.CHANGE_CANCELLED]
    assert dialog.isVisible() and dialog.save.isEnabled()
    assert dialog.requests[0].cancelled()
    assert results == []  # a cancelled request never reaches on_done


def test_cancelling_a_load_closes_the_dialog(dialog):
    results = []
    dialog.overlay.run(None, "Loading the profile…", results.append)
    dialog.overlay.cancel()
    assert not dialog.isVisible()
    assert results == []


def test_a_late_answer_after_cancel_is_ignored(dialog, monkeypatch):
    monkeypatch.setattr(loading.QMessageBox, "information", lambda *a: None)
    results = []
    future = dialog.overlay.run(None, "Scheduling the post…", results.append, change=True)
    dialog.overlay.cancel()
    future.set_running_or_notify_cancel()  # already cancelled: the late result can't land
    assert results == []


def test_closing_the_dialog_stops_waiting_for_a_load(dialog):
    future = dialog.overlay.run(None, "Loading chats…", lambda f: None)
    dialog.reject()
    assert future.cancelled()


def test_a_request_that_finishes_before_run_returns_still_completes(dialog):
    """The real run() can call back straight away when the coroutine is already done. That result used to be
    dropped as 'stale', leaving the overlay up forever."""
    done_future = Future()
    done_future.set_result("instant")

    def instant(coro, on_done):
        on_done(done_future)
        return done_future

    dialog.overlay.run_fn = instant
    results = []
    dialog.overlay.run(None, "Loading…", results.append)
    assert [f.result() for f in results] == ["instant"]
    assert not dialog.overlay.isVisible() and not dialog.overlay.busy and dialog.save.isEnabled()
