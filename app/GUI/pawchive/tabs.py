"""pawchive L2 作者作品页（P1）：tab 条 + 单作者网格 tab。

组件树::
    PawchiveTabsBar
    ├─ tab_strip: tab_bar (expanding) | toolbar (QFrame#PawchiveGridToolbar)
    │             ↑ 计数 / 排序 / 缩放 / 选择 / 下载 单例，作用于 active tab
    └─ stack → PawchiveGridTab × N (一个作者一个 tab) → PawchiveGridWidget（瀑布流）

页码导航由 PawchiveInterface 持有一份（tab 只发 nav_state_changed）；工具栏按钮组由
PawchiveTabsBar 持有一份，切 tab 刷新控件状态。依赖：PySide6 + qfluentwidgets + ui.* + 本包
模块，不 import app.GUI.danbooru.*。主题钩子 theme_mgr.subscribe(self._apply_theme)。
"""

import typing as t

from PySide6 import QtCore
from PySide6.QtCore import QSignalBlocker, Qt, Signal, qtTrId
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox,
    FluentIcon as FIF,
    Flyout,
    FlyoutAnimationManager,
    FlyoutAnimationType,
    FlyoutViewBase,
    PrimaryToolButton,
    PushButton,
    TabCloseButtonDisplayMode,
    TextBrowser,
    ToolButton,
    TransparentToolButton,
)

from ui.core.theme import theme_mgr
from ui.qfluent.components import CountBadge, CustomFlyout, RangeCalendar
from ui.qfluent.components.accent_tabs import AccentTabBar, apply_tab_strip_style

from .grid import PawchiveGridWidget
from .post_adapter import PawchivePostGroup

if t.TYPE_CHECKING:  # 只做注解，运行期不 import（浮窗懒创建）
    from .detail_preview import PawchiveViewerController
from .style import (
    DEFAULT_PAWCHIVE_ZOOM_INDEX,
    PAWCHIVE_CARD_METRICS,
    PawchiveUiPalette,
    build_grid_stylesheet,
    build_page_nav_stylesheet,
    build_selection_colors,
)

#: 会话内缩放档（设计稿要求落 ``pawchive_cfg.grid_zoom_index``；该 ConfigItem 由
#: core 侧 owner 追加，本模块先按鸭子类型读，缺失时退回会话默认值，不写 core/）。
ZOOM_CONFIG_ATTR = "grid_zoom_index"

#: 工具栏单行控件统一高度（px）。口径照 danbooru 工具行的「齐高」：整行控件同高，
#: 高度直接取放大/缩小按钮组这个 vbox 的高（``TOOLBAR_ZOOM_BUTTON_HEIGHT × 2``）。
#: 不齐高时 30 / 28 / 32 / 44 交错，行内上下沿对不齐，视觉上是"凹凸"的锯齿行。
TOOLBAR_CONTROL_HEIGHT = 44
#: 缩放组每枚按钮高度：两枚叠满一行 = ``TOOLBAR_CONTROL_HEIGHT``（vbox 间距 0）。
TOOLBAR_ZOOM_BUTTON_HEIGHT = TOOLBAR_CONTROL_HEIGHT // 2


#: 本模块经 ``_tr_or`` 使用的全部 i18n 键。**这份元组不是装饰**：``_tr_or`` 用变量
#: 调 ``qtTrId``，而 ``res/i18n/build.py`` 的 AST 扫描只认字面量实参，或"赋值右侧
#: tuple/list/dict 里的 id 形字符串"这条间接通道。没有它，这些键在 ``--check`` 里
#: 会被判 stale，P4 一跑 ``--sync`` 就会把它们从表里删掉。新增/改名键时必须同步这里。
_TABS_I18N_KEYS = (
    "pawchive.download.nothing_selected",
    "pawchive.grid.count",
    "pawchive.grid.count_loading",
    # 选择浮窗：日期范围触发器 + ✓ 提交
    "pawchive.action.date_range",
    "pawchive.action.apply_range",
    # 帖头详情浮窗
    "pawchive.post.detail",
    "pawchive.post.no_content",
    "pawchive.post.tags",
    "pawchive.grid.download_selected",
    "pawchive.grid.select_visible",
    "pawchive.grid.sort_newest",
    "pawchive.grid.sort_oldest",
)


def _tr_or(key: str, fallback: str) -> str:
    """``qtTrId`` 缺键时回退成 id 本身；此时用更短的兜底串（id 串会撑变形工具栏）。"""
    text = qtTrId(key)
    return fallback if text == key else text


def default_preview_fetcher(url: str) -> bytes:
    """缩略图取字节的唯一出口：``core.website.pawchive.api.fetch_bytes``。

    本函数只在 loader 的线程池里被调用（禁 GUI 主线程，契约 §三.10）。
    ``api.py`` 未落地时降级为抛错（卡片显示 ``preview_error`` 占位），
    **不在 UI 侧自建 UA/httpx**（PB-6：pawchive 目录零 UA/HTTP 常量）。
    """
    try:
        from core.website.pawchive.api import fetch_bytes
    except ImportError as exc:  # P0 尚未落地
        raise RuntimeError("core.website.pawchive.api.fetch_bytes unavailable") from exc
    return fetch_bytes(url, referer=f"https://{_pawchive_domain()}/")


def _pawchive_domain() -> str:
    try:
        from core.website.pawchive import domain
    except Exception:  # noqa: BLE001
        return "pawchive.pw"
    return str(domain or "pawchive.pw")


def _zoom_config():
    try:
        from core.config.qc import pawchive_cfg
    except Exception:  # noqa: BLE001 - 配置缺失不该挡住界面
        return None
    return getattr(pawchive_cfg, ZOOM_CONFIG_ATTR, None)


