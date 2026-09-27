"""MainWindow.busy — the guard that keeps two clients off one session file. Called unbound on a stand-in,
so no window (and no app-data folder) is created."""
from types import SimpleNamespace

from omnigram.store import Account
from omnigram.window import MainWindow


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
