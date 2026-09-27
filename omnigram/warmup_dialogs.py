"""Warm-up, own-account dialogues, online keeper and name randomizer dialogs.

Every job here stays inside the operator's own account pool: a warm-up only touches the account's
own presence, a dialogue is an exchange between two accounts the operator owns, the online keeper
only refreshes one account's online flag, and the randomizer writes names from the operator's own
list onto the operator's own accounts.
"""
import functools

from PySide6.QtGui import QIcon
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
    QVBoxLayout,
)

from omnigram import broadcast, randomizer, telegram, warmup
from omnigram.store import Account

Icon = QIcon.ThemeIcon


# ---- pure helpers ---------------------------------------------------------------------------------

def plan_summary(plan) -> str:
    """One-line preview of a warm-up plan: action count, day count, per-day range, total run time."""
    if not plan:
        return "no actions"
    counts = warmup.per_day(plan)
    days = len(counts)
    span = f"{min(counts.values())}–{max(counts.values())}/day" if days > 1 else f"{len(plan)}/day"
    return (f"{len(plan)} actions over {days} day(s) ({span}) · "
            f"{broadcast.human_time(warmup.total_seconds(plan))}")


def partner_config(partner: Account, fallback: tuple) -> dict:
    """The `partner` dict `telegram.dialogues` wants for the other account.

    `fallback` is `(session_path, api_id, api_hash)` of the operating account: the path is the
    partner's own session file (already resolved by the caller), and the credentials are used only
    for fields the partner does not carry itself.
    """
    session_path, api_id, api_hash = fallback
    return {"session": str(session_path), "api_id": partner.api_id or api_id,
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
    """A jittered plan of own-presence actions (read/view/react/join/online/pause) spread over days."""

    def __init__(self, window, account: Account):
        super().__init__(window)
        self.window, self.account = window, account
        self.key = f"warmup/{account.session}"
        self.setWindowTitle(f"Warm-up — {account.name or account.session}")
        self.setMinimumWidth(480)

        self.days = QSpinBox(minimum=1, maximum=90, value=7, suffix=" day(s)")
        self.per_day = QSpinBox(minimum=1, maximum=100, value=10, suffix=" /day")
        self.ramp = QCheckBox("Ramp up: day one runs at 40% of the target", checked=True)
        self.min_delay = QSpinBox(minimum=0, maximum=86400, value=60, suffix=" s")
        self.max_delay = QSpinBox(minimum=0, maximum=86400, value=600, suffix=" s")
        self.kinds = {kind: QCheckBox(kind, checked=True) for kind in warmup.ACTIONS}
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
        form.addRow("Delay between actions", self.min_delay)
        form.addRow("…up to", self.max_delay)
        form.addRow("Kinds", kind_row)
        form.addRow("Channels to view/react/join", self.targets)
        form.addRow(QLabel("Only this account is touched: it reads, views and reacts to chats it already reads, "
                           "and joins the links you list. Links are joined by this account only.", objectName="muted"))
        form.addRow("Plan", self.preview)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.toggle)

        for spin in (self.days, self.per_day, self.min_delay, self.max_delay):
            spin.valueChanged.connect(self.refresh_preview)
        self.ramp.toggled.connect(self.refresh_preview)
        for box in self.kinds.values():
            box.toggled.connect(self.refresh_preview)
        self.refresh_preview()
        self.refresh()

    def plan(self) -> list[warmup.Action]:
        """The plan shown in the preview (and used by Start), rebuilt from the current settings."""
        try:
            self._plan = warmup.warmup_plan(
                self.days.value(), self.per_day.value(), ramp=self.ramp.isChecked(),
                min_delay=self.min_delay.value(), max_delay=self.max_delay.value(),
                kinds=[kind for kind, box in self.kinds.items() if box.isChecked()])
        except ValueError:
            self._plan = []
        return self._plan

    def refresh_preview(self):
        plan = self.plan()
        self.preview.setText(plan_summary(plan) if plan else "✗ pick at least one kind, with min delay ≤ max delay")

    def refresh(self):
        running = self.window.task_running(self.key)
        self.toggle.setText("Stop" if running else "Start")
        for widget in (self.days, self.per_day, self.ramp, self.min_delay, self.max_delay, self.targets,
                       *self.kinds.values()):
            widget.setEnabled(not running)

    def on_toggle(self):
        if self.window.task_running(self.key):
            self.window.stop_task(self.key)
            self.refresh()
            return
        if not self.plan():
            QMessageBox.warning(self, "Warm-up", "Pick at least one action kind and keep min delay ≤ max delay.")
            return
        targets = [line.strip() for line in self.targets.toPlainText().splitlines() if line.strip()]
        self.window.start_task(self.key, self.window.call(self.account, telegram.warmup_run, self._plan, targets,
                                                          self.window.emitter(self.account)), "warming up")
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
