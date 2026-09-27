"""Promotion dialogs: mass joining, inviting users, channel boosting, story views.

The non-trivial parsing (what a pasted box means, how an @name resolves against a chat's members)
lives in module-level pure functions so it is testable without a network or a display. The dialogs
only wire those to widgets and to the ``telegram`` coroutines through ``window.run``/``start_task``.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from omnigram import telegram
from omnigram.broadcast import Recipient, parse_targets
from omnigram.parser import parse_filter
from omnigram.store import Account


@dataclass
class Participant(Recipient):
    """A broadcast.Recipient that also carries parse_participants' bot/deleted flags, so that
    parser.parse_filter can apply its audience rules to fetched members."""
    bot: bool = False
    deleted: bool = False


def parse_links(text: str) -> list[str]:
    """Paste-friendly link list: one per line, blank lines and '#…' comments dropped, order kept,
    exact duplicates collapsed."""
    out, seen = [], set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def pick_by_username(rows: list[dict], username: str) -> int | None:
    """The id of the participant whose username matches (case-insensitive, '@' optional), else None."""
    wanted = (username or "").strip().lstrip("@").lower()
    for row in rows:
        if wanted and (row.get("username") or "").lower() == wanted:
            return row.get("id")
    return None


def peer_targets(text: str) -> list[int | str]:
    """Pasted peers -> what telegram.view_stories wants: numeric ids as int, '@names' left as strings
    for Telethon to resolve. broadcast.parse_targets keeps the order and drops duplicates."""
    return [r.id if r.id else f"@{r.username}" for r in parse_targets(text)]


def collect_lines(users: list[dict]) -> list[str]:
    """telegram.parse_participants dicts -> the users box's lines: '@username', else the numeric id.
    Bots and deleted accounts are dropped (parser.parse_filter's default audience rules)."""
    people = [Participant(id=u.get("id", 0), first_name=u.get("first_name", ""), last_name=u.get("last_name", ""),
                          username=u.get("username", ""), phone=u.get("phone", ""),
                          bot=bool(u.get("bot")), deleted=bool(u.get("deleted"))) for u in users]
    return [f"@{p.username}" if p.username else str(p.id) for p in parse_filter(people)]


def resolve_usernames(window, account: Account, chat_id: int, usernames: list[str], on_result, done, log):
    """Resolve '@names' to user ids one at a time: parse_participants(search=name) on `chat_id`, then
    pick_by_username. on_result(id) per hit, done() once the list is exhausted, log(line) per miss."""
    pending = list(usernames)

    def step():
        if not pending:
            done()
            return
        name = pending.pop(0)
        window.run(window.call(account, telegram.parse_participants, chat_id, 0, name),
                   lambda f, n=name: land(n, f))

    def land(name, future):
        try:
            user_id = pick_by_username(future.result(), name)
        except Exception as e:
            log(f"@{name}: {type(e).__name__}: {e}")
        else:
            if user_id:
                on_result(user_id)
            else:
                log(f"@{name} is not in this chat")
        step()

    step()


class JoinDialog(QDialog):
    """Join many accounts to channels/groups by link, one account+link pair at a time.

    Sequential by design: each pair gets its own short connection through window.run, so one account
    at a time is connecting and the log stays readable. Stop drops the rest of the queue.
    """

    def __init__(self, window, accounts: list[Account]):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Subscription / mass joining")
        self.setMinimumSize(480, 400)
        self.links = QPlainTextEdit(placeholderText="@name, t.me/… or https://t.me/+hash, one per line")
        self.progress = QLabel("0/0")
        self.run_button = QPushButton("Run", objectName="primary")
        self.run_button.clicked.connect(self.on_run)
        self.stop = QPushButton("Stop", enabled=False)
        self.stop.clicked.connect(self.on_stop)
        allowed = window.allow_connect(accounts)
        if allowed is None:
            self.allowed, note = [], "Cancelled — nothing will be joined."
        else:
            skipped = len(accounts) - len(allowed)
            self.allowed = allowed
            note = f"{skipped} proxy-less account(s) skipped." if skipped else ""
        row = QHBoxLayout()
        row.addWidget(self.progress)
        row.addWidget(self.run_button)
        row.addWidget(self.stop)
        row.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.links)
        layout.addWidget(QLabel("Joining is done by the selected accounts; Telegram's own limits apply.",
                                objectName="muted"))
        layout.addWidget(QLabel(note, objectName="muted"))
        layout.addLayout(row)
        self.queue: list[tuple[Account, str]] = []
        self.total = self.done = 0
        self.running = False
        self.run_button.setEnabled(bool(self.allowed))

    def on_run(self):
        links = parse_links(self.links.toPlainText())
        if not links:
            QMessageBox.information(self, "Mass joining", "Add at least one link first.")
            return
        self.queue = [(account, link) for link in links for account in self.allowed]
        self.total, self.done, self.running = len(self.queue), 0, True
        self.run_button.setEnabled(False)
        self.stop.setEnabled(True)
        self.links.setEnabled(False)
        self.next_pair()

    def next_pair(self):
        if not self.running:
            return
        if not self.queue:
            self.finish("finished")
            return
        account, link = self.queue.pop(0)
        self.window.run(self.window.call(account, telegram.join, link),
                        lambda f, a=account, l=link: self.on_joined(a, l, f))

    def on_joined(self, account, link, future):
        name = account.name or account.session
        try:
            self.window.log(f"✓ [{name}] joined {future.result()}")
        except Exception as e:
            self.window.log(f"✗ [{name}] {link}: {type(e).__name__}: {e}")
        self.done += 1
        self.progress.setText(f"{self.done}/{self.total}")
        self.next_pair()

    def on_stop(self):
        self.running = False
        self.queue.clear()
        self.finish("stopped")

    def finish(self, what: str):
        self.running = False
        self.run_button.setEnabled(bool(self.allowed))
        self.stop.setEnabled(False)
        self.links.setEnabled(True)
        self.window.log(f"◉ mass joining {what} ({self.done}/{self.total})")


