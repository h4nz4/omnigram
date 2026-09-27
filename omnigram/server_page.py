"""The Server page (sidebar → Server): where the desktop installs, updates and connects to an Omnigram server over
SSH, and sees what runs there. The work itself is remote.py's; this is its GUI."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from omnigram import __version__, icons
from omnigram.remote import RemoteError, ServerConfig, find_wheel, install
from omnigram.server import API

STATES = {"off": "Not connected", "connecting": "Connecting…", "online": "Connected", "offline": "Offline — reconnecting"}


class ServerPage(QWidget):
    def __init__(self, window):
        super().__init__(objectName="page")
        self.window = window
        self.remote = window.remote
        config = self.remote.config
        self.host = QLineEdit(config.host, placeholderText="server address, e.g. 203.0.113.7 or vps.example.com")
        self.port = QSpinBox(minimum=1, maximum=65535, value=config.port)
        self.user = QLineEdit(config.user, placeholderText="the Linux user Omnigram runs as")
        self.key = QLineEdit(config.key, placeholderText="your SSH private key file")
        browse = QPushButton(icons.get("folder-open"), "")
        browse.setToolTip("Pick the private key file")
        browse.clicked.connect(self.pick_key)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key, 1)
        key_row.addWidget(browse)

        self.install_button = QPushButton(icons.get("download"), "Install / update")
        self.install_button.clicked.connect(self.on_install)
        self.connect_button = QPushButton(icons.get("server"), "Connect", objectName="primary")
        self.connect_button.clicked.connect(self.on_connect)
        self.backup_button = QPushButton(icons.get("archive"), "Download backup…")
        self.backup_button.clicked.connect(self.on_backup)
        buttons = QHBoxLayout()
        for button in (self.connect_button, self.install_button, self.backup_button):
            buttons.addWidget(button)
        buttons.addStretch()

        self.status = QLabel(objectName="muted", wordWrap=True)
        self.overview = QPlainTextEdit(readOnly=True)
        self.overview.setFixedHeight(130)
        self.output = QPlainTextEdit(readOnly=True, objectName="log")
        self.output.setPlaceholderText("Install and connection steps appear here.")

        form = QFormLayout()
        form.addRow(QLabel("Keeps accounts working while this app — or this computer — is off: warm-ups, the AI "
                           "autopilot, funnels, listeners, the online keeper and the status bot run on a Linux "
                           "server you control. Accounts you move there run only there.", objectName="muted",
                           wordWrap=True))
        form.addRow("Host", self.host)
        form.addRow("SSH port", self.port)
        form.addRow("User", self.user)
        form.addRow("SSH key", key_row)
        form.addRow(QLabel("Key login only: no password is asked for or stored. The server needs Linux with systemd; "
                           "its API listens only on a private socket reached through SSH.", objectName="muted",
                           wordWrap=True))
        box = QVBoxLayout(self)
        box.setContentsMargins(20, 16, 20, 8)
        box.addWidget(QLabel("Server", objectName="pageTitle"))
        box.addLayout(form)
        box.addLayout(buttons)
        box.addWidget(self.status)
        box.addWidget(self.overview)
        box.addWidget(self.output, 1)
        self.refresh()

    # ---- helpers ----------------------------------------------------------------------------------------------

    def say(self, line: str):
        self.output.appendPlainText(f"{datetime.now():%H:%M:%S}  {line}")

    def pick_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "SSH private key", str(Path.home() / ".ssh"))
        if path:
            self.key.setText(path)

    def form_config(self) -> ServerConfig:
        old = self.remote.config
        config = ServerConfig(self.host.text().strip(), self.port.value(), self.user.text().strip(),
                              self.key.text().strip())
        if (config.host, config.port, config.user) == (old.host, old.port, old.user):
            config.socket = old.socket
        return config

    def apply_form(self) -> bool:
        """Save the form. Refused while accounts run on the current server: they'd be stranded there."""
        config = self.form_config()
        old = self.remote.config
        if (config.host, config.port) != (old.host, old.port) and old.host:
            if on_server := [a for a in self.window.model.accounts if a.placement == "server"]:
                names = ", ".join(a.name or a.session for a in on_server[:5])
                QMessageBox.warning(self, "Server", f"{len(on_server)} account(s) still run on {old.host}: {names}. "
                                                    "Move them back first (right-click → Move back to this "
                                                    "computer).")
                self.host.setText(old.host)
                self.port.setValue(old.port)
                return False
        if not config.ready:
            QMessageBox.information(self, "Server", "Fill in the host, user and SSH key first.")
            return False
        self.remote.configure(config)
        return True

    def trust_host(self) -> bool:
        """First contact: show the server's host key fingerprints; nothing is trusted without the user's OK."""
        ssh = self.remote.ssh
        if ssh.trusted():
            return True
        try:
            lines, fingerprints = ssh.scan()
        except RemoteError as e:
            QMessageBox.warning(self, "Server", str(e))
            return False
        box = QMessageBox(QMessageBox.Question, "Trust this server?",
                          f"First connection to {self.remote.config.host}. Its SSH host key fingerprints are:\n\n"
                          + "\n".join(fingerprints) + "\n\nCompare them with what your hosting provider shows "
                          "(or `ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub` on the server). Trust them?", parent=self)
        trust = box.addButton("Trust", QMessageBox.AcceptRole)
        cancel = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not trust:
            return False
        ssh.trust(lines)
        self.say(f"trusted {self.remote.config.host}'s host key")
        return True

    # ---- actions ----------------------------------------------------------------------------------------------

    def on_install(self):
        if not self.apply_form() or not self.trust_host():
            return
        try:
            wheel = find_wheel(self.window.store.sessions.parent / "tmp")
        except RemoteError as e:
            QMessageBox.warning(self, "Server", f"No server package to install: {e}")
            return
        self.install_button.setEnabled(False)
        self.say(f"installing Omnigram {__version__} on {self.remote.config.target}…")
        step = lambda text: self.window._call.emit(lambda: self.say(text))  # noqa: E731 - from the Telethon thread

        def done(future):
            self.install_button.setEnabled(True)
            try:
                socket = future.result()
            except Exception as e:
                self.say(f"✗ {e}")
                return
            self.remote.configure(ServerConfig(**{**vars(self.remote.config), "socket": socket}))
            self.say("✓ installed and running; connecting…")
            self.remote.stop()
            self.connect()

        self.window.run(install(self.remote.ssh, wheel, step), done)

    def on_connect(self):
        if self.remote.state in ("online", "connecting", "offline"):
            self.remote.stop()
            self.window.settings.set("server_connect", False)
            self.say("disconnected")
            return
        if self.apply_form() and self.trust_host():
            self.connect()

    def connect(self):
        self.window.settings.set("server_connect", True)
        self.say(f"connecting to {self.remote.config.target}…")
        self.remote.start()
        self.refresh()

    def on_backup(self):
        default = f"omnigram-server-backup-{datetime.now():%Y%m%d-%H%M}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Download server backup", default, "Zip archive (*.zip)")
        if not path:
            return
        self.backup_button.setEnabled(False)
        self.say("downloading the server's backup…")

        def done(future):
            self.backup_button.setEnabled(True)
            try:
                future.result()
            except Exception as e:
                self.say(f"✗ backup: {e}")
                return
            self.say(f"✓ saved {path} — Accounts → Restore sessions reads it")

        self.window.run(self.remote.download_backup(Path(path)), done)

    # ---- state ------------------------------------------------------------------------------------------------

    def outdated(self) -> bool:
        return self.remote.state == "online" and (self.remote.api != API or self.remote.version != __version__)

    def refresh(self):
        r = self.remote
        state = STATES.get(r.state, r.state)
        if r.state == "online":
            state += f" — Omnigram {r.version} on {r.config.host}"
        if self.outdated():
            state += f". This app is {__version__}: press Install / update."
        if r.error and r.state != "online":
            state += f" ({r.error})"
        self.status.setText(state)
        self.connect_button.setText("Disconnect" if r.state != "off" else "Connect")
        self.backup_button.setEnabled(r.state == "online")
        on_server = [a for a in self.window.model.accounts if a.placement == "server"]
        lines = [f"Accounts on the server: {len(on_server)}"]
        lines += [f"  {a.name or a.session}" for a in on_server[:10]]
        lines.append(f"Jobs running there: {len(r.jobs)}")
        lines += [f"  {verb}" for verb in list(r.jobs.values())[:10]]
        self.overview.setPlainText("\n".join(lines))

