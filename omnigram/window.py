import asyncio
import html
import shutil
from collections import Counter
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPointF,
    QSettings,
    QSize,
    QSortFilterProxyModel,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QIcon,
    QIntValidator,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omnigram import __version__, icons, proxies, telegram, warmup
from omnigram.audience_dialogs import FunnelDialog, NumberCheckerDialog, ParserDialog
from omnigram.chat_window import ChatWindow
from omnigram.backup import export_backup, import_backup
from omnigram.content_dialogs import ClonerDialog, ForwarderDialog, ReporterDialog
from omnigram.dialogs import (
    ChatsDialog,
    InfoDialog,
    ListenerDialog,
    PasswordDialog,
    ProfileDialog,
    ProxyPage,
    ScheduleDialog,
    SessionsDialog,
)
from omnigram.importers import import_session, import_tdata, read_session_json
from omnigram.mailing_dialogs import (
    AutoPostDialog,
    BroadcastDialog,
    CommentNowDialog,
    TemplatesDialog,
    WatchDialog,
)
from omnigram.promotion_dialogs import (
    BoostDialog,
    InviterDialog,
    JoinDialog,
    StoryViewDialog,
)
from omnigram.store import Account, Store
from omnigram.template_store import TemplateStore
from omnigram.warmup_dialogs import (
    DialoguesDialog,
    OnlineKeeperDialog,
    RandomizerDialog,
    WarmupDialog,
)


STYLE = """
* { font-family: "Segoe UI", "Inter", sans-serif; font-size: 13px; color: #e4e6eb; }
QMainWindow, #page { background: #111214; }
#sidebar { background: #16171a; border-right: 1px solid #25272b; }
#appTitle { font-size: 17px; font-weight: 600; }
#pageTitle { font-size: 20px; font-weight: 600; }
#muted, #cardLabel, #section { color: #8b8f98; font-size: 11px; }
#section { font-weight: 600; letter-spacing: 1px; padding: 10px 10px 2px; }
#card { background: #17181c; border: 1px solid #2a2c31; border-radius: 8px; }
#cardValue { font-size: 16px; font-weight: 600; }
QLineEdit, QComboBox { background: #1a1b1f; border: 1px solid #2a2c31; border-radius: 6px; padding: 6px 10px; }
QLineEdit:focus, QComboBox:focus { border-color: #3b82f6; }
QPushButton { background: #1a1b1f; border: 1px solid #2a2c31; border-radius: 6px; padding: 7px 14px; }
QPushButton:hover { background: #22242a; }
QPushButton#primary { background: #3b82f6; border-color: #3b82f6; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #2f74e6; }
QPushButton#danger { color: #f87171; }
QPushButton:disabled, QPushButton#danger:disabled { color: #55585f; border-color: #1f2024; }
QListView { background: #111214; border: none; outline: 0; }
QPlainTextEdit#composer { background: #1a1b1f; border: 1px solid #2a2c31; border-radius: 6px; padding: 4px 6px; }
QPlainTextEdit#composer:focus { border-color: #3b82f6; }
QListWidget, QTreeWidget { background: transparent; border: none; outline: 0; }
QListWidget::item { padding: 7px 8px; border-radius: 6px; }
QListWidget::item:hover { background: #1f2125; }
QTreeWidget::item { padding: 5px 4px; border-radius: 6px; }
QTreeWidget::item:hover { background: #1f2125; }
QTreeWidget::item:has-children { color: #b9bdc5; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; }
QTreeWidget::item:disabled { color: #55585f; font-style: italic; }
QTreeWidget::branch { background: transparent; }
QTableView { background: #111214; border: none; selection-background-color: #1d2533; outline: 0; }
QTableView::item { border-bottom: 1px solid #1f2125; padding: 0 6px; }
QHeaderView::section { background: #111214; color: #8b8f98; border: none; border-bottom: 1px solid #25272b;
                       padding: 8px 6px; font-size: 11px; }
#log { background: #0d0e10; border: none; border-top: 1px solid #25272b; color: #9aa0a6;
       font-family: Consolas, "JetBrains Mono", monospace; font-size: 12px; }
QStatusBar { background: #16171a; color: #8b8f98; border-top: 1px solid #25272b; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #2e3136; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
"""

COLUMNS = ["", "#", "Name", "Username", "Phone", "Status", "Spam", "Geo", "Proxy", "Folder", "Roles"]
FIELDS = [None, None, "name", "username", "phone", "status", "spam", "geo", "proxy", "folder", "roles"]
STATUS_COLORS = {"active": "#22c55e", "dead": "#ef4444", "error": "#f59e0b", "unknown": "#6b7280",
                 "cooldown": "#a855f7"}
SORT_ROLE = Qt.UserRole

# Sidebar functions grouped by category: (id, label, Lucide icon name — see icons.py, handler(window)). Each id must be
# unique across all categories — it doubles as the favorites key persisted in QSettings.
CATEGORIES = [
    ("Accounts", [
        ("import_sessions", "Import sessions", "file-input", lambda w: w.import_sessions()),
        ("import_tdata", "Import tdata", "folder-input", lambda w: w.import_tdata()),
        ("login_phone", "Log in with number", "phone", lambda w: w.login_with_phone()),
        ("check_selected", "Check selected", "list-checks", lambda w: w.check(w.targets())),
        ("check_all", "Check all", "refresh-cw", lambda w: w.check(w.model.accounts)),
        ("export", "Export selected", "file-output", lambda w: w.export()),
        ("spam_check", "Spam checker", "shield-alert", lambda w: w.check_spam(w.targets())),
        ("backup", "Session backups", "archive", lambda w: w.backup_export()),
        ("restore", "Restore sessions", "archive-restore", lambda w: w.backup_restore()),
        ("dashboard", "Dashboard", "layout-dashboard", lambda w: w.show_dashboard()),
        ("account_stats", "Account statistics", "chart-column", lambda w: w.show_account_stats()),
        ("remote_bot", "Remote control (bot)", "bot", lambda w: w.toggle_bot()),
        ("account_actions", "Account actions", "sliders-horizontal", lambda w: w.context_menu()),
        ("chats", "Chats", "message-circle-more", lambda w: w.open_chats()),
    ]),
    ("Mailing", [
        ("broadcast", "Broadcast", "megaphone", lambda w: w.open_broadcast()),
        ("templates", "Templates", "file-text", lambda w: w.open_templates()),
        ("scheduler", "Scheduler", "calendar-clock", lambda w: w.open_scheduler()),
        ("auto_post", "Auto-posting", "send", lambda w: w.open_auto_post()),
    ]),
    ("Comments & reactions", [
        ("auto_watch", "Auto-comments & reactions", "message-square-heart", lambda w: w.open_watch()),
        ("comment_now", "Comment on latest posts", "message-square-plus", lambda w: w.open_comment_now()),
        ("moderator", "Channel moderator", "shield-check", lambda w: w.open_listener_dialog()),
    ]),
    ("Audience", [
        ("parser", "Parser", "scan-search", lambda w: w.open_parser()),
        ("word_monitor", "Word monitoring", "text-search", lambda w: w.open_listener_dialog()),
        ("dm_funnel", "DM funnel", "message-circle", lambda w: w.open_funnel("dm")),
        ("drip_funnel", "Drip funnel", "timer", lambda w: w.open_funnel("drip")),
        ("channel_search", "Channel search", "search", lambda w: w.search_channels()),
        ("number_checker", "Number checker", "book-user", lambda w: w.open_number_checker()),
        ("chat_dump", "Chat dumper", "file-down", lambda w: w.open_chat_dumper()),
    ]),
    ("Promotion", [
        ("inviter", "Inviter", "user-plus", lambda w: w.open_inviter()),
        ("subscription", "Subscription", "bell-plus", lambda w: w.open_join()),
        ("mass_join", "Mass joining", "log-in", lambda w: w.open_join()),
        ("join_requests", "Join requests", "user-check", lambda w: w.open_join_requests()),
        ("boost", "Boosting", "rocket", lambda w: w.open_boost()),
        ("story_views", "Mass story viewing", "circle-play", lambda w: w.open_story_views()),
    ]),
    ("Content", [
        ("forwarder", "Forwarder", "forward", lambda w: w.open_forwarder()),
        ("cloner", "Chat / channel cloner", "copy", lambda w: w.open_cloner()),
        ("chat_create", "Chat creator", "square-plus", lambda w: w.create_chat()),
        ("auto_reply", "Auto-responder (AI)", "sparkles", lambda w: w.open_listener_dialog()),
        ("link_first_dm", "Link on first DM", "link", lambda w: w.open_listener_dialog()),
        ("reporter", "Reporter", "flag", lambda w: w.open_reporter()),
    ]),
    ("Warm-up", [
        ("warmup", "Account warm-up", "flame", lambda w: w.open_warmup()),
        ("dialogues", "Dialogues", "messages-square", lambda w: w.open_dialogues()),
        ("online_keeper", "Online keeper", "activity", lambda w: w.open_online_keeper()),
    ]),
    ("Converters & security", [
        ("twofa", "2FA manager", "key-round", lambda w: w.open_password_dialog()),
        ("sessions_access", "Sessions & access", "monitor-smartphone", lambda w: w.open_sessions_dialog()),
        ("stars", "Stars & gifts", "star", lambda w: w.show_stars()),
        ("randomizer", "Randomizer", "shuffle", lambda w: w.open_randomizer()),
    ]),
    ("Maintenance", [
        ("profiles", "Profiles", "user-pen", lambda w: w.open_profile_dialog()),
        ("proxy_manager", "Proxies", "network", lambda w: w.show_proxies()),
        ("proxy_check", "Check proxy", "radar", lambda w: w.check_proxies(w.targets())),
        ("sort_status", "Sort by status", "arrow-down-wide-narrow", lambda w: w.sort_by_status(w.targets())),
        ("chat_cleanup", "Chat cleanup", "eraser", lambda w: w.open_chat_cleanup()),
    ]),
]
FUNCS = {item_id: (label, icon, handler) for _, items in CATEGORIES for item_id, label, icon, handler in items}

