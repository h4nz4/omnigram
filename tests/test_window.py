"""MainWindow guards, called unbound on a stand-in so no window (and no app-data folder) is created:
busy() keeps two clients off one session file; warn_direct() is the own-IP confirmation."""
import os
from types import SimpleNamespace

import pytest

from omnigram.store import Account
from omnigram.window import MainWindow


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def drive_warning(qapp, action):
    """Show warn_direct and let `action(box, connect_button)` answer it; returns (answer, what action saw)."""
    from PySide6.QtCore import QTimer
    seen = {}

    def answer():
        box = qapp.activeModalWidget()
        connect = next(b for b in box.buttons() if b.text() == "Connect from my IP")
        seen["modal"] = box.isModal()
        seen["enabled_before"] = connect.isEnabled()
        action(box, connect)

    QTimer.singleShot(0, answer)
    return MainWindow.warn_direct(None, "acc has no proxy."), seen


def test_connecting_without_a_proxy_needs_the_ip_acknowledgement(qapp):
    def acknowledge_then_connect(box, connect):
        box.checkBox().setChecked(True)
        connect.click()

    answer, seen = drive_warning(qapp, acknowledge_then_connect)
    assert answer == "connect"
    assert seen == {"modal": True, "enabled_before": False}


def test_clicking_connect_without_the_acknowledgement_does_nothing(qapp):
    def click_blind(box, connect):
        connect.click()  # disabled: ignored, the dialog stays open
        seen_open = box.isVisible()
        box.reject()  # Esc
        assert seen_open

    answer, _ = drive_warning(qapp, click_blind)
    assert answer is None


def state(pending=(), listeners=(), tasks=()):
    return SimpleNamespace(pending=set(pending), listeners=dict.fromkeys(listeners), tasks=dict.fromkeys(tasks))


def test_a_running_task_holds_the_session():
    """The bug: one_target only looked at checks and listeners, so a second client could open a session
    a warm-up (or any long job) was using."""
    a = Account("a")
    assert MainWindow.busy(state(tasks=["warmup/a"]), a)
    assert not MainWindow.busy(state(tasks=["warmup/ab", "warmup/b"]), a)


def test_a_jobs_own_dialog_may_still_open_to_stop_it():
    a = Account("a")
    assert not MainWindow.busy(state(tasks=["online/a"]), a, own="online")
    assert MainWindow.busy(state(tasks=["online/a", "broadcast/a"]), a, own="online")


def test_checks_and_listeners_hold_the_session():
    a = Account("a")
    assert MainWindow.busy(state(pending=["a"]), a)
    assert MainWindow.busy(state(listeners=["a"]), a)
    assert not MainWindow.busy(state(listeners=["a"]), a, allow_listening=True)
