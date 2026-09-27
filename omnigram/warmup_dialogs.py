"""Warm-up, own-account dialogues, online keeper and name randomizer dialogs.

Every job here stays inside the operator's own account pool: a warm-up only touches the account's
own presence, a dialogue is an exchange between two accounts the operator owns, the online keeper
only refreshes one account's online flag, and the randomizer writes names from the operator's own
list onto the operator's own accounts.
"""
import functools
from datetime import datetime, timezone

from PySide6.QtCore import QTime
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
)

from omnigram import randomizer, telegram, warmup
from omnigram.store import Account



# ---- pure helpers ---------------------------------------------------------------------------------

def plan_summary(plan) -> str:
    """One-line preview of a scheduled warm-up: action count, day count, per-day range, when it ends
    (in this computer's local time)."""
    if not plan:
        return "no actions"
    counts = warmup.per_day(plan)
    days = len(counts)
    span = f"{min(counts.values())}–{max(counts.values())}/day" if days > 1 else f"{len(plan)}/day"
    ends = datetime.fromtimestamp(max(a.at for a in plan))
    return f"{len(plan)} actions over {days} day(s) ({span}) · ends {ends:%a %d %b %H:%M}"


def partner_config(partner: Account, fallback: tuple) -> dict:
    """The `partner` dict `telegram.dialogues` wants for the other account.

    `fallback` is `(session_path, api_id, api_hash)` of the operating account: the path is the
    partner's own session file (already resolved by the caller), and the credentials are used only
    for fields the partner does not carry itself.
    """
    session_path, api_id, api_hash = fallback
    return {"session": session_path, "api_id": partner.api_id or api_id,
            "api_hash": partner.api_hash or api_hash, "proxy": partner.proxy}


def split_names(text: str) -> list[str]:
    """Pasted names: one per line, trimmed, blanks dropped, repeats removed (first wins)."""
    out: list[str] = []
    for line in text.splitlines():
        name = line.strip()
        if name and name not in out:
            out.append(name)
    return out


def name_pairs(assignment: dict[int, str]) -> list[tuple[str, str]]:
    """`{index: full name}` -> `[(first, last)]` in index order; splits on the first space."""
    pairs = []
    for index in sorted(assignment):
        first, _, last = assignment[index].partition(" ")
        pairs.append((first, last))
    return pairs


# ---- dialogs --------------------------------------------------------------------------------------