def load_zoom_index() -> int:
    item = _zoom_config()
    value = getattr(item, "value", DEFAULT_PAWCHIVE_ZOOM_INDEX) if item is not None else (
        DEFAULT_PAWCHIVE_ZOOM_INDEX
    )
    try:
        index = int(value)
    except (TypeError, ValueError):
        index = DEFAULT_PAWCHIVE_ZOOM_INDEX
    return max(0, min(len(PAWCHIVE_CARD_METRICS) - 1, index))


def save_zoom_index(index: int) -> None:
    item = _zoom_config()
    if item is None:
        return
    try:
        item.value = int(index)
    except Exception:  # noqa: BLE001 - 只影响下次启动的档位记忆
        pass


class PawchivePageNav(QFrame):
    """页码导航 FAB：``‹ 3 ›``。自持实现，样式在 pawchive.qss 的 page_nav 段。

    2026-11-17 修正：中间那颗页码**不是按钮**（原来用 ``PushButton`` + 永久
    ``setEnabled(False)``，三个控件在截图里全是灰的、看起来坏了）。页码只是状态，
    用 ``QLabel``；``‹``/``›`` 用透明图标按钮，由 ``has_more_results`` 决定是否可点，
    不再拿 ``total_pages is None`` 当作"永远有下一页"。
    """

    page_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PawchivePageNav")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.prev_btn = TransparentToolButton(FIF.LEFT_ARROW, self)
        self.prev_btn.setFixedSize(28, 28)
        self.prev_btn.setIconSize(QtCore.QSize(14, 14))
        self.prev_btn.clicked.connect(lambda: self._jump(-1))
        self.label = QLabel("1", self)
        self.label.setObjectName("PawchivePageLabel")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMinimumWidth(46)
        self.next_btn = TransparentToolButton(FIF.RIGHT_ARROW, self)
        self.next_btn.setFixedSize(28, 28)
        self.next_btn.setIconSize(QtCore.QSize(14, 14))
        self.next_btn.clicked.connect(lambda: self._jump(1))
        for widget in (self.prev_btn, self.label, self.next_btn):
            layout.addWidget(widget)
        self._page = 1
        self._has_more = False
        self._sync()
        self.setVisible(False)

    def set_state(self, *, page: int, has_more: bool = False, loading: bool = False):
        self._page = max(1, int(page))
        self._has_more = bool(has_more)
        self._sync(loading=loading)

    def _sync(self, loading: bool = False):
        self.label.setText(str(self._page))
        self.prev_btn.setEnabled(self._page > 1)
        # 懒加载下 has_more 可能不准：下一页永远可点，边界交给 _jump / 宿主。
        self.next_btn.setEnabled(True)
        # 只有一页且没有下一页时整组隐藏：不要出现"1 + 两颗灰箭头"这种坏控件。
        self.setVisible(self._page > 1 or self._has_more or loading)

    def _jump(self, delta: int):
        target = self._page + int(delta)
        if target < 1:
            return
        self.page_requested.emit(target)

    def apply_theme(self):
        self.setStyleSheet(build_page_nav_stylesheet())


class PawchiveSelectScopeView(FlyoutViewBase):
    """「选择」浮窗：第一行全选本页可见，第二行日期范围 + ✓ 提交。

    为什么合成一个浮窗：两个动作都是"选择"，散在工具栏上会挤掉缩放与下载；
    收进一个图标后工具栏只占 26px，且时间范围这种低频操作不再暴露在常用面上。

    日期范围用自研 ``RangeCalendar``（社区版没有 Pro 的 ``RangeCalendarPicker``）：
    一个按钮显示当前区间，点开在同一浮窗内展开月历；**选完两端不立刻发请求**，
    由 ✓ 按钮提交——避免用户还在点第二个日期时就触发跨页补选。
    """

    closed = Signal()
    select_visible_requested = Signal()
    date_range_changed = Signal(object, object)  # (QDate start, QDate end)
    #: 浮窗内容高度变了（月历展开/收起）→ 让 Flyout.adjustSize() 跟上。
    size_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 12, 16, 12)
        self._layout.setSpacing(8)

        # row 1：全选本页可见（占满整行，点哪儿都行）
        self.select_visible_btn = PushButton(
            FIF.CHECKBOX, _tr_or("pawchive.grid.select_visible", "Select visible"), self
        )
        self.select_visible_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.select_visible_btn.clicked.connect(self._on_select_visible)
        self._layout.addWidget(self.select_visible_btn)

        # row 2：日期范围触发器 + 提交按钮。
        range_row = QHBoxLayout()
        range_row.setSpacing(6)
        self.range_btn = PushButton(FIF.CALENDAR, "", self)
        self.range_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.range_btn.setToolTip(_tr_or("pawchive.action.date_range", "Date range"))
        self.range_btn.clicked.connect(self._toggle_calendar)
        self.ensureBtn = ToolButton(FIF.ACCEPT, self)
        self.ensureBtn.setToolTip(
            _tr_or("pawchive.action.apply_range", "Apply date range")
        )
        self.ensureBtn.clicked.connect(self._on_ensure_range)
        range_row.addWidget(self.range_btn, 1)
        range_row.addWidget(self.ensureBtn, 0)
        self._layout.addLayout(range_row)

        # 月历默认收起；两端齐了才回填按钮文字，点 ✓ 才发 date_range_changed。
        self.range_calendar = RangeCalendar(self)
        self.range_calendar.hide()
        self.range_calendar.rangeChanged.connect(self._on_calendar_range)
        self._layout.addWidget(self.range_calendar)

        default_start = QtCore.QDate.currentDate().addMonths(-1)
        default_end = QtCore.QDate.currentDate()
        self.range_calendar.set_range(default_start, default_end)
        self._set_range_text(default_start, default_end)

    # ---- 交互 ----
    def _toggle_calendar(self):
        expanded = not self.range_calendar.isVisible()
        self.range_calendar.setVisible(expanded)
        self.range_btn.setProperty("calendarOpen", bool(expanded))
        self.range_btn.style().unpolish(self.range_btn)
        self.range_btn.style().polish(self.range_btn)
        self.size_changed.emit()

    def _on_calendar_range(self, start, end):
        self._set_range_text(start, end)

    def _on_select_visible(self):
        self.select_visible_requested.emit()
        self.closed.emit()

    def _on_ensure_range(self):
        start, end = self.range_calendar.range()
        if not (start.isValid() and end.isValid()):
            return
        self.date_range_changed.emit(start, end)
        self.closed.emit()

    def _set_range_text(self, start, end):
        full_text = f"{start.toString('yyyy-MM-dd')} → {end.toString('yyyy-MM-dd')}"
        # 同年只显示 MM-dd：完整年份在月历抬头和 tooltip 里都有，按钮不必撑到 400px。
        if start.year() == end.year():
            shown = f"{start.toString('MM-dd')} → {end.toString('MM-dd')}"
        else:
            shown = full_text
        self.range_btn.setText(shown)
        self.range_btn.setToolTip(f"{_tr_or('pawchive.action.date_range', 'Date range')}: {full_text}")

    def selected_range(self):
        """当前待提交区间（测试/探针用）。"""
        return self.range_calendar.range()


