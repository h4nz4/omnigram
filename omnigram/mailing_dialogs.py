"""Mailing dialogs: template CRUD, broadcast, auto-posting, auto-comments/reactions, comment now.

Every dialog takes the MainWindow and (except TemplatesDialog) an already-vetted Account. Pure
helpers (parsing pasted text, formatting the plan estimate) live at module level so they are
tested in tests/test_mailing_dialogs.py; the classes only wire widgets to window.run/start_task.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from random import Random

from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from omnigram import broadcast, icons, telegram
from omnigram.parser import parse_filter
from omnigram.store import Account
from omnigram.templates import render, validate


# Sample values for the live preview: every snippet the dialogs insert resolves here.
PREVIEW_CONTEXT = {"first_name": "Anna", "last_name": "Smith", "name": "Anna Smith", "username": "anna",
                   "phone": "+12025550143", "id": 100000001}
SNIPPETS = ("{first_name}", "{last_name}", "{username}", "{phone}", "{rand: a | b}")
_SEPARATOR = re.compile(r"(?m)^[ \t]*-{3,}[ \t]*$")


def split_posts(text: str) -> list[str]:
    """A body of posts separated by lines of '---': trim each post, drop empty ones."""
    return [chunk.strip() for chunk in _SEPARATOR.split(text or "") if chunk.strip()]


def comment_texts(text: str) -> list[str]:
    """Comment alternatives, one per line, trimmed; empty list means "no comments"."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def targets_text(recipients: Iterable[broadcast.Recipient]) -> str:
    """The paste-back form of an audience: one '@username' per line, or the numeric id when there is none."""
    return "\n".join(f"@{r.username}" if r.username else str(r.id) for r in recipients)


def to_recipients(users: Iterable[dict]) -> list[broadcast.Recipient]:
    """telegram.parse_participants dicts -> Recipients, carrying the bot/deleted flags parse_filter reads."""
    out = []
    for u in users:
        recipient = broadcast.Recipient(id=u["id"], first_name=u.get("first_name", ""),
                                        last_name=u.get("last_name", ""), username=u.get("username", ""),
                                        phone=u.get("phone", ""))
        recipient.bot, recipient.deleted = bool(u.get("bot")), bool(u.get("deleted"))
        out.append(recipient)
    return out


def plan_summary(steps: Sequence[broadcast.Step]) -> str:
    """"12 recipients · about 3 min 20 s" — the estimate shown under the pacing fields."""
    if not steps:
        return "no recipients"
    plural = "" if len(steps) == 1 else "s"
    return f"{len(steps)} recipient{plural} · about {broadcast.human_time(broadcast.total_seconds(steps))}"


def fill_chats(combo: QComboBox, chats: Iterable[tuple[int, str]], placeholder: str):
    """(id, label) chats -> combo entries; an empty list shows `placeholder` instead of a silent blank."""
    combo.clear()
    for chat_id, label in chats:
        combo.addItem(label, chat_id)
    if not chats:
        combo.addItem(placeholder)


def muted(text: str) -> QLabel:
    """A dim hint label (the QSS styles #muted)."""
    return QLabel(text, objectName="muted")


def row(*widgets) -> QHBoxLayout:
    """One left-aligned horizontal row of widgets."""
    layout = QHBoxLayout()
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch()
    return layout


def inline(*pairs) -> QHBoxLayout:
    """"Label widget Label widget …" on one row."""
    layout = QHBoxLayout()
    for label, widget in pairs:
        layout.addWidget(QLabel(label))
        layout.addWidget(widget)
    layout.addStretch()
    return layout