# The accounts table's right-click menu: (label, Lucide icon, handler(window)); None = separator.
# Single-account actions first, then checks, organising, cooldown, and the destructive one last.
ACCOUNT_MENU = [
    ("Open chats…", "message-circle-more", lambda w: w.open_chats()),
    ("Profile…", "user-pen", lambda w: w.open_profile_dialog()),
    ("2FA manager…", "key-round", lambda w: w.open_password_dialog()),
    ("Sessions & access…", "monitor-smartphone", lambda w: w.open_sessions_dialog()),
    ("Account statistics…", "chart-column", lambda w: w.show_account_stats()),
    ("Listener (monitor / auto-reply / moderate)…", "text-search", lambda w: w.open_listener_dialog()),
    None,
    ("Check", "refresh-cw", lambda w: w.check(w.targets())),
    ("Check for spam (@SpamBot)", "shield-alert", lambda w: w.check_spam(w.targets())),
    ("Check proxy", "radar", lambda w: w.check_proxies(w.targets())),
    None,
    ("Set proxy…", "network", lambda w: w.edit_field("proxy")),
    ("Set folder…", "folder-open", lambda w: w.edit_field("folder")),
    ("Set roles…", "tag", lambda w: w.edit_field("roles")),
    ("Sort into folder by status", "arrow-down-wide-narrow", lambda w: w.sort_by_status(w.targets())),
    None,
    ("Park (cooldown)", "snowflake", lambda w: w.set_cooldown(w.targets(), True)),
    ("Revive from cooldown", "play", lambda w: w.set_cooldown(w.targets(), False)),
    None,
    ("Proxies…", "globe", lambda w: w.show_proxies()),
    ("Export…", "file-output", lambda w: w.export()),
    None,
    ("Move to trash", "trash", lambda w: w.trash(w.targets())),
]


def no_proxy_text(direct: list[Account], total: int, shown: int = 5) -> str:
    """The own-IP warning's headline: which accounts would connect from the user's IP (first `shown` by name)."""
    names = [a.name or a.session for a in direct]
    if total == 1:
        return f"{names[0]} has no proxy."
    more = f" +{len(names) - shown} more" if len(names) > shown else ""
    return f"{len(names)} of {total} accounts have no proxy: {', '.join(names[:shown])}{more}."


HEADING_COLOR = "#b9bdc5"  # sidebar category headings; ~9:1 on the sidebar background


def chevron(expanded: bool) -> QIcon:
    """Expander arrow for sidebar headings: ▸ collapsed, ▾ expanded. Drawn in code (2x for HiDPI screens), so
    packaged builds need no image files. The stylesheet's ::branch rule hides Qt's own arrows."""
    pixmap = QPixmap(32, 32)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(HEADING_COLOR), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    points = [(4.5, 6.5), (8, 10), (11.5, 6.5)] if expanded else [(6.5, 4.5), (10, 8), (6.5, 11.5)]
    painter.drawPolyline([QPointF(x, y) for x, y in points])
    painter.end()
    return QIcon(pixmap)


def dot(color: str) -> QIcon:
    pixmap = QPixmap(12, 12)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(3, 3, 6, 6)
    painter.end()
    return QIcon(pixmap)


class AccountModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.accounts: list[Account] = []
        self.checked: set[str] = set()  # session names ticked in the checkbox column
        self.dots = {status: dot(color) for status, color in STATUS_COLORS.items()}

    def set_accounts(self, accounts: list[Account]):
        self.beginResetModel()
        self.accounts = accounts
        self.checked &= {a.session for a in accounts}
        self.endResetModel()

    def set_checked(self, sessions: set[str], on: bool):
        self.checked = self.checked | sessions if on else self.checked - sessions
        self.dataChanged.emit(self.index(0, 0), self.index(len(self.accounts) - 1, 0), [Qt.CheckStateRole])

    def account_changed(self, account: Account):
        for row, a in enumerate(self.accounts):
            if a is account:
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.accounts)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def flags(self, index):
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        return flags | Qt.ItemIsUserCheckable if index.column() == 0 else flags

    def data(self, index, role=Qt.DisplayRole):
        account, col = self.accounts[index.row()], index.column()
        if col == 0:
            if role == Qt.CheckStateRole:
                return Qt.Checked if account.session in self.checked else Qt.Unchecked
            return None
        value = index.row() + 1 if col == 1 else getattr(account, FIELDS[col])
        if role == SORT_ROLE:
            return value
        if role == Qt.DisplayRole:
            return account.status.capitalize() if col == 5 else (str(value) if value else "—")
        if role == Qt.DecorationRole and col == 5:
            return self.dots.get(account.status)
        if role == Qt.ForegroundRole and col in (1, 3, 6, 8, 9, 10):
            return QColor("#8b8f98")
        if role == Qt.TextAlignmentRole and col != 2:
            return Qt.AlignCenter
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if index.column() != 0 or role != Qt.CheckStateRole:
            return False
        self.set_checked({self.accounts[index.row()].session}, Qt.CheckState(value) == Qt.Checked)
        return True


class AccountFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.filters: dict[str, object] = {}  # key -> wanted value, None = any

    def set(self, text: str, filters: dict):
        self.beginFilterChange()
        self.text, self.filters = text.lower(), filters
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row, parent):
        a = self.sourceModel().accounts[row]
        values = {"status": a.status, "spam": a.spam, "geo": a.geo, "has_proxy": bool(a.proxy), "proxy": a.proxy}
        return (self.text in f"{a.name} {a.username} {a.phone} {a.session}".lower()
                and all(want is None or values[key] == want for key, want in self.filters.items()))


