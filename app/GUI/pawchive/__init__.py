import pathlib as p
import re
import typing as t
from datetime import datetime

from PySide6 import QtWidgets
from PySide6.QtWidgets import (
    QFrame, QGraphicsBlurEffect, QHBoxLayout, QLabel, QSpacerItem, QSizePolicy, QWidget,
)
from PySide6.QtCore import (
    Qt, QUrl, Signal, QObject, QRect, QAbstractTableModel, QModelIndex, QRunnable,
    QPropertyAnimation, QEasingCurve, QThreadPool, QTimer, qtTrId,
)
from ui.core.timer import safe_single_shot
from PySide6.QtGui import QGuiApplication, QDesktopServices, QPixmap, QColor
from qfluentwidgets import (
    LineEdit, PrimaryPushButton,
    VBoxLayout, FluentIcon as FIF,
    TransparentPushButton, TransparentToolButton, HyperlinkButton, PushButton, PrimaryToolButton, TransparentTogglePushButton,
    TableView, FlyoutViewBase, FlyoutAnimationType, TextEdit, ImageLabel,
    Flyout, CommandBarView, Action, InfoBar, InfoBarPosition, BodyLabel,
    IndeterminateProgressBar, ToolButton,
)
from qframelesswindow import FramelessWindow

from core.variables import ACGG_DOC
from deploy import curr_os
from core import temp_p
from core.config.qc import pawchive_cfg
from core.website.pawchive.db import AuthorQuery, PawchiveAuthor, PawchiveAuthorsDb
from core.website.pawchive import *
from ui.core.theme import theme_mgr
from ui.qfluent.components import CustomFlyout, CustomInfoBar
from ui.async_task import AsyncTaskManager, TaskConfig, TaskError, TaskInfoBarCenter
from app.GUI.pawchive.avatar import AvatarCache, close_client, fetch_avatar_bytes, is_jpeg, to_avatar_jpeg
from app.GUI.pawchive.env_probe import (
    EnvProber,
    KIND_ARIA2,
    KIND_REDIS,
    PROBE_TIMEOUT_MS,
    probe_aria2,
    probe_redis,
)
from app.GUI.pawchive.tabs import PawchivePageNav, PawchiveTabsBar
from app.GUI.pawchive.style import build_bottom_bar_stylesheet
from core.website.pawchive import DEFAULT_PAWCHIVE_SAVE_PATH, authors_db_path
from core.website.pawchive.db import (
    DB_REASON_EMPTY,
    DB_REASON_MISSING,
    DB_REASON_NO_ROWS,
    pawchive_db_valid,
)

#: 环境提示/遮罩用到的 i18n 键字面量清单——`_tr` 走变量调用，AST 扫描看不见，
#: 必须在这里声明，否则 `build.py --sync` 会把它们当无引用键删掉。
_PAWCHIVE_SORT_COLUMNS = ("name", "service", "updated", "favorited")
#: 作品覆盖层与贴底操作条之间留 8px，避免网格最后一行贴着底栏上沿。
_WORKS_BOTTOM_GAP = 8
#: 环境提示条贴在底栏上沿之上的间隙 / 左边距 / 多条堆叠间距（px）。
#: 用 InfoBarPosition.NONE 自行定位：BOTTOM_LEFT manager 的 slide 动画会把
#: 提示条拉回父窗口底边、压住底栏按钮（见 _place_env_notices）。
_ENV_NOTICE_BOTTOM_GAP = 8
_ENV_NOTICE_MARGIN = 24
_ENV_NOTICE_SPACING = 16
#: 列表请求失败自动重试：用户实测点到第 3~4 次才成功，说明链路抖动是常态。
_PAWCHIVE_PAGE_RETRY_LIMIT = 5
_PAWCHIVE_PAGE_RETRY_BASE_MS = 700
#: 环境提示/遮罩用到的 i18n 键字面量清单（见上）。
_PAWCHIVE_ENV_I18N_KEYS = (
    "pawchive.env.aria2_down",
    "pawchive.env.redis_down",
    "pawchive.env.go_settings",
    "pawchive.env.go_settings_hint",
    "pawchive.author_db.gate_hint",
    # 列表请求超时（`_explain_list_error`）
    "pawchive.error.timeout",
    # 列表请求失败自动重试的进度提示（`_on_failed`）
    "pawchive.error.retrying",
    # 下载失败的四类人话（`_explain_download_error` 按异常分派）
    "pawchive.error.proxy_missing",
    "pawchive.error.network",
    "pawchive.error.generic",
    "pawchive.error.blocked",
)
_PAWCHIVE_WINDOW_SIZE = 384
_PAWCHIVE_FTS_TASK_ID = "pawchive_authors_fts_ensure"
# 遮罩消散：与 jsoneri 检查层同一套扫描算法（宽 0 → 满宽，OutCubic）。
GATE_REVEAL_MS = 500


def _download_authors_db_task():
    """AsyncTask 线程体：拉取预构建作者库并落盘（返回 ReleaseAssetResult）。"""
    import asyncio

    from core.website.preprocess_script import ScriptEntryReleaseAsset, _ensure_data_client

    async def _run():
        client = _ensure_data_client()
        try:
            return await ScriptEntryReleaseAsset(client).ensure()
        finally:
            await client.aclose()

    return asyncio.run(_run())


