"""Bulk content dialogs for one account: forward messages, clone a chat, report messages."""
import re

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from omnigram import telegram
from omnigram.store import Account


def parse_ids(text: str) -> list[int]:
    """Message ids from pasted text: "1-5, 8, 12" -> [1,2,3,4,5,8,12]. Sorted, deduped, garbage ignored."""
    ids: set[int] = set()
    for token in re.split(r"[,;\s]+", re.sub(r"\s*-\s*", "-", text)):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)-(\d+)", token)
        if match:
            low, high = sorted(int(part) for part in match.groups())
            ids.update(range(low, high + 1))
        elif token.isdigit():
            ids.add(int(token))
    return sorted(ids)


def row(*widgets) -> QHBoxLayout:
    """One button row."""
    layout = QHBoxLayout()
    for widget in widgets:
        layout.addWidget(widget)
    return layout


def fill_chats(combo: QComboBox, chats: list[tuple[int, str]]):
    """Fill a chat picker from telegram.dialogs() output."""
    combo.clear()
    for chat_id, label in chats:
        combo.addItem(label, chat_id)


class ForwarderDialog(QDialog):
    """Forward the latest messages from one chat into another, on one connection."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"forward/{account.session}"
        self.ready = False
        self.setWindowTitle(f"Forward messages — {account.name or account.session}")
        self.setMinimumWidth(460)

        self.source = QComboBox(enabled=False)
        self.target = QComboBox(enabled=False)
        self.limit = QSpinBox(minimum=0, maximum=1_000_000, specialValueText="everything (0)")
        self.run = QPushButton("Run", objectName="primary", enabled=False)
        self.run.clicked.connect(self.on_run)
        self.stop = QPushButton("Stop", enabled=False)
        self.stop.clicked.connect(self.on_stop)

        form = QFormLayout()
        form.addRow("From", self.source)
        form.addRow("To", self.target)
        form.addRow("Limit", self.limit)
        form.addRow(QLabel("A large limit means many messages; 0 forwards everything.", objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row(self.run, self.stop))
        window.run(window.call(account, telegram.dialogs), self.on_loaded)
        self.refresh()

    def on_loaded(self, future):
        try:
            chats = future.result()
        except Exception as e:
            QMessageBox.warning(self, "Forward messages", str(e))
            self.window.log(f"✗ [{self.account.name or self.account.session}] {e}")
            return
        for combo in (self.source, self.target):
            fill_chats(combo, chats)
        self.target.setCurrentIndex(1 if chats else 0)  # never the same default chat twice
        self.ready = bool(chats)
        self.refresh()

    def refresh(self):
        running = self.window.task_running(self.key)
        for widget in (self.source, self.target, self.limit):
            widget.setEnabled(self.ready and not running)
        self.run.setEnabled(self.ready and not running)
        self.stop.setEnabled(running)

    def on_run(self):
        if self.source.currentData() == self.target.currentData():
            QMessageBox.warning(self, "Forward messages", "Pick two different chats.")
            return
        self.window.start_task(
            self.key,
            self.window.call(self.account, telegram.forward, self.source.currentData(), self.target.currentData(),
                             self.limit.value(), self.window.emitter(self.account)),
            "forwarding")
        self.refresh()

    def on_stop(self):
        self.window.stop_task(self.key)
        self.refresh()


class ClonerDialog(QDialog):
    """Copy a chat's messages into a new chat the account owns (real copies, no forward links)."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"clone/{account.session}"
        self.ready, self.auto_title = False, ""
        self.setWindowTitle(f"Clone chat — {account.name or account.session}")
        self.setMinimumWidth(460)

        self.source = QComboBox(enabled=False)
        self.source.currentTextChanged.connect(self.on_source)
        self.title = QLineEdit()
        self.kind = QComboBox()
        self.kind.addItems(["Group", "Channel"])
        self.limit = QSpinBox(minimum=0, maximum=1_000_000, value=100, specialValueText="everything (0)")
        self.run = QPushButton("Run", objectName="primary", enabled=False)
        self.run.clicked.connect(self.on_run)
        self.stop = QPushButton("Stop", enabled=False)
        self.stop.clicked.connect(self.on_stop)

        form = QFormLayout()
        form.addRow("Source", self.source)
        form.addRow("New title", self.title)
        form.addRow("New chat kind", self.kind)
        form.addRow("Limit", self.limit)
        form.addRow(QLabel("Messages are copied (not forwarded) into a new chat this account owns; "
                           "0 copies everything.", objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row(self.run, self.stop))
        window.run(window.call(account, telegram.dialogs), self.on_loaded)
        self.refresh()

    def on_loaded(self, future):
        try:
            chats = future.result()
        except Exception as e:
            QMessageBox.warning(self, "Clone chat", str(e))
            self.window.log(f"✗ [{self.account.name or self.account.session}] {e}")
            return
        fill_chats(self.source, chats)
        self.ready = bool(chats)
        self.source.setEnabled(self.ready)
        self.on_source()
        self.refresh()

    def on_source(self):
        if self.title.text().strip() and self.title.text() != self.auto_title:
            return  # the user typed their own title
        self.auto_title = self.source.currentText() + " copy"
        self.title.setText(self.auto_title)

    def refresh(self):
        running = self.window.task_running(self.key)
        self.run.setEnabled(self.ready and not running)
        self.stop.setEnabled(running)

    def on_run(self):
        if not self.title.text().strip():
            QMessageBox.warning(self, "Clone chat", "Give the new chat a title.")
            return
        self.window.start_task(
            self.key,
            self.window.call(self.account, telegram.clone_chat, self.source.currentData(), self.title.text().strip(),
                             self.kind.currentText() == "Channel", self.limit.value(),
                             self.window.emitter(self.account)),
            "cloning")
        self.refresh()

    def on_stop(self):
        self.window.stop_task(self.key)
        self.refresh()


class ReporterDialog(QDialog):
    """Report specific messages of a chat to Telegram's moderation, on the operator's instruction."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.ready = False
        self.setWindowTitle(f"Report messages — {account.name or account.session}")
        self.setMinimumWidth(460)

        self.chat = QComboBox(enabled=False)
        self.ids = QLineEdit(placeholderText="e.g. 1-5, 8, 12")
        self.reason = QPlainTextEdit(placeholderText="optional free text")
        self.reason.setFixedHeight(70)
        self.report = QPushButton("Report", objectName="primary", enabled=False)
        self.report.clicked.connect(self.on_report)

        form = QFormLayout()
        form.addRow("Chat", self.chat)
        form.addRow("Message ids", self.ids)
        form.addRow("Reason", self.reason)
        form.addRow(QLabel("Ranges and lists are accepted; reports go to Telegram's moderation for review.",
                           objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.report)
        window.run(window.call(account, telegram.dialogs), self.on_loaded)

    def on_loaded(self, future):
        try:
            chats = future.result()
        except Exception as e:
            QMessageBox.warning(self, "Report messages", str(e))
            self.window.log(f"✗ [{self.account.name or self.account.session}] {e}")
            return
        fill_chats(self.chat, chats)
        self.ready = bool(chats)
        self.chat.setEnabled(self.ready)
        self.report.setEnabled(self.ready)

    def on_report(self):
        message_ids = parse_ids(self.ids.text())
        if not message_ids:
            QMessageBox.warning(self, "Report messages", "Give at least one message id, e.g. 1-5, 8.")
            return
        self.report.setEnabled(False)
        self.window.run(
            self.window.call(self.account, telegram.report, self.chat.currentData(), message_ids,
                             self.reason.toPlainText().strip()),
            self.on_done)

    def on_done(self, future):
        self.report.setEnabled(True)
        name = self.account.name or self.account.session
        try:
            self.window.log(f"✓ [{name}] reported {future.result()} message(s) in {self.chat.currentText()}")
        except Exception as e:
            QMessageBox.warning(self, "Report messages", str(e))
            self.window.log(f"✗ [{name}] {type(e).__name__}: {e}")