"""Audience dialogs: chat participant parser, DM/drip funnel, phone-number checker.

Same shape as dialogs.py: each dialog takes the MainWindow and one already-vetted Account, does its
Telegram work through ``window.run(window.call(...))`` and renders the result on the GUI thread.
The decision-shaped parts (which funnel steps are due, how results are rendered, how pasted text is
split) live in module-level functions so they can be tested without a display.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from omnigram import broadcast, icons, parser, phones, telegram
from omnigram.broadcast import Recipient
from omnigram.funnel import (
    Funnel,
    Step,
    advance,
    due_entries,
    load,
    new_entry,
    progress,
    render_step,
    save,
)
from omnigram.store import Account



# ---- pure helpers (tested in tests/test_audience_dialogs.py) -------------------------------------

def recipient_for(key) -> Recipient:
    """A funnel key — a numeric id or a username string — as a broadcast.Recipient."""
    return Recipient(id=key) if isinstance(key, int) else Recipient(id=0, username=key)


def due_steps(funnel_: Funnel, entries: dict, now: datetime) -> list[tuple[object, str]]:
    """[(key, rendered message)] for every subscription whose next step is due at `now`."""
    return [(key, render_step(funnel_, entries[key]["step"])) for key in due_entries(entries, funnel_, now)]


def recipients_csv(recipients: Sequence[Recipient]) -> str:
    """CSV with a header row, for the parser's export button."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["id", "first_name", "last_name", "username", "phone"])
    for r in recipients:
        writer.writerow([r.id, r.first_name, r.last_name, r.username, r.phone])
    return out.getvalue()


def usernames_text(recipients: Sequence[Recipient]) -> str:
    """One '@username' per line for the clipboard; a user without one falls back to the numeric id."""
    return "\n".join(f"@{r.username}" if r.username else str(r.id) for r in recipients)


def results_text(rows: Sequence[dict]) -> str:
    """One line per checked number: the registration mark, then name/username when Telegram knows them."""
    lines = []
    for row in rows:
        mark = "✓ registered" if row.get("registered") else "✗ not registered"
        if row.get("retry"):
            mark += " (retry)"
        line = f"{row.get('phone', '')}  {mark}"
        if row.get("name"):
            line += f"  {row['name']}"
        if row.get("username"):
            line += f"  @{row['username']}"
        if row.get("self"):
            line += "  (this account — reported without importing)"
        lines.append(line)
    return "\n".join(lines)


def rows_csv(rows: Sequence[dict]) -> str:
    """CSV of ``telegram.check_numbers`` rows, for the number checker's export button."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["phone", "registered", "id", "username", "name", "retry"])
    for r in rows:
        writer.writerow([r.get("phone", ""), r.get("registered", False), r.get("id", 0), r.get("username", ""),
                         r.get("name", ""), r.get("retry", False)])
    return out.getvalue()


def parse_steps(text: str) -> list[Step]:
    """Pasted funnel steps: 'hours | template' per line (also ':' or a tab); '#…' lines are ignored."""
    steps = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, sep, tail = line.partition("|")
        if not sep:
            head, sep, tail = line.partition(":")
        if not sep:
            head, sep, tail = line.partition("\t")
        try:
            hours = float(head.strip())
        except ValueError:
            continue
        if sep and tail.strip():
            steps.append(Step(hours=hours, template=tail.strip()))
    return steps


def steps_from_table(rows: Sequence[Sequence[str]]) -> list[Step]:
    """The funnel editor's (hours, template) cells -> Step list; rows without a template are dropped."""
    steps = []
    for hours, template in rows:
        try:
            delay = float(str(hours).strip())
        except ValueError:
            delay = 0.0
        text = str(template).strip()
        if text:
            steps.append(Step(hours=delay, template=text))
    return steps


def split_numbers(text: str) -> list[str]:
    """Pasted numbers: split on newlines, commas and semicolons; blanks dropped."""
    return [part.strip() for part in re.split(r"[,\n;]+", text or "") if part.strip()]


def add_subscribers(entries: dict, text: str, funnel_: Funnel, now: datetime) -> int:
    """Subscribe every pasted target that is not already in `entries`; returns how many were added.

    The key is the numeric id when there is one, else the username — the same convention
    ``funnel.deserialize`` uses, so a saved funnel round-trips.
    """
    added = 0
    for r in broadcast.parse_targets(text):
        key = r.id or r.username
        if key and key not in entries:
            entries[key] = new_entry(now, funnel_)
            added += 1
    return added


