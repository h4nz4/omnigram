"""AI dialogs: the profile editor (account defaults, or one chat's overrides) and the AI autopilot dialog.

Profiles live in `<data>/ai/<session>.json` (ai.ProfileStore). Every write goes through ProfileStore.update, the
same locked path the background autopilot uses, so an edit here never overwrites a state change it made.
"""
from __future__ import annotations

from PySide6.QtCore import QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
)

from omnigram import ai, icons
from omnigram.store import Account

LANGUAGES = ["English", "Russian", "Ukrainian", "Serbian", "Croatian", "Spanish", "German", "French", "Italian",
             "Portuguese", "Polish", "Turkish", "Arabic", "Chinese", "Hindi", "Indonesian", "Vietnamese"]
MODE_LABELS = {"off": "Off", "draft": "Draft — suggest, I send", "auto": "Auto — reply on its own"}


def _combo(options: dict[str, str], value: str) -> QComboBox:
    box = QComboBox()
    for key, label in options.items():
        box.addItem(label, key)
    box.setCurrentIndex(max(0, box.findData(value)))
    return box


def _language_box(value: str, allow_none: bool) -> QComboBox:
    box = QComboBox(editable=True)
    if allow_none:
        box.addItem("")
    box.addItems(LANGUAGES)
    box.setCurrentText(value)
    return box


