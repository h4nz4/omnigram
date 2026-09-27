"""A Telegram-style chat window for one account: chat list on the left, conversation on the right.

The window owns one live connection (telegram.ChatClient) for as long as it is open; it is registered as the
"chat/<session>" task, so the account shows as busy and nothing else opens its session meanwhile. Everything
the connection returns is plain data (omnigram.chat); live events arrive through MainWindow's GUI-thread signal.

Both lists are Model/View with painted rows (no widget per chat or per message), so long chat lists and
histories stay cheap. Read receipts are opt-in per window ("Mark as read"), except that sending marks a chat
read, as Telegram does.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QRectF, QSize, QSortFilterProxyModel, Qt, QUrl
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QFontMetrics,
    QImage,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPixmap,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from omnigram import chat, icons, telegram
from omnigram.loading import LoadingOverlay
from omnigram.store import Account

MSG = Qt.UserRole + 1  # model role carrying the chat.Chat / chat.Msg itself
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
OUT_BUBBLE, IN_BUBBLE = QColor("#2b5278"), QColor("#1f2125")
TEXT, MUTED, ACCENT = QColor("#e4e6eb"), QColor("#8b8f98"), QColor("#6ab3f3")


# ---- models ---------------------------------------------------------------------------------------

class ChatListModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.chats: list[chat.Chat] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.chats)

    def data(self, index, role=Qt.DisplayRole):
        c = self.chats[index.row()]
        return c.title if role == Qt.DisplayRole else c if role == MSG else None

    def reset(self, chats: list[chat.Chat]):
        self.beginResetModel()
        self.chats = list(chats)
        self.endResetModel()

    def extend(self, chats: list[chat.Chat]):
        known = {c.id for c in self.chats}
        new = [c for c in chats if c.id not in known]
        if new:
            self.beginInsertRows(QModelIndex(), len(self.chats), len(self.chats) + len(new) - 1)
            self.chats.extend(new)
            self.endInsertRows()

    def row_of(self, chat_id: int) -> int:
        return next((i for i, c in enumerate(self.chats) if c.id == chat_id), -1)

    def bump(self, msg: chat.Msg, unread: bool) -> bool:
        """A new message moved its chat to the top with a fresh preview; False if the chat isn't loaded yet."""
        row = self.row_of(msg.chat_id)
        if row < 0:
            return False
        c = self.chats[row]
        c.last_text, c.last_date = chat.preview(msg), msg.date
        c.unread += 1 if unread else 0
        if row:
            self.beginMoveRows(QModelIndex(), row, row, QModelIndex(), 0)
            self.chats.insert(0, self.chats.pop(row))
            self.endMoveRows()
        self.dataChanged.emit(self.index(0), self.index(0))
        return True

    def set_unread(self, chat_id: int, count: int):
        row = self.row_of(chat_id)
        if row >= 0:
            self.chats[row].unread = count
            self.dataChanged.emit(self.index(row), self.index(row))

    def refresh_top(self, fresh: list[chat.Chat]):
        """A fresh first page: its chats (new ones included) go on top, the rest keep their order below."""
        ids = {c.id for c in fresh}
        self.beginResetModel()
        self.chats = list(fresh) + [c for c in self.chats if c.id not in ids]
        self.endResetModel()


class MessageModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.msgs: list[chat.Msg] = []
        self.by_id: dict[int, chat.Msg] = {}
        self.thumbs: dict[int, QPixmap] = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.msgs)

    def data(self, index, role=Qt.DisplayRole):
        m = self.msgs[index.row()]
        return m.text if role == Qt.DisplayRole else m if role == MSG else None

    def _set(self, msgs: list[chat.Msg]):
        self.msgs = msgs
        self.by_id = {m.id: m for m in msgs}

    def reset(self, msgs: list[chat.Msg]):
        self.beginResetModel()
        self._set(list(msgs))
        self.thumbs = {}
        self.endResetModel()

    def merge(self, incoming: list[chat.Msg]):
        """Older pages, live messages and edits, without duplicates. Appends and prepends insert rows (the view
        keeps its place); only an out-of-order mix falls back to a reset."""
        merged = chat.merge(self.msgs, incoming)
        old_ids, new_ids = [m.id for m in self.msgs], [m.id for m in merged]
        added = len(new_ids) - len(old_ids)
        if added == 0 and new_ids == old_ids:  # edits only
            self._set(merged)
            self.dataChanged.emit(self.index(0), self.index(len(merged) - 1))
        elif added > 0 and new_ids[:len(old_ids)] == old_ids:  # newer messages
            self.beginInsertRows(QModelIndex(), len(old_ids), len(new_ids) - 1)
            self._set(merged)
            self.endInsertRows()
        elif added > 0 and new_ids[added:] == old_ids:  # an older page
            self.beginInsertRows(QModelIndex(), 0, added - 1)
            self._set(merged)
            self.endInsertRows()
        else:
            self.beginResetModel()
            self._set(merged)
            self.endResetModel()

    def remove(self, ids: set[int]):
        for row in reversed([i for i, m in enumerate(self.msgs) if m.id in ids]):
            self.beginRemoveRows(QModelIndex(), row, row)
            self._set(self.msgs[:row] + self.msgs[row + 1:])
            self.endRemoveRows()

    def find(self, msg_id: int) -> chat.Msg | None:
        return self.by_id.get(msg_id)

    def row_of(self, msg_id: int) -> int:
        msg = self.by_id.get(msg_id)
        return self.msgs.index(msg) if msg is not None else -1

    def set_thumb(self, msg_id: int, pixmap: QPixmap) -> QModelIndex:
        self.thumbs[msg_id] = pixmap
        row = self.row_of(msg_id)
        if row < 0:
            return QModelIndex()
        index = self.index(row)
        self.dataChanged.emit(index, index)
        return index


# ---- painting -------------------------------------------------------------------------------------

def _time(value: datetime | None) -> str:
    if value is None:
        return ""
    local = value.astimezone()
    return f"{local:%H:%M}" if local.date() == datetime.now().date() else f"{local:%d.%m.%y}"


class ChatRowDelegate(QStyledItemDelegate):
    HEIGHT = 58

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.HEIGHT)

    def paint(self, painter: QPainter, option, index):
        c: chat.Chat = index.data(MSG)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        r = option.rect
        if option.state & QStyle.State_Selected:
            painter.fillRect(r, QColor("#1d2533"))
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(r, QColor("#1a1b1f"))
        bold = QFont(option.font)
        bold.setBold(True)
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.9)
        when = _time(c.last_date)
        painter.setFont(small)
        painter.setPen(MUTED)
        time_w = QFontMetrics(small).horizontalAdvance(when) + 4
        painter.drawText(QRect(r.right() - time_w - 10, r.top() + 8, time_w, 20), Qt.AlignRight, when)
        painter.setFont(bold)
        painter.setPen(TEXT)
        title_rect = QRect(r.left() + 12, r.top() + 8, r.width() - time_w - 30, 20)
        painter.drawText(title_rect, Qt.AlignLeft, QFontMetrics(bold).elidedText(c.title, Qt.ElideRight,
                                                                                  title_rect.width()))
        badge_w = 0
        if c.unread:
            text = str(c.unread) if c.unread < 1000 else "999+"
            badge_w = max(20, QFontMetrics(small).horizontalAdvance(text) + 12)
            badge = QRectF(r.right() - badge_w - 10, r.top() + 31, badge_w, 18)
            painter.setBrush(QColor("#3b82f6"))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(badge, 9, 9)
            painter.setFont(small)
            painter.setPen(QColor("white"))
            painter.drawText(badge, Qt.AlignCenter, text)
        painter.setFont(small)
        painter.setPen(MUTED)
        line_rect = QRect(r.left() + 12, r.top() + 31, r.width() - badge_w - 34, 20)
        painter.drawText(line_rect, Qt.AlignLeft, QFontMetrics(small).elidedText(c.last_text, Qt.ElideRight,
                                                                                  line_rect.width()))
        painter.restore()