# ---- widget glue ---------------------------------------------------------------------------------

def steps_pane(on_paste) -> tuple[QTableWidget, QVBoxLayout]:
    """The funnel's steps editor: the table, its row buttons and the heading, as one pane."""
    table = QTableWidget(0, 2)
    table.setHorizontalHeaderLabels(["Wait (hours)", "Template"])
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.ExtendedSelection)
    add = QPushButton(icons.get("plus"), "Add row")
    add.clicked.connect(lambda: table.insertRow(table.rowCount()))
    remove = QPushButton(icons.get("trash"), "Remove selected")
    remove.clicked.connect(lambda: remove_selected_rows(table))
    paste = QPushButton("Paste steps")
    paste.clicked.connect(on_paste)
    buttons = QHBoxLayout()
    for button in (add, remove, paste):
        buttons.addWidget(button)
    buttons.addStretch()
    pane = QVBoxLayout()
    pane.addWidget(QLabel("Steps — the wait, then the message ({first_name} etc. work):", objectName="muted"))
    pane.addWidget(table, 1)
    pane.addLayout(buttons)
    return table, pane


def subscribers_pane(on_add) -> tuple[QPlainTextEdit, QPushButton]:
    """The funnel's subscriber box and its Add button."""
    edit = QPlainTextEdit(placeholderText="@usernames or numeric ids, one per line")
    edit.setFixedHeight(80)
    button = QPushButton(icons.get("plus"), "Add to funnel")
    button.clicked.connect(on_add)
    return edit, button


def steps_table_rows(table: QTableWidget) -> list[tuple[str, str]]:
    """The (hours, template) text in every row of the steps table."""
    return [(table.item(r, 0).text() if table.item(r, 0) else "",
             table.item(r, 1).text() if table.item(r, 1) else "") for r in range(table.rowCount())]


def fill_steps_table(table: QTableWidget, steps: Sequence[Step]):
    """Replace the steps table's contents."""
    table.setRowCount(len(steps))
    for row, step in enumerate(steps):
        table.setItem(row, 0, QTableWidgetItem(str(step.hours)))
        table.setItem(row, 1, QTableWidgetItem(step.template))


def remove_selected_rows(table: QTableWidget):
    """Drop the table's selected rows, bottom-up so the remaining indices stay valid."""
    for row in sorted({i.row() for i in table.selectedIndexes()}, reverse=True):
        table.removeRow(row)


def reload_funnel_names(combo: QComboBox, directory: Path):
    """Fill the funnel picker with the saved funnels, keeping the 'Load saved…' entry first."""
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("Load saved…", None)
    for path in sorted(directory.glob("*.json")):
        combo.addItem(path.stem, path)
    combo.blockSignals(False)


# ---- dialogs -------------------------------------------------------------------------------------