class ProfileEditor(QDialog):
    """Edit an ai.Profile. For a chat, fields start at the effective values (account defaults + overrides); saving
    stores only what differs, and "Use account defaults" drops the chat's overrides (keeping its mode)."""

    def __init__(self, parent, title: str, profile: ai.Profile, for_chat: bool):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.for_chat = for_chat
        self.reset_requested = False
        p = profile
        self.mode = _combo(MODE_LABELS, p.mode)
        self.model = QLineEdit(p.model, placeholderText="the app's default model (Settings → AI)")
        self.about = QPlainTextEdit(p.about_me, placeholderText="Who you are, in your words: name, what you do, "
                                                                "where you live, how you usually talk…")
        self.about.setFixedHeight(80)
        self.primary = _language_box(p.primary_language, allow_none=False)
        self.secondary = _language_box(p.secondary_language, allow_none=True)
        self.instructions = QPlainTextEdit(p.instructions, placeholderText="Anything else: topics to avoid, how you "
                                                                            "sign off, facts it may share…")
        self.instructions.setFixedHeight(60)
        self.formality = _combo({"casual": "Casual", "neutral": "Neutral", "formal": "Formal"}, p.formality)
        self.length = _combo({"short": "Short", "medium": "Medium", "long": "Long"}, p.length)
        self.emoji = _combo({"none": "None", "some": "Some", "lots": "Lots"}, p.emoji)
        self.delay_min = QSpinBox(minimum=0, maximum=3600, value=p.delay_min, suffix=" s")
        self.delay_max = QSpinBox(minimum=0, maximum=3600, value=p.delay_max, suffix=" s")
        delay = QHBoxLayout()
        delay.addWidget(self.delay_min)
        delay.addWidget(QLabel("to"))
        delay.addWidget(self.delay_max)
        delay.addStretch()
        self.typing = QCheckBox("Show “typing…” for about as long as a person would", checked=p.typing)
        self.split = QCheckBox("Send long replies as a few shorter messages", checked=p.split)
        self.follow_up = QCheckBox("Ask follow-up questions and keep the conversation going", checked=p.follow_up)
        self.max_in_row = QSpinBox(minimum=1, maximum=100, value=p.max_in_row)
        self.active = QCheckBox("Only reply automatically between", checked=p.active_hours)
        self.active_start = QTimeEdit(QTime.fromString(p.active_start, "HH:mm"), displayFormat="HH:mm")
        self.active_end = QTimeEdit(QTime.fromString(p.active_end, "HH:mm"), displayFormat="HH:mm")
        hours = QHBoxLayout()
        hours.addWidget(self.active)
        hours.addWidget(self.active_start)
        hours.addWidget(QLabel("and"))
        hours.addWidget(self.active_end)
        hours.addStretch()
        self.context = QSpinBox(minimum=2, maximum=100, value=p.context, suffix=" messages")
        self.group_max = QSpinBox(minimum=1, maximum=60, value=p.group_max_per_hour, suffix=" an hour")
        self.group_gap = QSpinBox(minimum=0, maximum=240, value=p.group_cooldown, suffix=" min apart")
        group = QHBoxLayout()
        group.addWidget(self.group_max)
        group.addWidget(self.group_gap)
        group.addStretch()

        form = QFormLayout()
        if for_chat:
            form.addRow("AI in this chat", self.mode)
        else:
            form.addRow(QLabel("These apply to every chat of this account. Turn the AI on per chat (chat window → "
                               "AI: draft / auto); a chat can also override any of these there (AI settings…).",
                               objectName="muted", wordWrap=True))
        form.addRow("Model", self.model)
        form.addRow("About me", self.about)
        form.addRow(QLabel("The AI writes as you, the account's real owner — never as someone else.",
                           objectName="muted"))
        form.addRow("Primary language", self.primary)
        form.addRow("Secondary language", self.secondary)
        form.addRow(QLabel("It answers in the contact's language when that is one of these; otherwise in the "
                           "primary.", objectName="muted"))
        form.addRow("Instructions", self.instructions)
        form.addRow("Tone", self.formality)
        form.addRow("Reply length", self.length)
        form.addRow("Emoji", self.emoji)
        form.addRow("Reply after", delay)
        form.addRow(self.typing)
        form.addRow(self.split)
        form.addRow(self.follow_up)
        form.addRow("Replies in a row, then hand back", self.max_in_row)
        form.addRow(hours)
        form.addRow("Context", self.context)
        form.addRow("In groups, at most", group)
        form.addRow(QLabel("In groups it answers only when addressed: a mention, a reply to it, or a message Jev "
                           "reads as meant for you while you're in the conversation. Mentions beyond the limit "
                           "are skipped, not answered later.", objectName="muted", wordWrap=True))

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if for_chat:
            reset = buttons.addButton("Use account defaults", QDialogButtonBox.ResetRole)
            reset.clicked.connect(self.on_reset)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def on_reset(self):
        self.reset_requested = True
        self.accept()

    def values(self) -> dict:
        low, high = sorted((self.delay_min.value(), self.delay_max.value()))
        return {"mode": self.mode.currentData() if self.for_chat else "off", "model": self.model.text().strip(),
                "about_me": self.about.toPlainText().strip(), "primary_language": self.primary.currentText().strip(),
                "secondary_language": self.secondary.currentText().strip(),
                "instructions": self.instructions.toPlainText().strip(), "formality": self.formality.currentData(),
                "length": self.length.currentData(), "emoji": self.emoji.currentData(), "delay_min": low,
                "delay_max": high, "typing": self.typing.isChecked(), "split": self.split.isChecked(),
                "follow_up": self.follow_up.isChecked(), "max_in_row": self.max_in_row.value(),
                "active_hours": self.active.isChecked(), "active_start": self.active_start.time().toString("HH:mm"),
                "active_end": self.active_end.time().toString("HH:mm"), "context": self.context.value(),
                "group_max_per_hour": self.group_max.value(), "group_cooldown": self.group_gap.value()}


def edit_account_defaults(parent, window, account: Account) -> bool:
    path = window.ai_store_path(account)
    store = ai.ProfileStore.load(path)
    editor = ProfileEditor(parent, f"AI settings for all of {account.name or account.session}'s chats",
                           ai.resolve(store.defaults, {}),
                           for_chat=False)
    if editor.exec() != QDialog.Accepted:
        return False
    values = editor.values()
    values.pop("mode")
    ai.ProfileStore.update(path, lambda s: setattr(s, "defaults", values))
    return True


def edit_chat_profile(parent, window, account: Account, c) -> bool:
    """One chat's AI settings (`c` a chat.Chat). Switching a group to Auto obeys window.group_auto_refusal."""
    path = window.ai_store_path(account)
    store = ai.ProfileStore.load(path)
    chat_id, title = c.id, c.title
    editor = ProfileEditor(parent, f"AI in {title}", store.profile(chat_id), for_chat=True)
    if editor.exec() != QDialog.Accepted:
        return False
    values = editor.values()
    switching_on = values["mode"] == "auto" and store.profile(chat_id).mode != "auto"
    if switching_on and (why := window.group_auto_refusal(account, c)):
        QMessageBox.information(parent, "AI", why)
        return False

    def change(s: ai.ProfileStore):
        previous = s.profile(chat_id).mode
        s.set_override(chat_id, {"mode": values["mode"]} if editor.reset_requested else values)
        s.chats[str(chat_id)].update(title=title, admin=c.admin)
        if values["mode"] == "auto" and previous != "auto":
            s.set_state(chat_id, ai.ChatState())  # switching Auto on (again) starts fresh: unpaused, no flag

    ai.ProfileStore.update(path, change)
    return True