class TemplatesDialog(QDialog):
    """Message templates on disk: pick one on the left, edit it with a live preview on the right."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Message templates")
        self.setMinimumSize(640, 420)
        self.templates: dict[str, str] = {}

        self.list = QListWidget()
        self.list.currentTextChanged.connect(self.on_pick)
        self.name = QLineEdit(placeholderText="template name")
        self.name.textChanged.connect(self.refresh)
        self.editor = QPlainTextEdit(placeholderText="message text")
        self.editor.textChanged.connect(self.refresh)
        self.preview = muted("")
        self.preview.setWordWrap(True)
        self.problems = muted("")
        self.problems.setWordWrap(True)
        self.save = QPushButton("Save", objectName="primary")
        self.save.clicked.connect(self.on_save)

        actions = QHBoxLayout()
        for text, icon, name, handler in [("New", "plus", "", self.on_new),
                                          ("Delete", "trash", "danger", self.on_delete)]:
            button = QPushButton(icons.get(icon), text, objectName=name)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.save)
        snippets = QHBoxLayout()
        for snippet in SNIPPETS:
            button = QPushButton(snippet, toolTip=f"insert {snippet} at the cursor")
            button.clicked.connect(lambda _, s=snippet: self.insert(s))
            snippets.addWidget(button)
        snippets.addStretch()

        side = QVBoxLayout()
        side.addWidget(self.name)
        side.addWidget(self.editor, 1)
        side.addLayout(snippets)
        side.addWidget(self.preview)
        side.addWidget(self.problems)
        columns = QHBoxLayout()
        columns.addWidget(self.list, 1)
        columns.addLayout(side, 2)
        layout = QVBoxLayout(self)
        layout.addLayout(columns, 1)
        layout.addLayout(actions)
        self.reload()

    def reload(self, select: str = ""):
        self.templates = self.window.template_store.load()
        self.list.clear()
        self.list.addItems(list(self.templates))
        if select:
            matches = self.list.findItems(select, Qt.MatchExactly)
            if matches:
                self.list.setCurrentItem(matches[0])

    def on_pick(self, name: str):
        self.name.setText(name)
        self.editor.setPlainText(self.templates.get(name, ""))

    def on_new(self):
        self.list.setCurrentItem(None)
        self.name.clear()
        self.editor.clear()

    def insert(self, snippet: str):
        self.editor.insertPlainText(snippet)
        self.editor.setFocus()

    def refresh(self):
        text = self.editor.toPlainText()
        self.preview.setText("Preview: " + render(text, PREVIEW_CONTEXT, seed=Random(0)) if text.strip()
                             else "Preview: nothing to show yet")
        problems = validate(text)
        self.problems.setText(" · ".join(problems))
        self.save.setEnabled(bool(self.name.text().strip()) and not problems)

    def on_save(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.information(self, "Templates", "Give the template a name first.")
            return
        self.window.template_store.upsert(name, self.editor.toPlainText())
        self.window.log(f"✓ template '{name}' saved")
        self.reload(select=name)

    def on_delete(self):
        name = self.name.text().strip()
        if name not in self.templates:
            return
        self.window.template_store.remove(name)
        self.window.log(f"✓ template '{name}' deleted")
        self.reload()
        self.on_new()


class BroadcastDialog(QDialog):
    """One-off mailing: paste or collect recipients, write the text, pace the sends, start/stop the job."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"broadcast/{account.session}"
        self.chats: list[tuple[int, str]] = []
        self.setWindowTitle(f"Broadcast — {account.name or account.session}")
        self.setMinimumSize(560, 620)

        self.recipients = QPlainTextEdit(placeholderText="@username or numeric id, one per line; '#…' ignored")
        self.limit = QSpinBox(minimum=0, maximum=100000, value=0, specialValueText="all")
        self.keyword = QLineEdit(placeholderText="keyword in name/username")
        self.require_username = QCheckBox("only users with a @username")
        self.include_bots = QCheckBox("include bots")
        self.collect = QPushButton("Collect from chat…")
        self.collect.clicked.connect(self.on_collect)

        self.template_box = QComboBox()
        self.template_box.addItems(self.window.template_store.load())
        self.template_box.currentTextChanged.connect(self.on_template)
        self.template = QPlainTextEdit(placeholderText="message text; {first_name}, {rand: a | b}, \\n")
        self.template.setFixedHeight(130)
        self.preview = QPushButton("Preview")
        self.preview.clicked.connect(self.on_preview)

        self.min_delay = QSpinBox(minimum=0, maximum=3600, value=5, suffix=" s")
        self.max_delay = QSpinBox(minimum=0, maximum=3600, value=15, suffix=" s")
        self.per_hour = QSpinBox(minimum=0, maximum=10000, value=0, specialValueText="no cap")
        for signal in (self.recipients.textChanged, self.template.textChanged, self.min_delay.valueChanged,
                       self.max_delay.valueChanged, self.per_hour.valueChanged):
            signal.connect(self.schedule_preview)
        self._preview_soon = QTimer(self, singleShot=True, interval=200)  # coalesce keystrokes
        self._preview_soon.timeout.connect(self.refresh_preview)

        self.estimate = muted("")
        self.start = QPushButton("Start", objectName="primary")
        self.start.clicked.connect(self.on_start)
        self.stop = QPushButton("Stop")
        self.stop.clicked.connect(self.on_stop)

        form = QFormLayout()
        form.addRow("Recipients", self.recipients)
        form.addRow("Collect", row(self.collect, muted("members to read"), self.limit))
        form.addRow("Filter", row(self.keyword, self.require_username, self.include_bots))
        form.addRow("Template", row(self.template_box, self.preview))
        form.addRow("Text", self.template)
        form.addRow("Pacing", inline(("Delay", self.min_delay), ("to", self.max_delay),
                                     ("Per hour", self.per_hour)))
        form.addRow(self.estimate)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row(self.start, self.stop))
        self._tick = QTimer(self, interval=1000)  # the job ends on its own; poll so the buttons catch up
        self._tick.timeout.connect(self.refresh)
        self._tick.start()
        self.refresh_preview()
        self.refresh()
        window.run(window.call(account, telegram.dialogs), self.on_chats)

    def on_chats(self, future):
        try:
            self.chats = future.result()
        except Exception as e:
            self.collect.setEnabled(False)
            self.collect.setToolTip(str(e))
            self.window.log(f"✗ [{self.account.name or self.account.session}] chats: {type(e).__name__}: {e}")
            return
        self.collect.setEnabled(bool(self.chats))

    def on_collect(self):
        labels = [label for _, label in self.chats]
        label, ok = QInputDialog.getItem(self, "Collect recipients", "Chat to read members from", labels, 0, False)
        if not ok:
            return
        self.collect.setEnabled(False)
        chat_id = self.chats[labels.index(label)][0]
        self.window.run(self.window.call(self.account, telegram.parse_participants, chat_id, self.limit.value()),
                        self.on_collected)

    def on_collected(self, future):
        self.collect.setEnabled(True)
        name = self.account.name or self.account.session
        try:
            users = future.result()
        except Exception as e:
            self.window.log(f"✗ [{name}] collect: {type(e).__name__}: {e}")
            QMessageBox.warning(self, "Could not read participants", str(e))
            return
        kept = parse_filter(to_recipients(users), keyword=self.keyword.text(),
                            require_username=self.require_username.isChecked(),
                            include_bots=self.include_bots.isChecked())
        self.recipients.setPlainText(targets_text(kept))
        self.window.log(f"✓ [{name}] collected {len(kept)} of {len(users)} members")

    def on_template(self, name: str):
        templates = self.window.template_store.load()
        if name in templates:
            self.template.setPlainText(templates[name])

    def on_preview(self):
        text = self.template.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "Preview", "Write the message first.")
            return
        QMessageBox.information(self, "Preview", render(text, PREVIEW_CONTEXT, seed=Random(0)))

    def schedule_preview(self):
        self._preview_soon.start()

    def build_steps(self, seed: Random | None = None) -> list[broadcast.Step]:
        """The plan for the current box/pacing (seed makes the estimate reproducible)."""
        return broadcast.plan(broadcast.parse_targets(self.recipients.toPlainText()), self.template.toPlainText(),
                              min_delay=self.min_delay.value(), max_delay=self.max_delay.value(),
                              per_hour=self.per_hour.value(), seed=seed)

    def refresh_preview(self):
        if self.min_delay.value() > self.max_delay.value():
            self.estimate.setText("min delay must not exceed max delay")
            return
        self.estimate.setText(plan_summary(self.build_steps(seed=Random(0))))

    def on_start(self):
        if not broadcast.parse_targets(self.recipients.toPlainText()) or not self.template.toPlainText().strip():
            QMessageBox.information(self, "Broadcast", "Add recipients and write the message first.")
            return
        if self.min_delay.value() > self.max_delay.value():
            QMessageBox.warning(self, "Broadcast", "Min delay must not exceed max delay.")
            return
        self.window.start_task(self.key, self.window.call(self.account, telegram.send_many, self.build_steps(),
                                                          self.window.emitter(self.account)), "broadcast")
        self.refresh()

    def on_stop(self):
        self.window.stop_task(self.key)
        self.refresh()

    def refresh(self):
        running = self.window.task_running(self.key)
        self.start.setEnabled(not running)
        self.stop.setEnabled(running)