class PawchiveGateLayer(QFrame):
    """pawchive 不可用时的遮罩层：模糊底 + 垂直居中 DlBtn / progressBar / infobar。

    只在 interface 内容区内覆盖（侧边栏不受影响，可自由切走）。三种态由
    :meth:`apply_state` 驱动；可用时走 ``dismiss`` 的消散动画（宽→0）。
    """

    download_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PawchiveGateLayer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # 底：模糊当前页面快照（快照而非实时模糊——遮罩自身不能参与抓图）
        self._blur_source = QLabel(self)
        self._blur_source.setScaledContents(True)
        self._blur_source.lower()
        blur = QGraphicsBlurEffect(self._blur_source)
        blur.setBlurRadius(14)
        self._blur_source.setGraphicsEffect(blur)

        layout = VBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 28)
        layout.setSpacing(12)
        layout.addStretch(1)

        self.dlBtn = PrimaryPushButton(FIF.DOWNLOAD, qtTrId("pawchive.author_db.download"), self)
        self.dlBtn.setFixedHeight(36)
        self.dlBtn.clicked.connect(self.download_requested)
        self.dlBtn.hide()
        layout.addWidget(self.dlBtn, 0, Qt.AlignmentFlag.AlignHCenter)

        self.progressBar = IndeterminateProgressBar(self, start=False)
        self.progressBar.setFixedHeight(6)
        self.progressBar.hide()
        layout.addWidget(self.progressBar)

        self.infoBar = BodyLabel("", self)
        self.infoBar.setObjectName("PawchiveGateInfo")
        self.infoBar.setWordWrap(True)
        self.infoBar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.infoBar.hide()
        layout.addWidget(self.infoBar)

        layout.addStretch(1)

        self._reveal = QPropertyAnimation(self, b"geometry", self)
        self._reveal.setDuration(GATE_REVEAL_MS)
        self._reveal.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._dismiss = QPropertyAnimation(self, b"geometry", self)
        self._dismiss.setDuration(GATE_REVEAL_MS)
        self._dismiss.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._dismiss.finished.connect(self._on_dismissed)
        self.hide()

    def apply_state(self, *, message: str, show_download: bool, busy: bool):
        """``message`` 常驻提示文案；``show_download`` 决定 DlBtn；``busy`` 跑进度条。"""
        self.infoBar.setText(message)
        self.infoBar.show()
        self.dlBtn.setVisible(show_download)
        self.dlBtn.setEnabled(show_download and not busy)
        if busy:
            self.progressBar.show()
            if not self.progressBar.isStarted():
                self.progressBar.start()
        else:
            self.progressBar.stop()
            self.progressBar.hide()

    def reveal(self, rect):
        """扫入：抓页面快照做模糊底，再宽 0 → 满宽（与 jsoneri 检查层同算法）。

        快照抓取内聚在此：先确保自身不可见（否则会抓到上一帧的遮罩），
        抓完再进入扫入动画，调用方无需关心顺序。
        """
        self.hide()
        self._blur_source.setPixmap(self.parent().grab())
        self._reveal.stop()
        self._dismiss.stop()
        self.raise_()
        self.show()
        start = QRect(rect.x(), rect.y(), 0, rect.height())
        self.setGeometry(start)
        self._reveal.setStartValue(start)
        self._reveal.setEndValue(rect)
        self._reveal.start()

    def dismiss(self, rect):
        """消散：满宽 → 宽 0，结束后隐藏（用独立动画对象，不与 reveal 抢 finished）。"""
        self._reveal.stop()
        self._dismiss.stop()
        self._dismiss.setStartValue(QRect(rect.x(), rect.y(), rect.width(), rect.height()))
        self._dismiss.setEndValue(QRect(rect.x(), rect.y(), 0, rect.height()))
        self._dismiss.start()

    def _on_dismissed(self):
        # 动画结束后归位，避免下次 reveal 从 0 宽开始错位。
        self.hide()
        self.setGeometry(0, 0, self.parent().width(), self.parent().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._blur_source.setGeometry(0, 0, self.width(), self.height())


class FilterView(FlyoutViewBase):
    closed = Signal()  # 添加closed信号
    
    def __init__(self, parent=None):
        super(FilterView, self).__init__(parent)
        self.interface = parent
        self.width = int(parent.width() * 0.6)
        self.setupUi()

    def setupUi(self):
        self.layout = VBoxLayout(self)
        
        first_row = QtWidgets.QHBoxLayout()
        self.textEdit = TextEdit(self)
        self.textEdit.setPlaceholderText(qtTrId("pawchive.filter.rules_hint"))
        self.textEdit.setPlainText(pawchive_cfg.filterText.value)
        first_row.addWidget(self.textEdit)
        
        second_row = QtWidgets.QHBoxLayout()
        self.linkBtn = HyperlinkButton(FIF.LINK, f"{ACGG_DOC}/script/pawchive.html#%F0%9F%9A%80-%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B", qtTrId("pawchive.filter.view_example"), self)
        spacerItem = QSpacerItem(10, 10, QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.svBtn = PrimaryToolButton(FIF.SAVE, self)
        self.svBtn.clicked.connect(self.save)
        self.closeBtn = TransparentToolButton(FIF.CLOSE, self)
        self.closeBtn.clicked.connect(self.closed)
        second_row.addWidget(self.linkBtn)
        second_row.addItem(spacerItem)
        second_row.addWidget(self.svBtn)
        second_row.addWidget(self.closeBtn)

        self.layout.addLayout(first_row)
        self.layout.addLayout(second_row)
        self.setFixedWidth(self.width)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Preferred)

    def save(self):
        pawchive_cfg.filterText.value = self.textEdit.toPlainText()
        pawchive_cfg.save()
        self.closeBtn.click()


class _TableQueryTask(QRunnable):
    """一次表模型 SQL（count / 一窗 fetch）。**只读 DB，不碰模型成员**。

    必须异步：这些查询在 sort()/apply_filter()/滚动取窗时被主线程直接调用，
    大库上耗时数百毫秒，就是"打开作者表卡一下、搜索框每敲一个字卡两秒"的来源。
    PawchiveAuthorsDb 每次查询自建连接（closing(self._connect())），工作线程并发读安全。
    """

    def __init__(self, owner: "VirtualPawchiveTableModel", generation: int, kind: str, func):
        super().__init__()
        self._owner = owner
        self._generation = generation
        self._kind = kind
        self._func = func
        self.setAutoDelete(True)

    def run(self):
        try:
            result = self._func()
        except Exception:  # noqa: BLE001 - 查库失败按空结果处理，不炸表
            result = None
        try:
            self._owner._query_done.emit(self._generation, self._kind, result)
        except RuntimeError:
            # 模型/窗已销毁（关作者表）：结果无意义，丢弃。
            pass


class VirtualPawchiveTableModel(QAbstractTableModel):
    """SQL-backed Pawchive table model: count + window cache, no full load_all.

    **所有 SQL 都在工作线程**（见 ``_TableQueryTask``），``data()`` / ``rowCount()``
    只读内存里的 count 与窗口缓存，永不阻塞界面线程。
    """

    #: 内部：worker → 主线程（跨线程自动排队）。
    _query_done = Signal(int, str, object)

    def __init__(self, authors_db: PawchiveAuthorsDb):
        super().__init__()
        self.authors_db = authors_db
        self.headers = [qtTrId("pawchive.table.column.author"), qtTrId("pawchive.table.column.platform"), qtTrId("pawchive.table.column.updated"), qtTrId("pawchive.table.column.favorites")]
        self.favorite_headers = [qtTrId("pawchive.table.column.avatar"), qtTrId("pawchive.table.column.author"), qtTrId("pawchive.table.column.platform"), qtTrId("pawchive.table.column.updated"), qtTrId("pawchive.table.column.favorites")]
        self.current_row_author = None
        self.favorites_only_mode = False
        self._query = AuthorQuery()
        self._row_count = 0
        self._window_offset = 0
        self._window_authors: list[PawchiveAuthor] = []
        #: 在飞的窗口请求 ``(offset, limit)``；同窗重复请求靠它去重（滚动会连发 data()）。
        self._window_pending: t.Optional[t.Tuple[int, int]] = None
        #: 查询代次：每次换 query 自增，旧代结果回来直接丢（否则快速改过滤会错位）。
        self._generation = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)  # 单连接串行：SQLite 并发读无收益，只增抖动
        self._query_done.connect(self._on_query_done, Qt.ConnectionType.QueuedConnection)
        self._reload_count()

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return self._row_count

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return 5 if self.favorites_only_mode else 4

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() < 0 or index.row() >= self._row_count:
            return None

        author = self._author_at_row(index.row())
        if author is None:
            # 窗口还没回来：只登记取窗，**不阻塞**；到货后发 dataChanged 重绘。
            self._schedule_window(index.row())
            return None
        col = index.column()

        if self.favorites_only_mode:
            if col == 0:
                return None
            col -= 1

        if role == Qt.DisplayRole:
            if col == 0:
                return author.name
            if col == 1:
                return author.service
            if col == 2:
                return datetime.fromtimestamp(author.updated).strftime(r'%Y-%m-%d')
            if col == 3:
                return str(author.favorited)
        elif role == Qt.UserRole:
            if col == 2:
                return author.updated
            if col == 3:
                return author.favorited
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            headers = self.favorite_headers if self.favorites_only_mode else self.headers
            if 0 <= section < len(headers):
                return headers[section]
        return None

    def sort(self, column, order):
        logical_column = column
        if self.favorites_only_mode:
            if column == 0:
                return
            logical_column = column - 1
        if logical_column < 0 or logical_column >= len(_PAWCHIVE_SORT_COLUMNS):
            return
        sort_column = _PAWCHIVE_SORT_COLUMNS[logical_column]
        sort_desc = order == Qt.DescendingOrder
        if self._query.sort_column == sort_column and self._query.sort_desc == sort_desc:
            return
        self._query = AuthorQuery(
            text=self._query.text,
            favorite_ids=self._query.favorite_ids,
            sort_column=sort_column,
            sort_desc=sort_desc,
        )
        self._reload_count(reset_window=True)

    def apply_filter(self, filter_text: str):
        if self.favorites_only_mode:
            return
        text = (filter_text or "").strip()
        if text == self._query.text:
            return
        self._query = AuthorQuery(
            text=text,
            favorite_ids=None,
            sort_column=self._query.sort_column,
            sort_desc=self._query.sort_desc,
        )
        self._reload_count(reset_window=True)

    def get_author_at_row(self, row: int) -> PawchiveAuthor | None:
        """同步取作者（右键/双击/头像同步用）。

        窗口没就绪时返回 ``None`` 并登记取窗 —— 调用方本来就按"取不到"处理，
        这里不为了拿一行去查库。
        """
        author = self._author_at_row(row)
        if author is None:
            self._schedule_window(row)
        return author

    def _author_at_row(self, row: int) -> PawchiveAuthor | None:
        """纯内存查：命中窗口返回作者，否则 ``None``（绝不触发 SQL）。"""
        if row < 0 or row >= self._row_count:
            return None
        window_index = row - self._window_offset
        if window_index < 0 or window_index >= len(self._window_authors):
            return None
        return self._window_authors[window_index]

    def show_favorites_only(self, favorite_ids):
        favorite_tuple = tuple(str(item) for item in favorite_ids)
        self.favorites_only_mode = True
        self._query = AuthorQuery(
            text="",
            favorite_ids=favorite_tuple,
            sort_column=self._query.sort_column,
            sort_desc=self._query.sort_desc,
        )
        self._reload_count(reset_window=True)

    def show_all_authors(self):
        self.favorites_only_mode = False
        self._query = AuthorQuery(
            text="",
            favorite_ids=None,
            sort_column=self._query.sort_column,
            sort_desc=self._query.sort_desc,
        )
        self._reload_count(reset_window=True)

    def _reload_count(self, *, reset_window: bool = False):
        """换查询：先清空（立刻可见的"重算中"），count 在 worker 里跑。

        清空会让表短暂空白 —— 这是有意的：另一条路是主线程等 514ms~1.9s 的
        ``COUNT(*)``，那才是用户抱怨的卡顿。
        """
        self._generation += 1
        generation = self._generation
        if reset_window:
            self._window_offset = 0
            self._window_authors = []
            self._window_pending = None
        self.beginResetModel()
        self._row_count = 0
        self.endResetModel()
        query = self._query
        self._pool.start(
            _TableQueryTask(
                self, generation, "count", lambda q=query: self.authors_db.count(q)
            )
        )

    def _schedule_window(self, focus_row: int):
        """登记取窗（幂等）：同一窗在飞时不重复排队。"""
        if focus_row < 0 or focus_row >= self._row_count:
            return
        if self._author_at_row(focus_row) is not None:
            return
        if self._window_pending is not None:
            # 已有窗口在飞就不再排队：``data()`` 在滚动时会对每个可见格调一次，
            # 逐行发起会把线程池灌满旧窗口。在飞窗口回来会发 dataChanged，
            # 视图重绘时会带着最新行号再请求一次，缺口自己补上。
            return
        half_window = _PAWCHIVE_WINDOW_SIZE // 2
        offset = max(0, focus_row - half_window)
        self._window_pending = (offset, _PAWCHIVE_WINDOW_SIZE)
        query = self._query
        generation = self._generation
        limit = _PAWCHIVE_WINDOW_SIZE
        # 带回 offset：worker 回来时主线程的 ``_window_offset`` 可能已被别的请求改过，
        # 用发送时的 offset 定位才不会把 A 窗的数据贴到 B 窗位置上。
        self._pool.start(
            _TableQueryTask(
                self,
                generation,
                "window",
                lambda q=query, o=offset, n=limit: (
                    o,
                    self.authors_db.fetch(q, offset=o, limit=n),
                ),
            )
        )

    def _on_query_done(self, generation: int, kind: str, result):
        if generation != self._generation:
            return  # 过期代次：用户已经改了过滤/排序，旧结果不许覆盖
        if kind == "count":
            count = int(result or 0)
            self.beginResetModel()
            self._row_count = count
            self._window_offset = 0
            self._window_authors = []
            self._window_pending = None
            self.endResetModel()
            return
        self._window_pending = None
        payload = result or ()
        if len(payload) != 2:
            return
        offset, authors = int(payload[0]), list(payload[1] or ())
        if not authors:
            return
        self._window_offset = offset
        self._window_authors = authors
        # 只刷这一窗：整表 dataChanged 会让视图对 11 万行做无效重绘。
        top = self.index(offset, 0)
        bottom = self.index(min(self._row_count - 1, offset + len(authors) - 1), 3)
        if top.isValid() and bottom.isValid():
            self.dataChanged.emit(top, bottom)