class WarmupDialog(QDialog):
    """Own-presence actions (read/view/react/join/online/pause) spread over days, inside active hours.

    Works on one account or many. With several, every account gets its own random plan in its own
    timezone, and only the kinds that need no shared channel list are offered (warmup.BULK_ACTIONS).
    """

    def __init__(self, window, accounts: list[Account]):
        super().__init__(window)
        self.window, self.accounts = window, accounts
        self.bulk = len(accounts) > 1
        who = f"{len(accounts)} accounts" if self.bulk else accounts[0].name or accounts[0].session
        self.setWindowTitle(f"Warm-up — {who}")
        self.setMinimumWidth(480)

        self.days = QSpinBox(minimum=1, maximum=90, value=7, suffix=" day(s)")
        self.per_day = QSpinBox(minimum=1, maximum=100, value=10, suffix=" /day")
        self.ramp = QCheckBox("Ramp up: day one runs at 40% of the target", checked=True)
        self.window_start = QTimeEdit(QTime(warmup.WINDOW[0].hour, 0), displayFormat="HH:mm")
        self.window_end = QTimeEdit(QTime(warmup.WINDOW[1].hour, 0), displayFormat="HH:mm")
        hours = QHBoxLayout()
        hours.addWidget(self.window_start)
        hours.addWidget(QLabel("to"))
        hours.addWidget(self.window_end)
        hours.addStretch()
        self.min_gap = QSpinBox(minimum=warmup.MIN_GAP, maximum=86400, value=60, suffix=" s")
        self.kinds = {kind: QCheckBox(kind, checked=True)
                      for kind in (warmup.BULK_ACTIONS if self.bulk else warmup.ACTIONS)}
        kind_row = QHBoxLayout()
        for box in self.kinds.values():
            kind_row.addWidget(box)
        kind_row.addStretch()
        self.targets = QPlainTextEdit(placeholderText="@channel or t.me/... for view · react · join, one per line")
        self.targets.setFixedHeight(70)
        self.preview = QLabel(objectName="muted")
        self.toggle = QPushButton(objectName="primary")
        self.toggle.clicked.connect(self.on_toggle)

        form = QFormLayout()
        form.addRow("Days", self.days)
        form.addRow("Actions per day", self.per_day)
        form.addRow(self.ramp)
        form.addRow("Active hours", hours)
        form.addRow("Min gap between actions", self.min_gap)
        form.addRow("Kinds", kind_row)
        if self.bulk:
            note = ("Each account gets its own plan and only touches its own presence (read its dialogs, go "
                    "online). Viewing, reacting and joining need a channel list, so they are single-account only.")
        else:
            form.addRow("Channels to view/react/join", self.targets)
            note = ("Only this account is touched: it reads, views and reacts to chats it already reads, "
                    "and joins the links you list.")
        form.addRow(QLabel(note + " Active hours follow each account's proxy exit-IP timezone (Proxies → "
                                  "Geo all); without one, this computer's time. A restart resumes the run.",
                           objectName="muted", wordWrap=True))
        form.addRow("Plan", self.preview)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.toggle)

        for spin in (self.days, self.per_day, self.min_gap):
            spin.valueChanged.connect(self.refresh_preview)
        for edit in (self.window_start, self.window_end):
            edit.timeChanged.connect(self.refresh_preview)
        self.ramp.toggled.connect(self.refresh_preview)
        for box in self.kinds.values():
            box.toggled.connect(self.refresh_preview)
        self.refresh_preview()
        self.refresh()

    def plan(self, tz=None) -> list[warmup.Action]:
        """A fresh random plan for the current settings, scheduled from now in `tz`. Raises ValueError."""
        plan = warmup.warmup_plan(self.days.value(), self.per_day.value(), ramp=self.ramp.isChecked(),
                                  kinds=[kind for kind, box in self.kinds.items() if box.isChecked()])
        return warmup.schedule(plan, datetime.now(timezone.utc), tz, min_gap=self.min_gap.value(),
                               window=(self.window_start.time().toPython(), self.window_end.time().toPython()))

    def refresh_preview(self):
        try:
            text = plan_summary(self.plan()) + (" · each account" if self.bulk else "")
        except ValueError as e:
            text = f"✗ {e}"
        self.preview.setText(text)

    def running(self) -> list[Account]:
        return [a for a in self.accounts if self.window.task_running(f"warmup/{a.session}")]

    def refresh(self):
        running = self.running()
        self.toggle.setText(f"Stop ({len(running)} running)" if self.bulk and running
                            else "Stop" if running else "Start")
        for widget in (self.days, self.per_day, self.ramp, self.window_start, self.window_end, self.min_gap,
                       self.targets, *self.kinds.values()):
            widget.setEnabled(not running)

    def on_toggle(self):
        if running := self.running():
            for account in running:
                self.window.stop_task(f"warmup/{account.session}")
            self.refresh()
            return
        try:
            self.plan()
        except ValueError as e:
            QMessageBox.warning(self, "Warm-up", str(e))
            return
        if self.window.credentials() is None:
            return
        free = [a for a in self.accounts if not self.window.busy(a)]
        if not free:
            QMessageBox.warning(self, "Warm-up", "Those accounts are busy (being checked, running a job, or listening).")
            return
        allowed = self.window.allow_connect(free)
        if not allowed:
            return
        targets = [] if self.bulk else [line.strip() for line in self.targets.toPlainText().splitlines()
                                        if line.strip()]
        zones = self.window.proxy_zones()
        for account in allowed:
            self.window.start_warmup(account, self.plan(warmup.zone(zones.get(account.proxy, ""))), targets)
        skipped = len(self.accounts) - len(allowed)
        self.window.log(f"→ warm-up started on {len(allowed)} account(s)"
                        + (f", skipped {skipped} busy or without a proxy" if skipped else ""))
        self.refresh()