class MainWindow(QMainWindow):
    _call = Signal(object)  # emitted from the Telethon thread, delivered on the GUI thread

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Omnigram {__version__}")
        self.resize(1200, 760)
        self._call.connect(self._invoke)
        self.store = Store(Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)))
        self.model = AccountModel()
        self.filter = AccountFilter()
        self.filter.setSourceModel(self.model)
        self.filter.setSortRole(SORT_ROLE)
        self.model.dataChanged.connect(lambda *_: self.refresh_count())  # keep the ticked/shown counts live
        self.pending: set[str] = set()  # sessions being checked; a session file can't be opened twice
        self.listeners: dict[str, Future] = {}  # session -> running telegram.listen; holds the file too
        self.tasks: dict[str, Future] = {}  # "kind/session" -> running long job (broadcast, watcher, warm-up)
        self.bot: Future | None = None  # running telegram.status_bot
        self.chat_windows: dict[str, ChatWindow] = {}  # session -> its open chat window (one per account)
        self.work = Counter()  # "verb ok|failed" -> count, for the dashboard
        self._save_soon = QTimer(self, singleShot=True, interval=300)  # coalesces bulk saves; see changed_soon
        self._save_soon.timeout.connect(self.changed)
        self._dashboard_soon = QTimer(self, singleShot=True, interval=300)  # a batch of tasks = one redraw
        self._dashboard_soon.timeout.connect(self.refresh_dashboard)

        self.stack = QStackedWidget()
        self.accounts_page = self.stack.addWidget(self._accounts_page())
        self.settings_page = self.stack.addWidget(self._settings_page())
        self.proxy_page = ProxyPage(self, AccountFilter())
        self.proxies_page = self.stack.addWidget(self.proxy_page)
        self.dashboard = QLabel(textFormat=Qt.RichText, alignment=Qt.AlignTop | Qt.AlignLeft)
        dashboard = QWidget(objectName="page")
        box = QVBoxLayout(dashboard)
        box.setContentsMargins(20, 16, 20, 8)
        box.addWidget(QLabel("Dashboard", objectName="pageTitle"))
        scroll = QScrollArea(widgetResizable=True, frameShape=QFrame.NoFrame)
        scroll.setWidget(self.dashboard)
        box.addWidget(scroll, 1)
        self.dashboard_page = self.stack.addWidget(dashboard)
        self.log_view = QPlainTextEdit(objectName="log", readOnly=True, maximumBlockCount=2000)
        self.log_view.setFixedHeight(110)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        main.addWidget(self.stack)
        main.addWidget(self.log_view)
        root = QHBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._sidebar())
        root.addLayout(main)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")
        self.reload()
        self.resume_warmups()

    # ---- layout ---------------------------------------------------------------------------------

    def _sidebar(self) -> QWidget:
        def nav(entries) -> QListWidget:
            widget = QListWidget(selectionMode=QAbstractItemView.NoSelection, iconSize=QSize(16, 16),
                                 horizontalScrollBarPolicy=Qt.ScrollBarAlwaysOff,
                                 verticalScrollBarPolicy=Qt.ScrollBarAlwaysOff)
            for text, icon, action in entries:
                item = QListWidgetItem(icons.get(icon), text)
                item.setData(Qt.UserRole, action)
                widget.addItem(item)
            widget.itemClicked.connect(lambda item: item.data(Qt.UserRole)())
            widget.setFixedHeight(sum(widget.sizeHintForRow(i) for i in range(widget.count())) + 4)
            return widget

        # "Accounts" (the page, not the category below) stays a plain pinned entry: it's where you land.
        top = nav([("Accounts", "users", lambda: self.stack.setCurrentIndex(self.accounts_page)),
                   ("Proxies", "network", self.show_proxies)])

        self.tree = QTreeWidget(headerHidden=True, indentation=14, iconSize=QSize(16, 16))
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree.itemClicked.connect(self.on_tree_item_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.tree_context_menu)

        # Headings carry a drawn chevron instead of Qt's branch arrows (no root decoration column). Categories
        # start collapsed — there are dozens of functions; the search box opens the ones that match.
        self.tree.setRootIsDecorated(False)
        self.chevrons = {True: chevron(True), False: chevron(False)}
        self.tree.itemExpanded.connect(lambda item: item.setIcon(0, self.chevrons[True]))
        self.tree.itemCollapsed.connect(lambda item: item.setIcon(0, self.chevrons[False]))
        self.favorites_item = QTreeWidgetItem(["FAVORITES"])
        self.favorites_item.setIcon(0, self.chevrons[False])
        self.tree.addTopLevelItem(self.favorites_item)
        for category, items in CATEGORIES:
            cat_item = QTreeWidgetItem([category.upper()])
            cat_item.setIcon(0, self.chevrons[False])
            self.tree.addTopLevelItem(cat_item)
            for item_id, label, icon, _ in items:
                leaf = QTreeWidgetItem(cat_item, [label])
                leaf.setIcon(0, icons.get(icon))
                leaf.setData(0, Qt.UserRole, item_id)
        self.refresh_favorites()

        bottom = nav([
            ("Work statistics", "layout-dashboard", self.show_dashboard),
            ("Settings", "settings", self.show_settings),
            ("About", "info", self.about),
        ])
        search = QLineEdit(placeholderText="Search functions…")
        search.addAction(icons.get("search"), QLineEdit.LeadingPosition)
        search.textChanged.connect(self.filter_tree)

        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(220)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(8, 14, 8, 8)
        layout.addWidget(QLabel("Omnigram", objectName="appTitle"))
        layout.addWidget(QLabel("Account manager", objectName="muted"))
        layout.addSpacing(10)
        layout.addWidget(search)
        layout.addWidget(top)
        layout.addWidget(self.tree, 1)
        layout.addWidget(bottom)
        return sidebar

    def _accounts_page(self) -> QWidget:
        self.cards = {}
        cards = QHBoxLayout()
        for label, color in [("total", "#e4e6eb"), ("active", "#22c55e"), ("dead", "#8b8f98"),
                             ("spamblock", "#8b8f98"), ("cooldown", "#a855f7"), ("no proxy", "#ef4444")]:
            card = QFrame(objectName="card")
            card.setFixedSize(74, 46)
            box = QVBoxLayout(card)
            box.setContentsMargins(4, 4, 4, 4)
            box.setSpacing(0)
            self.cards[label] = QLabel("0", objectName="cardValue", alignment=Qt.AlignCenter)
            self.cards[label].setStyleSheet(f"color: {color}")
            box.addWidget(self.cards[label])
            box.addWidget(QLabel(label, objectName="cardLabel", alignment=Qt.AlignCenter))
            cards.addWidget(card)

        self.select_all = QCheckBox("Select all")
        self.select_all.toggled.connect(self.select_visible)
        self.count_label = QLabel(objectName="muted")  # "N shown · M total · K ticked", kept live for big pools
        selectors = QHBoxLayout()
        selectors.addWidget(self.select_all)
        selectors.addSpacing(8)
        selectors.addWidget(self.count_label)
        selectors.addStretch()
        title = QVBoxLayout()
        title.addWidget(QLabel("Accounts", objectName="pageTitle"))
        title.addLayout(selectors)
        header = QHBoxLayout()
        header.addLayout(title)
        header.addStretch()
        header.addLayout(cards)

        self.search = QLineEdit(placeholderText="Search by name, username or phone")
        self.search.textChanged.connect(self.apply_filters)
        self.combos = {}
        for key, options in [
            ("status", [("All statuses", None), ("Active", "active"), ("Dead", "dead"), ("Error", "error"),
                        ("Cooldown", "cooldown"), ("Unchecked", "unknown")]),
            ("spam", [("Spam: all", None), ("Clean", "clean"), ("Limited", "limited"), ("Unchecked", "")]),
            ("geo", [("Geo: all", None)]),  # filled from the accounts in refresh_geo
            ("has_proxy", [("Proxy: all", None), ("With proxy", True), ("Without proxy", False)]),
        ]:
            combo = self.combos[key] = QComboBox(minimumWidth=130)
            for text, value in options:
                combo.addItem(text, value)
            combo.currentIndexChanged.connect(self.apply_filters)
        reset = QPushButton("Reset")
        reset.clicked.connect(self.reset_filters)
        filters = QHBoxLayout()
        filters.addWidget(self.search, 1)
        for combo in self.combos.values():
            filters.addWidget(combo)
        filters.addWidget(reset)

        actions = QHBoxLayout()
        for text, icon, name, handler in [
            ("Check all", "refresh-cw", "primary", lambda: self.check(self.model.accounts)),
            ("Export", "file-output", "", self.export),
            ("Delete dead", "trash", "danger",
             lambda: self.trash([a for a in self.model.accounts if a.status == "dead"])),
        ]:
            button = QPushButton(icons.get(icon), text, objectName=name)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch()

        self.table = QTableView(sortingEnabled=True, showGrid=False, wordWrap=False)
        self.table.setModel(self.filter)
        self.table.sortByColumn(1, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        head = self.table.horizontalHeader()
        head.setDefaultSectionSize(84)
        head.setSectionResizeMode(2, QHeaderView.Stretch)
        for col, width in [(0, 36), (1, 44), (4, 110), (7, 50), (8, 120)]:
            head.resizeSection(col, width)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.doubleClicked.connect(
            lambda index: index.column() and self.open_chats(self.model.accounts[self.filter.mapToSource(index).row()]))

        page = QWidget(objectName="page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 8)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addLayout(filters)
        layout.addLayout(actions)
        layout.addWidget(self.table, 1)
        return page

    def _settings_page(self) -> QWidget:
        settings = QSettings()
        api_id = QLineEdit(str(settings.value("api_id", "")), validator=QIntValidator(1, 2**31 - 1))
        api_hash = QLineEdit(settings.value("api_hash", ""), echoMode=QLineEdit.Password)
        bot_token = QLineEdit(settings.value("bot_token", ""), echoMode=QLineEdit.Password,
                              placeholderText="from @BotFather; optional")
        bot_owner = QLineEdit(str(settings.value("bot_owner", "")), placeholderText="numeric id; the only user it answers")
        self.bot_proxy = QComboBox()  # filled from the pool each time the page is shown, see show_settings
        save = QPushButton("Save", objectName="primary")

        def store():
            settings.setValue("api_id", api_id.text())
            settings.setValue("api_hash", api_hash.text().strip())
            settings.setValue("bot_token", bot_token.text().strip())
            settings.setValue("bot_owner", bot_owner.text().strip())
            settings.setValue("bot_proxy", self.bot_proxy.currentData() or "")
            self.statusBar().showMessage("Settings saved", 3000)

        save.clicked.connect(store)
        open_folder = QPushButton(icons.get("folder-open"), "Open data folder")
        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.sessions.parent))))

        form = QFormLayout()
        form.addRow("api_id", api_id)
        form.addRow("api_hash", api_hash)
        form.addRow(QLabel("Get both at my.telegram.org → API development tools. Optional once you import a\n"
                           "session with its JSON: accounts use their own, others fall back to these, then to those.",
                           objectName="muted"))
        form.addRow("Status bot token", bot_token)
        form.addRow("Your user id", bot_owner)
        form.addRow("Status bot proxy", self.bot_proxy)
        form.addRow(QLabel("Remote control bot: answers /stats and /check, only to that user id.\n"
                           "Account statistics shows the id of any imported account. Without a proxy the bot\n"
                           "connects from your own IP, and starting it asks you to confirm that.",
                           objectName="muted"))
        form.addRow(save)
        form.addRow(QLabel(f"Data: {self.store.sessions.parent}", objectName="muted"))
        form.addRow(open_folder)
        page = QWidget(objectName="page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 8)
        layout.addWidget(QLabel("Settings", objectName="pageTitle"))
        layout.addLayout(form)
        layout.addStretch()
        page.setMaximumWidth(560)
        return page

    # ---- sidebar tree -----------------------------------------------------------------------------

    def favorites(self) -> list[str]:
        return QSettings().value("favorites", [])

    def toggle_favorite(self, item_id: str):
        favorites = self.favorites()
        favorites = [f for f in favorites if f != item_id] if item_id in favorites else favorites + [item_id]
        QSettings().setValue("favorites", favorites)
        self.refresh_favorites()

    def refresh_favorites(self):
        favorites = self.favorites()
        while self.favorites_item.childCount():
            self.favorites_item.removeChild(self.favorites_item.child(0))
        if not favorites:
            placeholder = QTreeWidgetItem(["Right-click to pin functions"])
            placeholder.setToolTip(0, "Empty. Right-click any function below → Add to favorites")
            placeholder.setDisabled(True)
            self.favorites_item.addChild(placeholder)
        for item_id in favorites:
            label, icon, _ = FUNCS[item_id]
            leaf = QTreeWidgetItem(self.favorites_item, [label])
            leaf.setIcon(0, icons.get(icon))
            leaf.setData(0, Qt.UserRole, item_id)
        self.favorites_item.setExpanded(True)  # setExpanded before it had children doesn't stick

    def on_tree_item_clicked(self, item: QTreeWidgetItem, _column: int):
        item_id = item.data(0, Qt.UserRole)
        if item_id is None:  # a category (or favorites) heading: just toggle it open/closed
            item.setExpanded(not item.isExpanded())
        else:
            FUNCS[item_id][2](self)

    def tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        item_id = item.data(0, Qt.UserRole) if item else None
        if item_id is None:
            return
        menu = QMenu(self)
        verb = "Remove from" if item_id in self.favorites() else "Add to"
        menu.addAction(f"{verb} favorites", lambda: self.toggle_favorite(item_id))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def filter_tree(self, text: str):
        text = text.lower()
        for i in range(self.tree.topLevelItemCount()):
            category = self.tree.topLevelItem(i)
            if category is self.favorites_item:
                continue
            visible = 0
            for j in range(category.childCount()):
                leaf = category.child(j)
                match = not text or text in leaf.text(0).lower()
                leaf.setHidden(not match)
                visible += match
            category.setHidden(bool(text) and not visible)
            if text:
                category.setExpanded(True)

    # ---- plumbing -------------------------------------------------------------------------------

    @Slot(object)
    def _invoke(self, fn):
        fn()

    def run(self, coro, on_done) -> Future:
        """Schedule coro on the Telethon loop; on_done(future) runs back on the GUI thread."""
        future = asyncio.run_coroutine_threadsafe(coro, telegram.LOOP)
        future.add_done_callback(lambda f: self._call.emit(lambda: on_done(f)))
        return future

    def call(self, account: Account, coro_fn, *args):
        """coro_fn(session_path, api_id, api_hash, proxy, *args); credentials() must already have passed."""
        return coro_fn(self.store.path(account), *self.credentials(account), account.proxy, *args)

    def log_result(self, account: Account, future: Future, describe):
        name = account.name or account.session
        try:
            self.log(f"✓ [{name}] {describe(future.result())}")
        except Exception as e:
            self.log(f"✗ [{name}] {type(e).__name__}: {e}")

    def log(self, message: str):
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def emitter(self, account: Account, symbol: str = "◉"):
        """A line-emitter for long jobs: emit(str) is called on the Telethon thread; the line reaches
        the log on the GUI thread. Every new long-running feature should use this instead of its own."""
        name = account.name or account.session
        return lambda line: self._call.emit(lambda: self.log(f"{symbol} [{name}] {line}"))

    def reload(self):
        """Re-read the sessions folder, keeping the live Account objects so in-flight checks still land."""
        current = {a.session: a for a in self.model.accounts}
        self.model.set_accounts([current.get(a.session, a) for a in self.store.load()])
        self.changed()

    def changed_soon(self):
        """Coalesce save+recompute during bulk work: N per-account results cost one save, not N.
        A batch always ends with a direct changed() (see on_account_result), so nothing is lost."""
        self._save_soon.start()  # QTimer, singleShot; restarting just pushes the deadline out

    def changed(self):
        self._save_soon.stop()
        accounts = self.model.accounts
        self.store.save(accounts)
        for label, count in [
            ("total", len(accounts)),
            ("active", sum(a.status == "active" for a in accounts)),
            ("dead", sum(a.status == "dead" for a in accounts)),
            ("spamblock", sum(a.spam == "limited" for a in accounts)),
            ("cooldown", sum(a.status == "cooldown" for a in accounts)),
            ("no proxy", sum(not a.proxy for a in accounts)),
        ]:
            self.cards[label].setText(str(count))
        geo = self.combos["geo"]
        wanted = geo.currentData()
        geo.blockSignals(True)
        while geo.count() > 1:
            geo.removeItem(1)
        for region in sorted({a.geo for a in accounts} - {""}):
            geo.addItem(region, region)
        geo.setCurrentIndex(max(0, geo.findData(wanted)) if wanted else 0)
        geo.blockSignals(False)
        self.apply_filters()
        self.refresh_dashboard()

    def summary(self) -> list[tuple[str, list[tuple[str, object]]]]:
        accounts = self.model.accounts
        breakdown = lambda values: Counter(values).most_common()
        return [
            ("Status", breakdown(a.status for a in accounts)),
            ("Spam", breakdown(a.spam or "unchecked" for a in accounts)),
            ("Geo", breakdown(a.geo or "unknown" for a in accounts)),
            ("Folder", breakdown(a.folder or "none" for a in accounts)),
            ("Proxy", breakdown("with proxy" if a.proxy else "without proxy" for a in accounts)),
            ("Work this session", sorted(self.work.items()) + [("listeners running", len(self.listeners)),
                                                               ("long jobs running", len(self.tasks)),
                                                               ("status bot", "on" if self.bot else "off")]),
        ]

    def summary_text(self) -> str:
        return f"{len(self.model.accounts)} account(s)\n\n" + "\n\n".join(
            title + "\n" + "\n".join(f"  {label}: {value}" for label, value in rows) for title, rows in self.summary())

    def refresh_dashboard(self):
        sections = "".join(
            f"<h3>{title}</h3><table cellpadding=3>" + "".join(
                f"<tr><td width=180>{html.escape(str(label))}</td><td><b>{value}</b></td></tr>" for label, value in rows)
            + "</table>" for title, rows in self.summary())
        self.dashboard.setText(f"<p>{len(self.model.accounts)} account(s)</p>{sections}")

    def show_dashboard(self):
        self.refresh_dashboard()
        self.stack.setCurrentIndex(self.dashboard_page)

    def apply_filters(self):
        self.filter.set(self.search.text(), {key: combo.currentData() for key, combo in self.combos.items()})
        self.refresh_count()

    def refresh_count(self):
        shown, total, ticked = self.filter.rowCount(), len(self.model.accounts), len(self.model.checked)
        parts = [f"{shown} shown" if shown != total else f"{total} total"]
        if shown != total:
            parts.append(f"{total} total")
        if ticked:
            parts.append(f"{ticked} ticked")
        self.count_label.setText("  ·  ".join(parts))

    def reset_filters(self):
        self.search.clear()
        for combo in self.combos.values():
            combo.setCurrentIndex(0)

    def visible_accounts(self) -> list[Account]:
        return [self.model.accounts[self.filter.mapToSource(self.filter.index(row, 0)).row()]
                for row in range(self.filter.rowCount())]

    def select_visible(self, on: bool):
        self.model.set_checked({a.session for a in self.visible_accounts()}, on)

    def targets(self) -> list[Account]:
        """Ticked accounts, or the highlighted rows when nothing is ticked."""
        ticked = [a for a in self.model.accounts if a.session in self.model.checked]
        if ticked:
            return ticked
        rows = {self.filter.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
        return [self.model.accounts[row] for row in sorted(rows)]

    def context_menu(self, pos=None):
        """Right-click on the table; with no pos (sidebar "Account actions") it opens at the cursor."""
        menu = QMenu(self)
        for entry in ACCOUNT_MENU:
            if entry is None:
                menu.addSeparator()
                continue
            label, icon, handler = entry
            # QMenu reads "&" as a keyboard-shortcut marker ("Sessions & access" showed as "Sessions _access")
            menu.addAction(icons.get(icon), label.replace("&", "&&"), lambda h=handler: h(self))
        menu.exec(self.table.viewport().mapToGlobal(pos) if pos else QCursor.pos())

    @property
    def template_store(self) -> TemplateStore:
        """Named templates (templates.json) shared by mailing dialogs and the first-DM link."""
        return TemplateStore(self.store.templates)

    def busy(self, account: Account, own: str = "", allow_listening: bool = False) -> bool:
        """The session file is held: being checked, listening, or running a long job. `own` is the job kind
        whose dialog is opening (it must still open, to show Stop); `allow_listening` likewise for listeners."""
        s = account.session
        return (s in self.pending or (s in self.listeners and not allow_listening)
                or any(key.endswith(f"/{s}") and key != f"{own}/{s}" for key in self.tasks))

    def one_target(self, allow_listening: bool = False, own: str = "", account: Account | None = None) -> Account | None:
        """Exactly one ticked/selected account (or `account`, e.g. a double-clicked row), with credentials set,
        its session file free, and a proxy (or the user's explicit OK to connect from their own IP).
        `own`: see busy()."""
        accounts = [account] if account else self.targets()
        if len(accounts) != 1:
            self.statusBar().showMessage("Tick or select exactly one account for this", 3000)
            return None
        account = accounts[0]
        if self.busy(account, own, allow_listening):
            self.statusBar().showMessage("That account is busy (being checked, running a job, or listening)", 3000)
            return None
        if not self.credentials(account):
            return None
        if account.session in self.listeners:  # already connected; the dialog only manages it
            return account
        return account if self.allow_connect([account]) else None

    def warn_direct(self, text: str, skip_label: str = "") -> str | None:
        """The own-IP warning. Returns 'connect', 'skip' (only offered when skip_label is set) or None = cancel.
        Modal. Cancel is the default button, so Enter/Esc never exposes the user's IP, and "Connect" stays
        disabled until the user ticks that they understand their IP will be exposed."""
        box = QMessageBox(QMessageBox.Warning, "No proxy — your own IP", text, parent=self)
        box.setWindowModality(Qt.ApplicationModal)
        box.setInformativeText("Connecting without a proxy shows Telegram your real IP address. "
                               "Assign a proxy first (sidebar → Proxies) to avoid that.")
        understood = QCheckBox("I understand this exposes my real IP address to Telegram", box)  # parent: box owns it
        box.setCheckBox(understood)
        connect = box.addButton("Connect from my IP", QMessageBox.DestructiveRole)
        connect.setObjectName("danger")
        connect.setEnabled(False)
        understood.toggled.connect(connect.setEnabled)
        skip = box.addButton(skip_label, QMessageBox.AcceptRole) if skip_label else None
        cancel = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        return "connect" if clicked is connect else "skip" if skip is not None and clicked is skip else None

    def allow_connect(self, accounts: list[Account], ask: bool = True) -> list[Account] | None:
        """Every path that connects an account goes through here. Accounts with a proxy pass; proxy-less ones
        need the user's OK. ask=False (nobody at the screen, e.g. the status bot) leaves them out.
        Returns the accounts that may connect, or None if the user cancelled."""
        direct = [a for a in accounts if not a.proxy]
        if not direct:
            return accounts
        proxied = [a for a in accounts if a.proxy]
        if not ask:
            return proxied
        answer = self.warn_direct(no_proxy_text(direct, len(accounts)), f"Skip those {len(direct)}" if proxied else "")
        return accounts if answer == "connect" else proxied if answer == "skip" else None

    def open_password_dialog(self):
        if account := self.one_target():
            PasswordDialog(self, account).exec()

    def open_sessions_dialog(self):
        if account := self.one_target():
            SessionsDialog(self, account).exec()

    def open_profile_dialog(self):
        if account := self.one_target():
            ProfileDialog(self, account).exec()

    def show_account_stats(self):
        if account := self.one_target():
            InfoDialog(self, f"Statistics — {account.name or account.session}",
                       self.call(account, telegram.account_stats),
                       lambda stats: "\n".join(f"{key}: {value}" for key, value in stats.items()),
                       status="Loading account statistics…").exec()

    def show_stars(self):
        if account := self.one_target():
            InfoDialog(self, f"Stars & gifts — {account.name or account.session}",
                       self.call(account, telegram.stars_and_gifts),
                       lambda r: f"Stars: {r['stars']}\n\nGifts on profile ({len(r['gifts'])}):\n"
                                 + ("\n".join(r["gifts"]) or "none"),
                       status="Loading stars and gifts…").exec()

    def search_channels(self):
        if not (account := self.one_target()):
            return
        query, ok = QInputDialog.getText(self, "Channel search", "Search public groups and channels:")
        if ok and query.strip():
            InfoDialog(self, f"Search: {query.strip()}", self.call(account, telegram.search_public, query.strip()),
                       lambda lines: "\n".join(lines) or "No results.",
                       status=f"Searching for “{query.strip()}”…").exec()

    def confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def open_chat_cleanup(self):
        if not (a := self.one_target()):
            return

        def leave(ids):
            if self.confirm("Chat cleanup", f"Leave {len(ids)} chat(s)? Groups and channels are left; "
                                            "private chats are deleted for this account only."):
                return self.call(a, telegram.leave, ids)

        ChatsDialog(self, a, "Chat cleanup", lambda: self.call(a, telegram.dialogs),
                    [("Leave / delete ticked", leave, lambda n: f"left or deleted {n} chat(s)")]).exec()

    def open_join_requests(self):
        if not (a := self.one_target()):
            return

        def decline(ids):
            if self.confirm("Join requests", f"Decline every pending request in {len(ids)} chat(s)?"):
                return self.call(a, telegram.resolve_join_requests, ids, False)

        ChatsDialog(self, a, "Join requests", lambda: self.call(a, telegram.join_requests), [
            ("Approve all pending", lambda ids: self.call(a, telegram.resolve_join_requests, ids, True),
             lambda n: f"approved pending join requests in {n} chat(s)"),
            ("Decline all pending", decline, lambda n: f"declined pending join requests in {n} chat(s)"),
        ]).exec()

    def open_chat_dumper(self):
        if not (a := self.one_target()):
            return

        def dump(ids):
            folder = QFileDialog.getExistingDirectory(self, "Save chat dumps (JSON) to")
            return self.call(a, telegram.dump, ids, folder) if folder else None

        ChatsDialog(self, a, "Chat dumper", lambda: self.call(a, telegram.dialogs),
                    [("Export ticked to JSON…", dump, lambda n: f"dumped {n} message(s)")]).exec()

    def create_chat(self):
        if not (account := self.one_target()):
            return
        title, ok = QInputDialog.getText(self, "Chat creator", "Title:")
        if not ok or not title.strip():
            return
        kind, ok = QInputDialog.getItem(self, "Chat creator", "Type:", ["Group", "Channel"], 0, False)
        if not ok:
            return
        about, ok = QInputDialog.getText(self, "Chat creator", "Description (optional):")
        if not ok:
            return
        self.run(self.call(account, telegram.create_chat, title.strip(), about.strip(), kind == "Channel"),
                 lambda f: self.log_result(account, f, lambda t: f"created {kind.lower()} '{t}'"))

    def open_scheduler(self):
        if account := self.one_target():
            ScheduleDialog(self, account).exec()

    # ---- mailing, engagement, audience, promotion, content, warm-up ------------------------------

    def open_templates(self):
        TemplatesDialog(self).exec()

    def open_broadcast(self):
        if account := self.one_target(own="broadcast"):
            BroadcastDialog(self, account).exec()

    def open_auto_post(self):
        if account := self.one_target():
            AutoPostDialog(self, account).exec()

    def open_watch(self):
        if account := self.one_target(own="watch"):
            WatchDialog(self, account).exec()

    def open_comment_now(self):
        if account := self.one_target():
            CommentNowDialog(self, account).exec()

    def open_parser(self):
        if account := self.one_target():
            ParserDialog(self, account).exec()

    def open_funnel(self, kind: str):
        if account := self.one_target(own="funnel"):
            FunnelDialog(self, account, kind).exec()

    def open_number_checker(self):
        if account := self.one_target():
            NumberCheckerDialog(self, account).exec()

    def open_join(self):
        accounts = self.targets()
        if not accounts:
            self.statusBar().showMessage("Tick or select the accounts that should join", 3000)
            return
        JoinDialog(self, accounts).exec()

    def open_inviter(self):
        if account := self.one_target(own="invite"):
            InviterDialog(self, account).exec()

    def open_boost(self):
        if account := self.one_target():
            BoostDialog(self, account).exec()

    def open_story_views(self):
        if account := self.one_target(own="stories"):
            StoryViewDialog(self, account).exec()

    def open_forwarder(self):
        if account := self.one_target(own="forward"):
            ForwarderDialog(self, account).exec()

    def open_cloner(self):
        if account := self.one_target(own="clone"):
            ClonerDialog(self, account).exec()

    def open_reporter(self):
        if account := self.one_target():
            ReporterDialog(self, account).exec()

    def open_warmup(self):
        accounts = self.targets()
        if not accounts:
            self.statusBar().showMessage("Tick or select the accounts to warm up", 3000)
            return
        WarmupDialog(self, accounts).exec()

    def open_dialogues(self):
        if account := self.one_target(own="dialogues"):
            DialoguesDialog(self, account).exec()

    def open_online_keeper(self):
        if account := self.one_target(own="online"):
            OnlineKeeperDialog(self, account).exec()

    def open_randomizer(self):
        accounts = self.targets()
        if not accounts:
            self.statusBar().showMessage("Tick or select the accounts to randomize", 3000)
            return
        RandomizerDialog(self, accounts).exec()

    # ---- long-running: listeners and the status bot ----------------------------------------------

    def open_listener_dialog(self):
        if account := self.one_target(allow_listening=True):
            ListenerDialog(self, account).exec()

    def start_listener(self, account: Account, keywords: list[str], away: str, banned: list[str],
                       first_dm: str = "", ai: dict | None = None):
        future = self.run(self.call(account, telegram.listen, keywords, away, banned, self.emitter(account),
                                    first_dm, ai),
                          lambda f: self.on_listener_done(account, f))
        self.listeners[account.session] = future
        self.refresh_dashboard()

    def stop_listener(self, account: Account):
        if future := self.listeners.pop(account.session, None):
            future.cancel()
        self.refresh_dashboard()

    def on_listener_done(self, account: Account, future: Future):
        if self.listeners.get(account.session) is future:  # not already popped by stop_listener
            del self.listeners[account.session]
        if future.cancelled():
            self.log(f"◉ [{account.name or account.session}] listener stopped")
        else:
            self.log_result(account, future, lambda _: "listener disconnected")
        self.refresh_dashboard()

    # ---- long jobs with no dedicated dialog state (broadcast, watcher, warm-up, keeper) ----------

    def start_task(self, key: str, coro, verb: str, on_done=None, quiet: bool = False) -> Future:
        """Run a cancellable long job under `key` (by convention 'kind/session'). It shows on the
        dashboard and marks the session busy, exactly like a listener; `stop_task(key)` cancels it.
        `on_done()` runs on the GUI thread however the job ends; `quiet` skips the "started" line (bulk
        starters log one line for the batch)."""
        future = self.run(coro, lambda f: self.on_task_done(key, verb, f, on_done))
        self.tasks[key] = future
        if not quiet:
            self.log(f"→ {verb} started")
        self._dashboard_soon.start()
        return future

    def stop_task(self, key: str) -> bool:
        if future := self.tasks.get(key):
            future.cancel()
            return True
        return False

    def task_running(self, key: str) -> bool:
        return key in self.tasks

    def on_task_done(self, key: str, verb: str, future: Future, on_done=None):
        if on_done:
            on_done()
        if self.tasks.get(key) is not future:
            return  # stopped on purpose, stop_task already popped it
        del self.tasks[key]
        if future.cancelled():
            self.log(f"◉ {verb} stopped")
        else:
            try:
                result = future.result()
                self.log(f"✓ {verb} finished" + (f": {result}" if result else ""))
            except Exception as e:
                self.log(f"✗ {verb}: {type(e).__name__}: {e}")
        self._dashboard_soon.start()

    # ---- warm-up: runs per account, saved to disk so a multi-day run survives a restart ----------

    def proxy_zones(self) -> dict[str, str]:
        """Proxy URL -> exit-IP timezone, from the pool's geo probe. Build once per batch, not per account."""
        return {p.url: p.tz for p in self.store.load_proxies() if p.tz}

    def start_warmup(self, account: Account, actions: list, targets: list[str], done: int = 0):
        """Run a scheduled warm-up; its progress is saved after every action and the file removed when the
        run ends (finished, failed or stopped). Quitting the app leaves the file, so resume_warmups picks it up."""
        key, path = f"warmup/{account.session}", self.store.warmup / f"{account.session}.json"
        warmup.save(path, actions, targets, done)

        def progress(n):  # Telethon thread
            # A stopped run can still finish its current action; don't let that recreate the file.
            self._call.emit(lambda: self.tasks.get(key) is future and warmup.save(path, actions, targets, n))

        future = self.start_task(key, self.call(account, telegram.warmup_run, actions, targets,
                                                self.emitter(account), done, progress),
                                 f"warm-up [{account.name or account.session}]",
                                 on_done=lambda: path.unlink(missing_ok=True), quiet=True)

    def resume_warmups(self):
        """At launch: pick up every warm-up the last run was in the middle of. Nobody may be at the screen,
        so proxy-less accounts are skipped with a log line (the own-IP rule), never prompted."""
        accounts = {a.session: a for a in self.model.accounts}
        resumed = 0
        for path in sorted(self.store.warmup.glob("*.json")):
            account = accounts.get(path.stem)
            try:
                actions, targets, done = warmup.load(path)
            except (OSError, ValueError, KeyError, TypeError):
                account = None
            if account is None:  # session gone, or the file is unreadable
                path.unlink(missing_ok=True)
                continue
            name = account.name or account.session
            if not self.allow_connect([account], ask=False):
                self.log(f"◉ [{name}] warm-up not resumed: no proxy (assign one and restart, or start it again)")
                continue
            if self.credentials(account) is None:
                return
            self.start_warmup(account, actions, targets, done)
            resumed += 1
        if resumed:
            self.log(f"→ resumed {resumed} warm-up(s)")

    def show_settings(self):
        """Open the Settings page, refreshing the bot's proxy choices from the pool (it changes meanwhile)."""
        saved = QSettings().value("bot_proxy", "")
        pool = self.store.load_proxies()
        self.bot_proxy.clear()
        self.bot_proxy.addItem("No proxy — your own IP", "")
        for p in pool:
            self.bot_proxy.addItem(p.name or proxies.describe(p.url)[1], p.url)
        if saved and saved not in {p.url for p in pool}:
            self.bot_proxy.addItem(f"{proxies.describe(saved)[1]} (not in the pool)", saved)
        self.bot_proxy.setCurrentIndex(max(0, self.bot_proxy.findData(saved)))
        self.stack.setCurrentIndex(self.settings_page)

    def toggle_bot(self):
        if self.bot:
            self.bot.cancel()
            self.bot = None
            self.log("◉ status bot stopped")
            self.refresh_dashboard()
            return
        credentials = self.credentials()
        if credentials is None:
            return
        settings = QSettings()
        token, owner = settings.value("bot_token", ""), str(settings.value("bot_owner", ""))
        if not token or not owner.isdigit():
            self.show_settings()
            self.statusBar().showMessage("Set the status bot token and your user id first")
            return
        proxy = settings.value("bot_proxy", "")
        if not proxy and self.warn_direct("The status bot has no proxy (Settings → Status bot proxy).") != "connect":
            return
        self.bot = self.run(telegram.status_bot(*credentials, token, int(owner), self.bot_answer, proxy),
                            self.on_bot_done)
        self.log("◉ status bot started (answers /stats and /check from your user id only)")
        self.refresh_dashboard()

    def on_bot_done(self, future: Future):
        if future is not self.bot:  # stopped on purpose
            return
        self.bot = None
        try:
            future.result()
            self.log("◉ status bot disconnected")
        except Exception as e:
            self.log(f"✗ status bot: {type(e).__name__}: {e}")
        self.refresh_dashboard()

    def on_gui(self, fn):
        """Called on the Telethon thread: run fn() on the GUI thread and hand its result back as an awaitable."""
        result = Future()

        def compute():
            try:
                result.set_result(fn())
            except Exception as e:
                result.set_exception(e)

        self._call.emit(compute)
        return asyncio.wrap_future(result)

    def bot_answer(self, command: str):
        return self.on_gui(lambda: self.bot_command(command))

    def ask_user(self, prompt: str, secret: bool):
        """telegram.login's `ask`: a text prompt on the GUI thread; None if cancelled. Answers are never logged."""
        def ask():
            text, ok = QInputDialog.getText(self, "Log in with number", prompt,
                                            QLineEdit.Password if secret else QLineEdit.Normal)
            return text if ok else None
        return self.on_gui(ask)

    def login_with_phone(self):
        credentials = self.credentials()
        if credentials is None:
            return
        phone, ok = QInputDialog.getText(self, "Log in with number", "Phone number, international format (+…):")
        digits = "".join(c for c in phone if c.isdigit())
        if not ok or not digits:
            return
        path = self.store.sessions / f"{digits}.session"
        if path.exists() or digits in self.pending:
            QMessageBox.warning(self, "Log in with number", f"There is already a session for +{digits}.")
            return
        proxy = self.pick_login_proxy()
        if proxy is None:
            return
        self.pending.add(digits)  # keeps reload()/checks off the half-made session file
        self.log(f"→ logging in +{digits}" + (" via proxy…" if proxy else " from your own IP…"))
        self.run(telegram.login(path, *credentials, proxy, f"+{digits}", self.ask_user),
                 lambda f: self.on_login(path, credentials, proxy, f))

    def pick_login_proxy(self) -> str | None:
        """A pool proxy URL, '' after the user accepted the own-IP warning, or None = cancel."""
        pool = self.store.load_proxies()
        direct = "No proxy — connect from my own IP"
        labels = [f"{i}. {p.name or proxies.describe(p.url)[1]}  ({proxies.describe(p.url)[0]}"
                  + (", last ping failed)" if p.ping is not None and p.ping < 0 else ")")
                  for i, p in enumerate(pool, 1)]  # numbered, so same-named proxies stay distinct
        if pool:
            choice, ok = QInputDialog.getItem(self, "Log in with number", "Connect through:", labels + [direct], 0,
                                              False)
            if not ok:
                return None
            if choice != direct:
                return pool[labels.index(choice)].url
        return "" if self.warn_direct("This login has no proxy.") == "connect" else None

    def on_login(self, path: Path, credentials: tuple[int, str], proxy: str, future: Future):
        self.pending.discard(path.stem)
        try:
            fields = future.result()
        except Exception as e:
            for leftover in (path, path.with_name(path.name + "-journal")):
                leftover.unlink(missing_ok=True)
            self.log(f"✗ login +{path.stem}: {type(e).__name__}: {e}")
            self.reload()  # drop it from the table if a reload picked it up meanwhile
            return
        self.reload()
        for account in self.model.accounts:
            if account.session == path.stem:
                for key, value in fields.items():
                    setattr(account, key, value)
                account.api_id, account.api_hash = credentials  # the session belongs to this app from now on
                account.proxy = proxy  # keep using the address it logged in from
        self.changed()
        self.log(f"✓ logged in {fields['name'] or '+' + path.stem}")

    def bot_command(self, command: str) -> str:
        if command == "stats":
            return self.summary_text()
        if command == "check":
            # Nobody may be at the screen to answer the own-IP warning, so proxy-less accounts are skipped.
            self.check(self.model.accounts, ask=False)
            direct = sum(not a.proxy for a in self.model.accounts)
            skipped = f" Skipped {direct} without a proxy (check those from the app)." if direct else ""
            return f"Checking {len(self.model.accounts) - direct} account(s).{skipped} Send /stats in a minute."
        return "Omnigram status bot. Commands: /stats, /check"

    # ---- actions --------------------------------------------------------------------------------

    def import_sessions(self):
        """Pick .session and/or .json files; a same-named .json next to a session supplies its api_id/api_hash."""
        files, _ = QFileDialog.getOpenFileNames(self, "Import sessions", "", "Sessions and session JSON (*.session *.json)")
        extra: dict[str, dict] = {}  # session stem -> fields from its JSON
        for session in dict.fromkeys(Path(f).with_suffix(".session") for f in files):
            if not session.exists():  # sqlite3.connect would create it
                self.log(f"✗ {session.with_suffix('.json').name}: no {session.name} next to it")
                continue
            try:
                import_session(session, self.store.sessions)
                if session.with_suffix(".json").exists():
                    extra[session.stem] = read_session_json(session.with_suffix(".json"))
                note = " + JSON (api_id/api_hash)" if extra.get(session.stem, {}).get("api_hash") else ""
                self.log(f"✓ imported {session.name}{note}")
            except Exception as e:
                self.log(f"✗ {session.name}: {e}")
        self.reload()
        for account in self.model.accounts:
            for key, value in extra.get(account.session, {}).items():
                setattr(account, key, value)
        if extra:
            self.changed()

    def import_tdata(self):
        folder = QFileDialog.getExistingDirectory(self, "Select tdata folder")
        if not folder:
            return
        passcode = ""
        while True:
            try:
                imported = import_tdata(Path(folder), self.store.sessions, passcode)
                break
            except ValueError:
                passcode, ok = QInputDialog.getText(
                    self, "Local passcode", "tdata is passcode-protected (or the passcode was wrong):",
                    QLineEdit.Password)
                if not ok:
                    return
            except Exception as e:
                self.log(f"✗ tdata {folder}: {e}")
                return
        self.log(f"✓ tdata: imported {len(imported)} account(s)")
        self.reload()

    def credentials(self, account: Account | None = None) -> tuple[int, str] | None:
        """api_id/api_hash: the account's own (session JSON / number login), else Settings, else any imported
        account's. None after sending the user to Settings when there are none at all."""
        if account and account.api_id and account.api_hash:
            return account.api_id, account.api_hash
        settings = QSettings()
        if settings.value("api_id") and settings.value("api_hash"):
            return int(settings.value("api_id")), settings.value("api_hash")
        for a in self.model.accounts:
            if a.api_id and a.api_hash:
                return a.api_id, a.api_hash
        self.show_settings()
        self.statusBar().showMessage("Set api_id and api_hash, or import a session with its JSON")
        return None

    def check(self, accounts: list[Account], ask: bool = True):
        self.run_per_account(accounts, "checking", telegram.check, self.apply_check_result, ask)

    def apply_check_result(self, account: Account, result) -> str:
        if isinstance(result, telegram.NotAuthorized):
            account.status = "dead"
            return "not authorized"
        if isinstance(result, Exception):
            account.status = "error"
            return f"{type(result).__name__}: {result}"
        for key, value in result.items():
            setattr(account, key, value)
        return f"→ {account.status}"

    def check_spam(self, accounts: list[Account]):
        self.run_per_account(accounts, "spam-checking", telegram.check_spam, self.apply_spam_result)

    def apply_spam_result(self, account: Account, result) -> str:
        if isinstance(result, Exception):
            return f"{type(result).__name__}: {result}"
        account.spam = result["spam"]
        return f"{result['spam']} — {result.get('spam_detail', '')}"

    def run_per_account(self, accounts: list[Account], verb: str, coro_fn, apply_result, ask: bool = True):
        """coro_fn(session_path, api_id, api_hash, proxy) -> dict.
        apply_result(account, dict | Exception) -> a one-line log string; it mutates `account` as needed.
        ask=False: proxy-less accounts are skipped instead of prompting (see allow_connect)."""
        if self.credentials() is None:
            return
        free = [a for a in accounts if not self.busy(a)]
        todo = self.allow_connect(free, ask) if free else []
        if not todo:
            return
        if skipped := len(free) - len(todo):
            self.log(f"→ skipped {skipped} account(s) without a proxy")
        self.log(f"→ {verb} {len(todo)} account(s)…")
        for account in todo:
            self.pending.add(account.session)
            self.run(self.call(account, coro_fn),
                     lambda f, a=account: self.on_account_result(a, f, verb, apply_result))
        self.statusBar().showMessage(f"{verb.capitalize()} {len(self.pending)} account(s)…")

    def on_account_result(self, account: Account, future: Future, verb: str, apply_result):
        self.pending.discard(account.session)
        try:
            result = future.result()
        except Exception as e:
            result = e
        outcome = apply_result(account, result)
        failed = isinstance(result, Exception)
        self.work[f"{verb} {'failed' if failed else 'ok'}"] += 1
        symbol = "✗" if failed else "✓"
        self.log(f"{symbol} [{account.name or account.session}] {outcome}")
        self.model.account_changed(account)
        if self.pending:
            self.changed_soon()  # coalesce; the final result below flushes with a direct changed()
            self.statusBar().showMessage(f"{verb.capitalize()} {len(self.pending)} account(s)…")
        else:
            self.changed()
            self.log(f"✓ {verb} finished")
            self.statusBar().showMessage("Ready")

    def edit_field(self, field: str):
        accounts = self.targets()
        if not accounts:
            return
        hint = " (socks5|socks4|http://[user:pass@]host:port or host:port[:user:pass], empty = none)" \
            if field == "proxy" else ""
        value, ok = QInputDialog.getText(self, f"Set {field}", f"{field} for {len(accounts)} account(s){hint}:",
                                         text=getattr(accounts[0], field))
        if not ok:
            return
        value = value.strip()
        if field == "proxy" and value:
            try:
                value = proxies.normalize(value)
            except ValueError as e:
                QMessageBox.warning(self, "Invalid proxy", str(e))
                return
        for account in accounts:
            setattr(account, field, value)
            self.model.account_changed(account)
        self.changed()

    def export(self):
        accounts = self.targets()
        if not accounts:
            self.statusBar().showMessage("Tick or select accounts to export", 3000)
            return
        folder = QFileDialog.getExistingDirectory(self, f"Export {len(accounts)} session(s) to")
        if not folder:
            return
        for account in accounts:
            shutil.copy2(self.store.path(account), folder)
        self.log(f"✓ exported {len(accounts)} session(s) to {folder}")

    def trash(self, accounts: list[Account]):
        accounts = [a for a in accounts if not self.busy(a)]
        if not accounts:
            return
        if QMessageBox.question(self, "Move to trash",
                                f"Move {len(accounts)} session(s) to {self.store.trash}?") != QMessageBox.Yes:
            return
        for account in accounts:
            self.store.move_to_trash(account)
        self.log(f"✓ moved {len(accounts)} session(s) to trash")
        self.reload()

    def about(self):
        QMessageBox.about(self, "About Omnigram",
                          "Omnigram — Telegram account manager.\nImports Telethon/Pyrogram sessions and tdata.")

    def sort_by_status(self, accounts: list[Account]):
        """'Maintenance → Sort by status': files a session's Folder field the way one drag-sorts by hand."""
        if not accounts:
            self.statusBar().showMessage("Tick or select accounts first", 3000)
            return
        for account in accounts:
            account.folder = account.status
            self.model.account_changed(account)
        self.changed()
        self.log(f"✓ sorted {len(accounts)} account(s) into folders by status")

    def set_cooldown(self, accounts: list[Account], parked: bool):
        """Park accounts (status 'cooldown', counted by the card of the same name) or revive them."""
        if not accounts:
            self.statusBar().showMessage("Tick or select accounts first", 3000)
            return
        for account in accounts:
            account.status = "cooldown" if parked else "unknown"
            self.model.account_changed(account)
        self.changed()
        self.log(f"✓ parked {len(accounts)} account(s) for cooldown" if parked
                 else f"✓ revived {len(accounts)} account(s)")

    def check_proxies(self, accounts: list[Account]):
        targets = [a for a in accounts if a.proxy]
        if not targets:
            self.statusBar().showMessage("No account here has a proxy set", 3000)
            return
        self.log(f"→ testing {len(targets)} proxy/proxies…")
        for account in targets:
            self.run(proxies.ping(account.proxy), lambda f, a=account: self.on_proxy_result(a, f))

    def open_chats(self, account: Account | None = None):
        """The account's Telegram-style chat window (right-click → Open chats…, double-click, or sidebar → Chats).
        One per account: asking again brings the open one to the front."""
        chosen = [account] if account else self.targets()
        if len(chosen) == 1 and (open_window := self.chat_windows.get(chosen[0].session)):
            open_window.showNormal()
            open_window.raise_()
            open_window.activateWindow()
            return
        if target := self.one_target(own="chat", account=account):
            chat_window = self.chat_windows[target.session] = ChatWindow(self, target)
            chat_window.show()

    def closeEvent(self, event):
        for chat_window in list(self.chat_windows.values()):  # separate windows would keep the app running
            chat_window.close()
        super().closeEvent(event)

    def show_proxies(self):
        """The Proxies page. Ticks carry over from the Accounts page (same model); the pool is re-read."""
        self.proxy_page.reload()
        self.stack.setCurrentIndex(self.proxies_page)

    def on_proxy_result(self, account: Account, future: Future):
        try:
            ms = future.result()
            self.work["proxy test ok"] += 1
            self.log(f"✓ [{account.name or account.session}] proxy {account.proxy} reachable, {ms} ms")
        except Exception as e:
            self.work["proxy test failed"] += 1
            self.log(f"✗ [{account.name or account.session}] proxy {account.proxy}: {e}")
        self.refresh_dashboard()

    def backup_export(self):
        accounts = self.targets()
        if not accounts:
            self.statusBar().showMessage("Tick or select accounts to back up", 3000)
            return
        default = f"omnigram-backup-{datetime.now():%Y%m%d-%H%M}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Export backup", default, "Zip archive (*.zip)")
        if not path:
            return
        export_backup(self.store, accounts, Path(path))
        self.log(f"✓ backed up {len(accounts)} account(s) to {path}")

    def backup_restore(self):
        path, _ = QFileDialog.getOpenFileName(self, "Restore backup", "", "Zip archive (*.zip)")
        if not path:
            return
        try:
            imported, skipped = import_backup(self.store, Path(path))
        except Exception as e:
            QMessageBox.warning(self, "Restore failed", str(e))
            return
        self.log(f"✓ restored {len(imported)} account(s)" + (f", skipped {len(skipped)} already present" if skipped else ""))
        self.reload()