class BubbleDelegate(QStyledItemDelegate):
    """Paints one message as a Telegram-style bubble: yours on the right, sender names in groups, a date header
    when the day changes, reply/forward lines, a media preview, text, and time (+ "edited")."""

    PAD, GAP, HEADER, THUMB_MAX, THUMB_PLACEHOLDER = 10, 6, 34, 280, QSize(220, 150)

    def __init__(self, view: QListView, model: MessageModel, is_group):
        super().__init__(view)
        self.view, self.model, self.is_group = view, model, is_group
        self.media_rects: dict[int, QRect] = {}  # message id -> media area, relative to the row, for clicks
        self.docs: dict[tuple, QTextDocument] = {}  # (id, text, width) -> laid-out text; layout runs per paint

    # -- layout --

    def _fonts(self, option):
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.85)
        bold = QFont(option.font)
        bold.setBold(True)
        return option.font, small, bold

    def _doc(self, msg: chat.Msg, font: QFont, width: int) -> QTextDocument:
        key = (msg.id, msg.text, width)
        if key not in self.docs:
            if len(self.docs) > 2000:  # a long session: drop the cache rather than grow without bound
                self.docs.clear()
            self.docs[key] = self._new_doc(msg.text, font, width)
        return self.docs[key]

    def _new_doc(self, text: str, font: QFont, width: int) -> QTextDocument:
        doc = QTextDocument()
        doc.setDefaultFont(font)
        doc.setDocumentMargin(0)
        doc.setPlainText(text)
        doc.setTextWidth(width)
        doc.setTextWidth(min(width, doc.idealWidth() + 1))  # shrink-wrap short messages
        return doc

    def _thumb_size(self, msg: chat.Msg) -> QSize:
        pixmap = self.model.thumbs.get(msg.id)
        if pixmap is None:
            return self.THUMB_PLACEHOLDER
        size = pixmap.size() / max(1.0, pixmap.devicePixelRatio())
        return size.scaled(QSize(self.THUMB_MAX, self.THUMB_MAX), Qt.KeepAspectRatio) \
            if size.width() > self.THUMB_MAX or size.height() > self.THUMB_MAX else size

    def layout(self, option, index) -> dict:
        msg: chat.Msg = index.data(MSG)
        font, small, bold = self._fonts(option)
        width = option.rect.width() or self.view.viewport().width()
        max_inner = int(min(width * 0.72, 560)) - 2 * self.PAD
        rows = self.model.msgs
        previous = rows[index.row() - 1] if index.row() > 0 else None
        parts = []  # (kind, height, width, payload)
        fm, fm_small = QFontMetrics(font), QFontMetrics(small)
        if self.is_group() and not msg.out and msg.sender:
            parts.append(("sender", QFontMetrics(bold).height(), QFontMetrics(bold).horizontalAdvance(msg.sender),
                          msg.sender))
        if msg.forwarded:
            parts.append(("forward", fm_small.height(), fm_small.horizontalAdvance(msg.forwarded), msg.forwarded))
        if msg.reply_to:
            quoted = self.model.find(msg.reply_to)
            line = f"{(quoted.sender or 'You') if quoted else 'Reply'}: {chat.preview(quoted) if quoted else '…'}"
            parts.append(("reply", fm_small.height() + 6, min(max_inner, fm_small.horizontalAdvance(line) + 12),
                          line))
        if msg.has_thumb:
            size = self._thumb_size(msg)
            parts.append(("thumb", size.height(), size.width(), None))
        elif msg.media:
            label = ("⬇  " if msg.media not in ("location", "contact", "poll") else "") + msg.media_label
            parts.append(("media", fm.height() + 4, fm.horizontalAdvance(label), label))
        if msg.text:
            doc = self._doc(msg, font, max_inner)
            parts.append(("text", int(doc.size().height()), int(doc.textWidth()), doc))
        footer = _time(msg.date) + ("  edited" if msg.edited else "")
        footer_w = fm_small.horizontalAdvance(footer)
        inner_w = max([p[2] for p in parts] + [footer_w])
        inner_h = sum(p[1] for p in parts) + self.GAP * max(0, len(parts) - 1) + fm_small.height() + 2
        header = self.HEADER if chat.starts_day(previous, msg) else 0
        return {"msg": msg, "parts": parts, "footer": footer, "inner": QSize(inner_w, inner_h), "header": header,
                "fonts": (font, small, bold)}

    def sizeHint(self, option, index):
        lay = self.layout(option, index)
        return QSize(option.rect.width(), lay["header"] + lay["inner"].height() + 2 * self.PAD + 6)

    # -- paint --

    def paint(self, painter: QPainter, option, index):
        lay = self.layout(option, index)
        msg: chat.Msg = lay["msg"]
        font, small, bold = lay["fonts"]
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        r = option.rect
        top = r.top()
        if lay["header"]:
            label = chat.day_label(msg.date.astimezone().date(), datetime.now().date())
            painter.setFont(small)
            w = QFontMetrics(small).horizontalAdvance(label) + 20
            pill = QRectF(r.center().x() - w / 2, top + 8, w, 20)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#1a1b1f"))
            painter.drawRoundedRect(pill, 10, 10)
            painter.setPen(MUTED)
            painter.drawText(pill, Qt.AlignCenter, label)
            top += lay["header"]
        bubble_w = lay["inner"].width() + 2 * self.PAD
        bubble_h = lay["inner"].height() + 2 * self.PAD
        left = r.right() - bubble_w - 12 if msg.out else r.left() + 12
        bubble = QRectF(left, top + 3, bubble_w, bubble_h)
        path = QPainterPath()
        path.addRoundedRect(bubble, 12, 12)
        painter.fillPath(path, OUT_BUBBLE if msg.out else IN_BUBBLE)
        if option.state & QStyle.State_Selected:
            painter.setPen(ACCENT)
            painter.drawPath(path)
        x, y = int(bubble.left()) + self.PAD, int(bubble.top()) + self.PAD
        for kind, height, width, payload in lay["parts"]:
            if kind == "sender":
                painter.setFont(bold)
                painter.setPen(ACCENT)
                painter.drawText(QRect(x, y, width + 2, height), Qt.AlignLeft, payload)
            elif kind == "forward":
                painter.setFont(small)
                painter.setPen(ACCENT)
                painter.drawText(QRect(x, y, width + 2, height), Qt.AlignLeft, payload)
            elif kind == "reply":
                painter.fillRect(QRect(x, y + 2, 3, height - 4), ACCENT)
                painter.setFont(small)
                painter.setPen(MUTED)
                painter.drawText(QRect(x + 9, y + 2, width - 9, height - 4), Qt.AlignLeft | Qt.AlignVCenter,
                                 QFontMetrics(small).elidedText(payload, Qt.ElideRight, width - 9))
            elif kind == "thumb":
                area = QRect(x, y, width, height)
                pixmap = self.model.thumbs.get(msg.id)
                clip = QPainterPath()
                clip.addRoundedRect(QRectF(area), 8, 8)
                if pixmap is not None:
                    painter.save()
                    painter.setClipPath(clip)
                    painter.drawPixmap(area, pixmap)
                    painter.restore()
                else:
                    painter.fillPath(clip, QColor("#15161a"))
                    painter.setFont(small)
                    painter.setPen(MUTED)
                    painter.drawText(area, Qt.AlignCenter, msg.media_label)
                if msg.media in ("video", "gif", "document"):
                    painter.setFont(small)
                    tag = QRect(area.left() + 6, area.top() + 6, QFontMetrics(small).horizontalAdvance(
                        msg.media_label) + 12, 18)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(0, 0, 0, 150))
                    painter.drawRoundedRect(tag, 9, 9)
                    painter.setPen(QColor("white"))
                    painter.drawText(tag, Qt.AlignCenter, msg.media_label)
                self.media_rects[msg.id] = area.translated(-r.left(), -r.top())
            elif kind == "media":
                painter.setFont(font)
                painter.setPen(ACCENT)
                area = QRect(x, y, width + 2, height)
                painter.drawText(area, Qt.AlignLeft | Qt.AlignVCenter, payload)
                self.media_rects[msg.id] = area.translated(-r.left(), -r.top())
            elif kind == "text":
                painter.save()
                painter.translate(x, y)
                context = QAbstractTextDocumentLayout.PaintContext()
                context.palette.setColor(QPalette.Text, TEXT)
                payload.documentLayout().draw(painter, context)
                painter.restore()
            y += height + self.GAP
        painter.setFont(small)
        painter.setPen(QColor("#a9c7e6") if msg.out else MUTED)
        foot_h = QFontMetrics(small).height()
        painter.drawText(QRect(int(bubble.right()) - self.PAD - lay["inner"].width(),
                               int(bubble.bottom()) - self.PAD - foot_h, lay["inner"].width(), foot_h),
                         Qt.AlignRight, lay["footer"])
        painter.restore()