class AutoPostDialog(QDialog):
    """Queue a series of posts into Telegram's scheduler: one post per '---' block, evenly spaced."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.setWindowTitle(f"Auto-posting — {account.name or account.session}")
        self.setMinimumSize(520, 520)

        self.chat = QComboBox()
        self.chat.addItem("Loading…")
        self.posts = QPlainTextEdit(placeholderText="post text; separate posts with a line of ---")
        self.when = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600), calendarPopup=True,
                                  displayFormat="yyyy-MM-dd HH:mm")
        self.interval = QSpinBox(minimum=1, maximum=720, value=24, suffix=" h")
        self.count = muted("")
        self.schedule = QPushButton("Schedule series", objectName="primary", enabled=False)
        self.schedule.clicked.connect(self.on_schedule)
        self.now = QPushButton("Post first now", enabled=False)
        self.now.clicked.connect(self.on_now)
        for signal in (self.posts.textChanged, self.when.dateTimeChanged, self.interval.valueChanged):
            signal.connect(self.refresh_count)

        form = QFormLayout()
        form.addRow("Chat", self.chat)
        form.addRow("Posts", self.posts)
        form.addRow(muted("A line of --- separates posts. Telegram posts them itself, "
                          "even with Omnigram closed."))
        form.addRow("First at", self.when)
        form.addRow("Every", self.interval)
        form.addRow(self.count)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row(self.schedule, self.now))
        self.refresh_count()
        window.run(window.call(account, telegram.dialogs, "post_messages"), self.on_chats)

    def on_chats(self, future):
        try:
            chats = future.result()
        except Exception as e:
            self.chat.clear()
            self.chat.addItem(f"Could not load chats: {e}")
            self.schedule.setEnabled(False)
            self.now.setEnabled(False)
            return
        fill_chats(self.chat, chats, "No channel or group where this account may post as admin")
        self.schedule.setEnabled(bool(chats))
        self.now.setEnabled(bool(chats))

    def refresh_count(self, *_):
        posts = split_posts(self.posts.toPlainText())
        if not posts:
            self.count.setText("no posts yet")
            return
        last = self.when.dateTime().addSecs(self.interval.value() * 3600 * (len(posts) - 1))
        plural = "" if len(posts) == 1 else "s"
        self.count.setText(f"{len(posts)} post{plural} · last one at {last.toString('yyyy-MM-dd HH:mm')}")

    def set_busy(self, busy: bool):
        usable = not busy and self.chat.currentData() is not None
        self.schedule.setEnabled(usable)
        self.now.setEnabled(usable)

    def on_schedule(self):
        posts = split_posts(self.posts.toPlainText())
        if not posts or self.chat.currentData() is None:
            QMessageBox.information(self, "Auto-posting", "Pick a chat and write at least one post.")
            return
        first = self.when.dateTime().toPython().astimezone()
        if first < datetime.now().astimezone() + timedelta(minutes=1):
            QMessageBox.warning(self, "Auto-posting", "Pick a time at least a minute ahead.")
            return
        self.set_busy(True)
        self.window.run(self.window.call(self.account, telegram.schedule_series, self.chat.currentData(), posts,
                                         first, float(self.interval.value())), self.on_scheduled)

    def on_now(self):
        posts = split_posts(self.posts.toPlainText())
        if not posts or self.chat.currentData() is None:
            QMessageBox.information(self, "Auto-posting", "Pick a chat and write at least one post.")
            return
        self.set_busy(True)
        self.window.run(self.window.call(self.account, telegram.post_now, self.chat.currentData(), posts[0]),
                        self.on_posted)

    def on_scheduled(self, future):
        self.set_busy(False)
        name = self.account.name or self.account.session
        try:
            count = future.result()
        except Exception as e:
            self.window.log(f"✗ [{name}] auto-post: {type(e).__name__}: {e}")
            QMessageBox.warning(self, "Could not schedule", str(e))
            return
        self.window.log(f"✓ [{name}] {count} post(s) queued in {self.chat.currentText()}")

    def on_posted(self, future):
        self.set_busy(False)
        name = self.account.name or self.account.session
        try:
            future.result()
        except Exception as e:
            self.window.log(f"✗ [{name}] post: {type(e).__name__}: {e}")
            QMessageBox.warning(self, "Could not post", str(e))
            return
        self.window.log(f"✓ [{name}] posted the first post to {self.chat.currentText()}")


class WatchDialog(QDialog):
    """Auto-reactions/comments: react to every new post in the ticked chats, optionally comment too."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"watch/{account.session}"
        self.setWindowTitle(f"Auto-comments & reactions — {account.name or account.session}")
        self.setMinimumSize(520, 560)

        self.list = QListWidget()
        self.list.addItem("Loading…")
        self.reaction = QLineEdit("👍", placeholderText="empty = no reaction")
        self.comments = QPlainTextEdit(placeholderText="one comment per line; empty = react only")
        self.comments.setFixedHeight(90)
        self.start = QPushButton("Start", objectName="primary")
        self.start.clicked.connect(self.on_start)
        self.stop = QPushButton("Stop")
        self.stop.clicked.connect(self.on_stop)

        form = QFormLayout()
        form.addRow("Chats", self.list)
        form.addRow(muted("Tick the chats to watch: every new post or message gets the reaction below."))
        form.addRow("Reaction", self.reaction)
        form.addRow("Comments", self.comments)
        form.addRow(muted("Comments need a channel with a linked discussion group; groups get reactions only."))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row(self.start, self.stop))
        self._tick = QTimer(self, interval=1000)  # the runner ends on its own; poll so the buttons catch up
        self._tick.timeout.connect(self.refresh)
        self._tick.start()
        self.refresh()
        window.run(window.call(account, telegram.dialogs), self.on_chats)

    def on_chats(self, future):
        self.list.clear()
        try:
            chats = future.result()
        except Exception as e:
            self.list.addItem(f"Could not load chats: {e}")
            return
        if not chats:
            self.list.addItem("Nothing here.")
        for chat_id, label in chats:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, chat_id)
            item.setCheckState(Qt.Unchecked)
            self.list.addItem(item)

    def ticked(self) -> list[int]:
        items = (self.list.item(i) for i in range(self.list.count()))
        return [item.data(Qt.UserRole) for item in items
                if item.checkState() == Qt.Checked and isinstance(item.data(Qt.UserRole), int)]

    def on_start(self):
        ids = self.ticked()
        if not ids:
            QMessageBox.information(self, "Auto-comments", "Tick at least one chat to watch.")
            return
        self.window.start_task(self.key, self.window.call(self.account, telegram.watch_react, ids,
                                                          comment_texts(self.comments.toPlainText()),
                                                          self.reaction.text().strip(),
                                                          self.window.emitter(self.account)), "watching")
        self.refresh()

    def on_stop(self):
        self.window.stop_task(self.key)
        self.refresh()

    def refresh(self):
        running = self.window.task_running(self.key)
        self.start.setEnabled(not running)
        self.stop.setEnabled(running)