class PawchiveTableView(FramelessWindow):
    """Pawchive作者表格视图"""
    closed = Signal()

    def __init__(self, authors_db: PawchiveAuthorsDb, parent):
        super().__init__()
        self.interface = parent
        self.authors_db = authors_db
        self._table_initialized = False

        self._avatar_size = 32
        self._avatar_col_width = 44
        self._avatar_row_height = 40
        self._avatar_task_mgr = AsyncTaskManager(self)
        self._avatar_cache = AvatarCache(temp_p.joinpath("pawchive_avatars.pkl"), accept=is_jpeg)
        self._avatar_cache.load()
        self._avatar_widgets: t.Dict[int, ImageLabel] = {}
        self._avatar_pending: t.List[t.Tuple[PawchiveAuthor, ImageLabel]] = []
        self._avatar_max_concurrent = 12
        self._avatar_max_attempts = 3
        self._avatar_attempts: t.Dict[str, int] = {}
        self._avatar_sync_scheduled = False
        self._avatar_save_timer = QTimer(self)
        self._avatar_save_timer.setSingleShot(True)
        self._avatar_save_timer.timeout.connect(self._avatar_cache.save)
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(200)
        self._search_debounce.timeout.connect(self._apply_pending_search)
        self._pending_search_text = ""

        self.titleBar.minBtn.hide()
        self.titleBar.maxBtn.hide()
        self.titleBar.closeBtn.hide()

        parent_width = parent.width()
        parent_height = parent.height()
        window_width = int(parent_width * 0.9)
        window_height = int(parent_height * 0.7)

        self.resize(window_width, window_height)
        screen = QGuiApplication.primaryScreen()
        screen_geo = screen.geometry()
        self.move(
            int((screen_geo.width() - window_width) / 2),
            int((screen_geo.height() - window_height) / 2)
        )

        self.layout = VBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)

        self.virtual_model = VirtualPawchiveTableModel(authors_db)

        self.set_table()
        self.setupUi()

    def setupUi(self):
        first_row = QHBoxLayout()
        first_row.addWidget(self.tableView)

        second_row = QHBoxLayout()
        spacerItem = QtWidgets.QSpacerItem(40, 20, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
        self.closeBtn = TransparentToolButton(FIF.CLOSE, self)
        self.closeBtn.clicked.connect(self.hide)
        self.searchEdit = LineEdit(self)
        self.searchEdit.setPlaceholderText(qtTrId("pawchive.table.search_placeholder"))
        self.searchEdit.textChanged.connect(self._on_search_text_changed)
        self.searchEdit.setClearButtonEnabled(True)
        self.favBtn = TransparentTogglePushButton(FIF.HEART, qtTrId("pawchive.table.view_favorites"), self)
        self.favBtn.clicked.connect(self.toggle_favorites_view)
        second_row.addWidget(self.searchEdit)
        second_row.addWidget(self.favBtn)
        second_row.addItem(spacerItem)
        second_row.addWidget(self.closeBtn)

        self.layout.addLayout(first_row)
        self.layout.addLayout(second_row)

    def set_table(self):
        self.tableView = TableView(self)
        self.tableView.setBorderRadius(15)
        self.tableView.setWordWrap(False)
        table_width = self.width()
        table_height = self.height() - 60
        self.tableView.setFixedSize(table_width, table_height)
        self.tableView.verticalHeader().hide()

        self.tableView.setModel(self.virtual_model)
        self.tableView.horizontalHeader().setStretchLastSection(True)

        self.tableView.setSortingEnabled(True)
        self.tableView.horizontalHeader().setSortIndicatorShown(True)
        self.tableView.horizontalHeader().setSortIndicator(3, Qt.DescendingOrder)

        self.virtual_model.layoutChanged.connect(self._schedule_avatar_sync)
        self.virtual_model.modelReset.connect(self._schedule_avatar_sync)
        # 窗口是异步取的：modelReset 到达时行数据往往还没回来（作者为 None），
        # 头像列会整片空着。窗口到货发 dataChanged，这里再补一轮头像同步。
        self.virtual_model.dataChanged.connect(self._schedule_avatar_sync)

        self._apply_column_layout()

        self.tableView.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tableView.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)

        self.tableView.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tableView.customContextMenuRequested.connect(self.on_right_click)
        # P1：双击作者 → 打开 L2 作品网格（旧链 send_to_input 保留，P5 才清）。
        self.tableView.doubleClicked.connect(self._on_row_double_clicked)

    def _on_row_double_clicked(self, index):
        if not index.isValid():
            return
        author = self.virtual_model.get_author_at_row(index.row())
        if author is None:
            return
        # 统一走本类入口：出图后由 open_author_works 负责收起作者表，
        # 双击与右键「查看作品」关窗行为必须一致（旧代码直连 interface 不会关窗）。
        self.open_author_works(author)

    def _on_search_text_changed(self, text: str):
        self._pending_search_text = text
        self._search_debounce.start()

    def _apply_pending_search(self):
        self.virtual_model.apply_filter(self._pending_search_text)

    def filter_table(self, text):
        self.virtual_model.apply_filter(text)

    def toggle_favorites_view(self):
        if self.favBtn.isChecked():
            self.searchEdit.clear()
            self.searchEdit.setDisabled(True)
            self.show_favorites_only()
        else:
            self.searchEdit.setDisabled(False)
            self.show_all_authors()

    def show_favorites_only(self):
        favorite_ids = pawchive_cfg.fav.get_favorites()
        self.virtual_model.show_favorites_only(favorite_ids)
        self._apply_column_layout()
        self._schedule_avatar_sync()

    def show_all_authors(self):
        self._clear_avatar_widgets()
        self.virtual_model.show_all_authors()
        self._apply_column_layout()

    def _apply_column_layout(self):
        tb_width = self.tableView.width()
        vh = self.tableView.verticalHeader()
        if self.virtual_model.favorites_only_mode:
            vh.setDefaultSectionSize(self._avatar_row_height)
            self.tableView.setColumnWidth(0, self._avatar_col_width)
            self.tableView.setColumnWidth(1, int(tb_width * 0.26))
            self.tableView.setColumnWidth(2, int(tb_width * 0.18))
            self.tableView.setColumnWidth(3, int(tb_width * 0.24))
            self.tableView.setColumnWidth(4, int(tb_width * 0.18))
        else:
            vh.setDefaultSectionSize(30)
            self.tableView.setColumnWidth(0, int(tb_width * 0.3))
            self.tableView.setColumnWidth(1, int(tb_width * 0.2))
            self.tableView.setColumnWidth(2, int(tb_width * 0.25))
            self.tableView.setColumnWidth(3, int(tb_width * 0.2))

    def _schedule_avatar_sync(self):
        if self._avatar_sync_scheduled:
            return
        self._avatar_sync_scheduled = True
        safe_single_shot(0, self._sync_avatar_widgets)

    def _sync_avatar_widgets(self):
        self._avatar_sync_scheduled = False
        if not self.virtual_model.favorites_only_mode:
            return

        self._clear_avatar_widgets()
        rows = self.virtual_model.rowCount()
        for row in range(rows):
            idx = self.virtual_model.index(row, 0)
            author = self.virtual_model.get_author_at_row(row)
            if not author:
                continue

            label = ImageLabel(self.tableView)
            label.scaledToHeight(self._avatar_size)
            label.scaledToWidth(self._avatar_size)
            self._set_placeholder(label)
            label.setProperty("avatar_key", self._avatar_key(author))
            self.tableView.setIndexWidget(idx, label)
            self._avatar_widgets[row] = label

            cached = self._avatar_cache.get(self._avatar_key(author))
            if cached:
                self._apply_bytes_to_label(label, cached)
                continue
            self._start_download(author, label)

    def _clear_avatar_widgets(self):
        self._avatar_pending.clear()
        self._avatar_attempts.clear()
        for w in self._avatar_widgets.values():
            if self._widget_alive(w):
                w.setParent(None)
                w.deleteLater()
        self._avatar_widgets.clear()

    def _start_download(self, author: PawchiveAuthor, label: ImageLabel):
        key = self._avatar_key(author)
        task_id = f"av_{key}"
        if self._avatar_task_mgr.is_task_running(task_id):
            return
        if len(self._avatar_task_mgr.get_running_tasks()) >= self._avatar_max_concurrent:
            self._avatar_pending.append((author, label))
            return

        config = TaskConfig(
            task_func=lambda _url=author.avatar: fetch_avatar_bytes(_url),
            success_callback=lambda data, _k=key, _l=label: self._on_download_ok(_k, _l, data),
            error_callback=lambda err, _k=key, _a=author, _l=label: self._on_download_error(_k, _a, _l),
            tooltip_title="",
            show_success_info=False,
            show_error_info=False,
            tooltip_parent=None,
        )
        self._avatar_task_mgr.execute_task(task_id, config)

    def _on_download_ok(self, key: str, label: ImageLabel, data: bytes):
        self._avatar_attempts.pop(key, None)
        scaled = to_avatar_jpeg(data, self._avatar_size)
        if not scaled:
            self._drain_pending()
            return
        self._avatar_cache.set(key, scaled)
        self._avatar_save_timer.start(800)
        if not self._widget_alive(label):
            self._drain_pending()
            return
        try:
            if label.property("avatar_key") != key:
                self._drain_pending()
                return
        except RuntimeError:
            self._drain_pending()
            return
        self._apply_bytes_to_label(label, scaled)
        self._drain_pending()

    def _on_download_error(self, key: str, author: PawchiveAuthor, label: ImageLabel):
        # fetch_avatar_bytes 内部已做瞬时错误退避重试；这里再排队一轮更晚的重试，
        # 覆盖「整段网络抖动超过单次任务重试窗口」的情况，避免头像停留在灰占位。
        attempts = self._avatar_attempts.get(key, 0) + 1
        self._avatar_attempts[key] = attempts
        if attempts < self._avatar_max_attempts and self._widget_alive(label):
            self._avatar_pending.append((author, label))
        self._drain_pending()

    def _apply_bytes_to_label(self, label: ImageLabel, img_bytes: bytes):
        pm = QPixmap()
        if not pm.loadFromData(img_bytes):
            return
        label.setImage(pm)
        label.setFixedSize(self._avatar_size, self._avatar_size)

    def _set_placeholder(self, label: ImageLabel):
        ph = QPixmap(self._avatar_size, self._avatar_size)
        ph.fill(QColor(235, 235, 235))
        label.setImage(ph)
        label.setFixedSize(self._avatar_size, self._avatar_size)

    @staticmethod
    def _avatar_key(author: PawchiveAuthor) -> str:
        return f"{author.service}:{author.id}"

    @staticmethod
    def _widget_alive(w) -> bool:
        if w is None:
            return False
        try:
            w.objectName()
            return True
        except RuntimeError:
            return False

    def _drain_pending(self):
        if not self.virtual_model.favorites_only_mode:
            self._avatar_pending.clear()
            return
        while self._avatar_pending and len(self._avatar_task_mgr.get_running_tasks()) < self._avatar_max_concurrent:
            author, label = self._avatar_pending.pop(0)
            if not self._widget_alive(label):
                continue
            self._start_download(author, label)
            break

    def closeEvent(self, event):
        self._avatar_task_mgr.cancel_all_tasks()
        close_client()
        if self._avatar_save_timer.isActive():
            self._avatar_save_timer.stop()
        self._avatar_cache.save()
        super().closeEvent(event)

    def on_right_click(self, position):
        index = self.tableView.indexAt(position)
        if not index.isValid():
            return

        model_row = index.row()
        selected_author = self.virtual_model.get_author_at_row(model_row)
        if not selected_author:
            return

        self.virtual_model.current_row_author = selected_author

        commandBar = CommandBarView()
        # 两个"看作品"入口图标不同但旧文案同名（都是「查看其作品」），用户分不出
        # 点哪个会在本页开网格、哪个会踢到浏览器 —— 文案必须各自说清落点。
        works_action = Action(FIF.PHOTO, qtTrId("pawchive.table.browse_works"))
        works_action.triggered.connect(lambda: self.open_author_works(selected_author))
        link_action = Action(FIF.LINK, qtTrId("pawchive.table.open_in_browser"))
        link_action.triggered.connect(lambda: self.open_author_link(selected_author))
        fav_action = Action(FIF.HEART, qtTrId("pawchive.table.favorite_locally"))
        fav_action.triggered.connect(lambda: self.fav_author(selected_author))
        # 顺序 = 主任务（在本页看）→ 备选（浏览器）→ 本地动作。
        # 「发送至输入框」不再出现：宿主没有 ``focus_author``，P5 又删了输入框，
        # 该动作只会弹一条 InfoBar，留着就是"以为还能用"的误导源。
        commandBar.addAction(works_action)
        commandBar.addAction(link_action)
        commandBar.addAction(fav_action)
        commandBar.resizeToSuitableWidth()

        target_pos = self.tableView.mapToGlobal(position)
        Flyout.make(commandBar, target=target_pos, parent=self, aniType=FlyoutAnimationType.FADE_IN)

    def open_author_works(self, author):
        """双击/右键「查看其作品」→ 在 L2 网格里打开该作者，并收起作者表。

        出图后作品层铺满页面，作者表若继续浮在上层只会挡住网格；实例不销毁，
        底栏「作者表格」按钮经 ``_present_table_window`` 可随时重新 show。
        """
        self.interface.open_author_works(author)
        self.hide()

    def open_author_link(self, author):
        """打开作者链接"""
        author_url = f"https://{domain}/{author.service}/user/{author.id}"
        QDesktopServices.openUrl(QUrl(author_url))

    def fav_author(self, author):
        is_favorited = pawchive_cfg.fav.toggle_favorite(author.id)
        action_text = qtTrId("pawchive.favorite.added") if is_favorited else qtTrId("pawchive.favorite.removed")
        at = InfoBar.success if is_favorited else InfoBar.warning
        at(
            title="", content=qtTrId("pawchive.table.action_label").format(action_text=action_text, author_name=author.name),
            orient=Qt.Horizontal, position=InfoBarPosition.TOP,
            duration=2000, parent=self
        )


