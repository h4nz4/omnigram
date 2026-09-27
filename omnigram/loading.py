"""The loading overlay every account dialog shows while a Telegram request runs.

A request can take a while (connecting through a slow proxy, Telethon's own retries), so the dialog dims, its
inputs lock, and a spinner shows what is happening with a seconds counter. There is no automatic timeout:
after SLOW_AFTER seconds the overlay suggests cancelling, and the user decides.

Cancel stops *waiting*; it cannot recall a request Telegram already received. So cancelling a load closes the
dialog (there is nothing to show), while cancelling a change keeps it open and says the change may have
applied.
"""
import time

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

SLOW_AFTER = 20  # seconds before the overlay suggests the proxy may be slow
CHANGE_CANCELLED = "Stopped waiting. Telegram may still have applied the change — reopen to check."


class Spinner(QWidget):
    """An indeterminate spinning arc (painted, so it needs no image or animation files)."""

    def __init__(self, parent=None, size: int = 36):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.angle = 0
        self.timer = QTimer(self, interval=16)
        self.timer.timeout.connect(self.step)

    def step(self):
        self.angle = (self.angle + 6) % 360
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
        painter.setPen(QPen(QColor("#2a2c31"), 3.5))
        painter.drawEllipse(rect)
        painter.setPen(QPen(QColor("#3b82f6"), 3.5, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(rect, -self.angle * 16, 100 * 16)


class LoadingOverlay(QWidget):
    """Covers `parent` while `run()`'s request is in flight. `run_fn` is MainWindow.run (injectable for tests)."""

    def __init__(self, parent: QWidget, run_fn):
        super().__init__(parent)
        self.run_fn = run_fn
        self.future = None
        self.token = None  # identifies the request being waited for; None = idle
        self.change = False
        self.started = 0.0
        self.locked: list[QWidget] = []
        self.spinner = Spinner()
        self.status = QLabel(alignment=Qt.AlignCenter, wordWrap=True)
        self.hint = QLabel("Slow proxy? You can cancel.", objectName="muted", alignment=Qt.AlignCenter)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel)
        layout = QVBoxLayout(self)
        layout.addStretch()
        layout.addWidget(self.spinner, 0, Qt.AlignHCenter)
        layout.addWidget(self.status)
        layout.addWidget(self.hint)
        layout.addWidget(self.cancel_button, 0, Qt.AlignHCenter)
        layout.addStretch()
        self.ticker = QTimer(self, interval=500)
        self.ticker.timeout.connect(self.tick)
        self.text = ""
        self.hide()
        parent.installEventFilter(self)
        if hasattr(parent, "finished"):  # a dialog closed mid-load: stop waiting for data nobody will see
            parent.finished.connect(lambda _: self.future and not self.change and self.future.cancel())

    # ---- API ------------------------------------------------------------------------------------

    def run(self, coro, status: str, on_done, change: bool = False):
        """Show the overlay, run `coro`, then hide it and call `on_done(future)` — unless the user cancelled.
        `change`: the request modifies something on Telegram (decides what Cancel says). A new run() while one
        is in flight replaces it (e.g. picking another chat while the first still loads)."""
        if self.busy:
            previous, self.future, self.token = self.future, None, None
            if previous is not None:
                previous.cancel()
            self.stop()
        self.text, self.change = status, change
        self.started = time.monotonic()
        self.show_over_parent()
        # The token is set before the request starts: a request that finishes before run_fn even returns (its
        # callback fires straight away) must still count as the current one, or the overlay would stay up.
        token = self.token = object()
        future = self.run_fn(coro, lambda f: self.finished(token, f, on_done))
        if self.token is token:  # still waiting (not finished already)
            self.future = future
        return future

    @property
    def busy(self) -> bool:
        return self.token is not None

    def cancel(self):
        future, self.future, self.token = self.future, None, None
        self.stop()
        if future is not None:
            future.cancel()
        if self.change:
            QMessageBox.information(self.parentWidget(), "Stopped", CHANGE_CANCELLED)
        elif hasattr(self.parentWidget(), "reject"):
            self.parentWidget().reject()

    # ---- internals ------------------------------------------------------------------------------

    def finished(self, token, future, on_done):
        if token is not self.token:  # cancelled by the user (or superseded): they no longer wait for it
            return
        self.future = self.token = None
        self.stop()
        on_done(future)

    def show_over_parent(self):
        parent = self.parentWidget()
        self.locked = [w for w in parent.findChildren(QWidget, options=Qt.FindDirectChildrenOnly)
                       if w is not self and w.isEnabled()]
        for widget in self.locked:
            widget.setEnabled(False)
        self.setGeometry(parent.rect())
        self.raise_()
        self.show()
        self.cancel_button.setFocus()
        self.spinner.timer.start()
        self.ticker.start()
        self.tick()

    def stop(self):
        self.ticker.stop()
        self.spinner.timer.stop()
        self.hide()
        for widget in self.locked:
            widget.setEnabled(True)
        self.locked = []

    def tick(self):
        elapsed = int(time.monotonic() - self.started)
        self.status.setText(f"{self.text} {elapsed} s" if elapsed else self.text)
        self.hint.setVisible(elapsed >= SLOW_AFTER)

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Resize:
            self.setGeometry(watched.rect())
        return False

    def paintEvent(self, _event):
        QPainter(self).fillRect(self.rect(), QColor(17, 18, 20, 215))
