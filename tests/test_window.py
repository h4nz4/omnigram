"""MainWindow guards, called unbound on a stand-in so no window (and no app-data folder) is created:
warn_direct() is the own-IP confirmation. (Which session is busy is the engine's: tests/test_engine.py.)"""
from omnigram.store import Account
from omnigram.window import MainWindow


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


def test_the_warning_names_the_accounts_that_would_expose_the_ip():
    from omnigram.window import no_proxy_text
    assert no_proxy_text([Account("s1", name="Alice")], 1) == "Alice has no proxy."
    two = [Account("s1", name="Alice"), Account("s2")]
    assert no_proxy_text(two, 10) == "2 of 10 accounts have no proxy: Alice, s2."
    many = [Account(f"s{i}") for i in range(8)]
    assert no_proxy_text(many, 40) == "8 of 40 accounts have no proxy: s0, s1, s2, s3, s4 +3 more."


def test_the_account_menu_puts_single_account_actions_first_and_trash_last():
    from omnigram.window import ACCOUNT_MENU
    labels = [entry[0] if entry else "—" for entry in ACCOUNT_MENU]
    assert labels[:6] == ["Open chats…", "Profile…", "2FA manager…", "Sessions & access…",
                          "Account statistics…", "Listener (monitor / auto-reply / moderate)…"]
    assert labels[-2:] == ["—", "Move to trash"]
    assert labels.count("—") == 5