class _GridDownloadBridge(QObject):
    """把 worker 线程的 ``on_progress``/``on_complete`` 搬回 GUI 线程。

    ``PawchiveService.submit`` 的回调在 ``asyncio.run`` 所在的工作线程里被调用，
    而 ``PawchiveCardWidget.set_progress`` 会 ``update()`` 并改 Qt 属性 —— 跨线程直接
    碰控件是未定义行为（崩溃或静默不刷新）。这里只做一件事：把参数塞进 Signal，
    由 Qt 的排队连接送回主线程，槽再去动控件。

    ``key_for`` 的映射表在 ``_task`` 里由 ``plan_items`` 的结果填充（identity →
    CardKey），因为只有服务层知道 identity 怎么算（UI 不重算 sha1）。
    """

    progress = Signal(object)  # PawchiveProgress
    completed = Signal(str, bool)

    def __init__(self, owner, tab):
        super().__init__(owner)
        self._owner = owner
        self._tab = tab
        self._idents: t.Dict[str, t.Any] = {}
        self.progress.connect(
            lambda p, _t=tab: owner._on_grid_download_progress(_t, p),
            Qt.QueuedConnection,
        )
        self.completed.connect(
            lambda _ident, ok, _t=tab: self._on_complete(_t, ok),
            Qt.QueuedConnection,
        )

    def set_idents(self, idents):
        self._idents = dict(idents or {})

    def key_for(self, identity_key):
        return self._idents.get(identity_key)

    # ---- worker 线程调用（只 emit，不碰控件） ----
    def emit_progress(self, progress):
        if progress.identity_key not in self._idents:
            return
        self.progress.emit(progress)

    def emit_complete(self, identity_key, ok):
        self.completed.emit(identity_key, bool(ok))

    # ---- 主线程槽 ----
    def _on_complete(self, tab, ok: bool):
        _ = tab, ok
        # 徽标口径由批量完成回调统一收口（``_on_grid_download_done``）；单文件完成
        # 不逐次改徽标，避免 N 次 set_count + move（06 §5-5）。