class DialoguesDialog(QDialog):
    """A scripted exchange between two of the operator's own accounts, alternating opening and reply."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"dialogues/{account.session}"
        self.setWindowTitle(f"Dialogues — {account.name or account.session}")
        self.setMinimumWidth(460)

        self.partner = QComboBox()
        for other in window.model.accounts:
            if other.session != account.session:
                self.partner.addItem(other.name or other.session, other)
        if not self.partner.count():
            self.partner.addItem("No other account imported", None)
        self.opening = QPlainTextEdit(placeholderText="opening line; {round} is filled in per round")
        self.opening.setFixedHeight(70)
        self.reply = QPlainTextEdit(placeholderText="the partner's reply; {round} works here too")
        self.reply.setFixedHeight(70)
        self.rounds = QSpinBox(minimum=1, maximum=100, value=5)
        self.pause = QSpinBox(minimum=1, maximum=3600, value=30, suffix=" s")
        self.toggle = QPushButton(objectName="primary")
        self.toggle.clicked.connect(self.on_toggle)

        form = QFormLayout()
        form.addRow("Partner account", self.partner)
        form.addRow("Opening line", self.opening)
        form.addRow("Reply", self.reply)
        form.addRow("Rounds", self.rounds)
        form.addRow("Pause between messages", self.pause)
        form.addRow(QLabel("The exchange is only between your own accounts — the opening and reply alternate "
                           "between this account and the partner; nobody else is contacted.", objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.toggle)
        self.refresh()

    def refresh(self):
        running = self.window.task_running(self.key)
        self.toggle.setText("Stop" if running else "Start")
        for widget in (self.partner, self.opening, self.reply, self.rounds, self.pause):
            widget.setEnabled(not running)

    def on_toggle(self):
        if self.window.task_running(self.key):
            self.window.stop_task(self.key)
            self.refresh()
            return
        partner = self.partner.currentData()
        opening, reply = self.opening.toPlainText().strip(), self.reply.toPlainText().strip()
        if partner is None:
            QMessageBox.warning(self, "Dialogues", "Import a second account to talk to.")
            return
        if not opening or not reply:
            QMessageBox.warning(self, "Dialogues", "Write both the opening line and the reply.")
            return
        # The partner connects too: its session must be free, and its own proxy (or the user's OK) applies.
        if self.window.busy(partner):
            QMessageBox.warning(self, "Dialogues", "The partner account is busy (being checked, running a job, "
                                                   "or listening).")
            return
        if not self.window.allow_connect([partner]):
            return
        cfg = partner_config(partner, (self.window.store.path(partner), *self.window.credentials(self.account)))
        self.window.start_task(self.key, self.window.call(self.account, telegram.dialogues, cfg, opening, reply,
                                                          self.rounds.value(), float(self.pause.value()),
                                                          self.window.emitter(self.account)), "dialogues")
        self.refresh()


class OnlineKeeperDialog(QDialog):
    """Keep one account's online flag fresh for a chosen number of minutes."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"online/{account.session}"
        self.setWindowTitle(f"Online keeper — {account.name or account.session}")
        self.setMinimumWidth(380)

        self.minutes = QSpinBox(minimum=1, maximum=1440, value=60, suffix=" min")
        self.toggle = QPushButton(objectName="primary")
        self.toggle.clicked.connect(self.on_toggle)

        form = QFormLayout()
        form.addRow("Keep online for", self.minutes)
        form.addRow(QLabel("Telegram shows the account as online only while this runs; it goes offline when "
                           "you stop it or the time is up.", objectName="muted"))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.toggle)
        self.refresh()

    def refresh(self):
        running = self.window.task_running(self.key)
        self.toggle.setText("Stop" if running else "Start")
        self.minutes.setEnabled(not running)

    def on_toggle(self):
        if self.window.task_running(self.key):
            self.window.stop_task(self.key)
            self.refresh()
            return
        self.window.start_task(self.key, self.window.call(self.account, telegram.online_keeper, self.minutes.value(),
                                                          self.window.emitter(self.account)), "keeping online")
        self.refresh()