class PawchivePostDetailView(FlyoutViewBase):
    """帖内容浮窗：一个常驻 ``TextBrowser``，每次只换 HTML（不重复创建）。

    必须是 HTML 渲染：帖正文本身是 HTML（FANBOX 的 ``<p>`` / ``<br>``），用 setText 会把
    标签原样显示出来。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        self.browser = TextBrowser(self)
        self.browser.setReadOnly(True)
        self.browser.setOpenExternalLinks(True)
        self.browser.setMinimumWidth(380)
        self.browser.setMinimumHeight(220)
        self.browser.setMaximumHeight(320)
        layout.addWidget(self.browser)

    def set_text(self, text: str):
        self.browser.setHtml(text or "")


class PawchiveGridTab(QFrame):
    """一个作者一个 tab：工具栏 + 网格（页码导航由宿主持有）。"""

    #: tab 状态变了（翻页 / 加载态变化）→ 宿主重读本 tab 的 grid.state 刷新页码导航。
    #: 无参数 trigger 语义：宿主直接从 tab.grid.state 读，tab 不持有 nav 控件。
    nav_state_changed = Signal()
    #: 工具栏状态变了（计数 / 下载按钮 / badge）→ tabsbar 用 active tab 刷新单例工具栏。
    toolbar_state_changed = Signal()
    request_next_page = Signal()
    request_page = Signal(int)
    request_close = Signal()
    request_open_detail = Signal(object)
    #: 07 §2.9 契约名：卡单击缩略图 → ``(CardKey)``。与 ``request_open_detail`` 同源
    #: 同值（后者是 05 稿的历史名，保留以免动网格侧信号链）。
    detail_opened = Signal(object)
    download_requested = Signal(object)
    retry_requested = Signal()
    #: 浮窗里选了时间范围 → (QDate start, QDate end)。宿主负责按范围选并自动翻页。
    date_range_requested = Signal(object, object)
    #: 本 tab 的浮窗被中键关闭（tab 条用它武装跨 tab 防抖时间戳）。
    middle_click_closed = Signal()

    def __init__(
        self,
        *,
        author_name: str,
        service: str = "",
        creator_id: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.author_name = str(author_name or "")
        self.service = str(service or "")
        self.creator_id = str(creator_id or "")
        self.setObjectName("PawchiveGridTab")
        self.zoom_index = load_zoom_index()
        self._loaded_groups: list[PawchivePostGroup] = []
        self._sort_desc = True
        #: 06 §2.7 徽标三口径中的两个非选中口径（在途 / 失败文件数）。
        self._active_count = 0
        self._failed_count = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 8)
        layout.setSpacing(8)
        self.grid = PawchiveGridWidget(
            card_metrics=PAWCHIVE_CARD_METRICS[self.zoom_index],
            preview_fetcher=default_preview_fetcher,
            parent=self,
        )
        layout.addWidget(self.grid, 1)

        self.grid.selection_count_changed.connect(self._on_selection_count_changed)
        self.grid.request_next_page.connect(self.request_next_page.emit)
        self.grid.request_open_detail.connect(self._on_card_detail_requested)
        self.grid.request_close.connect(self.request_close.emit)
        self.grid.download_requested.connect(self.download_requested.emit)
        self.grid.retry_requested.connect(self.retry_requested.emit)
        self.grid.group_detail_requested.connect(self._on_group_detail_requested)

        # 工具栏按钮组已上移到 PawchiveTabsBar 的 tab_strip（单例，作用于 active tab）。
        # 徽标同样挂在 tabsbar：CountBadge 锚点按 target 按钮坐标算、parent 决定坐标系，
        # 留在 tab 上会拿到错误坐标。tab 状态变化只发信号，由 tabsbar 刷新徽标。
        self.apply_theme()

    # ---- 工具栏（按钮组已上移到 PawchiveTabsBar） ----
    def _on_select_scope_clicked(self, anchor):
        """打开「选择」浮窗；``anchor`` 是 tabsbar 上的「选择」按钮。

        定位**全部交给 qfluentwidgets**：``target`` 传按钮控件 + ``DROP_DOWN``，
        就是 ``FastCalendarPicker`` 的同款用法（管理器把浮窗挂在按钮下方并夹进屏幕）。
        **不要再自己算 QPoint**——旧实现把按钮右上角当浮窗左上角，浮窗整体跑到按钮
        右侧，窄窗口还会越界。
        """
        view = PawchiveSelectScopeView(self)
        view.select_visible_requested.connect(self.grid.select_visible)
        view.date_range_changed.connect(self.date_range_requested.emit)
        flyout = CustomFlyout.make(
            view, anchor, self.window(),
            aniType=FlyoutAnimationType.DROP_DOWN,
        )

        def _resize_flyout():
            try:
                flyout.adjustSize()
            except RuntimeError:  # 浮窗已关闭并 deleteLater
                pass

        view.size_changed.connect(_resize_flyout)

    def _on_selection_count_changed(self, _count: int):
        self._sync_download_button()

    # ---- L3 详情浮窗（07 §2.6 / §2.9） ----
    @property
    def result_list(self) -> list[PawchivePostGroup]:
        """已加载结果序列（翻页序列来源，07 契约 7 ②）：只读、不触发请求。"""
        return list(self._loaded_groups)

    def viewer_controller(self) -> "PawchiveViewerController":
        """懒创建控制器：浮窗与控制器都是会话级单例（构造一次、hide 复用）。"""
        controller = getattr(self, "_viewer_controller", None)
        if controller is None:
            from .detail_preview import PawchiveViewerController
            from .viewer import PawchiveImageViewer

            self._viewer = PawchiveImageViewer(self)
            # 浮窗中键关闭 → tab 条武装防抖（挡掉弹起的第二次 release 关 tab）。
            self._viewer.middle_click_closed.connect(self.middle_click_closed.emit)
            controller = PawchiveViewerController(self._viewer, parent=self)
            self._viewer_controller = controller
        return controller

    def _on_card_detail_requested(self, key):
        """卡单击 → 打开浮窗；同时把既有信号名 request_open_detail 转发出去。"""
        self.request_open_detail.emit(key)
        self.detail_opened.emit(key)
        self.viewer_controller().open_key(self, key)

    # ---- 帖内容浮窗（一个 tab 只建一份 browser + flyout）----
    def _ensure_detail_flyout(self):
        flyout = getattr(self, "_detail_flyout", None)
        if flyout is not None:
            return flyout
        view = PawchivePostDetailView(self)
        flyout = Flyout(view, self.window(), isDeleteOnClose=False)
        self._detail_view = view
        self._detail_flyout = flyout
        return flyout

    def _on_group_detail_requested(self, group, anchor):
        """组头详情按钮：复用同一个 TextBrowser，只 setText + 按按钮定位。

        浮窗不得越过 AcggrMainWindow 右边界：``FlyoutAnimationManager`` 只保证
        不超出屏幕，主窗比屏幕窄时贴右侧的组头按钮会把浮窗顶出去。这里在管理器的
        结果上再夹一次到宿主窗口矩形内，并按可用宽度收紧内容。
        """
        flyout = self._ensure_detail_flyout()
        self._detail_view.set_text(self._post_detail_text(group))
        host = self.window()
        available_width = 520
        if host is not None:
            available_width = max(260, host.width() - 24)
        self._detail_view.setMaximumWidth(available_width)
        flyout.adjustSize()
        position = FlyoutAnimationManager.make(
            FlyoutAnimationType.DROP_DOWN, flyout
        ).position(anchor)
        if host is not None:
            host_rect = host.rect()
            origin = host.mapToGlobal(host_rect.topLeft())
            right_limit = origin.x() + host_rect.width() - flyout.width()
            bottom_limit = origin.y() + host_rect.height() - flyout.height()
            position = QtCore.QPoint(
                min(max(position.x(), origin.x()), max(origin.x(), right_limit)),
                min(max(position.y(), origin.y()), max(origin.y(), bottom_limit)),
            )
        flyout.exec(position, FlyoutAnimationType.DROP_DOWN)

    @staticmethod
    def _post_detail_text(group) -> str:
        """帖内容 → 可直接 ``setHtml`` 的片段（正文按 HTML 渲染，元信息转义）。"""
        import html as _html

        items = list(getattr(group, "items", None) or ())
        post = getattr(items[0], "post", None) if items else None
        raw = dict(getattr(post, "raw", None) or {})
        body = ""
        for key in ("content", "description", "body", "text"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                body = value.strip()
                break
        title = str(getattr(post, "title", "") or group.title or "")
        parts: list[str] = []
        if title:
            parts.append(f"<h3 style='margin:0 0 4px 0;'>{_html.escape(title)}</h3>")
        parts.append(
            "<p style='margin:0 0 6px 0; color:#8a8a8a;'>"
            + _html.escape(
                _tr_or("pawchive.grid.post_meta", "{published} · {count}").format(
                    published=group.published or "-", count=group.count
                )
            )
            + "</p>"
        )
        tags = str(getattr(post, "tags", "") or "").strip()
        if tags:
            parts.append(
                "<p style='margin:0 0 8px 0;'><b>"
                + _html.escape(_tr_or("pawchive.post.tags", "Tags"))
                + ":</b> "
                + _html.escape(tags)
                + "</p>"
            )
        if body:
            # 正文是远端 HTML：QTextDocument 不执行脚本，但可能带 <style> 影响整篇；
            # 只做最小清理，保留排版标签（用户要看到渲染后的 <p>，不是标签源码）。
            body_html = body
            if "<" not in body_html:
                body_html = _html.escape(body_html).replace("\n", "<br>")
            parts.append(f"<div>{body_html}</div>")
        else:
            parts.append(
                "<p style='color:#8a8a8a;'>"
                + _html.escape(_tr_or("pawchive.post.no_content", "No text content"))
                + "</p>"
            )
        return "".join(parts)

    def _sync_download_button(self):
        """下载按钮/badge 由 tabsbar 的单例工具栏呈现，tab 只发刷新通知。"""
        self.toolbar_state_changed.emit()

    def _sync_badge(self, *, selected_count: t.Optional[int] = None):
        """保留入口兼容调用方；徽标实际刷新在 ``tabsbar.sync_download_badge(tab)``。"""
        self.toolbar_state_changed.emit()

    def selected_download_count(self) -> int:
        """待下载选中数（减已下），供 tabsbar 单例工具栏读取。"""
        return len(self.grid.selected_keys() - set(self.grid.downloaded_keys))

    def download_badge_state(self) -> tuple[int, str]:
        """``(count, color)``；``count==0`` → tabsbar 隐藏徽标。

        06 §2.7 三口径优先级：``failed > active > selected``。
        """
        selected = self.selected_download_count()
        if self._failed_count > 0:
            return self._failed_count, "#C42B1C"
        if self._active_count > 0:
            return self._active_count, "#409EFF"
        if selected > 0:
            return selected, "#409EFF"
        return 0, "#409EFF"

    def download_tool_tip(self) -> str:
        if (self.selected_download_count() == 0
                and not self._active_count and not self._failed_count):
            return _tr_or("pawchive.download.nothing_selected", "")
        return ""

    # ---- 下载态（06 §2.2 / §2.7，由 download controller 调用） ----
    def set_download_activity(self, *, active: int = 0, failed: int = 0):
        self._active_count = max(0, int(active))
        self._failed_count = max(0, int(failed))
        self._sync_badge()

    def set_card_progress(self, key, ratio):
        self.grid.set_card_progress(key, ratio)

    def set_card_download_state(self, key, state):
        self.grid.set_card_download_state(key, state)

    def set_card_failed(self, key, error_message: str = ""):
        self.grid.set_card_failed(key, error_message)

    def apply_downloaded(self, keys) -> int:
        """页级回填入口（06 §2.4 刷新时机 1/4）：返回新命中数。"""
        hits = self.grid.apply_downloaded(keys)
        if hits:
            self._sync_download_button()
        return hits

    def flush_download_dirty(self) -> int:
        """刷新时机 2/3：激活 / showEvent / 完成事件后调用。"""
        hits = self.grid.flush_download_dirty()
        if hits:
            self._sync_download_button()
        return hits

    def _on_download_clicked(self):
        # 三条前置（06 §2.5）：减已下、减无 URL，空集静默返回。
        keys = set(self.grid.selected_keys()) & self.grid.selectable_keys()
        keys -= set(self.grid.downloaded_keys)
        if keys:
            self.download_requested.emit(keys)

    def showEvent(self, event):
        super().showEvent(event)
        # 刷新时机 3（06 §2.4.1）：后台 tab 被激活时清脏集（zoom-pattern）。
        self.flush_download_dirty()

    # ---- 排序（UI 本地排：不向服务层要 sort 参数，契约 §三.12） ----
    def _on_sort_changed(self, index: int):
        desc = index == 0
        if desc == self._sort_desc:
            return
        self._sort_desc = desc
        self._rebuild()

    def _sorted_groups(self) -> list[PawchivePostGroup]:
        return sorted(
            self._loaded_groups,
            key=lambda group: (group.published or "", group.post_id),
            reverse=self._sort_desc,
        )

    def _rebuild(self):
        if not self._loaded_groups:
            return
        groups = self._sorted_groups()
        self.grid.reset()
        self.grid.append_page(groups, has_more=True, offset=0)

    # ---- 缩放 ----
    def apply_zoom(self, index: int):
        index = max(0, min(len(PAWCHIVE_CARD_METRICS) - 1, int(index)))
        if index == self.zoom_index:
            return
        self.zoom_index = index
        metrics = PAWCHIVE_CARD_METRICS[index]
        self.grid.card_metrics = metrics
        for card in self.grid.card_widgets.values():
            card.apply_metrics(metrics, refresh_preview=False)
        self.grid._refresh_grid_layout()
        self.grid.preview_loader.set_edge(metrics.preview_edge)
        self.grid._request_visible_previews()
        save_zoom_index(index)

    # ---- 数据 ----
    def append_page(
        self, groups: t.Sequence[PawchivePostGroup], *, has_more: bool, offset: int
    ) -> int:
        self._loaded_groups.extend(groups)
        inserted = self.grid.append_page(groups, has_more=has_more, offset=offset)
        self.sync_counts()
        self.sync_page_nav()
        return inserted

    def reset(self):
        self._loaded_groups.clear()
        self.grid.reset()
        self.sync_counts()
        self.sync_page_nav()

    def count_text(self) -> str:
        """计数文案；加载中且无内容时区分「加载中…」，不写「0 / 0」。

        由 tabsbar 单例工具栏读取（计数控件已上移，tab 不再持有 label）。
        """
        if self.grid.state.loading and self.grid.state.post_count == 0:
            return _tr_or("pawchive.grid.count_loading", "Loading…")
        return _tr_or("pawchive.grid.count", "{post_count} / {image_count}").format(
            post_count=self.grid.state.post_count,
            image_count=self.grid.state.image_count,
        )

    def sync_counts(self):
        self.toolbar_state_changed.emit()

    def sync_page_nav(self):
        #: 只发状态信号：宿主持有的那一份 nav 直接从本 tab.grid.state 读，tab 不缓存状态。
        self.nav_state_changed.emit()

    def set_active(self, active: bool) -> None:
        """只让当前可见的 tab 拉缩略图：多 tab 同时解码是主线程卡死的直接原因。"""
        self.grid.set_preview_active(bool(active))

    def clear_error(self) -> None:
        """清掉"上次失败"的账面状态（成功加载后由宿主调用）。"""
        self.grid.state.error = False

    # ---- 主题 ----
    def apply_theme(self):
        palette = PawchiveUiPalette.current()
        self.grid.apply_theme()
        border, background = build_selection_colors(palette)
        self.grid.selection_controller.apply_theme(
            selection_border=border, selection_background=background
        )
        for card in self.grid.card_widgets.values():
            card.apply_theme()
        # L3 浮窗是会话级单例（hide 复用），样式只在构造期渲染一次；漏了这一行，
        # dark → light 之后浮窗还停在旧主题（frame 深色 + 文字被染黑 = 除图片外全看不见）。
        # 调用路径与 danbooru 对齐：宿主主题回调 → viewer.apply_theme()。
        viewer = getattr(self, "_viewer", None)
        if viewer is not None:
            viewer.apply_theme()

    def shutdown(self):
        self.grid.shutdown()
        viewer = getattr(self, "_viewer", None)
        if viewer is not None:
            viewer.hide()
        detail_flyout = getattr(self, "_detail_flyout", None)
        if detail_flyout is not None:
            try:
                detail_flyout.close()
            except RuntimeError:
                pass


class PawchiveTabsBar(QWidget):
    """作者作品页的 tab 条：一个作者一个 ``PawchiveGridTab``。"""

    request_close = Signal()
    #: 当前作品 tab 变了（None = 没有 tab）。宿主用它换底栏里的页码导航。
    active_tab_changed = Signal(object)

    #: 与 danbooru 同一套中键防抖（浮窗中键关闭 + 网格中键关 tab 共用一个时间戳）：
    #: 磨损中键一次点击弹成两次 release，第一次关掉浮窗后弹起的第二次落在网格上——
    #: 不共享时间戳就会"关个图顺手把 tab 也关了"。时间戳跨 tab 共享（弹起的 release
    #: 落在哪个 tab 取决于浮窗关闭后哪个 tab 变为当前，按 tab 存状态抓不住）。
    MIDDLE_CLICK_CLOSE_DEBOUNCE_MS = 400
    #: 同一手势的重复 release 去重窗口（比 viewer 余波短）：避免把用户 200ms 内的
    #: 两次**真实**点击也吞掉（那是"点了没反应"，另一种体感缺陷）。
    MIDDLE_CLICK_REPEAT_MS = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PawchiveTabsBar")
        self._tabs: dict[str, PawchiveGridTab] = {}
        self._last_middle_click_at: float = 0.0
        #: 浮窗中键关闭的时刻（独立于 tab 关闭）：只用于挡弹起的第二次 release。
        self._viewer_close_at: float = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.tab_strip = QFrame(self)
        self.tab_strip.setObjectName("PawchiveTabStrip")
        strip_layout = QHBoxLayout(self.tab_strip)
        strip_layout.setContentsMargins(6, 2, 6, 2)
        strip_layout.setSpacing(0)
        self.tab_bar = AccentTabBar(self.tab_strip)
        self.tab_bar.setScrollable(True)
        self.tab_bar.setTabMaximumWidth(200)
        self.tab_bar.setTabShadowEnabled(False)
        self.tab_bar.setAddButtonVisible(False)
        self.tab_bar.setCloseButtonDisplayMode(TabCloseButtonDisplayMode.ON_HOVER)
        self.tab_bar.enableTransparentBackground()
        self.tab_bar.itemLayout.setContentsMargins(0, 5, 0, 5)
        self.tab_bar.itemLayout.setSpacing(6)
        self.tab_strip.tab_bar = self.tab_bar
        # tab 组吃掉工具条前的全部剩余宽度（绿区铺满、消掉中间那段蓝色空档）。
        # **不加** addStretch：加了会与 tab_bar 的 stretch 对半分，蓝色空档就出现。
        # 每个 tab 自身仍是固定宽（AccentTabBar setMinimum/MaximumWidth(200)），
        # 变宽的只是逐条条容器。
        strip_layout.addWidget(self.tab_bar, 1)
        self._build_shared_toolbar(strip_layout)
        layout.addWidget(self.tab_strip)

        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)

        self.tab_bar.currentChanged.connect(self.stack.setCurrentIndex)
        self.tab_bar.currentChanged.connect(self._on_active_tab_changed)
        self.tab_bar.tabCloseRequested.connect(self._on_tab_close_requested)
        theme_mgr.subscribe(self._apply_theme)
        self.destroyed.connect(self._on_destroyed)
        self._apply_theme()
        self._sync_shared_toolbar()

    # ---- tab 管理 ----
    @staticmethod
    def tab_key(service: str, creator_id: str, author_name: str = "") -> str:
        return f"{service or '-'}:{creator_id or author_name}"

    # ---- 共享工具栏（单例，作用于 active tab） ----
    def _build_shared_toolbar(self, strip_layout: QHBoxLayout) -> None:
        """把计数/排序/缩放/选择/下载按钮组装进 tab_strip，与 tab_bar 同一行。

        按钮组只有这一份；handler 全部作用于 ``self.active_tab()``。
        """
        self.toolbar = QFrame(self.tab_strip)
        self.toolbar.setObjectName("PawchiveGridToolbar")
        row = QHBoxLayout(self.toolbar)
        row.setContentsMargins(6, 2, 6, 2)
        # 按钮组间距对齐 danbooru title_row 的 4px（上一轮只做了齐高，间距仍比
        # danbooru 宽 2px，两站并排看会显得 pawchive 更松）。
        row.setSpacing(4)

        self.count_label = QLabel(self.toolbar)
        self.count_label.setObjectName("PawchiveGridCount")
        self.count_label.setText("0 / 0")
        self.count_label.setFixedHeight(TOOLBAR_CONTROL_HEIGHT)
        row.addWidget(self.count_label, 0, Qt.AlignVCenter)

        self.sort_box = ComboBox(self.toolbar)
        self.sort_box.setObjectName("PawchiveGridSort")
        self.sort_box.setFixedWidth(104)
        self.sort_box.setFixedHeight(TOOLBAR_CONTROL_HEIGHT)
        self.sort_box.addItems([
            _tr_or("pawchive.grid.sort_newest", "Newest"),
            _tr_or("pawchive.grid.sort_oldest", "Oldest"),
        ])
        self.sort_box.currentIndexChanged.connect(self._on_sort_changed)
        row.addWidget(self.sort_box, 0, Qt.AlignVCenter)

        zoom_group = QVBoxLayout()
        zoom_group.setContentsMargins(2, 0, 2, 0)
        zoom_group.setSpacing(0)
        self.zoom_in_btn = ToolButton(QIcon(':/zoomin.svg'), self.toolbar)
        self.zoom_in_btn.setMaximumHeight(TOOLBAR_ZOOM_BUTTON_HEIGHT)
        self.zoom_out_btn = ToolButton(QIcon(':/zoomout.svg'), self.toolbar)
        self.zoom_out_btn.setMaximumHeight(TOOLBAR_ZOOM_BUTTON_HEIGHT)
        self.zoom_in_btn.clicked.connect(self._on_zoom_in)
        self.zoom_out_btn.clicked.connect(self._on_zoom_out)
        zoom_group.addWidget(self.zoom_in_btn)
        zoom_group.addWidget(self.zoom_out_btn)
        row.addLayout(zoom_group)

        self.select_visible_btn = ToolButton(FIF.CHECKBOX, self.toolbar)
        self.select_visible_btn.setFixedSize(28, TOOLBAR_CONTROL_HEIGHT)
        self.select_visible_btn.setToolTip(
            _tr_or("pawchive.grid.select_visible", "Select visible")
        )
        self.select_visible_btn.clicked.connect(self._on_select_scope_clicked)
        row.addWidget(self.select_visible_btn, 0, Qt.AlignVCenter)

        self.download_btn = PrimaryToolButton(FIF.DOWNLOAD, self.toolbar)
        self.download_btn.setObjectName("PawchiveGridDownload")
        self.download_btn.setFixedHeight(TOOLBAR_CONTROL_HEIGHT)
        self.download_btn.setMinimumWidth(64)
        self.download_btn.setIconSize(QtCore.QSize(18, 18))
        self.download_btn.clicked.connect(self._on_download_clicked)
        row.addWidget(self.download_btn)
        strip_layout.addWidget(self.toolbar)

        # CountBadge parent 挂 tab_strip：锚点按 download_btn 坐标算、parent 决定
        # 坐标系；挂 toolbar 上 y 恒为负会被裁掉上沿。division=2 = 骑在右上角。
        self.download_badge = CountBadge(
            parent=self.tab_strip, target=self.download_btn, division=2
        )
        self.download_badge.hide()

    def _sync_shared_toolbar(self) -> None:
        """按 active tab 状态刷新单例工具栏；无 active tab 时整组禁用。"""
        tab = self.active_tab()
        enabled = tab is not None
        for widget in (
            self.count_label, self.sort_box, self.zoom_in_btn, self.zoom_out_btn,
            self.select_visible_btn, self.download_btn,
        ):
            widget.setEnabled(enabled)
        if tab is None:
            self.count_label.setText("0 / 0")
            self.download_btn.setDisabled(True)
            self.download_btn.setToolTip("")
            self.sync_download_badge(None)
            return
        self.count_label.setText(tab.count_text())
        with QSignalBlocker(self.sort_box):
            self.sort_box.setCurrentIndex(0 if tab._sort_desc else 1)
        self.download_btn.setDisabled(tab.selected_download_count() == 0)
        self.download_btn.setToolTip(tab.download_tool_tip())
        self.sync_download_badge(tab)

    def sync_download_badge(self, tab: t.Optional[PawchiveGridTab]) -> None:
        """badge 三口径（failed > active > selected）；``tab=None`` 隐藏。"""
        if tab is None:
            self.download_badge.hide()
            return
        count, color = tab.download_badge_state()
        if count == 0:
            self.download_badge.hide()
            return
        self.download_badge.apply_style(light_bg=color, dark_bg=color)
        if count != self.download_badge._count:
            self.download_badge.set_count(count)
        self.download_badge.show()
        self.download_badge.badge.raise_()

    def _on_sort_changed(self, index: int) -> None:
        tab = self.active_tab()
        if tab is not None:
            tab._on_sort_changed(index)

    def _on_zoom_in(self) -> None:
        tab = self.active_tab()
        if tab is not None:
            tab.apply_zoom(tab.zoom_index + 1)

    def _on_zoom_out(self) -> None:
        tab = self.active_tab()
        if tab is not None:
            tab.apply_zoom(tab.zoom_index - 1)

    def _on_select_scope_clicked(self) -> None:
        tab = self.active_tab()
        if tab is not None:
            tab._on_select_scope_clicked(self.select_visible_btn)

    def _on_download_clicked(self) -> None:
        tab = self.active_tab()
        if tab is not None:
            tab._on_download_clicked()

    def _select_index(self, index: int) -> None:
        """程序化切 tab：``TabBar.setCurrentIndex`` **不发** ``currentChanged``（qfw 只有
        真实点击的 ``_onItemPressed`` 才发），所以这里必须显式同步 ``stack`` 与 active
        状态——否则新开的 tab 内容要"点一下旧 tab 再点新 tab"才出来（用户现场）。
        """
        count = self.stack.count()
        if count <= 0:
            return
        index = max(0, min(int(index), count - 1))
        self.tab_bar.setCurrentIndex(index)
        self.stack.setCurrentIndex(index)
        self._on_active_tab_changed(index)

    def open_author(
        self, *, author_name: str, service: str = "", creator_id: str = ""
    ) -> PawchiveGridTab:
        key = self.tab_key(service, creator_id, author_name)
        existing = self._tabs.get(key)
        if existing is not None:
            self._select_index(self.stack.indexOf(existing))
            return existing
        tab = PawchiveGridTab(
            author_name=author_name, service=service, creator_id=creator_id, parent=self.stack
        )
        tab.request_close.connect(lambda _tab=tab: self.close_tab(_tab))
        tab.middle_click_closed.connect(self.note_middle_click_close)
        tab.toolbar_state_changed.connect(self._on_tab_toolbar_state_changed)
        index = self.stack.addWidget(tab)
        # tab 文案 {author}({platform})；缺一侧时只显示非空部分，避免空括号。
        parts = [part for part in (str(author_name or ""), str(service or "")) if part]
        tab_text = f"{parts[0]}({parts[1]})" if len(parts) == 2 else (
            parts[0] if parts else key
        )
        # 不挂 per-tab onClick lambda：qfw 的 `_onItemPressed` 已按 `items.index(sender)`
        # 正确选中；再叠一个闭包捕获的旧 index，关过 tab 后索引漂移会点错项。
        self.tab_bar.addTab(routeKey=key, text=tab_text, icon=FIF.PEOPLE)
        self._tabs[key] = tab
        self._select_index(index)
        tab.apply_theme()
        self._sync_shared_toolbar()
        return tab

    def _on_tab_toolbar_state_changed(self) -> None:
        """任一 tab 的工具栏状态变了 → 刷新单例工具栏（只读 active tab 状态）。

        不依赖 ``sender()``：PySide6 里直接 ``signal.emit()`` 的 sender 可能是 None；
        无条件刷新成本可忽略，且结果永远反映当前 active tab。
        """
        self._sync_shared_toolbar()

    def active_tab(self) -> t.Optional[PawchiveGridTab]:
        widget = self.stack.currentWidget()
        return widget if isinstance(widget, PawchiveGridTab) else None

    def _on_active_tab_changed(self, _index: int = 0) -> None:
        """只让当前 tab 拉缩略图；顺带刷新单例工具栏并告诉宿主（底栏页码用）。"""
        current = self.active_tab()
        for tab in self._tabs.values():
            tab.set_active(tab is current)
        self._sync_shared_toolbar()
        self.active_tab_changed.emit(current)

    def showEvent(self, event):
        super().showEvent(event)
        self._on_active_tab_changed()

    def hideEvent(self, event):
        super().hideEvent(event)
        # 整个作品层收起：所有 tab 停掉缩略图队列，别在后台继续解码。
        for tab in self._tabs.values():
            tab.set_active(False)

    def tab_for(self, key: str) -> t.Optional[PawchiveGridTab]:
        return self._tabs.get(key)

    def close_all(self):
        for tab in list(self._tabs.values()):
            tab.shutdown()
        self._tabs.clear()
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
            widget.deleteLater()
        self._sync_shared_toolbar()
        self.active_tab_changed.emit(None)

    # ---- 中键关闭：防抖（与 danbooru 同一套语义） ----
    def note_middle_click_close(self) -> None:
        """浮窗中键关闭时武装时间戳，挡掉弹起的第二次 release 关 tab。"""
        import time as _time

        self._viewer_close_at = _time.monotonic()

    def close_current(self) -> None:
        """关当前 tab（防抖版）：中键手势的兼容入口，按 active tab 定位。"""
        tab = self.active_tab()
        if tab is not None:
            self.close_tab(tab)

    def close_tab(self, tab: "PawchiveGridTab") -> None:
        """关闭**发起请求的那个 tab**（防抖版）。

        不能按 ``tab_bar.currentIndex()`` 关：中键 release 到达时，bar 的当前索引
        可能与发起请求的 tab 不一致（程序化切换 / 关闭后的索引平移），那会关错甚至
        连关两个。这里用 ``stack.indexOf(tab)`` 精确定位。

        两个窗口分开：
        - viewer 中键关闭后的 400ms 内到达的 release 是**余波**，一律丢弃；
        - 同一手势的重复 release 用 120ms 去重，但不吞掉用户 200ms 内的第二次真实点击。
        """
        import time as _time

        now = _time.monotonic()
        if (now - self._viewer_close_at) * 1000.0 < self.MIDDLE_CLICK_CLOSE_DEBOUNCE_MS:
            return
        if (now - self._last_middle_click_at) * 1000.0 < self.MIDDLE_CLICK_REPEAT_MS:
            return
        index = self.stack.indexOf(tab)
        if index < 0:
            return
        self._last_middle_click_at = now
        self._on_tab_close_requested(index)

    def _on_tab_close_requested(self, index: int):
        widget = self.stack.widget(index)
        if not isinstance(widget, PawchiveGridTab):
            return
        widget.shutdown()
        for key, tab in list(self._tabs.items()):
            if tab is widget:
                self._tabs.pop(key, None)
                break
        self.tab_bar.removeTab(index)
        self.stack.removeWidget(widget)
        widget.deleteLater()
        # removeTab 只在"关的是当前项"时补 emit；非当前项关闭后索引会平移，
        # 这里显式把 stack/activity 对齐到 bar 现档，避免越界或错页。
        if self.stack.count() > 0:
            self._select_index(self.tab_bar.currentIndex())
        else:
            self._on_active_tab_changed(-1)
        self._sync_shared_toolbar()

    # ---- 主题 ----
    def _apply_theme(self, *_args):
        apply_tab_strip_style(self.tab_strip)
        toolbar_css = build_grid_stylesheet(PawchiveUiPalette.current())
        self.toolbar.setStyleSheet(toolbar_css)
        for tab in self._tabs.values():
            tab.apply_theme()

    def _on_destroyed(self, _obj=None):
        theme_mgr.unsubscribe(self._apply_theme)


__all__ = [
    "PawchiveGridTab",
    "PawchivePageNav",
    "PawchiveTabsBar",
    "load_zoom_index",
    "save_zoom_index",
]