class ParserDialog(QDialog):
    """Audience parser: collect a chat's participants, filter them, and export the keepers."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.recipients: list[Recipient] = []
        self.setWindowTitle(f"Audience parser — {account.name or account.session}")
        self.setMinimumSize(600, 560)

        self.chat = QComboBox()
        self.chat.addItem("Loading…")
        self.limit = QSpinBox(minimum=0, maximum=1_000_000, specialValueText="all")
        self.search = QLineEdit(placeholderText="Telegram-side search (optional)")
        self.keyword = QLineEdit(placeholderText="name or @username contains")
        self.require_username = QCheckBox("Require @username")
        self.include_bots = QCheckBox("Include bots")
        self.parse = QPushButton(icons.get("search"), "Parse", objectName="primary", enabled=False)
        self.parse.clicked.connect(self.on_parse)

        form = QFormLayout()
        form.addRow("Chat", self.chat)
        form.addRow("Limit", self.limit)
        form.addRow("Search", self.search)
        form.addRow("Keyword filter", self.keyword)
        form.addRow(self.require_username)
        form.addRow(self.include_bots)

        self.count = QLabel("Nothing parsed yet.", objectName="muted")
        self.result = QPlainTextEdit(readOnly=True)
        self.copy = QPushButton(icons.get("copy"), "Copy @usernames", enabled=False)
        self.copy.clicked.connect(self.on_copy)
        self.export = QPushButton(icons.get("save"), "Export CSV…", enabled=False)
        self.export.clicked.connect(self.on_export)
        actions = QHBoxLayout()
        actions.addWidget(self.count, 1)
        actions.addWidget(self.copy)
        actions.addWidget(self.export)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.parse)
        layout.addWidget(self.result, 1)
        layout.addLayout(actions)
        window.run(window.call(account, telegram.dialogs), self.on_chats)

    def on_chats(self, future):
        self.chat.clear()
        try:
            chats = future.result()
        except Exception as e:
            self.chat.addItem(f"Could not load chats: {e}")
            return
        for chat_id, label in chats:
            self.chat.addItem(label, chat_id)
        self.parse.setEnabled(bool(chats))
        if not chats:
            self.chat.addItem("No chats available")

    def on_parse(self):
        chat_id = self.chat.currentData()
        if chat_id is None:
            return
        self.parse.setEnabled(False)
        self.parse.setText("Parsing…")
        self.window.run(self.window.call(self.account, telegram.parse_participants, chat_id,
                                         self.limit.value(), self.search.text().strip()), self.on_parsed)

    def on_parsed(self, future):
        self.parse.setEnabled(True)
        self.parse.setText("Parse")
        try:
            users = future.result()
        except Exception as e:
            QMessageBox.warning(self, "Could not parse", str(e))
            return
        found = []
        for u in users:
            r = Recipient(id=u["id"], first_name=u["first_name"], last_name=u["last_name"],
                          username=u["username"], phone=u["phone"])
            r.bot, r.deleted = u.get("bot", False), u.get("deleted", False)  # parser.parse_filter reads these
            found.append(r)
        self.recipients = parser.parse_filter(found, keyword=self.keyword.text(),
                                              require_username=self.require_username.isChecked(),
                                              include_bots=self.include_bots.isChecked())
        self.result.setPlainText("\n".join(parser.format_recipients(self.recipients)))
        self.count.setText(f"{len(self.recipients)} of {len(found)} kept")
        self.copy.setEnabled(bool(self.recipients))
        self.export.setEnabled(bool(self.recipients))

    def on_copy(self):
        QGuiApplication.clipboard().setText(usernames_text(self.recipients))
        self.window.statusBar().showMessage(f"Copied {len(self.recipients)} targets", 3000)

    def on_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export recipients", "recipients.csv", "CSV (*.csv)")
        if not path:
            return
        Path(path).write_text(recipients_csv(self.recipients), "utf-8")
        self.window.log(f"✓ [{self.account.name or self.account.session}] exported "
                        f"{len(self.recipients)} recipients to {path}")


class FunnelDialog(QDialog):
    """DM / drip funnel: an ordered list of messages, each due N hours after the previous one."""

    def __init__(self, window, account: Account, kind: str = "drip"):
        super().__init__(window)
        self.window, self.account, self.kind = window, account, kind
        self.funnel = Funnel(name="", kind=kind)
        self.entries: dict = {}
        self.busy = False
        self.setWindowTitle(f"{'Drip' if kind == 'drip' else 'DM'} funnel — {account.name or account.session}")
        self.setMinimumSize(640, 600)

        self.name = QLineEdit(placeholderText="funnel name (becomes the file name)")
        self.existing = QComboBox()
        reload_funnel_names(self.existing, window.store.funnels)
        self.existing.currentIndexChanged.connect(self.on_load)
        save_button = QPushButton(icons.get("save"), "Save")
        save_button.clicked.connect(self.on_save)
        top = QHBoxLayout()
        top.addWidget(self.name, 1)
        top.addWidget(self.existing)
        top.addWidget(save_button)

        self.steps, pane = steps_pane(self.on_paste)
        self.subscribers, self.add_subs = subscribers_pane(self.on_add)
        self.progress_label = QLabel("", objectName="muted")
        self.send_due = QPushButton(icons.get("play"), "Send due now", objectName="primary")
        self.send_due.clicked.connect(lambda: self.send_due_now())
        self.auto = QCheckBox("Send due steps automatically, once a minute while this window is open")
        self.auto.toggled.connect(self.on_auto)
        self.timer = QTimer(self, interval=60_000)
        self.timer.timeout.connect(lambda: self.send_due_now(auto=True))
        self.watch = QPushButton(icons.get("play"), "Watch in the background")
        self.watch.clicked.connect(self.on_watch)
        self._watch_tick = QTimer(self, interval=1000)  # the watch runs as a window task; poll its state
        self._watch_tick.timeout.connect(self.refresh_watch)
        self._watch_tick.start()

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(pane, 1)
        layout.addWidget(QLabel("Subscribers:", objectName="muted"))
        layout.addWidget(self.subscribers)
        layout.addWidget(self.add_subs)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.send_due)
        layout.addWidget(self.watch)
        layout.addWidget(self.auto)
        layout.addWidget(QLabel("Omnigram must stay open for the drip to tick: DM sequences are not "
                                "scheduled inside Telegram. Watching keeps the funnel moving with this "
                                "window closed, and enrolls each person who writes to the account first.",
                                objectName="muted"))
        self.refresh()
        self.refresh_watch()

    def current_steps(self) -> list[Step]:
        return steps_from_table(steps_table_rows(self.steps))

    def refresh(self, note: str = ""):
        sent, total, percent = progress(self.entries, self.funnel)
        self.progress_label.setText(f"{len(self.entries)} subscribers · {sent}/{total} steps sent · "
                                    f"{percent:.0f}%" + (f" · {note}" if note else ""))

    def persist(self):
        if self.funnel.name:
            save(self.window.store.funnels / f"{self.funnel.name}.json", self.funnel, self.entries)

    def on_paste(self):
        steps = parse_steps(QGuiApplication.clipboard().text())
        if not steps:
            QMessageBox.information(self, "Paste steps", "Clipboard has no 'hours | template' lines.")
            return
        fill_steps_table(self.steps, steps)
        self.refresh(f"pasted {len(steps)} steps")

    def on_add(self):
        self.funnel.steps = self.current_steps()
        if not self.funnel.steps:
            QMessageBox.information(self, "Add to funnel", "Add at least one step first.")
            return
        added = add_subscribers(self.entries, self.subscribers.toPlainText(), self.funnel, datetime.now())
        self.subscribers.clear()
        self.refresh(f"added {added}")

    def on_save(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.information(self, "Save funnel", "Give the funnel a name first.")
            return
        self.funnel.name, self.funnel.steps = name, self.current_steps()
        self.persist()
        self.window.log(f"✓ [{self.account.name or self.account.session}] funnel '{name}' saved")
        reload_funnel_names(self.existing, self.window.store.funnels)

    def on_load(self, _index: int):
        path = self.existing.currentData()
        loaded = load(Path(path)) if path else None
        if loaded is None:
            return
        self.funnel, self.entries = loaded
        self.name.setText(self.funnel.name)
        fill_steps_table(self.steps, self.funnel.steps)
        self.refresh()

    def send_due_now(self, auto: bool = False):
        if self.busy:
            return
        self.funnel.steps = self.current_steps()
        due = due_steps(self.funnel, self.entries, datetime.now())
        if not due:
            if not auto:
                QMessageBox.information(self, "Send due", "Nothing is due right now.")
            return
        self.busy = True
        self.send_due.setEnabled(False)
        steps = [broadcast.Step(recipient_for(key), text, 0) for key, text in due]
        self.window.run(self.window.call(self.account, telegram.send_many, steps,
                                         self.window.emitter(self.account)), lambda f: self.on_sent(f, due))

    def on_sent(self, future, due: list):
        self.busy = False
        self.send_due.setEnabled(True)
        name = self.account.name or self.account.session
        try:
            result = future.result()
        except Exception as e:
            self.window.log(f"✗ [{name}] funnel send: {type(e).__name__}: {e}")
            QMessageBox.warning(self, "Send failed", str(e))
            return
        now = datetime.now()
        for key, _ in due:  # advance every step we attempted; send_many reports only totals
            if key in self.entries:
                advance(self.entries[key], self.funnel, now)
        self.persist()
        self.refresh(f"sent {result.get('sent', 0)}, failed {result.get('failed', 0)}")

    def on_auto(self, on: bool):
        if on:
            self.timer.start()
            self.send_due_now(auto=True)
        else:
            self.timer.stop()

    def watch_key(self) -> str:
        return f"funnel/{self.account.session}"

    def refresh_watch(self):
        running = self.window.task_running(self.watch_key())
        self.watch.setText("Stop watching" if running else "Watch in the background")
        self.auto.setEnabled(not running)  # one sender only: the watch or the in-window timer

    def on_watch(self):
        if self.window.task_running(self.watch_key()):
            self.window.stop_task(self.watch_key())
            self.refresh("background watch stopped")
            return
        if not self.funnel.name:
            QMessageBox.information(self, "Watch funnel", "Save the funnel first — the watch reads it from disk.")
            return
        self.funnel.steps = self.current_steps()
        if not self.funnel.steps:
            QMessageBox.information(self, "Watch funnel", "Add at least one step first.")
            return
        self.persist()  # the watcher reads the saved file: flush the current edits first
        self.auto.setChecked(False)
        path = str(self.window.store.funnels / f"{self.funnel.name}.json")
        self.window.start_task(self.watch_key(),
                               self.window.call(self.account, telegram.funnel_watch, path,
                                                self.window.emitter(self.account)),
                               f"funnel '{self.funnel.name}'")
        self.refresh("watching in the background")


class NumberCheckerDialog(QDialog):
    """Which of these phone numbers are on Telegram; contacts are imported for the call and deleted after."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.clean: list[str] = []
        self.rows: list[dict] = []
        self.setWindowTitle(f"Number checker — {account.name or account.session}")
        self.setMinimumSize(600, 520)

        self.numbers = QPlainTextEdit(placeholderText="+12025550143\n+44 7700 900000, +4915112345678")
        self.numbers.setFixedHeight(120)
        self.normalize = QPushButton(icons.get("wand-sparkles"), "Normalize")
        self.normalize.clicked.connect(self.on_normalize)
        self.preview = QLabel("", objectName="muted")
        self.run = QPushButton(icons.get("play"), "Run", objectName="primary")
        self.run.clicked.connect(self.on_run)
        tools = QHBoxLayout()
        tools.addWidget(self.normalize)
        tools.addWidget(self.preview, 1)
        tools.addWidget(self.run)

        self.results = QPlainTextEdit(readOnly=True)
        self.export = QPushButton(icons.get("save"), "Export CSV…", enabled=False)
        self.export.clicked.connect(self.on_export)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Numbers (one per line or comma-separated):", objectName="muted"))
        layout.addWidget(self.numbers)
        layout.addLayout(tools)
        layout.addWidget(QLabel("Checking imports the numbers as contacts for the duration of the call and "
                                "deletes them again.", objectName="muted"))
        layout.addWidget(self.results, 1)
        layout.addWidget(self.export)

    def on_normalize(self):
        self.clean = phones.check_list(split_numbers(self.numbers.toPlainText()))
        preview = ", ".join(self.clean[:3]) + ("…" if len(self.clean) > 3 else "")
        self.preview.setText(f"{len(self.clean)} valid number(s)" + (f": {preview}" if preview else ""))

    def on_run(self):
        numbers = self.clean or phones.check_list(split_numbers(self.numbers.toPlainText()))
        if not numbers:
            QMessageBox.information(self, "Number checker", "Paste at least one valid number.")
            return
        self.clean = numbers
        self.run.setEnabled(False)
        self.run.setText("Checking…")
        self.window.run(self.window.call(self.account, telegram.check_numbers, numbers), self.on_checked)

    def on_checked(self, future):
        self.run.setEnabled(True)
        self.run.setText("Run")
        try:
            self.rows = future.result()
        except Exception as e:
            QMessageBox.warning(self, "Check failed", str(e))
            return
        self.results.setPlainText(results_text(self.rows))
        self.export.setEnabled(bool(self.rows))
        registered = sum(1 for r in self.rows if r.get("registered"))
        self.window.log(f"✓ [{self.account.name or self.account.session}] checked {len(self.rows)} numbers, "
                        f"{registered} registered")

    def on_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export results", "numbers.csv", "CSV (*.csv)")
        if not path:
            return
        Path(path).write_text(rows_csv(self.rows), "utf-8")
        self.window.log(f"✓ [{self.account.name or self.account.session}] exported {len(self.rows)} rows to {path}")