class CommentNowDialog(QDialog):
    """Comment on a channel's newest posts right now, through its linked discussion group."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.setWindowTitle(f"Comment on latest posts — {account.name or account.session}")
        self.setMinimumWidth(460)

        self.chat = QComboBox()
        self.chat.addItem("Loading…")
        self.texts = QPlainTextEdit(placeholderText="one comment per line; reused in order")
        self.texts.setFixedHeight(110)
        self.limit = QSpinBox(minimum=1, maximum=50, value=5)
        self.run = QPushButton("Run", objectName="primary", enabled=False)
        self.run.clicked.connect(self.on_run)

        form = QFormLayout()
        form.addRow("Chat", self.chat)
        form.addRow("Comments", self.texts)
        form.addRow(muted("The account must be able to comment, so it has to be in the channel's "
                          "discussion group."))
        form.addRow("Newest posts to comment on", self.limit)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.run)
        window.run(window.call(account, telegram.dialogs), self.on_chats)

    def on_chats(self, future):
        try:
            chats = future.result()
        except Exception as e:
            self.chat.clear()
            self.chat.addItem(f"Could not load chats: {e}")
            self.run.setEnabled(False)
            return
        fill_chats(self.chat, chats, "No chats for this account")
        self.run.setEnabled(bool(chats))

    def on_run(self):
        texts = comment_texts(self.texts.toPlainText())
        if not texts or self.chat.currentData() is None:
            QMessageBox.information(self, "Comment", "Pick a chat and write at least one comment.")
            return
        self.run.setEnabled(False)
        self.window.run(self.window.call(self.account, telegram.comment_latest, self.chat.currentData(), texts,
                                         self.limit.value()), self.on_done)

    def on_done(self, future):
        self.run.setEnabled(True)
        name = self.account.name or self.account.session
        try:
            count = future.result()
        except Exception as e:
            self.window.log(f"✗ [{name}] comment: {type(e).__name__}: {e}")
            QMessageBox.warning(self, "Could not comment", str(e))
            return
        self.window.log(f"✓ [{name}] commented on {count} post(s) in {self.chat.currentText()}")