class AutopilotDialog(QDialog):
    """Start/stop the background AI autopilot for the selected accounts; see their Auto chats and flags."""

    def __init__(self, window, accounts: list[Account]):
        super().__init__(window)
        self.window, self.accounts = window, accounts
        self.setWindowTitle("AI autopilot")
        self.setMinimumSize(560, 400)
        self.account = QComboBox()
        for account in accounts:
            self.account.addItem(account.name or account.session, account)
        self.account.currentIndexChanged.connect(self.refresh)
        defaults = QPushButton(icons.get("sliders-horizontal"), "Account AI settings…")
        defaults.clicked.connect(self.on_defaults)
        clear = QPushButton(icons.get("eraser"), "Clear flags")
        clear.setToolTip("Resume every paused or flagged chat of this account")
        clear.clicked.connect(self.on_clear)
        pick = QHBoxLayout()
        pick.addWidget(self.account, 1)
        pick.addWidget(defaults)
        pick.addWidget(clear)
        self.summary = QPlainTextEdit(readOnly=True)
        self.toggle = QPushButton(objectName="primary")
        self.toggle.clicked.connect(self.on_toggle)
        note = QLabel("The autopilot answers the chats you set to Auto (chat window → AI), also with the chat window "
                      "closed, and comes back by itself after a restart. The chat window shares its connection, so "
                      "you can open it meanwhile; other jobs on the account wait.",
                      objectName="muted", wordWrap=True)
        layout = QVBoxLayout(self)
        layout.addLayout(pick)
        layout.addWidget(self.summary, 1)
        layout.addWidget(note)
        layout.addWidget(self.toggle)
        self.refresh()

    def running(self) -> list[Account]:
        return [a for a in self.accounts if self.window.task_running(f"autopilot/{a.session}")]

    def refresh(self):
        lines = []
        for account in self.accounts:
            store = ai.ProfileStore.load(self.window.ai_store_path(account))
            state = "running" if self.window.task_running(f"autopilot/{account.session}") else "stopped"
            auto = store.auto_chats()
            lines.append(f"{account.name or account.session} — {state}, {len(auto)} chat(s) in Auto")
            for chat_id, entry in store.chats.items():
                flag = entry.get("state", {}).get("flag")
                if flag:
                    lines.append(f"   ⚑ {entry.get('title', chat_id)}: {flag}")
        if not self.window.ai_config().ready:
            lines.insert(0, "✗ No AI provider yet: set one up in Settings → AI.\n")
        elif not self.window.ai_config().jev_ready:
            lines.insert(0, "Tip: set up Jev in Settings → AI so the autopilot can tell when not to answer.\n")
        self.summary.setPlainText("\n".join(lines))
        running = self.running()
        self.toggle.setText(f"Stop ({len(running)} running)" if running else
                            f"Start for {len(self.accounts)} account(s)")

    def current(self) -> Account:
        return self.account.currentData()

    def on_defaults(self):
        if edit_account_defaults(self, self.window, self.current()):
            self.refresh()

    def on_clear(self):
        def clear(store: ai.ProfileStore):
            for chat_id in list(store.chats):
                store.set_state(int(chat_id), ai.ChatState())
        ai.ProfileStore.update(self.window.ai_store_path(self.current()), clear)
        self.refresh()

    def on_toggle(self):
        if running := self.running():
            for account in running:
                self.window.stop_task(f"autopilot/{account.session}")
            self.refresh()
            return
        if not self.window.ai_config().ready:
            QMessageBox.warning(self, "AI autopilot", "Set up an AI provider first (Settings → AI).")
            return
        self.window.start_autopilots(self.accounts)
        self.refresh()