class InviterDialog(QDialog):
    """Invite @usernames / numeric ids to a group or channel this account administers."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"invite/{account.session}"
        self.name = account.name or account.session
        self.chats: list[tuple[int, str]] = []
        self.chat_id: int | None = None
        self.ids: list[int] = []
        self.resolving = False
        self.setWindowTitle(f"Inviter — {self.name}")
        self.setMinimumWidth(480)
        self.chat = QComboBox()
        self.chat.addItem("Loading…")
        self.users = QPlainTextEdit(placeholderText="@usernames or numeric ids, one per line")
        self.users.setFixedHeight(120)
        collect = QPushButton("Collect from chat…")
        collect.clicked.connect(self.on_collect)
        self.run_button = QPushButton("Run", objectName="primary")
        self.run_button.clicked.connect(self.on_run)
        self.stop = QPushButton("Stop")
        self.stop.clicked.connect(self.on_stop)
        row = QHBoxLayout()
        row.addWidget(self.run_button)
        row.addWidget(self.stop)
        row.addStretch()
        form = QFormLayout()
        form.addRow("Chat", self.chat)
        form.addRow("Users", self.users)
        form.addRow(collect)
        form.addRow(QLabel("Inviting is done by this account; Telegram's own limits apply.", objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row)
        window.run(window.call(account, telegram.dialogs, "invite_users"), self.on_loaded)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()

    def on_loaded(self, future):
        self.chat.clear()
        try:
            self.chats = future.result()
        except Exception as e:
            self.chat.addItem(f"Could not load chats: {e}")
            return
        for chat_id, label in self.chats:
            self.chat.addItem(label, chat_id)
        if not self.chats:
            self.chat.addItem("No group or channel where this account may invite")
        self.refresh()

    def refresh(self):
        running = self.window.task_running(self.key)
        self.run_button.setEnabled(not running and not self.resolving and bool(self.chats))
        self.stop.setEnabled(running)

    def on_collect(self):
        if not self.chats:
            QMessageBox.information(self, "Collect from chat", "No chat loaded yet.")
            return
        labels = [label for _, label in self.chats]
        label, ok = QInputDialog.getItem(self, "Collect from chat", "Chat", labels, 0, False)
        if not ok:
            return
        chat_id = next(cid for cid, lab in self.chats if lab == label)
        self.window.run(self.window.call(self.account, telegram.parse_participants, chat_id, 0, ""),
                        self.on_collected)

    def on_collected(self, future):
        try:
            rows = future.result()
        except Exception as e:
            QMessageBox.warning(self, "Collect from chat", str(e))
            self.window.log(f"✗ [{self.name}] collect: {type(e).__name__}: {e}")
            return
        lines = collect_lines(rows)
        self.users.setPlainText("\n".join(lines))
        self.window.log(f"✓ [{self.name}] collected {len(lines)} user(s) from the chat")

    def on_run(self):
        chat_id = self.chat.currentData()
        if chat_id is None:
            QMessageBox.information(self, "Inviter", "Pick a chat first.")
            return
        targets = parse_targets(self.users.toPlainText())
        if not targets:
            QMessageBox.information(self, "Inviter", "Add some @usernames or numeric ids first.")
            return
        self.chat_id = chat_id
        self.ids = [t.id for t in targets if t.id]
        self.resolving = True
        self.refresh()
        resolve_usernames(self.window, self.account, chat_id, [t.username for t in targets if not t.id],
                          self.ids.append, self.start,
                          lambda line: self.window.log(f"✗ [{self.name}] {line}"))

    def start(self):
        self.resolving = False
        if not self.ids:
            QMessageBox.warning(self, "Inviter", "No user could be resolved to invite.")
            self.refresh()
            return
        self.window.start_task(self.key, self.window.call(self.account, telegram.invite_users, self.chat_id,
                                                          self.ids, self.window.emitter(self.account)), "inviting")
        self.refresh()

    def on_stop(self):
        self.window.stop_task(self.key)
        self.refresh()


class BoostDialog(QDialog):
    """Apply one premium boost slot to a channel with this account."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.setWindowTitle(f"Boost — {account.name or account.session}")
        self.setMinimumWidth(440)
        self.chat = QComboBox()
        self.chat.addItem("Loading…")
        self.boost = QPushButton("Boost", objectName="primary", enabled=False)
        self.boost.clicked.connect(self.on_boost)
        form = QFormLayout()
        form.addRow("Channel", self.chat)
        form.addRow(QLabel("Needs a Premium account that may boost this channel.", objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.boost)
        window.run(window.call(account, telegram.dialogs), self.on_loaded)

    def on_loaded(self, future):
        self.chat.clear()
        try:
            chats = future.result()
        except Exception as e:
            self.chat.addItem(f"Could not load chats: {e}")
            return
        for chat_id, label in chats:
            self.chat.addItem(label, chat_id)
        if not chats:
            self.chat.addItem("No chats")
        self.boost.setEnabled(bool(chats))

    def on_boost(self):
        self.boost.setEnabled(False)
        self.window.run(self.window.call(self.account, telegram.boost, self.chat.currentData()), self.on_boosted)

    def on_boosted(self, future):
        self.boost.setEnabled(True)
        self.window.log_result(self.account, future, lambda title: f"boosted {title}")


class StoryViewDialog(QDialog):
    """View the current stories of chosen peers as this account's own reads."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"stories/{account.session}"
        self.name = account.name or account.session
        self.chats: list[tuple[int, str]] = []
        self.setWindowTitle(f"Story views — {self.name}")
        self.setMinimumWidth(480)
        self.peers = QPlainTextEdit(placeholderText="@usernames or numeric ids, one per line")
        self.peers.setFixedHeight(140)
        pick = QPushButton("Pick from chats…")
        pick.clicked.connect(self.on_pick)
        self.run_button = QPushButton("Run", objectName="primary")
        self.run_button.clicked.connect(self.on_run)
        self.stop = QPushButton("Stop")
        self.stop.clicked.connect(self.on_stop)
        row = QHBoxLayout()
        row.addWidget(self.run_button)
        row.addWidget(self.stop)
        row.addStretch()
        form = QFormLayout()
        form.addRow("Peers", self.peers)
        form.addRow(pick)
        form.addRow(QLabel("Views each peer's current stories; progress appears in the log.",
                           objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row)
        window.run(window.call(account, telegram.dialogs), self.on_loaded)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()

    def on_loaded(self, future):
        try:
            self.chats = future.result()
        except Exception:  # the picker is optional; pasted peers still work
            self.chats = []

    def refresh(self):
        running = self.window.task_running(self.key)
        self.run_button.setEnabled(not running)
        self.stop.setEnabled(running)

    def on_pick(self):
        if not self.chats:
            QMessageBox.information(self, "Pick from chats", "No chats loaded yet.")
            return
        box = QDialog(self)
        box.setWindowTitle("Pick from chats")
        box.setMinimumSize(420, 360)
        listing = QListWidget()
        for chat_id, label in self.chats:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, chat_id)
            item.setCheckState(Qt.Unchecked)
            listing.addItem(item)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(box.accept)
        buttons.rejected.connect(box.reject)
        layout = QVBoxLayout(box)
        layout.addWidget(listing)
        layout.addWidget(buttons)
        if box.exec() != QDialog.Accepted:
            return
        picked = [str(listing.item(i).data(Qt.UserRole)) for i in range(listing.count())
                  if listing.item(i).checkState() == Qt.Checked]
        if picked:
            self.peers.setPlainText("\n".join(picked))

    def on_run(self):
        peers = peer_targets(self.peers.toPlainText())
        if not peers:
            QMessageBox.information(self, "Story views", "Add some peers first.")
            return
        self.window.start_task(self.key, self.window.call(self.account, telegram.view_stories, peers,
                                                          self.window.emitter(self.account)), "viewing stories")
        self.refresh()

    def on_stop(self):
        self.window.stop_task(self.key)
        self.refresh()
