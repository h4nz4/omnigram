"""Single-account dialogs: 2FA manager, active sessions, profile editor, chat lists, scheduler, listeners.

Each takes the MainWindow (for .run()/.credentials()/.store) and one Account, fetches its
current state over Telethon on open, and re-fetches after every change.
"""
import functools
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omnigram import icons, proxies, telegram
from omnigram.loading import LoadingOverlay
from omnigram.store import Account, Proxy



class PasswordDialog(QDialog):
    """Account → 2FA: shows whether cloud password is on, and lets you set/change/remove it."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.setWindowTitle(f"2FA — {account.name or account.session}")
        self.setMinimumWidth(360)

        self.state_label = QLabel("Loading current state…")
        self.current = QLineEdit(echoMode=QLineEdit.Password, placeholderText="required if 2FA is already on")
        self.new = QLineEdit(echoMode=QLineEdit.Password, placeholderText="leave blank to remove the password")
        self.hint = QLineEdit(placeholderText="optional hint")
        self.save = QPushButton("Save", objectName="primary")
        self.save.clicked.connect(self.on_save)

        form = QFormLayout()
        form.addRow(self.state_label)
        form.addRow("Current password", self.current)
        form.addRow("New password", self.new)
        form.addRow("Hint", self.hint)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.save)
        self.overlay = LoadingOverlay(self, window.run)
        self.load()

    def load(self):
        self.overlay.run(self.window.call(self.account, telegram.password_state), "Reading the 2FA state…",
                         self.on_state)

    def on_state(self, future):
        try:
            state = future.result()
        except Exception as e:
            self.state_label.setText(f"Could not read password state: {e}")
            return
        self.state_label.setText(
            f"2FA is currently {'ON' if state['has_password'] else 'OFF'}" +
            (f" (hint: {state['hint']})" if state["hint"] else ""))
        self.current.setEnabled(state["has_password"])

    def on_save(self):
        coro = self.window.call(self.account, telegram.set_password, self.current.text(), self.new.text(),
                                self.hint.text())
        self.current.clear(), self.new.clear()  # never kept around, whatever the outcome
        self.overlay.run(coro, "Updating the 2FA password…", self.on_saved, change=True)

    def on_saved(self, future):
        try:
            future.result()
        except Exception as e:
            QMessageBox.warning(self, "Could not change password", str(e))
            return
        self.window.log(f"✓ [{self.account.name or self.account.session}] 2FA password updated")
        self.hint.clear()
        self.load()


class SessionsDialog(QDialog):
    """Account → active sessions: list Telegram's own device list and let you sign the others out."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.setWindowTitle(f"Sessions & access — {account.name or account.session}")
        self.setMinimumSize(480, 320)

        self.list = QListWidget()
        self.terminate_all = QPushButton("Sign out all other sessions")
        self.terminate_all.clicked.connect(self.on_terminate_all)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list)
        layout.addWidget(self.terminate_all)
        layout.addWidget(buttons)
        self.overlay = LoadingOverlay(self, window.run)
        self.reload()

    def reload(self):
        self.overlay.run(self.window.call(self.account, telegram.authorizations), "Loading active sessions…",
                         self.on_loaded)

    def on_loaded(self, future):
        self.list.clear()
        try:
            sessions = future.result()
        except Exception as e:
            self.list.addItem(f"Could not load sessions: {e}")
            return
        for s in sessions:
            row = QWidget()
            text = QLabel(f"{'★ ' if s['current'] else ''}{s['device']} — {s['app']}\n"
                         f"{s['ip']} · {s['location']} · last active {s['active']}")
            box = QHBoxLayout(row)
            box.setContentsMargins(4, 4, 4, 4)
            box.addWidget(text, 1)
            if not s["current"]:
                kick = QPushButton("Sign out")
                kick.clicked.connect(lambda _, h=s["hash"]: self.on_terminate(h))
                box.addWidget(kick)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)

    def on_terminate(self, hash_: int):
        self.overlay.run(self.window.call(self.account, telegram.terminate_authorization, hash_),
                         "Signing that session out…", self.on_terminated, change=True)

    def on_terminate_all(self):
        self.overlay.run(self.window.call(self.account, telegram.terminate_other_authorizations),
                         "Signing out all other sessions…", self.on_terminated, change=True)

    def on_terminated(self, future):
        try:
            future.result()
        except Exception as e:
            QMessageBox.warning(self, "Could not sign out", str(e))
            return
        self.reload()