# ---- the window -----------------------------------------------------------------------------------

class Composer(QPlainTextEdit):
    """Enter sends, Shift+Enter makes a new line."""

    def __init__(self, on_send):
        super().__init__(placeholderText="Write a message…", objectName="composer")
        self.on_send = on_send
        self.setFixedHeight(64)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self.on_send()
            return
        super().keyPressEvent(event)


class ChatWindow(QWidget):
    def __init__(self, window, account: Account):
        super().__init__(None, objectName="page")
        self.window, self.account = window, account
        self.key = f"chat/{account.session}"
        self.closed = False
        self.current: chat.Chat | None = None
        self.reply_to: chat.Msg | None = None
        self.editing: chat.Msg | None = None
        self.loading_chats = self.loading_older = False
        self.more_chats = True
        self.no_older = False
        self.thumb_queue: list[chat.Msg] = []
        self.thumb_busy = False
        name = account.name or account.session
        self.setWindowTitle(f"Chats — {name}")
        self.setWindowIcon(icons.get("message-circle-more"))
        self.resize(1000, 700)
        self.downloads = window.store.sessions.parent / "downloads" / account.session

        # left: chat list
        self.chats = ChatListModel()
        self.chat_filter = QSortFilterProxyModel(filterCaseSensitivity=Qt.CaseInsensitive)
        self.chat_filter.setSourceModel(self.chats)
        self.search = QLineEdit(placeholderText="Search loaded chats")
        self.search.addAction(icons.get("search"), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self.chat_filter.setFilterFixedString)
        self.chat_list = QListView(uniformItemSizes=True, mouseTracking=True)
        self.chat_list.setModel(self.chat_filter)
        self.chat_list.setItemDelegate(ChatRowDelegate(self.chat_list))
        self.chat_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.chat_list.selectionModel().currentChanged.connect(self.on_chat_selected)
        self.chat_list.verticalScrollBar().valueChanged.connect(self.on_chat_scroll)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(8, 8, 0, 8)
        left_box.addWidget(self.search)
        left_box.addWidget(self.chat_list, 1)

        # right: conversation
        self.title = QLabel("Select a chat", objectName="pageTitle")
        self.status = QLabel(objectName="muted")
        self.mark_read = QCheckBox("Mark as read")
        self.mark_read.setToolTip("Off: reading here doesn't show 'seen' to the other side. Sending always marks "
                                  "the chat as read.")
        self.mark_read.toggled.connect(self.on_mark_read_toggled)
        open_folder = QPushButton(icons.get("folder-open"), "")
        open_folder.setToolTip("Open this account's downloads folder")
        open_folder.clicked.connect(self.open_downloads)
        head = QHBoxLayout()
        head.addWidget(self.title, 1)
        head.addWidget(self.status)
        head.addWidget(self.mark_read)
        head.addWidget(open_folder)

        self.messages = MessageModel()
        self.view = QListView(mouseTracking=True, wordWrap=True)
        self.view.setModel(self.messages)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.bubbles = BubbleDelegate(self.view, self.messages,
                                      lambda: bool(self.current and self.current.kind == "group"))
        self.view.setItemDelegate(self.bubbles)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.message_menu)
        self.view.clicked.connect(self.on_message_clicked)
        self.view.verticalScrollBar().valueChanged.connect(self.on_history_scroll)

        self.context_bar = QWidget()
        bar = QHBoxLayout(self.context_bar)
        bar.setContentsMargins(4, 0, 4, 0)
        self.context_label = QLabel(objectName="muted")
        cancel_context = QPushButton(icons.get("x"), "")
        cancel_context.setToolTip("Cancel")
        cancel_context.clicked.connect(self.clear_context)
        bar.addWidget(self.context_label, 1)
        bar.addWidget(cancel_context)
        self.context_bar.hide()
        self.composer = Composer(self.send)
        self.attach = QPushButton(icons.get("paperclip"), "")
        self.attach.setToolTip("Attach a photo or file")
        self.attach.clicked.connect(self.on_attach)
        self.send_button = QPushButton(icons.get("send-horizontal"), "", objectName="primary")
        self.send_button.setToolTip("Send (Enter)")
        self.send_button.clicked.connect(self.send)
        composer_row = QHBoxLayout()
        composer_row.addWidget(self.attach, 0, Qt.AlignBottom)
        composer_row.addWidget(self.composer, 1)
        composer_row.addWidget(self.send_button, 0, Qt.AlignBottom)
        self.input_area = QWidget()
        input_box = QVBoxLayout(self.input_area)
        input_box.setContentsMargins(0, 0, 0, 0)
        input_box.addWidget(self.context_bar)
        input_box.addLayout(composer_row)
        self.input_area.setEnabled(False)

        self.right = QWidget()
        right_box = QVBoxLayout(self.right)
        right_box.setContentsMargins(8, 8, 8, 8)
        right_box.addLayout(head)
        right_box.addWidget(self.view, 1)
        right_box.addWidget(self.input_area)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.right)
        splitter.setSizes([300, 700])
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(splitter)

        self.chat_overlay = LoadingOverlay(left, window.run)
        self.history_overlay = LoadingOverlay(self.right, window.run)
        self.overlay = LoadingOverlay(self, window.run)

        self.client = telegram.ChatClient(window.store.path(account), *window.credentials(account), account.proxy,
                                          self.emit_event)
        self.future = window.start_task(self.key, self.client.run(), f"chats [{name}]", on_done=self.on_ended)
        self.overlay.run(self.client.ready(), "Connecting…", self.on_ready)

    # ---- plumbing -------------------------------------------------------------------------------

    def emit_event(self, kind, payload):  # Telethon thread
        self.window._call.emit(lambda: None if self.closed else self.on_event(kind, payload))

    def call(self, coro, on_done):
        """Run one ChatClient coroutine; on_done(result) on success, errors shown in the status line."""
        def done(future):
            if self.closed or future.cancelled():
                return
            try:
                result = future.result()
            except Exception as e:
                self.status.setText(f"✗ {type(e).__name__}: {e}")
                return
            on_done(result)
        return self.window.run(coro, done)

    def reject(self):  # LoadingOverlay: cancelling "Connecting…" closes the window
        self.close()

    def closeEvent(self, event):
        self.closed = True
        self.window.stop_task(self.key)
        self.window.chat_windows.pop(self.account.session, None)
        super().closeEvent(event)

    def on_ended(self):
        """The connection ended (closed, cancelled, or failed): a failure is worth telling before closing."""
        if self.closed:
            return
        error = None if self.future.cancelled() else self.future.exception()
        if error is not None and not self.overlay.busy:  # while connecting, on_ready reports it instead
            QMessageBox.warning(self, "Chats", f"The connection ended: {type(error).__name__}: {error}")
        if error is not None:
            self.close()

    def on_ready(self, future):
        try:
            future.result()
        except Exception as e:
            QMessageBox.warning(self, "Chats", f"Could not connect: {e}")
            self.close()
            return
        self.load_chats()

    # ---- chat list ------------------------------------------------------------------------------

    def load_chats(self):
        """The first page, behind the list's overlay."""
        def done(future):
            try:
                chats, self.more_chats = future.result()
            except Exception as e:
                self.status.setText(f"✗ Could not load chats: {e}")
                return
            self.chats.reset(chats)

        self.chat_overlay.run(self.client.dialogs(), "Loading chats…", done)

    def load_more_chats(self):
        self.loading_chats = True

        def done(result):
            self.loading_chats = False
            chats, self.more_chats = result
            self.chats.extend(chats)

        self.call(self.client.dialogs(more=True), done)

    def refresh_top_chats(self):
        """A message arrived in a chat that isn't loaded yet: re-read the first page and keep the selection."""
        def done(result):
            self.chats.refresh_top(result[0])
            if self.current:
                row = self.chats.row_of(self.current.id)
                if row >= 0:
                    self.chat_list.selectionModel().blockSignals(True)
                    self.chat_list.setCurrentIndex(self.chat_filter.mapFromSource(self.chats.index(row)))
                    self.chat_list.selectionModel().blockSignals(False)

        self.call(self.client.dialogs(), done)

    def on_chat_scroll(self, value):
        bar = self.chat_list.verticalScrollBar()
        if value >= bar.maximum() - 5 and self.more_chats and not self.loading_chats and not self.search.text():
            self.load_more_chats()

    def on_chat_selected(self, current, _previous):
        if not current.isValid():
            return
        c: chat.Chat = current.data(MSG)
        self.current = c
        self.clear_context()
        self.title.setText(c.title)
        self.input_area.setEnabled(c.can_send)
        self.composer.setPlaceholderText("Write a message…" if c.can_send else "Only admins can post here")
        self.no_older = False
        self.messages.reset([])
        self.thumb_queue = []

        def done(future):
            try:
                msgs = future.result()
            except Exception as e:
                self.status.setText(f"✗ Could not load messages: {e}")
                return
            if self.current is not c:
                return
            self.messages.reset(msgs)
            self.no_older = len(msgs) < 50
            self.view.scrollToBottom()
            self.queue_thumbs(msgs)
            if c.unread and msgs and chat.mark_read_now(self.mark_read.isChecked(), "open"):
                self.read(c, msgs[-1].id)

        self.history_overlay.run(self.client.history(c.id), f"Loading {c.title}…", done)

    def read(self, c: chat.Chat, max_id: int):
        self.call(self.client.mark_read(c.id, max_id), lambda _: self.chats.set_unread(c.id, 0))

    def on_mark_read_toggled(self, on: bool):
        if on and self.current and self.current.unread and self.messages.msgs:
            self.read(self.current, self.messages.msgs[-1].id)

    # ---- history --------------------------------------------------------------------------------

    def on_history_scroll(self, value):
        if value > 5 or self.loading_older or self.no_older or not self.current or not self.messages.msgs:
            return
        self.loading_older = True
        c, oldest = self.current, self.messages.msgs[0].id
        bar = self.view.verticalScrollBar()
        from_bottom = bar.maximum() - bar.value()

        def done(msgs):
            self.loading_older = False
            if self.current is not c:
                return
            self.no_older = len(msgs) < 50
            if msgs:
                self.messages.merge(msgs)
                self.view.doItemsLayout()
                bar.setValue(bar.maximum() - from_bottom)
                self.queue_thumbs(msgs)

        self.call(self.client.history(c.id, before_id=oldest), done)

    def queue_thumbs(self, msgs: list[chat.Msg]):
        """Previews load one at a time, newest first, so a long history doesn't fire dozens of downloads."""
        self.thumb_queue.extend(m for m in reversed(msgs) if m.has_thumb and m.id not in self.messages.thumbs)
        self.next_thumb()

    def next_thumb(self):
        if self.thumb_busy or not self.thumb_queue:
            return
        msg = self.thumb_queue.pop(0)
        if msg.chat_id != (self.current.id if self.current else None):
            self.thumb_queue.clear()
            return
        self.thumb_busy = True

        def done(future):
            self.thumb_busy = False
            if self.closed:
                return
            data = None if future.cancelled() or future.exception() else future.result()
            image = QImage.fromData(data) if data else QImage()
            if not image.isNull():
                index = self.messages.set_thumb(msg.id, QPixmap.fromImage(image))
                if index.isValid():
                    self.bubbles.sizeHintChanged.emit(index)  # the real preview's size replaces the placeholder
            self.next_thumb()

        self.window.run(self.client.thumbnail(msg.chat_id, msg.id), done)

    # ---- live events ----------------------------------------------------------------------------

    def on_event(self, kind: str, payload):
        if kind == "progress":
            label, fraction = payload
            self.status.setText(f"{label} {fraction:.0%}" if fraction < 1 else "")
            return
        if kind == "deleted":
            chat_id, ids = payload
            if self.current and (chat_id == self.current.id or (chat_id is None and self.current.kind != "channel")):
                self.messages.remove(set(ids))
            return
        msg: chat.Msg = payload
        here = bool(self.current and msg.chat_id == self.current.id)
        if kind == "edited":
            if here and self.messages.find(msg.id):
                self.messages.merge([msg])
            return
        # a new message: move its chat up; count it unread unless it is ours or is being read right now
        reading = here and chat.mark_read_now(self.mark_read.isChecked(), "incoming")
        if not self.chats.bump(msg, unread=not msg.out and not reading):
            self.refresh_top_chats()  # a chat that isn't loaded yet just became the newest one
        if here:
            at_bottom = self.view.verticalScrollBar().value() >= self.view.verticalScrollBar().maximum() - 20
            self.messages.merge([msg])
            self.queue_thumbs([msg])
            if at_bottom or msg.out:
                self.view.scrollToBottom()
            if not msg.out and reading:
                self.read(self.current, msg.id)

    # ---- composing ------------------------------------------------------------------------------

    def clear_context(self):
        self.reply_to = self.editing = None
        self.context_bar.hide()

    def set_context(self, text: str):
        self.context_label.setText(text)
        self.context_bar.show()
        self.composer.setFocus()

    def send(self):
        text = self.composer.toPlainText().strip()
        if not text or not self.current or not self.send_button.isEnabled():
            return
        c = self.current
        if self.editing:
            coro = self.client.edit(c.id, self.editing.id, text)
        else:
            coro = self.client.send_text(c.id, text, self.reply_to.id if self.reply_to else None)
        self.send_button.setEnabled(False)
        self.status.setText("Sending…")

        def done(future):
            self.send_button.setEnabled(True)
            if self.closed or future.cancelled():
                return
            try:
                msg = future.result()
            except Exception as e:
                self.status.setText(f"✗ {type(e).__name__}: {e}")
                return  # the text stays in the box, so nothing typed is lost
            self.status.setText("")
            self.composer.clear()
            self.clear_context()
            self.after_sent(c, msg)

        self.window.run(coro, done)

    def after_sent(self, c: chat.Chat, msg: chat.Msg):
        if self.current is c:
            self.messages.merge([msg])
            self.view.scrollToBottom()
        self.chats.bump(msg, unread=False)
        if chat.mark_read_now(self.mark_read.isChecked(), "sent") and c.unread:
            self.read(c, msg.id)

    def on_attach(self):
        if not self.current:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Attach a photo or file")
        if not path:
            return
        compress = False
        if Path(path).suffix.lower() in IMAGE_SUFFIXES:
            box = QMessageBox(QMessageBox.Question, "Send picture", f"How should {Path(path).name} be sent?",
                              parent=self)
            photo = box.addButton("Photo (compressed)", QMessageBox.AcceptRole)
            box.addButton("File (original)", QMessageBox.AcceptRole)
            cancel = box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(photo)
            box.exec()
            if box.clickedButton() is cancel:
                return
            compress = box.clickedButton() is photo
        c, caption = self.current, self.composer.toPlainText().strip()
        reply = self.reply_to.id if self.reply_to else None
        self.attach.setEnabled(False)
        self.status.setText(f"Uploading {Path(path).name}…")

        def done(future):
            self.attach.setEnabled(True)
            if self.closed or future.cancelled():
                return
            try:
                msg = future.result()
            except Exception as e:
                self.status.setText(f"✗ Upload failed: {e}")
                return
            self.status.setText("")
            self.composer.clear()
            self.clear_context()
            self.after_sent(c, msg)
            self.queue_thumbs([msg])

        self.window.run(self.client.send_file(c.id, path, caption, compress, reply), done)

    # ---- message actions ------------------------------------------------------------------------

    def message_at(self, pos) -> chat.Msg | None:
        index = self.view.indexAt(pos)
        return index.data(MSG) if index.isValid() else None

    def message_menu(self, pos):
        msg = self.message_at(pos)
        if msg is None or not self.current:
            return
        menu = QMenu(self)
        if self.current.can_send:
            menu.addAction(icons.get("reply"), "Reply", lambda: self.start_reply(msg))
        if msg.text:
            menu.addAction(icons.get("copy"), "Copy text", lambda: QGuiApplication.clipboard().setText(msg.text))
        if msg.media and msg.media not in ("location", "contact", "poll"):
            menu.addAction(icons.get("download"), "Download", lambda: self.download(msg))
        if msg.out:
            if msg.text or not msg.media:
                menu.addAction(icons.get("pencil"), "Edit", lambda: self.start_edit(msg))
            menu.addAction(icons.get("trash"), "Delete…", lambda: self.delete(msg))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def start_reply(self, msg: chat.Msg):
        self.editing = None
        self.reply_to = msg
        self.set_context(f"Replying to {msg.sender or 'yourself'}: {chat.preview(msg)}")

    def start_edit(self, msg: chat.Msg):
        self.reply_to = None
        self.editing = msg
        self.composer.setPlainText(msg.text)
        self.set_context(f"Editing: {chat.preview(msg)}")

    def delete(self, msg: chat.Msg):
        c = self.current
        box = QMessageBox(QMessageBox.Question, "Delete message", "Delete this message?", parent=self)
        everyone = QCheckBox("Also delete for the other side" if c.kind in ("user", "bot") else
                             "Delete for everyone", box)
        everyone.setChecked(c.kind in ("user", "bot"))  # Telegram's own default in private chats
        box.setCheckBox(everyone)
        delete = box.addButton("Delete", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not delete:
            return
        revoke = everyone.isChecked()
        self.call(self.client.delete(c.id, [msg.id], revoke), lambda _: self.messages.remove({msg.id}))

    def on_message_clicked(self, index):
        msg: chat.Msg = index.data(MSG)
        area = self.bubbles.media_rects.get(msg.id)
        if area is None or msg.media in ("location", "contact", "poll"):
            return
        click = self.view.viewport().mapFromGlobal(QCursor.pos()) - self.view.visualRect(index).topLeft()
        if area.contains(click):
            self.download(msg)

    def download(self, msg: chat.Msg):
        self.status.setText(f"Downloading {msg.media_label}…")

        def done(path):
            self.status.setText("")
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

        self.call(self.client.download(msg.chat_id, msg.id, self.downloads), done)

    def open_downloads(self):
        self.downloads.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.downloads)))