class PawchiveInterface(QFrame):
    """Pawchive界面Widget类"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.parent_window = parent
        self.table_inited = False
        self.table_window = None
        self._table_task_mgr = AsyncTaskManager(self) if parent is not None else None
        self.setObjectName("PawchiveInterface")
        self.setupUi()
        self._gate = PawchiveGateLayer(self)
        self._gate.download_requested.connect(self._download_authors_db)
        self._gate_ready = False
        # P1：L2 作品网格层（懒创建，覆盖在页面上；P5 已清掉旧表单与日志台）。
        self.works_bar = None
        # R5：切换服务模式时禁止中断在跑的下载。置位源见 ``server_mode_switch_blockers``。
        self._download_busy = False
        # ---- 环境探测（异步）----
        # 不在主线程探：is_ready() 拿引擎锁后做 RPC ping，后台下载线程持锁时主线程就卡住
        # （见 env_probe 模块 docstring）。_env_probe_funcs 只在测试/探针里替换。
        self._env_prober = EnvProber(self)
        self._env_prober.aria2_ready.connect(
            self._on_env_aria2, Qt.ConnectionType.QueuedConnection
        )
        self._env_prober.redis_ready.connect(
            self._on_env_redis, Qt.ConnectionType.QueuedConnection
        )
        self._env_probe_funcs = (probe_aria2, probe_redis)
        #: 上次探测**发起**时间（monotonic）：节流按发起算，卡住的探测不该让节流放行。
        self._env_probe_at = 0.0
        #: 本轮还没回的服务（超时兜底按它逐个渲染）。
        self._env_pending: set[str] = set()
        #: 最近一次已知结论（仅用于诊断/探针，UI 状态以左下 InfoBar 为准）。
        self._env_values: dict[str, bool] = {}
        #: 缺失提示条（按 kind 去重）：常驻顶部条已删除，改为左下 InfoBar。
        self._env_notice_bars: dict[str, InfoBar] = {}
        #: 用户/定时器关掉的 kind：本轮缺失不再反复弹，依赖恢复或手动重探（force）时解除。
        self._env_notice_muted: set[str] = set()
        self._env_probe_timeout = QTimer(self)
        self._env_probe_timeout.setSingleShot(True)
        self._env_probe_timeout.setInterval(PROBE_TIMEOUT_MS)
        self._env_probe_timeout.timeout.connect(self._on_env_probe_timeout)
        theme_mgr.subscribe(self._apply_bottom_bar_theme)
        self.destroyed.connect(self._on_interface_destroyed)
        # R2/B1：viewer 首帧原图/缩略图决策要用的站点测速。singleShot(0) 挪出构造
        # 关键路径；真正的网络测速在 _kickoff 里丢后台线程（ensure_speed 同步阻塞）。
        # ensure_speed 内部按 site 去重（含失败负缓存），每次 __init__ 调都安全。
        QTimer.singleShot(0, self._kickoff_viewer_speed_probe)

    def _kickoff_viewer_speed_probe(self) -> None:
        """后台线程测一次 pawchive 下载速率（每进程一次，由引擎去重）。

        probe 用已导出的最小稳定资源 ``creators_txt``（纯文本端点，远小于任何图片），
        fetcher 走 service 层同步 client（``fetch_bytes``，自带按域 UA/代理）。
        ``ensure_speed`` 同步阻塞，必须在守护线程里跑，绝不占 GUI 线程。任何环节失败
        都静默——测速不可用 ⇒ 引擎保守出缩略图，不影响界面与预览。
        """
        try:
            from app.GUI.viewer.source_engine import ensure_speed
            from core.website.pawchive.api import fetch_bytes
            from core.website.pawchive import creators_txt as probe_url
        except Exception:  # noqa: BLE001 - 依赖尚未就绪/导入失败：跳过测速
            return
        if not probe_url:
            return

        def _run() -> None:
            try:
                ensure_speed("pawchive", probe_url, fetch_bytes)
            except Exception:  # noqa: BLE001 - 测速失败落负缓存/静默，绝不拖慢界面
                pass

        try:
            import threading

            threading.Thread(
                target=_run, name="pawchive-viewer-speed-probe", daemon=True
            ).start()
        except Exception:  # noqa: BLE001 - 线程起不来就跳过，绝不影响界面
            pass

    def _on_interface_destroyed(self, _obj=None) -> None:
        theme_mgr.unsubscribe(self._apply_bottom_bar_theme)

    def showEvent(self, event):
        super().showEvent(event)
        # 存储路径可能在设置页被改过：每次进页面重读一次，别让 tooltip 停在旧值。
        self._refresh_open_btn_hint()
        self._ensure_ready()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._gate.isVisible():
            self._gate.setGeometry(self.rect())
            self._gate.raise_()
        if self.works_bar is not None and self.works_bar.isVisible():
            self.works_bar.setGeometry(self._works_area())
            self.works_bar.raise_()
        self.bottom_bar.raise_()
        # env 条用 NONE 自行定位，不受 manager 的 resize eventFilter 管辖，
        # 窗口缩放后必须自己按新的底栏上沿重排。
        if self._env_notice_bars:
            self._place_env_notices()

    def _works_area(self) -> QRect:
        """L2 覆盖层可占的区域：**底栏之上**，绝不覆盖底栏按钮。

        交付现场事故：作品层用 ``self.rect()`` 铺满整页并 ``raise_()``，顶栏
        「作者表格 / 过滤规则 / 打开目录」三个按钮看得见却完全点不动——子控件把
        鼠标事件截走了。入口按钮现在收在贴底的操作条里，覆盖层只能占到它上沿；
        底栏始终是用户的退路（返回作者表、改过滤、开目录、翻页）。
        """
        bottom = self.bottom_bar.geometry().top() - _WORKS_BOTTOM_GAP
        if bottom <= 0:
            # 布局尚未算完（构造期 / 首次 show）：按底栏期望高度先兜底。
            bottom = (
                self.height()
                - self.bottom_bar.sizeHint().height()
                - _WORKS_BOTTOM_GAP
            )
        bottom = max(0, min(bottom, self.height()))
        return QRect(0, 0, self.width(), max(0, bottom))

    # ---- P1：作者 → 作品网格 ----
    def _ensure_works_bar(self) -> PawchiveTabsBar:
        if self.works_bar is None:
            self.works_bar = PawchiveTabsBar(self)
            self.works_bar.request_close.connect(self.close_author_works)
            self.works_bar.active_tab_changed.connect(self._sync_page_nav)
            self.works_bar.hide()
        return self.works_bar

    # ---- 底栏页码：interface 持有唯一一份导航（对齐 danbooru 的单实例架构）----
    def _sync_page_nav(self, tab=None):
        """用当前 active tab 的状态刷新**唯一**的 ``self.page_nav``。

        旧实现让每个 tab 各持一个 nav，靠 setParent/addWidget 搬进底栏；切/关 tab
        时搬移失败会残留孤儿 nav，底栏出现两套「‹ n ›」（截图现场事故）。现在
        nav 实例归 interface 全周期唯一一份，tab 只通过 ``grid.state`` 提供状态，
        本方法只改这一份控件的 state/visible——结构上不再可能重复。
        """
        bar = getattr(self, "works_bar", None)
        active = bar.active_tab() if bar is not None else None
        # 传入 tab 仅作提示；真正以 active_tab 为准，避免非 active tab 的状态
        # 通过 nav_state_changed 信号反向刷到界面上。
        del tab
        nav = self.page_nav
        if active is None or bar is None or not bar.isVisible():
            self.page_nav_holder.hide()
            self._nav_divider.hide()
            nav.hide()
            return
        state = active.grid.state
        page = state.offset // max(1, state.page_size) + 1
        nav.set_state(
            page=page,
            has_more=state.has_more_results,
            loading=state.loading,
        )
        nav.show()
        self.page_nav_holder.show()
        self._nav_divider.show()

    def _on_page_nav_requested(self, page: int):
        """唯一 nav 的翻页请求 → 当前 active tab 替换式加载目标页。"""
        bar = getattr(self, "works_bar", None)
        active = bar.active_tab() if bar is not None else None
        if active is None:
            return
        self._load_author_page(active, page=int(page), replace=True)

    def open_author_works(self, author):
        """双击/右键「查看其作品」→ 打开该作者的 L2 网格 tab。"""
        bar = self._ensure_works_bar()
        tab = bar.open_author(
            author_name=str(getattr(author, "name", "") or ""),
            service=str(getattr(author, "service", "") or ""),
            creator_id=str(getattr(author, "id", "") or ""),
        )
        tab.request_next_page.connect(lambda t=tab: self._load_author_page(t, next_page=True))
        tab.request_page.connect(lambda page, t=tab: self._load_author_page(t, page=page))
        tab.retry_requested.connect(lambda t=tab: self._load_author_page(t))
        # tab 数据更新（append/reset/加载态）只发状态信号，宿主据此刷新唯一 nav；
        # UniqueConnection 防止重复打开同一 tab 时重复接线。
        try:
            tab.nav_state_changed.connect(
                self._sync_page_nav, Qt.ConnectionType.UniqueConnection
            )
        except TypeError:  # 老版本 PySide 无 UniqueConnection
            if not getattr(tab, "_nav_hooked", False):
                tab.nav_state_changed.connect(self._sync_page_nav)
                tab._nav_hooked = True
        # P2：网格发起的批量下载（工具栏勾选 / 组头「下载本帖」）的唯一消费者。
        tab.download_requested.connect(lambda keys, t=tab: self._submit_grid_download(t, keys))
        # 浮窗里选时间范围：先选已加载的，再按需自动翻页补齐（缩略图仍走懒加载，
        # 不额外发请求 —— 翻页只多一次列表 API）。
        tab.date_range_requested.connect(
            lambda s, e, t=tab: self._on_date_range_requested(t, s, e)
        )
        bar.setGeometry(self._works_area())
        bar.show()
        bar.raise_()
        # 覆盖层只占内容区，但底栏仍需在最前：先让布局把 bottom_bar 排好，
        # 再抬一次，确保它不会被后建的子控件压住。
        self.bottom_bar.raise_()
        self._sync_page_nav(bar.active_tab())
        # 已存在但上次没加载成功（或从未加载）的 tab：重新进入作者页时重试。
        if not tab.grid.state.has_loaded_once or tab.grid.state.error:
            self._load_author_page(tab)
        return tab

    def close_author_works(self):
        if self.works_bar is not None:
            self.works_bar.hide()
        self._sync_page_nav(None)

    def _load_author_page(
        self,
        tab,
        *,
        page=None,
        next_page: bool = False,
        offset_override: t.Optional[int] = None,
        attempt: int = 1,
        reuse_seq: t.Optional[int] = None,
        replace: bool = False,
    ):
        """拉一页作品（P0/P2 服务层未落地时给出可读提示，不静默空屏）。

        取数走 ``PawchiveService.list_posts``（keyword-only，无 sort，契约 §三.12），
        排序由 tab 本地做。此处只做"取一页 → 转组 → append"的薄桥。

        失败自动重试 5 次（``_retry_author_page``）；``reuse_seq`` 让重试沿用同一
        代次，不会把"重试"误判成一次新请求。
        """
        offset = tab.grid.state.offset
        if offset_override is not None:
            offset = int(offset_override)
        elif page is not None:
            offset = (max(1, int(page)) - 1) * tab.grid.state.page_size
        elif next_page:
            offset = tab.grid.state.offset + tab.grid.state.page_size
        try:
            from core.website.pawchive.service import PawchiveService
        except ImportError:
            tab.grid.show_empty(qtTrId("pawchive.grid.loading_more"))
            return
        # 替换式翻页：先清空旧页，目标页由回包 append_page 重新填充。
        # offset 已在上方基于 page 算好，grid.reset() 清 state 不影响本次请求。
        if replace:
            tab.grid.reset()
        # 进入加载态：必须先清空错误/空态并亮出"加载中"。
        # 现场事故：请求还在飞时页面什么都不显示，顶栏只写「共 0 帖」，用户读成
        # "这个作者没有作品"（截图里就是「该作者没有可浏览的作品」），而实际上
        # 只是还没回来 / 失败了几十秒。加载中与空结果是两种状态，必须分开。
        tab.grid.hide_error()
        tab.grid.hide_empty()
        tab.grid.set_loading(True)
        # 顶栏计数/翻页跟着进"加载中"：否则首屏在飞时它一直写「0 / 0」。
        tab.sync_counts()
        tab.sync_page_nav()

        # 每次"新的"列表请求（翻页/重试入口）自增代次：同一 tab 上后发起的请求
        # 会作废旧回包。没有它，切 tab / 连点重试 / 自动翻页重叠时，旧结果会写进
        # 已经换过内容的网格（用户现场："内容残留在第一个 tab 上"）。
        seq = int(getattr(tab, "_load_seq", 0)) + 1
        tab._load_seq = seq
        attempt = max(1, int(attempt or 1))

        loader = getattr(self, "_works_page_task_mgr", None)
        if loader is None:
            loader = self._works_page_task_mgr = AsyncTaskManager(self)

        def _fetch(_service_cls=PawchiveService, _tab=tab, _offset=offset):
            import asyncio

            async def _run():
                service = _service_cls()
                try:
                    return await service.list_posts(
                        creator_id=_tab.creator_id,
                        service=_tab.service,
                        offset=_offset,
                    )
                finally:
                    await service.aclose()

            return asyncio.run(_run())

        def _is_current() -> bool:
            """tab 还活着、且这次请求仍是最新的一次（旧回包必须丢弃）。"""
            try:
                import shiboken6

                if not shiboken6.isValid(tab):
                    return False
            except Exception:  # noqa: BLE001 - 探测不到就当有效
                pass
            return int(getattr(tab, "_load_seq", 0)) == seq

        def _on_ok(page_obj, _tab=tab):
            if not _is_current():
                return
            _tab._page_retry_attempt = 0
            self._on_author_page_loaded(_tab, page_obj)

        def _on_failed(error, _tab=tab, _attempt=attempt, _offset=offset):
            if not _is_current():
                return
            # 网络抖动是常态：先自动重试，别让人手点。
            # 用 TOP 而非 BOTTOM_LEFT：BOTTOM_LEFT manager 的 slide 动画与 resize
            # eventFilter 会把条拉到父窗口底边、压住底栏按钮组。TOP 顶部居中，与下载失败条同一落位。
            if _attempt < _PAWCHIVE_PAGE_RETRY_LIMIT:
                InfoBar.info(
                    title="",
                    content=qtTrId("pawchive.error.retrying").format(
                        attempt=_attempt, total=_PAWCHIVE_PAGE_RETRY_LIMIT
                    ),
                    orient=Qt.Horizontal,
                    isClosable=False,
                    position=InfoBarPosition.TOP,
                    duration=1600,
                    parent=self,
                )
                delay = _PAWCHIVE_PAGE_RETRY_BASE_MS * _attempt
                QTimer.singleShot(
                    delay,
                    lambda: self._retry_author_page(
                        _tab, offset=_offset, attempt=_attempt + 1, seq=seq
                    ),
                )
                return
            # 5 次都失败：先收加载态再报错（两条同时亮会被读成"还在加载"）。
            _tab._range_paging = False
            _tab.grid.set_loading(False)
            _tab.sync_counts()
            _tab.sync_page_nav()
            _tab.grid.show_error(self._explain_list_error(error))

        loader.execute_task(
            # task_id 必须带 service + seq + attempt：同 creator_id 在两个平台的
            # 两个 tab 会撞 id，第二次请求被 `is_task_running` 静默拒绝。
            f"pawchive_posts_{tab.service}_{tab.creator_id}_{offset}_{seq}_{attempt}",
            TaskConfig(
                task_func=_fetch,
                success_callback=_on_ok,
                error_callback=_on_failed,
                show_success_info=False,
                show_error_info=False,
            ),
        )

    def _retry_author_page(self, tab, *, offset: int, attempt: int, seq: int) -> None:
        """自动重试入口：沿用同一页 offset，带代次检查，tab 已关就静默放弃。"""
        try:
            import shiboken6

            if not shiboken6.isValid(tab) or int(getattr(tab, "_load_seq", 0)) != seq:
                return
        except Exception:  # noqa: BLE001
            pass
        if not tab.grid.state.loading:
            return
        tab._page_retry_attempt = attempt
        self._load_author_page(tab, offset_override=offset, attempt=attempt, reuse_seq=seq)

    def _explain_list_error(self, error) -> str:
        """列表请求失败 → 用户看得懂的一句话（技术细节留给日志）。

        「卡几十秒然后弹一串异常类名」没有可操作性；这里只说"哪类问题 + 怎么办"，
        错误行右侧本身带「重试」按钮（grid.error_retry_btn）。
        """
        text = str(error or "")
        low = text.lower()
        if "requires proxies" in text or "pawchiveconfigerror" in low:
            return qtTrId("pawchive.error.proxy_missing")
        if any(k in text for k in ("Timeout", "timed out", "ReadTimeout", "ConnectTimeout")):
            return qtTrId("pawchive.error.timeout")
        if any(k in text for k in ("ConnectError", "网络", "Connection")):
            return qtTrId("pawchive.error.network")
        if "403" in text or "blocked" in low:
            return qtTrId("pawchive.error.blocked")
        return qtTrId("pawchive.error.generic")

    def _on_date_range_requested(self, tab, start, end):
        """用户选了时间范围：选中已加载的，必要时自动翻页补齐范围内帖子。

        为什么可以"自动翻页"：列表 API 一页 50 帖，缩略图是懒加载（只请求视口内的），
        所以翻页只增加**列表**请求，不会因此多下载一堆缩略图。翻到"已加载里最早的
        帖子早于范围起点"就停，不会无脑翻到底。
        """
        tab._date_range = (start, end)
        self._select_loaded_range(tab)
        self._page_for_range(tab)

    def _select_loaded_range(self, tab) -> int:
        rng = getattr(tab, "_date_range", None)
        if not rng:
            return 0
        return tab.grid.select_date_range(rng[0], rng[1])

    def _page_for_range(self, tab, *, continue_only: bool = False) -> None:
        """按范围继续翻页；已覆盖到起点或没有更多就停。"""
        rng = getattr(tab, "_date_range", None)
        if not rng:
            return
        if getattr(tab, "_range_paging", False) and not continue_only:
            return
        if not tab.grid.state.has_more_results:
            tab._range_paging = False
            return
        start_s = tab.grid._as_date_text(rng[0])
        oldest = tab.grid.oldest_loaded_date()
        if oldest and oldest <= start_s:
            tab._range_paging = False
            return
        tab._range_paging = True
        self._load_author_page(tab, next_page=True)

    def _submit_grid_download(self, tab, card_keys):
        """网格勾选集 → ``plan_items`` → ``submit``（进程内 aria2）。

        为什么必须走这条链，而不是 UI 自己拼 URL：

        - ``plan_items`` 每帖**现取一次 detail**：``temp_url`` 是 <1 天短签名
          （契约 §三.2），缓存或复用旧 URL 必然 403。
        - ``submit`` 自己负责已下去重、``NO_URL`` 分类、并发信号量、限速、记账；
          UI 只消费 ``PawchiveDownloadPlan``，不重算 identity、不直接连 aria2。

        完成数口径（06 §2.4.1 / D-5）：``submit`` 返回时 ``len(plan.to_submit)``
        已经是**真正落盘完成**的文件数（``_download_one`` 轮询到 complete 才记），
        所以提示必须说「已完成 N 张」，不能说「已提交 N 张」。
        """
        keys = list(dict.fromkeys(tuple(k) for k in (card_keys or ())))
        if not keys:
            return
        try:
            from core.website.pawchive.service import PawchiveService
        except ImportError:
            return
        # 分组：``plan_items`` 是帖级入口（一次 detail 换整帖的项）。
        by_post: t.Dict[str, t.List[str]] = {}
        posts: t.Dict[str, t.Any] = {}
        for key in keys:
            card = tab.grid.card_for_key(key)
            item = getattr(card, "post", None)
            post = getattr(item, "post", None)
            if post is None:
                continue
            post_id = str(key[0])
            posts.setdefault(post_id, post)
            by_post.setdefault(post_id, []).append(str(key[1]))
        if not by_post:
            return

        tab.set_download_activity(active=len(keys), failed=0)
        self._download_busy = True
        bridge = _GridDownloadBridge(self, tab)
        self._works_download_bridge = bridge  # 保命：任务跑完前不能被回收
        mgr = getattr(self, "_works_download_task_mgr", None)
        if mgr is None:
            mgr = self._works_download_task_mgr = AsyncTaskManager(self)

        def _task(_by_post=dict(by_post), _posts=dict(posts)):
            import asyncio

            async def _run():
                service = PawchiveService()
                try:
                    items = []
                    for post_id, paths in _by_post.items():
                        items.extend(await service.plan_items(_posts[post_id], paths))
                    if not items:
                        return None
                    # identity → CardKey：直接由 plan 里的 post/attachment 反推，
                    # UI 不自己算 sha1（契约 §三.10）。
                    idents = {
                        item.identity_key: (
                            str(item.post.post_id),
                            str(item.attachment.path),
                        )
                        for item in items
                    }
                    plan = await service.submit(
                        items,
                        on_progress=bridge.emit_progress,
                        on_complete=bridge.emit_complete,
                    )
                    return plan, idents
                finally:
                    await service.aclose()

            return asyncio.run(_run())

        mgr.execute_task(
            f"pawchive_submit_{tab.creator_id}_{len(keys)}",
            TaskConfig(
                task_func=_task,
                success_callback=lambda result, _tab=tab, _keys=keys: self._on_grid_download_done(
                    _tab, result, _keys
                ),
                error_callback=lambda error, _tab=tab: self._on_grid_download_failed(
                    _tab, error
                ),
                show_success_info=False,
                show_error_info=False,
            ),
        )

    def _on_grid_download_progress(self, tab, progress):
        """单文件进度 → 卡角环。``total_bytes is None`` 走不定式（禁 0 兜底）。"""
        key = self._works_download_bridge.key_for(progress.identity_key)
        if key is None:
            return
        state = str(getattr(progress, "state", "") or "")
        if state in ("complete", "removed"):
            tab.set_card_download_state(key, "complete")
            return
        if state == "error":
            tab.set_card_failed(key)
            return
        total = getattr(progress, "total_bytes", None)
        if not total:
            tab.set_card_progress(key, None)  # 不定式
            return
        done = int(getattr(progress, "completed_bytes", 0) or 0)
        tab.set_card_progress(key, min(1.0, done / float(total)))

    def _on_grid_download_done(self, tab, result, requested_keys):
        """完成回调：``to_submit`` 是完成数；失败数 = 提交前失败 + 提交报错。"""
        self._download_busy = False
        if result is None:
            tab.set_download_activity(active=0, failed=0)
            return
        plan, idents = result
        self._works_download_bridge.set_idents(idents)
        submitted = list(getattr(plan, "to_submit", ()) or ())
        pre_failed = list(getattr(plan, "failed_pre_submit", ()) or ())
        errors = list(getattr(plan, "submission_errors", ()) or ())
        failed_count = len(pre_failed) + len(errors)
        tab.set_download_activity(active=0, failed=failed_count)
        if submitted:
            done_keys = [idents[s.identity_key] for s in submitted if s.identity_key in idents]
            if done_keys:
                # 单个入口回灌：``apply_downloaded`` 内部按新增键计数并刷组头。
                tab.apply_downloaded(done_keys)
            InfoBar.success(
                title="",
                content=qtTrId("pawchive.grid.downloaded_progress").format(
                    done=len(submitted), total=len(requested_keys)
                ),
                orient=Qt.Horizontal,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
            return
        if failed_count:
            self._on_grid_download_failed(
                tab, errors[0] if errors else qtTrId("pawchive.error.unknown_reason")
            )

    def _on_grid_download_failed(self, tab, error):
        """把技术错误翻译成**用户能照做的一句话**，堆栈只进日志。

        现场事故：代理没配时抛 ``PawchiveConfigError``，整段 traceback 被
        ``InfoBar`` 原样糊在页面上（用户截图，60+ 行），既看不懂也不知道去哪改。

        这里按异常类型分派：每类给"是什么 + 怎么做"，能跳设置的就带可点动作。
        失败后同时重探环境（``force=True``）：引擎没起来时用户当场就能看到提示。
        """
        self._download_busy = False
        tab.set_download_activity(active=0, failed=1)

        message, action_text, action = self._explain_download_error(error)
        infobar = InfoBar.error(
            title="",
            content=message,
            orient=Qt.Horizontal,
            position=InfoBarPosition.TOP,
            duration=8000 if action is None else -1,  # 带动作的常驻，别让用户没点就消失
            parent=self,
        )
        if action is not None and action_text:
            from qfluentwidgets import HyperlinkButton

            link = HyperlinkButton(FIF.SETTING, "", action_text, infobar)
            link.clicked.connect(action)
            # InfoBar 的容器是 hBoxLayout（qfluentwidgets 实现细节，vBoxLayout 不存在）。
            infobar.hBoxLayout.addWidget(link)

        # 堆栈只进日志（用户看不到，开发者查得到）。
        detail = getattr(error, "debug_text", None)
        if detail:
            try:
                from loguru import logger

                logger.error(f"[pawchive] 下载失败: {detail}")
            except Exception:  # noqa: BLE001 - 日志失败不该影响提示
                pass
        self._refresh_env_notice(force=True)

    def _explain_download_error(self, error) -> tuple[str, str, t.Callable[[], None] | None]:
        """异常 → (用户文案, 动作按钮文字, 动作)。空动作表示只提示、无法一键修复。"""
        text = str(error or "")

        # 代理未配置：最容易踩、也最容易修——必须直接给跳到设置的动作。
        if "requires proxies" in text or "PawchiveConfigError" in text:
            return (
                qtTrId("pawchive.error.proxy_missing"),
                qtTrId("pawchive.env.go_settings"),
                lambda: self._goto_settings_page("network"),
            )
        if "403" in text or "blocked" in text.lower():
            return qtTrId("pawchive.error.blocked"), qtTrId("pawchive.env.go_settings"), (
                lambda: self._goto_settings_page("network")
            )
        # 连接类：断网 / 代理没开 / DoH 不通，都可重试，指路到设置查代理。
        if any(k in text for k in ("ConnectError", "Timeout", "网络", "timed out")):
            return qtTrId("pawchive.error.network"), qtTrId("pawchive.env.go_settings"), (
                lambda: self._goto_settings_page("network")
            )
        # 其它：不再把异常类名糊给用户，只说失败了、可重试。
        return qtTrId("pawchive.error.generic"), "", None

    def _on_author_page_loaded(self, tab, page_obj):
        posts = list(getattr(page_obj, "posts", ()) or ())
        tab._page_retry_attempt = 0
        if not posts:
            # S2 空作者（整页 0 帖）≠ S7 帖级零附件（组头副行）。`empty_post` 只服务
            # S7（08 §2.3.2 / 差 3：整页空态借它会与"还没加载"抢语义），
            # 整页空走 `show_empty_author()`（内部 `pawchive.grid.empty_author`）。
            tab.grid.show_empty_author()
            tab.grid.set_loading(False)
            tab.sync_counts()
            tab.sync_page_nav()
            return
        groups = self._build_post_groups(posts)
        tab.append_page(
            groups,
            has_more=bool(getattr(page_obj, "has_more", False)),
            offset=int(getattr(page_obj, "offset", tab.grid.state.offset) or 0),
        )
        tab.grid.set_loading(False)
        # 顶栏计数/页码在 append_page 里已同步；这里再收一次错误态，避免旧错误压在
        # 新内容上（用户现场："切回 tab 还显示连不上站点"）。
        tab.grid.hide_error()
        tab.clear_error()
        # 刷新时机 1（06 §2.4.1）：每页一次批量查已下载，喂 grid.apply_downloaded。
        self._backfill_downloaded(tab, groups)
        # 时间范围选择还在补页：新到的帖子先选中，再判断要不要继续翻。
        if getattr(tab, "_date_range", None):
            self._select_loaded_range(tab)
            self._page_for_range(tab, continue_only=True)

    def _backfill_downloaded(self, tab, groups):
        """页级已下载回填：identity_key 补 identity → is_downloaded_many 批量取。

        UI 不读库、不算 sha1（06 §2.4 / 契约 §三.10）：换算全走 04 的服务层函数。
        查询与换算搬进工作线程，只有 apply_downloaded（改控件）回主线程。
        service.py 未落地（ImportError）或查询异常时静默跳过——灰化是增强，不该挡住网格。
        """
        try:
            from core.website.pawchive.service import PawchiveService
        except ImportError:
            return
        keyed = [(item.key, item.post, item.attachment) for g in groups for item in g.items]
        if not keyed:
            return

        def _task(_keyed=keyed):
            service = PawchiveService()
            try:
                idents = {
                    key: service.identity_key(post, attachment)
                    for key, post, attachment in _keyed
                }
                hits = set(service.is_downloaded_many(idents.values()))
                return [key for key, ident in idents.items() if ident in hits]
            finally:
                import asyncio

                close = getattr(service, "aclose", None)
                if callable(close):
                    try:
                        asyncio.run(close())
                    except Exception:  # noqa: BLE001
                        pass

        mgr = getattr(self, "_works_backfill_task_mgr", None)
        if mgr is None:
            mgr = self._works_backfill_task_mgr = AsyncTaskManager(self)

        def _on_ok(downloaded, _tab=tab):
            if downloaded:
                _tab.apply_downloaded(downloaded)

        # 每次回填一个独立 id：同一作者连续翻页时键长度可能相同，复用 id 会命中
        # ``AsyncTaskManager`` 的"正在运行中"警告并吞掉这一页的回填。
        self._backfill_seq = getattr(self, "_backfill_seq", 0) + 1
        mgr.execute_task(
            f"pawchive_backfill_{tab.creator_id}_{self._backfill_seq}",
            TaskConfig(
                task_func=_task,
                success_callback=_on_ok,
                error_callback=None,  # 回填失败静默：灰化是增强
                show_success_info=False,
                show_error_info=False,
                show_tooltip=False,
            ),
        )

    @staticmethod
    def _build_post_groups(posts):
        """``(post, attachment)`` → ``PawchivePostGroup``。URL/可下载判定全由服务层注入。"""
        from core.website.pawchive.service import PawchiveService

        from app.GUI.pawchive.post_adapter import build_post_group

        try:
            service = PawchiveService()
        except Exception:  # noqa: BLE001 - 构造失败（缺配置）时退化为"不可下载"占位
            service = None

        def _thumbnail(attachment):
            return service.thumbnail_url(attachment) if service is not None else None

        def _downloadable(attachment):
            return (
                service.resolve_download_url(attachment) is not None
                if service is not None
                else False
            )

        return [
            build_post_group(
                post,
                thumbnail_url=_thumbnail,
                resolve_downloadable=_downloadable,
                collapsed_limit=12,
            )
            for post in posts
        ]

    def _ensure_ready(self):
        """进入页面即检测作者库：合格消散遮罩，不合格在遮罩内给下载出口。

        新页不依赖 redis（09 §2.3-C）：遮罩语义收窄为"作者库不合格即挡"，且只覆盖内容区
        （PawchiveGateLayer 不隐藏侧边栏，用户可自由切走）。

        作者库判据只能用 pawchive_db_valid（六步：文件在 / 非 0 字节 / 只读可开 /
        有 authors 表 / 行数 > 0 / FTS 行数一致），不许裸调 fts_ready()：坏库会让
        sqlite3.connect 抛 DatabaseError，0 行库会被误判为就绪。

        遮罩只管作者库；引擎/外部服务缺失不挡内容，一律交给左下角 CustomInfoBar
        解释（见 _refresh_env_notice）——用户即使没有 aria2 也能先浏览、勾选。
        """
        if self._gate_ready:
            self._refresh_env_notice()
            return
        ok, db_reason = pawchive_db_valid(authors_db_path())
        if not ok:
            self._gate.reveal(self.rect())
            self._gate.apply_state(
                message=self._author_db_message(db_reason), show_download=True, busy=False
            )
            self._refresh_env_notice()
            return
        # 作者库合格：消散遮罩，页面可用。
        self._gate_ready = True
        self._gate.dismiss(self.rect())
        self._refresh_env_notice()

    # ---- 环境提示：缺依赖时告诉用户"缺什么/影响什么/去哪补" ----
    #: 提示条自动消失时间：够看清+点「去设置」，又不至于像常驻条一样永远占着。
    ENV_NOTICE_DURATION_MS = 12000
    #: 两次探测的最小间隔：`showEvent` 每次切回页面都会触发，不节流会反复探端口。
    #: 语义是"上次**发起**探测的时间"（不是上次拿到结果的时间）：探测可能卡在引擎锁上
    #: 好几秒，按结果计时会让节流失效，切页时反复压线程池。
    ENV_PROBE_INTERVAL_MS = 5000

    def _refresh_env_notice(self, *, force: bool = False):
        """发起一轮环境探测；**本函数不做任何 IO，立即返回**。

        必须异步：AcggrAria2Engine.is_ready() 先拿引擎 RLock、再在锁内 RPC ping，
        后台下载线程持锁时主线程调它就是等锁。这里只把两个探测函数交给 EnvProber
        的线程池，结果经排队信号回来。

        渲染时机（避免"先弹后消失"）：只有拿到结果或 3s 超时才动提示条；
        探测期间不动 UI，首次进入则什么都不弹。
        """
        prober = getattr(self, "_env_prober", None)
        if prober is None:
            return

        import time as _time

        now = _time.monotonic()
        if not force and (now - self._env_probe_at) * 1000 < self.ENV_PROBE_INTERVAL_MS:
            return
        self._env_probe_at = now
        if force:
            # 手动重探（如下载失败）视为用户需要这条提示：解除上一轮的静音。
            self._env_notice_muted.clear()

        self._env_pending = {KIND_ARIA2, KIND_REDIS}
        aria2_probe, redis_probe = self._env_probe_funcs
        generation = prober.start(aria2_probe=aria2_probe, redis_probe=redis_probe)
        # 定时器必须绑定轮次：否则上一轮的超时会在新一轮结果已到之后再触发，
        # 把刚渲染好的"正常"状态又按"不可用"画回去。过期的超时一律忽略。
        self._env_timeout_generation = generation
        self._env_probe_timeout.start()

    def _on_env_aria2(self, ok: bool):
        self._on_env_result(KIND_ARIA2, ok)

    def _on_env_redis(self, ok: bool):
        self._on_env_result(KIND_REDIS, ok)

    def _on_env_result(self, kind: str, ok: bool):
        """工作线程结果回主线程（Signal 排队连接）：落状态 + 只重绘这一条提示。"""
        self._env_pending.discard(kind)
        self._env_values[kind] = bool(ok)
        self._render_env_notice(kind, bool(ok))
        if not self._env_pending:
            self._env_probe_timeout.stop()

    def _on_env_probe_timeout(self):
        """3s 未回：按"不可用"先渲染，不继续等（引擎锁被后台长时间持有时就是这样）。

        晚到的结果仍会走 ``_on_env_result`` 把提示改回真实状态——超时只是
        "先给个保守结论"，不是放弃这一轮。

        **必须校验轮次**：``QTimer`` 是复用同一个的，若不比对 generation，上一轮
        的超时会在新一轮出结果之后才到，把刚画好的"正常"按"不可用"覆盖回去。
        """
        prober = getattr(self, "_env_prober", None)
        expected = getattr(self, "_env_timeout_generation", -1)
        if prober is not None and expected != prober.generation:
            return
        for kind in sorted(self._env_pending):
            self._render_env_notice(kind, False)
        self._env_pending.clear()

    def _render_env_notice(self, kind: str, ok: bool):
        """按探测结果弹/收左下角 ``CustomInfoBar``（**不进布局，不挡顶栏**）。

        - 缺失：左下角一条，带「去设置」，``ENV_NOTICE_DURATION_MS`` 后自动消失；
          同一 kind 只保留一条，用户关掉后本轮不再弹（否则每 5s 探一次就等于常驻）。
        - 恢复：收起该 kind 的条并解除静音——下次真的坏了还能再提示。
        """
        if ok:
            self._close_env_notice(kind)
            self._env_notice_muted.discard(kind)
            return
        if kind in self._env_notice_muted or kind in self._env_notice_bars:
            return
        if kind == KIND_ARIA2:
            content = qtTrId("pawchive.env.aria2_down")
            level = "WARNING"
            on_action = self._goto_aria2_settings
        else:
            content = qtTrId("pawchive.env.redis_down")
            level = "INFORMATION"
            on_action = self._goto_redis_settings
        self._env_notice_bars[kind] = self._show_env_notice(
            kind, content, level=level, on_action=on_action
        )
        self._place_env_notices()

    def _show_env_notice(self, kind: str, content: str, *, level: str, on_action):
        link = HyperlinkButton(
            FIF.SETTING, "", qtTrId("pawchive.env.go_settings"), parent=None
        )
        link.clicked.connect(on_action)
        # 关键：用 NONE 而非 BOTTOM_LEFT。BOTTOM_LEFT manager 的 slide 动画与
        # resize eventFilter 会把 bar 拉回父窗口底边，正好压住底栏按钮组
        # （作者表格 / 过滤规则 / 打开目录 / 页码，截图现场事故）。NONE 不注册
        # 进任何 manager，位置完全由本类 _place_env_notices 按底栏上沿计算，
        # 且不影响其它页面的 BOTTOM_LEFT toast。
        bar = CustomInfoBar.show_custom(
            title="",
            content=content,
            parent=self,
            _type=level,
            ib_pos=InfoBarPosition.NONE,
            duration=self.ENV_NOTICE_DURATION_MS,
            widgets=[link],
        )
        if bar is not None:
            bar.closedSignal.connect(
                lambda _bar=bar, _kind=kind: self._on_env_notice_closed(_kind, _bar)
            )
        return bar

    def _place_env_notices(self):
        """把本页 env 提示条叠在底栏**上沿之上**（新的在最下、旧的依次上移）。

        ``InfoBarPosition.NONE`` 不走 manager，没有动画/事件过滤器抢位；尺寸
        取 bar 当前 sizeHint（show 之后已有效）。底栏高度用 sizeHint 兜底，
        保证首次 show 布局尚未算完时也落在正确一侧。
        """
        bottom_bar_height = self.bottom_bar.sizeHint().height() or 44
        top = max(0, self.height() - bottom_bar_height - _ENV_NOTICE_BOTTOM_GAP)
        y = top
        for bar in reversed(list(self._env_notice_bars.values())):
            if bar is None:
                continue
            try:
                bar.adjustSize()
                bar.move(_ENV_NOTICE_MARGIN, max(0, y - bar.height()))
            except RuntimeError:  # bar 已被销毁
                continue
            y -= bar.height() + _ENV_NOTICE_SPACING

    def _on_env_notice_closed(self, kind: str, bar):
        """提示条关闭（用户叉掉或到时淡出）：从账本移除并静音该 kind。"""
        if self._env_notice_bars.get(kind) is bar:
            self._env_notice_bars.pop(kind, None)
            self._env_notice_muted.add(kind)
        self._place_env_notices()

    def _close_env_notice(self, kind: str):
        """依赖恢复：主动收起提示条；先移出账本，避免被记成"用户关闭"。"""
        bar = self._env_notice_bars.pop(kind, None)
        if bar is not None:
            bar.close()
        self._place_env_notices()

    def _goto_aria2_settings(self):
        self._goto_settings_page("aria2")

    def _goto_redis_settings(self):
        self._goto_settings_page("redis")

    def _goto_settings_page(self, which: str):
        """跳到「设置」页（aria2 / redis 都是同一页里的分组卡）；失败退化为 InfoBar 指引。"""
        win = self.window()
        for attr in ("_ensure_and_switch", "_nav_click_handler"):
            fn = getattr(win, attr, None)
            if not callable(fn):
                continue
            try:
                # ``_nav_click_handler`` 返回闭包；``_ensure_and_switch`` 直接吃 key。
                fn("settings")() if attr == "_nav_click_handler" else fn("settings")
                return
            except Exception:  # noqa: BLE001 - 宿主实现可能不同
                continue
        InfoBar.info(
            title="",
            content=qtTrId("pawchive.env.go_settings_hint"),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    @staticmethod
    def _author_db_message(reason: str) -> str:
        """``pawchive_db_valid`` 的 reason → 页面文案。

        三条文案一一对应六个 reason：``missing`` 单独一条（要点"去下载"）；
        ``empty`` / ``no_rows`` 都是"库是空的、要重建"；其余（``not_sqlite`` /
        ``no_authors_table`` / ``fts_mismatch``）都是"库坏了、要重下"。
        """
        if reason == DB_REASON_MISSING:
            msg = qtTrId("pawchive.author_db.missing")
        elif reason in (DB_REASON_EMPTY, DB_REASON_NO_ROWS):
            msg = qtTrId("pawchive.author_db.empty")
        else:
            msg = qtTrId("pawchive.author_db.invalid")
        # 补一句"这跟下载引擎无关"，避免用户误以为要先去修 aria2/redis。
        return msg + "\n" + qtTrId("pawchive.author_db.gate_hint")

    def _download_authors_db(self):
        """下载预构建 pawchive.db 到 conf_dir，成功后消散遮罩。"""
        self._gate.apply_state(message=qtTrId("pawchive.author_db.downloading"), show_download=False, busy=True)
        self._table_task_mgr.execute_task(
            "pawchive_db_download",
            TaskConfig(
                task_func=_download_authors_db_task,
                success_callback=self._on_db_downloaded,
                error_callback=lambda error: self._gate.apply_state(
                    message=qtTrId("pawchive.download.failed").format(error=error), show_download=True, busy=False),
                show_success_info=False, show_error_info=False,
            ),
        )

    def _on_db_downloaded(self, result):
        if result.ready:
            self._gate_ready = True
            self._gate.dismiss(self.rect())
            return
        errors = "；".join(result.errors) or qtTrId("pawchive.error.unknown_reason")
        self._gate.apply_state(message=qtTrId("pawchive.download.failed_multi").format(errors=errors), show_download=True, busy=False)

    def server_mode_switch_blockers(self) -> list[str]:
        """方法名保留（宿主聚合按 descriptor 的 ``attr_name`` 调），判定源换成下载忙碌位。"""
        if self._download_busy:
            return ["pawchive script"]
        return []

    def setupUi(self):
        """页面骨架：内容区（作者表 / 作品覆盖层）+ 贴着窗口底边的操作条。

        旧四行表单（``kwEdit`` / 擦除 / 日期 / 运行）与运行日志台已随 P5 删除：
        取数走进程内 ``PawchiveService``，反馈走网格与 ``InfoBar``，不再有
        外置子进程与它的输出面。

        **入口按钮在底栏**（2026-11-17 用户现场裁定）：作者表格 / 过滤规则 / 打开
        保存目录都收到页面底边，和当前 tab 的页码导航同一排；顶边整条让给作品
        覆盖层。底栏必须是**独立贴边 QFrame**，不能是浮在内容上的圆角卡片——
        否则读成"后贴上去的一条便签"。
        """
        self.main_layout = VBoxLayout(self)
        # 贴边：默认 9px 边距会把底栏托起来，四周露缝。
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.bottom_bar = QFrame(self)
        self.bottom_bar.setObjectName("PawchiveBottomBar")
        bar_row = QHBoxLayout(self.bottom_bar)
        bar_row.setContentsMargins(12, 0, 12, 0)
        bar_row.setSpacing(4)
        bar_row.setAlignment(Qt.AlignVCenter)

        # 页面级入口不该是整页最重的按钮：透明命令按钮 + 32px，和内容区同一套节奏。
        self.showTbBtn = TransparentPushButton(
            FIF.BOOK_SHELF, qtTrId("pawchive.author_db.table_title"), self.bottom_bar
        )
        self.showTbBtn.setObjectName("PawchiveBottomAction")
        self.showTbBtn.setFixedHeight(32)
        self.showTbBtn.clicked.connect(self.show_pawchive_table)
        self.extraFilterBtn = TransparentPushButton(
            FIF.FILTER, qtTrId("pawchive.filter.title"), self.bottom_bar
        )
        self.extraFilterBtn.setObjectName("PawchiveBottomAction")
        self.extraFilterBtn.setFixedHeight(32)
        self.extraFilterBtn.clicked.connect(self.show_extra_filter)

        self.openBtn = ToolButton(FIF.FOLDER, self.bottom_bar)
        self.openBtn.setFixedSize(32, 32)
        self.openBtn.setToolTip(qtTrId("pawchive.open_dir.button"))
        self.openBtn.clicked.connect(self._open_save_dir)
        self._refresh_open_btn_hint()

        # 页码导航：interface 全周期唯一一份（对齐 danbooru 单实例架构），
        # tab 只供状态。没有作品 tab 时整组隐藏，不出现"第 1 页 + 两颗灰箭头"。
        self._nav_divider = QFrame(self.bottom_bar)
        self._nav_divider.setObjectName("PawchiveBottomDivider")
        self._nav_divider.hide()
        self.page_nav_holder = QWidget(self.bottom_bar)
        self.page_nav_slot = QHBoxLayout(self.page_nav_holder)
        self.page_nav_slot.setContentsMargins(0, 0, 0, 0)
        self.page_nav_slot.setSpacing(0)
        self.page_nav_holder.hide()
        self.page_nav = PawchivePageNav(self.page_nav_holder)
        self.page_nav_slot.addWidget(self.page_nav)
        self.page_nav.page_requested.connect(self._on_page_nav_requested)
        self.page_nav.hide()

        bar_row.addWidget(self.showTbBtn)
        bar_row.addWidget(self.extraFilterBtn)
        bar_row.addStretch(1)
        bar_row.addWidget(self.openBtn)
        bar_row.addWidget(self._nav_divider)
        bar_row.addWidget(self.page_nav_holder)

        self.main_layout.addStretch(1)
        self.main_layout.addWidget(self.bottom_bar)
        self.bottom_bar.setStyleSheet(build_bottom_bar_stylesheet())

    def _apply_bottom_bar_theme(self, *_args) -> None:
        """切主题时刷新底栏（页面级 QSS 只在构造时落一次，不订阅就会漂色）。"""
        try:
            self.bottom_bar.setStyleSheet(build_bottom_bar_stylesheet())
        except Exception:  # noqa: BLE001 - 主题文件缺失不该挡住页面
            pass
        nav = getattr(self, "page_nav", None)
        if nav is not None:
            nav.apply_theme()


    def _pawchive_save_path(self) -> p.Path:
        """保存目录：``conf.pawchive.sv_path`` 没设过 → 默认根目录。

        与下载侧 ``PawchiveServiceConfig.from_conf`` 的口径一致（``or DEFAULT``），
        所以"打开保存目录"打开的就是文件真正落地的地方。**绝不把 ``None`` 交给
        ``Path()``**：默认配置里 ``pawchive`` 段只有 ``cookie``，从没选过目录时
        ``.get('sv_path')`` 就是 ``None``，裸 ``Path(None)`` 会抛 ``TypeError``，
        槽函数静默炸掉 → 用户看到的是"按钮点了没反应"。
        """
        from core.script import conf as script_conf

        section = getattr(script_conf, "pawchive", None) or {}
        raw = str(section.get("sv_path") or "").strip()
        return p.Path(raw) if raw else p.Path(DEFAULT_PAWCHIVE_SAVE_PATH)

    def _refresh_open_btn_hint(self) -> None:
        """tooltip 跟着配置走：用户改完存储路径切回本页，悬停看到的就是新路径。"""
        label = qtTrId("pawchive.open_dir.button")
        try:
            self.openBtn.setToolTip(f"{label}\n{self._pawchive_save_path()}")
        except Exception:  # noqa: BLE001 - 纯提示，取不到值不该影响按钮可用性
            self.openBtn.setToolTip(label)

    def _open_save_dir(self) -> None:
        """打开保存目录；**目录不存在就如实告知，绝不替用户造目录**。

        保存路径由用户在 GUI 目录选择器里定，配置为空时回退的是 DEFAULT_PAWCHIVE_SAVE_PATH；
        自动 mkdir 会凭空造一个用户从没选过的目录并掩盖"盘符没接/目录被删/配错"。

        所以：不存在 → 明说路径 + 指路到设置页的目录选择器；存在 → 异步打开。
        必须异步：curr_os.open_folder 在 Windows 走 os.startfile，会阻塞主线程数百毫秒。
        """
        path = self._pawchive_save_path()
        if not path.is_dir():
            InfoBar.warning(
                title="", content=qtTrId("pawchive.open_save_dir.missing").format(path=str(path)),
                orient=Qt.Horizontal, position=InfoBarPosition.TOP,
                duration=6000, parent=self
            )
            return
        if self._table_task_mgr is None:
            self._open_folder_sync(path)
            return
        self._table_task_mgr.execute_task(
            "pawchive_open_save_dir",
            TaskConfig(
                task_func=lambda _p=path: curr_os.open_folder(_p),
                success_callback=None,
                error_callback=self._on_open_dir_failed,
                show_success_info=False,
                show_error_info=False,
                show_tooltip=False,
            ),
        )

    def _open_folder_sync(self, path) -> None:
        """无任务管理器时（``parent is None``，仅探针/无宿主场景）的退化路径。"""
        try:
            curr_os.open_folder(path)
        except OSError as exc:
            self._on_open_dir_failed(str(exc))

    def _on_open_dir_failed(self, error) -> None:
        InfoBar.error(
            title="", content=qtTrId("pawchive.open_save_dir.failed").format(error=error),
            orient=Qt.Horizontal, position=InfoBarPosition.TOP,
            duration=5000, parent=self
        )

    def show_extra_filter(self):
        CustomFlyout.make(
            view=FilterView(self), target=self.extraFilterBtn, parent=self,
            # PULL_UP = 浮窗整体落在按钮上侧、水平居中，自下而上滑入；
            # 旧 SLIDE_LEFT 会跑到按钮左侧，过滤规则浮窗应贴着底栏按钮向上展开。
            aniType=FlyoutAnimationType.PULL_UP
        )

    def show_pawchive_table(self):
        if self.table_inited and self.table_window is not None:
            self._present_table_window()
            return

        authors_db = PawchiveAuthorsDb(authors_db_path())

        if self._table_task_mgr is None:
            # 无宿主（探针场景）：没有线程池可用，只能同步走完。
            if not authors_db.fts_ready():
                authors_db.ensure_fts(force=True)
            self._open_table_window(authors_db)
            return

        if self._table_task_mgr.is_task_running(_PAWCHIVE_FTS_TASK_ID):
            return

        self.showTbBtn.setDisabled(True)

        # fts_ready() 也要进线程：它是一次 sqlite3.connect + 5 条查询，恰好卡在
        # 点击→出窗之间。判定与建索引合并成一个任务：worker 里先判，合格就直接
        # 返回，不合格才 ensure。
        def _build_fts(_db: PawchiveAuthorsDb = authors_db):
            if not _db.fts_ready():
                _db.ensure_fts(force=True)
            return _db

        config = TaskConfig(
            task_func=_build_fts,
            success_callback=self._on_fts_ready,
            error_callback=self._on_fts_failed,
            tooltip_title=qtTrId("pawchive.author_db.preparing"),
            tooltip_content=qtTrId("pawchive.author_db.building_index"),
            show_success_info=False,
            show_error_info=True,
            tooltip_parent=self,
        )
        self._table_task_mgr.execute_task(_PAWCHIVE_FTS_TASK_ID, config)

    def _on_fts_ready(self, authors_db: PawchiveAuthorsDb):
        self.showTbBtn.setDisabled(False)
        self._open_table_window(authors_db)

    def _on_fts_failed(self, error_message: str):
        self.showTbBtn.setDisabled(False)
        InfoBar.error(
            title="",
            content=qtTrId("pawchive.author_db.index_failed").format(error_message=error_message),
            orient=Qt.Horizontal,
            position=InfoBarPosition.TOP,
            duration=6000,
            parent=self,
        )

    def _open_table_window(self, authors_db: PawchiveAuthorsDb):
        self.table_window = PawchiveTableView(authors_db, self)
        # 关窗唯一路径 = 类内 closeBtn.clicked → self.hide()。旧代码在这里又接了
        # close + closed→hide：close 会走 closeEvent 落盘/取消任务，和"复用窗口"
        # 语义冲突，且 hide 被调两次；删掉重复接线，只保留类内那一条。
        self.table_inited = True
        self._present_table_window()

    def _present_table_window(self):
        window_pos = self.parent_window.pos()
        self.table_window.move(window_pos.x(), window_pos.y() + 10)
        self.table_window.show()
        self.table_window.raise_()
        self.table_window.activateWindow()