def profile_changes(before: dict, fields: dict) -> dict:
    """The fields the user actually changed, ready for telegram.update_profile. Values are trimmed and a leading
    '@' is dropped from the username. Only changes are sent: an omitted field stays as it is on Telegram, and an
    unchanged username would be rejected (USERNAME_NOT_MODIFIED)."""
    after = {key: value.strip() for key, value in fields.items()}
    after["username"] = after.get("username", "").lstrip("@")
    return {key: value for key, value in after.items() if value != before.get(key, "")}


class ProfileDialog(QDialog):
    """Account → profile: edit the name/username/bio Telegram shows for this account. The current values are
    loaded from Telegram on open (the local cache has no bio, and may be stale), and Save sends only changes."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.setWindowTitle(f"Profile — {account.name or account.session}")
        self.setMinimumWidth(360)
        self.before: dict = {}

        self.first = QLineEdit()
        self.last = QLineEdit()
        self.username = QLineEdit(placeholderText="none")
        self.about = QLineEdit(placeholderText="none")
        self.save = QPushButton("Save", objectName="primary")
        self.save.clicked.connect(self.on_save)

        form = QFormLayout()
        form.addRow("First name", self.first)
        form.addRow("Last name", self.last)
        form.addRow("Username", self.username)
        form.addRow("Bio", self.about)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.save)
        self.overlay = LoadingOverlay(self, window.run)
        self.overlay.run(window.call(account, telegram.get_profile), "Loading the profile…", self.on_loaded)

    def fields(self) -> dict:
        return {"first_name": self.first.text(), "last_name": self.last.text(), "username": self.username.text(),
                "about": self.about.text()}

    def on_loaded(self, future):
        try:
            self.before = future.result()
        except Exception as e:
            QMessageBox.warning(self, "Could not load the profile", str(e))
            self.reject()
            return
        self.first.setText(self.before["first_name"])
        self.last.setText(self.before["last_name"])
        self.username.setText(self.before["username"])
        self.about.setText(self.before["about"])

    def on_save(self):
        changes = profile_changes(self.before, self.fields())
        if not changes:
            self.accept()
            return
        self.overlay.run(self.window.call(self.account, functools.partial(telegram.update_profile, **changes)),
                         "Saving the profile…", self.on_saved, change=True)

    def on_saved(self, future):
        try:
            result = future.result()
        except Exception as e:
            QMessageBox.warning(self, "Could not update profile", str(e))
            return
        self.account.name, self.account.username = result["name"], result["username"]
        self.window.model.account_changed(self.account)
        self.window.changed()
        self.window.log(f"✓ [{self.account.name or self.account.session}] profile updated")
        self.accept()


class InfoDialog(QDialog):
    """Read-only text fetched once: render(result) -> str, shown when `coro` finishes."""

    def __init__(self, window, title: str, coro, render, status: str = "Loading…"):
        super().__init__(window)
        self.setWindowTitle(title)
        self.setMinimumSize(460, 360)
        self.render = render
        self.text = QPlainTextEdit(readOnly=True)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        layout.addWidget(buttons)
        self.overlay = LoadingOverlay(self, window.run)
        self.overlay.run(coro, status, self.on_done)

    def on_done(self, future):
        try:
            self.text.setPlainText(self.render(future.result()))
        except Exception as e:
            self.text.setPlainText(f"Failed: {type(e).__name__}: {e}")


class ChatsDialog(QDialog):
    """A tickable list of the account's chats plus buttons that act on the ticked ones.

    load() -> coroutine giving [(chat_id, label)]. actions: [(label, start(ids) -> coroutine | None, done(result) -> str)];
    `start` may ask the user things first and return None to cancel. The list reloads after every action.
    """

    def __init__(self, window, account: Account, title: str, load, actions):
        super().__init__(window)
        self.window, self.account, self.load = window, account, load
        self.setWindowTitle(f"{title} — {account.name or account.session}")
        self.setMinimumSize(520, 440)
        self.list = QListWidget()
        self.buttons: list[QPushButton] = []
        row = QHBoxLayout()
        for label, start, done in actions:
            button = QPushButton(label)
            button.clicked.connect(lambda _, lb=label, s=start, d=done: self.on_action(lb, s, d))
            self.buttons.append(button)
            row.addWidget(button)
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list)
        layout.addLayout(row)
        self.overlay = LoadingOverlay(self, window.run)
        self.reload()

    def reload(self):
        self.overlay.run(self.load(), "Loading chats…", self.on_loaded)

    def on_loaded(self, future):
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
        return [item.data(Qt.UserRole) for item in items if item.checkState() == Qt.Checked]

    def on_action(self, label, start, done):
        ids = self.ticked()
        if not ids:
            QMessageBox.information(self, "Nothing ticked", "Tick at least one chat first.")
            return
        coro = start(ids)
        if coro is None:
            return
        self.overlay.run(coro, f"{label.rstrip('…')} — {len(ids)} chat(s)…", lambda f: self.on_done(f, done),
                         change=True)

    def on_done(self, future, done):
        name = self.account.name or self.account.session
        try:
            self.window.log(f"✓ [{name}] {done(future.result())}")
        except Exception as e:
            self.window.log(f"✗ [{name}] {type(e).__name__}: {e}")
            QMessageBox.warning(self, "Failed", str(e))
        self.reload()


class ScheduleDialog(QDialog):
    """Queue a post in Telegram's scheduled messages, only for channels/groups the account may post to as admin."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.setWindowTitle(f"Schedule post — {account.name or account.session}")
        self.setMinimumWidth(460)
        self.chat = QComboBox()
        self.text = QPlainTextEdit()
        self.when = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600), calendarPopup=True,
                                  displayFormat="yyyy-MM-dd HH:mm")
        self.save = QPushButton("Schedule", objectName="primary", enabled=False)
        self.save.clicked.connect(self.on_save)
        form = QFormLayout()
        form.addRow("Chat", self.chat)
        form.addRow("Text", self.text)
        form.addRow("Send at", self.when)
        form.addRow(QLabel("Telegram sends it at that time even if Omnigram is closed.", objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.save)
        self.overlay = LoadingOverlay(self, window.run)
        self.overlay.run(window.call(account, telegram.dialogs, "post_messages"),
                         "Loading the chats this account may post to…", self.on_loaded)

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
            self.chat.addItem("No channel or group where this account may post as admin")
        self.save.setEnabled(bool(chats))

    def on_save(self):
        when = self.when.dateTime().toPython().astimezone()
        if not self.text.toPlainText().strip() or when < datetime.now().astimezone() + timedelta(minutes=1):
            QMessageBox.warning(self, "Schedule post", "Write some text and pick a time at least a minute ahead.")
            return
        self.overlay.run(self.window.call(self.account, telegram.schedule_post, self.chat.currentData(),
                                          self.text.toPlainText(), when), "Scheduling the post…", self.on_saved,
                         change=True)

    def on_saved(self, future):
        try:
            future.result()
        except Exception as e:
            QMessageBox.warning(self, "Could not schedule", str(e))
            return
        self.window.log(f"✓ [{self.account.name or self.account.session}] post scheduled in "
                        f"{self.chat.currentText()} for {self.when.dateTime().toString('yyyy-MM-dd HH:mm')}")
        self.accept()


class ListenerDialog(QDialog):
    """Word monitoring + auto-responder + channel moderator for one account: one connection, three optional jobs."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"listen/{account.session}"
        self.setWindowTitle(f"Listener — {account.name or account.session}")
        self.setMinimumWidth(480)
        saved = json.loads(window.settings.get(self.key, "{}"))
        self.keywords = QLineEdit(saved.get("keywords", ""), placeholderText="comma-separated; empty = off")
        self.away = QPlainTextEdit(saved.get("away", ""), placeholderText="empty = off")
        self.away.setFixedHeight(70)
        self.first_dm = QPlainTextEdit(saved.get("first_dm", ""),
                                       placeholderText="empty = off; supports {first_name}, {rand: a | b}")
        self.first_dm.setFixedHeight(70)
        self.banned = QLineEdit(saved.get("banned", ""), placeholderText="comma-separated; empty = off")
        self.toggle = QPushButton(objectName="primary")
        self.toggle.clicked.connect(self.on_toggle)
        form = QFormLayout()
        form.addRow("Word monitoring", self.keywords)
        form.addRow(QLabel("Logs incoming messages that contain a keyword, in any chat the account is in.",
                           objectName="muted"))
        form.addRow("Auto-responder", self.away)
        form.addRow(QLabel("Replies once per person to incoming private messages (not bots).", objectName="muted"))
        form.addRow("Link on first DM", self.first_dm)
        form.addRow(QLabel("Sent instead of the auto-responder text the first time each person writes.",
                           objectName="muted"))
        form.addRow(QLabel("AI replies moved to the AI autopilot (sidebar → Content → AI autopilot) and "
                           "the chat window's Draft/Auto modes.", objectName="muted"))
        form.addRow("Moderator: banned words", self.banned)
        form.addRow(QLabel("Deletes group messages containing these, where the account may delete messages.",
                           objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.toggle)
        self.refresh()

    def refresh(self):
        running = self.window.task_running(self.key)
        self.toggle.setText("Stop" if running else "Start")
        for field in (self.keywords, self.away, self.first_dm, self.banned):
            field.setEnabled(not running)

    def on_toggle(self):
        if self.window.task_running(self.key):
            self.window.stop_task(self.key)
        else:
            config = {"keywords": self.keywords.text(), "away": self.away.toPlainText().strip(),
                      "first_dm": self.first_dm.toPlainText().strip(), "banned": self.banned.text()}
            if not any(config.values()):
                QMessageBox.information(self, "Listener", "Fill in at least one of the jobs.")
                return
            self.window.settings.set(self.key, json.dumps(config))
            self.window.start_task(self.key, self.window.call(
                self.account, telegram.listen, telegram.words(config["keywords"]), config["away"],
                telegram.words(config["banned"]), self.window.emitter(self.account), config["first_dm"]),
                f"listener [{self.account.name or self.account.session}]")
        self.refresh()


class ProxyPage(QWidget):
    """The Proxies page: proxy pool on the left, accounts on the right. An account's proxy is its URL
    (Account.proxy); the pool (names, ping, geo) is saved to proxies.json. Proxies whose last ping failed are
    skipped when distributing.

    The account side is a view on the Accounts page's own model, so ticks are shared between the two pages
    and it stays cheap at thousands of accounts. `account_filter` is a fresh AccountFilter (window.py owns
    that class), so this page filters independently of the Accounts page."""

    COLUMNS = ["#", "Name", "Type", "Host:Port", "Accts", "Ping", "Geo"]
    ACCOUNT_COLUMNS = (0, 2, 8)  # tick, name, proxy — of window.COLUMNS
    SHOW = [("All accounts", {}), ("Without proxy", {"has_proxy": False}), ("With proxy", {"has_proxy": True}),
            ("On the selected proxy", None)]  # None: filled from the pool selection

    def __init__(self, window, account_filter):
        super().__init__(objectName="page")
        self.window = window
        self._refresh_soon = QTimer(self, singleShot=True, interval=300)  # coalesces table rebuilds during probes
        self._refresh_soon.timeout.connect(self.refresh)
        self.pool: list[Proxy] = []

        self.table = QTableWidget(0, len(self.COLUMNS), showGrid=False, wordWrap=False)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        for col in (1, 3):  # Name, Host:Port share what's left
            head.setSectionResizeMode(col, QHeaderView.Stretch)
        self.table.doubleClicked.connect(self.edit)

        tools = QGridLayout()
        for i, (text, icon, name, handler) in enumerate([
            ("Add", "plus", "", self.add), ("Edit", "pencil", "", self.edit),
            ("Delete", "trash", "danger", self.delete), ("Ping all", "activity", "", self.ping_all),
            ("Geo all", "globe", "", self.geo_all), ("Import", "file-input", "", self.import_list),
            ("Reseat", "shuffle", "", self.reseat),
        ]):
            button = QPushButton(icons.get(icon), text, objectName=name)
            button.clicked.connect(handler)
            tools.addWidget(button, i // 4, i % 4)
        tools.setColumnStretch(4, 1)

        self.filter = account_filter
        self.filter.setSourceModel(window.model)
        self.filter.setSortRole(Qt.UserRole)
        self.search = QLineEdit(placeholderText="Search accounts")
        self.show_combo = QComboBox()
        for text, _ in self.SHOW:
            self.show_combo.addItem(text)
        self.search.textChanged.connect(self.apply_filter)
        self.show_combo.currentIndexChanged.connect(self.apply_filter)
        self.table.itemSelectionChanged.connect(self.on_pool_selection)
        self.accounts = QTableView(sortingEnabled=True, showGrid=False, wordWrap=False)
        self.accounts.setModel(self.filter)
        self.accounts.setSelectionMode(QAbstractItemView.NoSelection)
        self.accounts.verticalHeader().hide()
        for col in range(window.model.columnCount()):
            self.accounts.setColumnHidden(col, col not in self.ACCOUNT_COLUMNS)
        head = self.accounts.horizontalHeader()
        head.resizeSection(0, 36)
        head.setSectionResizeMode(2, QHeaderView.Stretch)
        head.setSectionResizeMode(8, QHeaderView.Stretch)
        self.accounts.sortByColumn(2, Qt.AscendingOrder)
        self.tick_count = QLabel(objectName="muted")
        window.model.dataChanged.connect(lambda *_: self.refresh_ticks())
        window.model.modelReset.connect(self.refresh_ticks)
        tick_all, tick_none = QPushButton("Tick shown"), QPushButton("Untick shown")
        tick_all.clicked.connect(lambda: self.tick(True))
        tick_none.clicked.connect(lambda: self.tick(False))

        left = QVBoxLayout()
        left.addWidget(QLabel("Proxy pool:"))
        left.addWidget(self.table, 1)
        left.addLayout(tools)
        right = QVBoxLayout()
        right.addWidget(QLabel("Accounts (ticks are shared with the Accounts page):"))
        account_filters = QHBoxLayout()
        account_filters.addWidget(self.search, 1)
        account_filters.addWidget(self.show_combo)
        right.addLayout(account_filters)
        right.addWidget(self.accounts, 1)
        ticks = QHBoxLayout()
        ticks.addWidget(tick_all)
        ticks.addWidget(tick_none)
        ticks.addWidget(self.tick_count, 1)
        right.addLayout(ticks)
        panes = QHBoxLayout()
        panes.addLayout(left, 2)
        panes.addLayout(right, 1)

        assign = QPushButton("Assign the selected proxy to ticked accounts", objectName="primary")
        assign.clicked.connect(self.assign)
        unassign = QPushButton(icons.get("circle-minus"), "Remove proxy from ticked")
        unassign.clicked.connect(lambda: self.apply([(a, "") for a in self.ticked()]))
        assign_row = QHBoxLayout()
        assign_row.addWidget(assign, 1)
        assign_row.addWidget(unassign, 1)

        spread = QPushButton(icons.get("split"), "Distribute the whole pool over ticked")
        spread.clicked.connect(self.distribute)
        self.limit = QSpinBox(minimum=0, maximum=10000, specialValueText="no limit")
        spread_row = QHBoxLayout()
        spread_row.addWidget(spread)
        spread_row.addWidget(QLabel("at most"))
        spread_row.addWidget(self.limit)
        spread_row.addWidget(QLabel("accounts per proxy"))
        spread_row.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 8)
        layout.addWidget(QLabel("Proxies", objectName="pageTitle"))
        layout.addWidget(QLabel("Select a proxy on the left, tick accounts on the right, then Assign.",
                                objectName="muted"))
        layout.addLayout(panes, 1)
        layout.addLayout(assign_row)
        layout.addLayout(spread_row)
        self.refresh_ticks()

    # ---- state ----------------------------------------------------------------------------------

    def reload(self):
        """Called whenever the page is shown: re-read the pool (the status bot's picker and imports may have
        touched it) and adopt proxies set by hand on accounts (row menu → Set proxy)."""
        self.pool = self.window.store.load_proxies()
        known = {p.url for p in self.pool}
        for account in self.window.model.accounts:
            if account.proxy and account.proxy not in known:
                known.add(account.proxy)
                self.pool.append(Proxy(account.proxy, proxies.describe(account.proxy)[1]))
        self.save()
        self.apply_filter()

    def save(self):
        self.window.store.save_proxies(self.pool)
        self.refresh()

    def refresh(self):
        """Rebuild the pool table: O(pool + accounts) once, never per account. Account rows update themselves
        through the shared model."""
        load = Counter(a.proxy for a in self.window.model.accounts)
        selected = self.selected()
        self.table.setRowCount(len(self.pool))
        for row, p in enumerate(self.pool):
            kind, address = proxies.describe(p.url)
            ping = "—" if p.ping is None else "fail" if p.ping < 0 else f"{p.ping} ms"
            for col, value in enumerate([row + 1, p.name or address, kind, address, load[p.url], ping, p.geo or "—"]):
                cell = QTableWidgetItem(str(value))
                if col != 1:
                    cell.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, cell)
        if selected in self.pool:
            self.table.selectRow(self.pool.index(selected))

    def apply_filter(self):
        filters = self.SHOW[self.show_combo.currentIndex()][1]
        if filters is None:
            proxy = self.selected()
            filters = {"proxy": proxy.url if proxy else "\0none selected"}
        self.filter.set(self.search.text(), filters)

    def on_pool_selection(self):
        if self.SHOW[self.show_combo.currentIndex()][1] is None:
            self.apply_filter()

    def refresh_ticks(self):
        self.tick_count.setText(f"{len(self.window.model.checked)} ticked")

    def selected(self) -> Proxy | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.pool[rows[0].row()] if rows and rows[0].row() < len(self.pool) else None

    def need_selected(self) -> Proxy | None:
        proxy = self.selected()
        if proxy is None:
            QMessageBox.information(self, "Proxies", "Select a proxy in the pool first.")
        return proxy

    def tick(self, on: bool):
        """(Un)tick the accounts this page currently shows — one model update, not one per row."""
        accounts = self.window.model.accounts
        shown = {accounts[self.filter.mapToSource(self.filter.index(row, 0)).row()].session
                 for row in range(self.filter.rowCount())}
        self.window.model.set_checked(shown, on)

    def ticked(self) -> list[Account]:
        checked = self.window.model.checked
        return [a for a in self.window.model.accounts if a.session in checked]

    def apply(self, changes: list[tuple[Account, str]]):
        """Set Account.proxy for each (account, url) ('' = none), then save both the accounts and the pool."""
        for account, url in changes:
            account.proxy = url
            self.window.model.account_changed(account)
        if changes:
            self.window.changed()
            self.window.log(f"✓ proxy changed for {len(changes)} account(s)")
        self.save()

    def usable(self, exclude: str = "") -> list[str]:
        return [p.url for p in self.pool if p.url != exclude and (p.ping is None or p.ping >= 0)]

    # ---- pool editing ---------------------------------------------------------------------------

    def ask_proxy(self, title: str, current: Proxy | None = None) -> tuple[str, str] | None:
        text, ok = QInputDialog.getText(
            self, title, "Proxy: socks5|socks4|http://[user:pass@]host:port, or host:port[:user:pass] (= SOCKS5)",
            text=current.url if current else "")
        if not ok or not text.strip():
            return None
        try:
            url = proxies.normalize(text)
        except ValueError as e:
            QMessageBox.warning(self, title, str(e))
            return None
        if any(p.url == url and p is not current for p in self.pool):
            QMessageBox.warning(self, title, "That proxy is already in the pool.")
            return None
        name, ok = QInputDialog.getText(self, title, "Name:",
                                        text=current.name if current else proxies.describe(url)[1])
        return (url, name.strip()) if ok else None

    def add(self):
        if answer := self.ask_proxy("Add proxy"):
            self.pool.append(Proxy(*answer))
            self.save()

    def edit(self):
        if not (proxy := self.need_selected()) or not (answer := self.ask_proxy("Edit proxy", proxy)):
            return
        old, (proxy.url, proxy.name) = proxy.url, answer
        if proxy.url != old:
            proxy.ping, proxy.geo, proxy.tz = None, "", ""
            self.apply([(a, proxy.url) for a in self.window.model.accounts if a.proxy == old])
        else:
            self.save()

    def delete(self):
        if not (proxy := self.need_selected()):
            return
        users = [a for a in self.window.model.accounts if a.proxy == proxy.url]
        note = f"\n{len(users)} account(s) use it and will be left without a proxy." if users else ""
        if QMessageBox.question(self, "Delete proxy", f"Remove {proxy.name or proxy.url} from the pool?{note}") \
                != QMessageBox.Yes:
            return
        self.pool.remove(proxy)
        self.apply([(a, "") for a in users])

    def import_list(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import proxy list", "", "Text files (*.txt);;All files (*)")
        if not path:
            return
        kind, ok = QInputDialog.getItem(self, "Import proxy list", "Type for lines without a scheme:",
                                        ["SOCKS5", "SOCKS4", "HTTP"], 0, False)
        if not ok:
            return
        known, added, bad = {p.url for p in self.pool}, 0, 0
        for line in Path(path).read_text("utf-8", errors="replace").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                url = proxies.normalize(line, kind.lower())
            except ValueError:
                bad += 1
                continue
            if url not in known:
                known.add(url)
                self.pool.append(Proxy(url, proxies.describe(url)[1]))
                added += 1
        self.window.log(f"✓ imported {added} proxy/proxies" + (f", skipped {bad} unreadable line(s)" if bad else ""))
        self.save()

    # ---- probes ---------------------------------------------------------------------------------

    def probe_all(self, probe, store_result):
        """Run probe(url) for every pool entry; store_result(proxy, value | Exception), then coalesce the
        save+table rebuild so a big pool costs a bounded number of redraws, not one per proxy."""
        outstanding = [len(self.pool)]

        def done(future, p):
            try:
                store_result(p, future.result())
            except Exception as e:
                store_result(p, e)
            outstanding[0] -= 1
            self._refresh_soon.start()
            if outstanding[0] == 0:
                self.save()  # final flush; _refresh_soon only rebuilds the table, doesn't persist
        for proxy in list(self.pool):
            self.window.run(probe(proxy.url), lambda f, p=proxy: done(f, p))

    def ping_all(self):
        def store(p, result):
            p.ping = -1 if isinstance(result, Exception) else result
        self.probe_all(proxies.ping, store)

    def geo_all(self):
        def store(p, result):
            p.geo, p.tz = ("", "") if isinstance(result, Exception) else result
        self.probe_all(proxies.geo, store)

    # ---- assignment -----------------------------------------------------------------------------

    def assign(self):
        if not (proxy := self.need_selected()):
            return
        if not (accounts := self.ticked()):
            QMessageBox.information(self, "Proxies", "Tick at least one account on the right.")
            return
        self.apply([(a, proxy.url) for a in accounts])

    def spread(self, accounts: list[Account], pool: list[str]):
        """Least-loaded-first over `pool`, respecting the per-proxy limit; reports who didn't fit."""
        if not accounts or not pool:
            QMessageBox.information(self, "Proxies", "Nothing to do: no accounts, or no working proxies.")
            return
        moving = {a.session for a in accounts}
        load = Counter(a.proxy for a in self.window.model.accounts if a.session not in moving)
        plan = proxies.distribute([a.session for a in accounts], pool, load, self.limit.value())
        self.apply([(a, plan[a.session]) for a in accounts if a.session in plan])
        if left := len(accounts) - len(plan):
            QMessageBox.information(self, "Proxies", f"{left} account(s) didn't fit under the per-proxy "
                                                           "limit and kept their current proxy.")

    def distribute(self):
        self.spread(self.ticked(), self.usable())

    def reseat(self):
        """Move every account off the selected proxy (e.g. a dead one) onto the rest of the pool."""
        if proxy := self.need_selected():
            self.spread([a for a in self.window.model.accounts if a.proxy == proxy.url], self.usable(proxy.url))