class RandomizerDialog(QDialog):
    """Assign names from the operator's own list to a batch of the operator's own accounts."""

    def __init__(self, window, accounts: list[Account]):
        super().__init__(window)
        self.window, self.accounts = window, accounts
        self.setWindowTitle(f"Randomize names — {len(accounts)} account(s)")
        self.setMinimumSize(560, 460)

        self.names = QPlainTextEdit(placeholderText="one full name per line; when the list runs out, names repeat")
        self.names.setFixedHeight(110)
        self.bio = QCheckBox("Also set a random bio")
        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self.on_preview)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Account", "New name"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.apply = QPushButton("Apply", objectName="primary", enabled=False)
        self.apply.clicked.connect(self.on_apply)
        self._pairs: list[tuple[str, str]] = []
        self.queue: list[tuple[Account, tuple[str, str]]] = []

        row = QHBoxLayout()
        row.addWidget(self.preview_button)
        row.addWidget(self.bio)
        row.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.names)
        layout.addLayout(row)
        layout.addWidget(self.table)
        layout.addWidget(QLabel("Names come from your own list and only your accounts are updated.",
                                objectName="muted"))
        layout.addWidget(self.apply)

    def on_preview(self):
        names = split_names(self.names.toPlainText())
        self.table.setRowCount(0)
        self._pairs = []
        if not names or not self.accounts:
            self.apply.setEnabled(False)
            return
        self._pairs = name_pairs(randomizer.assign_names(len(self.accounts), names))
        self.table.setRowCount(len(self.accounts))
        for row, (account, (first, last)) in enumerate(zip(self.accounts, self._pairs)):
            self.table.setItem(row, 0, QTableWidgetItem(account.name or account.session))
            self.table.setItem(row, 1, QTableWidgetItem(" ".join(filter(None, [first, last]))))
        self.apply.setEnabled(True)

    def on_apply(self):
        if not self._pairs or self.window.credentials() is None:
            return
        allowed = self.window.allow_connect(self.accounts)
        if allowed is None:
            return
        sessions = {a.session for a in allowed}
        self.queue = [(a, pair) for a, pair in zip(self.accounts, self._pairs) if a.session in sessions]
        if not self.queue:
            return
        self.apply.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.window.log(f"→ randomizing {len(self.queue)} account(s)")
        self.next_account()

    def next_account(self):
        if not self.queue:
            self.apply.setEnabled(True)
            self.preview_button.setEnabled(True)
            self.window.changed()  # one save/recompute for the whole batch, never per account
            self.window.log("✓ randomizer finished")
            return
        account, (first, last) = self.queue.pop(0)
        fields = {"first_name": first, "last_name": last}
        if self.bio.isChecked():
            fields["about"] = randomizer.random_bio()
        self.window.run(self.window.call(account, functools.partial(telegram.update_profile, **fields)),
                        lambda f, a=account: self.on_result(a, f))

    def on_result(self, account: Account, future):
        name = account.name or account.session
        try:
            result = future.result()
        except Exception as e:
            self.window.log(f"✗ [{name}] {type(e).__name__}: {e}")
        else:
            account.name, account.username = result["name"], result["username"]
            self.window.model.account_changed(account)
            self.window.log(f"✓ [{name}] → {account.name or account.session}")
        self.next_account()
