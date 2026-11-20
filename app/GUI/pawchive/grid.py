"""pawchive 作品网格视图（P1）。

结构（05 §2.1 / M4）：QScrollArea.viewport → scroll_content → 外层 QVBoxLayout，每个帖一个
PawchivePostGroupBox（header 横跨整行 + flow_host 内层 FlowLayout 流动卡片）。
重排（05 §2.7，瀑布流）：卡宽只由 metrics.preview_edge 决定，高 = 宽 × 缩略图 (h/w)（夹
0.55~1.85）；缩略图到达改卡高，重排由 _schedule_reflow() 60ms 单次触发合并；内层 FlowLayout
宽度按 viewport().width() 下发，flow_host 用 setFixedHeight(heightForWidth(width)) 钉死。
复合键 CardKey = (post_id: str, attachment_path: str)；card_widgets / group_boxes /
_selectable_keys / _downloaded_keys / state.selected_card_keys 各司其职，只经公开方法暴露。
依赖：PySide6 + qfluentwidgets + 本包模块，卡片/度量/取字节/loader 注入。
"""

import typing as t
from dataclasses import dataclass, field

from PySide6 import QtCore
from PySide6.QtCore import Qt, Signal, qtTrId
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    FluentIcon as FIF,
    FlowLayout,
    IndeterminateProgressBar,
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    PushButton,
    RoundMenu,
    TransparentPushButton,
    TransparentToolButton,
)

from ui.qfluent import MonkeyPatch as FluentMonkeyPatch

from .post_adapter import CardKey, PawchiveCardItem, PawchivePostGroup
from .preview_loader import PawchivePreviewLoader
from .selection import PawchiveSelectionController

COLLAPSED_LIMIT = 12
PREVIEW_SCROLL_DEBOUNCE_MS = 120
NEXT_PAGE_THRESHOLD_PX = 200


#: 本模块经 _tr_or 使用的全部 i18n 键。_tr_or 用变量调 qtTrId，而 res/i18n/build.py 的
#: AST 扫描只认字面量实参或"赋值右侧 tuple/list/dict 里的 id 形字符串"这条间接通道；
#: 没有它，这些键在 --check 里会被判 stale、--sync 时从两张表里删掉。新增/改名键时同步这里。
_GRID_I18N_KEYS = (
    "pawchive.card.retry",
    "pawchive.card.reload_preview",
    "pawchive.download.selected_count",
    "pawchive.grid.clear_selection",
    "pawchive.grid.collapse_post",
    "pawchive.grid.downloaded_progress",
    "pawchive.grid.empty_author",
    "pawchive.grid.empty_filter",
    "pawchive.grid.empty_post",
    "pawchive.grid.expand_post",
    "pawchive.grid.loading_more",
    "pawchive.grid.post_meta",
    "pawchive.post.detail",
    "pawchive.post.non_previewable",
)


def _tr_or(key: str, fallback: str) -> str:
    """``qtTrId`` 缺键时回退成 id 本身；此时用更短的兜底串。

    id 串（``pawchive.grid.select_visible``）比兜底文案长得多，直接显示会把工具栏
    挤变形，所以缺键期用可读兜底（06 §T3 允许缺键，但要求"难看但可用"）。
    """
    text = qtTrId(key)
    return fallback if text == key else text


def _masonry_layout_factory(parent=None):
    """默认内层布局：瀑布流（卡片高度随真实宽高比）。延迟导入避免循环依赖。"""
    from .masonry import PawchiveMasonryLayout

    return PawchiveMasonryLayout(parent, spacing=4)


def default_card_factory(
    item: PawchiveCardItem, *, parent=None, metrics=None, already_downloaded: bool = False
):
    """默认工厂：**全关键字传参**（禁位置，01 A-3）。"""
    from .card import PawchiveCardWidget
    from .style import DEFAULT_PAWCHIVE_CARD_METRICS

    return PawchiveCardWidget(
        item,
        metrics=metrics or DEFAULT_PAWCHIVE_CARD_METRICS,
        already_downloaded=already_downloaded,
        parent=parent,
    )


@dataclass
class PawchiveGridState:
    """网格游标状态。照 ``DanbooruTabState`` 的形状，去掉搜索/计数等 danbooru 语义。"""

    offset: int = 0
    has_more_results: bool = False
    loading: bool = False
    has_loaded_once: bool = False
    #: 上次列表请求是否失败（宿主重进作者页时据此自动重试）。
    error: bool = False
    selected_card_keys: set = field(default_factory=set)
    post_count: int = 0
    image_count: int = 0
    page_size: int = 50

    def reset(self):
        self.offset = 0
        self.has_more_results = False
        self.loading = False
        self.has_loaded_once = False
        self.error = False
        self.selected_card_keys.clear()
        self.post_count = 0
        self.image_count = 0

    def advance_offset(self, page_size: t.Optional[int] = None):
        """``list_posts`` 的 offset 必须是 50 的倍数（IA v4 §9 / api_facts §3）。

        现盘 ``next_offset()`` 是 ``return self.offset``（无推进语义），已删。
        """
        step = max(1, int(page_size or self.page_size))
        self.offset += step
        return self.offset


class PawchiveGroupCheck(QCheckBox):
    """帖头三态勾。方形 indicator 天然区分层级（卡片是圆点）。

    ``sync`` 期间 ``blockSignals``，所以 ``toggled`` 只可能是用户点击。
    """

    select_requested = Signal(object, bool)  # (set[CardKey], checked)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PawchiveGroupCheck")
        self.setTristate(True)
        self.setCheckState(Qt.Unchecked)
        self.setCursor(Qt.PointingHandCursor)
        self.toggled.connect(self._on_toggled)
        self._selectable: set = set()

    def sync(self, *, selectable: set, selected: set, downloaded: set, total: int):
        _ = downloaded, total
        self._selectable = set(selectable)
        self.blockSignals(True)
        self.setEnabled(bool(selectable))
        if selectable and selected >= selectable:
            state = Qt.Checked
        elif selected:
            state = Qt.PartiallyChecked
        else:
            state = Qt.Unchecked
        self.setCheckState(state)
        self.blockSignals(False)

    def _on_toggled(self, checked: bool):
        # Checked → 清空本组；Unchecked / Partial → 全选本组（勾/取消都跳过已下与无 URL）。
        self.select_requested.emit(set(self._selectable), bool(checked))


class PawchivePostGroupBox(QWidget):
    """一个帖分组：帖头横跨整行 + 内层 FlowLayout。"""

    expand_toggled = Signal(str, bool)  # (post_id, expanded)
    select_requested = Signal(object, bool)  # (set[CardKey], checked)
    download_requested = Signal(object)  # (set[CardKey])
    #: 帖内容浮窗：携带本组与锚点按钮，由 tab 复用同一个 textBrowser 展示。
    detail_requested = Signal(object, object)  # (PawchivePostGroup, QWidget)
    #: 组右列不可预览资产的勾选变化 → (CardKey, checked)。
    asset_toggled = Signal(object, bool)
    #: 组内有卡因缩略图宽高比改变而换了高度：整页需要重排瀑布流。
    layout_dirty = Signal()

    def __init__(
        self,
        group: PawchivePostGroup,
        *,
        card_factory,
        card_metrics,
        downloaded_keys: t.Optional[set] = None,
        flow_layout_factory=None,
        parent=None,
    ):
        super().__init__(parent)
        self.group = group
        self._card_factory = card_factory
        self._card_metrics = card_metrics
        # 默认用瀑布流（卡片高度随真实宽高比）。FlowLayout 仍可注入，供旧判据与
        # 需要"等高行"的场景使用。
        self._flow_layout_factory = flow_layout_factory or _masonry_layout_factory
        self._downloaded_keys = downloaded_keys if downloaded_keys is not None else set()
        self._expanded = False
        self.cards: dict = {}
        # 能进瀑布流/进 viewer 的只有可渲染预览的附件；其余（压缩包/无缩略图）
        # 收进右侧勾选列表，永不建卡、不进 viewer 序列。
        self._preview_items = [
            item for item in group.items if item.has_renderable_preview_asset
        ]
        self._asset_items = [
            item for item in group.items if not item.has_renderable_preview_asset
        ]
        self.setObjectName("PawchivePostGroup")
        # 帖头文字可能很长：不让它把组框顶宽（顶宽会把卡片挤出右边缘）。
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        self.header = self._build_header()
        outer.addWidget(self.header)

        body = QWidget(self)
        body.setObjectName("PawchivePostGroupBody")
        body_row = QHBoxLayout(body)
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(6)

        self.flow_host = QWidget(body)
        self.flow_host.setObjectName("PawchiveFlowHost")
        # 宽度让外层决定、高度由本组自己钉死：`FlowLayout.sizeHint()` 给的是
        # 「不换行」的高度（12 张卡时上千像素，真实只占几百），不钉死会让
        # scroll_content 抬高上万像素、底部留一大片空白。
        self.flow_host.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.flow_layout = self._flow_layout_factory(self.flow_host)
        self.flow_layout.setContentsMargins(2, 2, 2, 2)
        self.flow_layout.setSpacing(4)
        body_row.addWidget(self.flow_host, 1)

        self.asset_list = self._build_asset_list(body) if self._asset_items else None
        if self.asset_list is not None:
            body_row.addWidget(self.asset_list, 0, Qt.AlignTop)
        outer.addWidget(body)

        self._populate()

    # ---- 帖头 ----
    def _build_header(self) -> QWidget:
        header = QWidget(self)
        header.setObjectName("PawchivePostGroupHeader")
        header.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        header.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(header)
        row.setContentsMargins(6, 3, 6, 3)
        row.setSpacing(8)

        self.group_check = PawchiveGroupCheck(header)
        self.group_check.select_requested.connect(self._on_group_check)
        row.addWidget(self.group_check)

        self.title_label = BodyLabel(header)
        self.title_label.setObjectName("PawchiveGroupTitle")
        self.title_label.setText(self.group.title or self.group.post_id)
        # 标题不得 setFixedWidth / Preferred 顶宽：顶宽会成为 scroll_content 的
        # minimumSizeHint，把内容撑到视口之外 → QScrollArea 永远留横向滚动条。
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.title_label.setMinimumWidth(0)
        row.addWidget(self.title_label, 1)

        if self.group.is_empty:
            meta_text = _tr_or("pawchive.grid.empty_post", "0")
        else:
            meta_text = _tr_or("pawchive.grid.post_meta", "{published} · {count}").format(
                published=self.group.published or "-", count=self.group.count
            )
        self.meta_label = QLabel(header)
        self.meta_label.setObjectName("PawchiveGroupMeta")
        self.meta_label.setText(meta_text)
        row.addWidget(self.meta_label)

        self.detail_btn = TransparentToolButton(FIF.DOCUMENT, header)
        self.detail_btn.setToolTip(_tr_or("pawchive.post.detail", "Post details"))
        self.detail_btn.clicked.connect(
            lambda: self.detail_requested.emit(self.group, self.detail_btn)
        )
        row.addWidget(self.detail_btn)

        self.download_btn = TransparentToolButton(FIF.DOWNLOAD, header)
        self.download_btn.setObjectName("PawchiveGroupDownload")
        self.download_btn.clicked.connect(self._on_download_clicked)
        row.addWidget(self.download_btn)

        self.expand_btn = PushButton(header)
        self.expand_btn.setObjectName("PawchiveGroupExpand")
        hidden_preview = self._hidden_preview_count()
        self.expand_btn.setVisible(hidden_preview > 0)
        if hidden_preview > 0:
            self.expand_btn.setText(
                _tr_or("pawchive.grid.expand_post", "+{count}").format(
                    count=hidden_preview
                )
            )
        self.expand_btn.clicked.connect(self._on_expand_clicked)
        row.addWidget(self.expand_btn)

        # 「已下 N/M」只归已下载计数（06 §2.4.1），**不喂选中数**（现盘缺陷 A-9）。
        self.count_label = QLabel(header)
        self.count_label.setObjectName("PawchiveGroupCount")
        self.count_label.setVisible(False)
        row.addWidget(self.count_label)
        return header

    # ---- 不可预览资产列表 ----
    def _build_asset_list(self, parent) -> QListWidget:
        """右侧多选列表：压缩包/无可渲染预览的下载资产，不进瀑布流、不进 viewer。

        2026-11-17 用户现场裁定：**不要自带背景/描边**——组内已有卡片这张"卡"，
        再套一层灰底圆角就是"第二张卡"，割裂感来源。列表做成透明附注列，
        宽 176、按内容定高，hover 只用中性色。
        """
        widget = QListWidget(parent)
        widget.setObjectName("PawchiveAssetList")
        widget.setFrameShape(QFrame.Shape.NoFrame)
        widget.setFixedWidth(176)
        widget.setMinimumHeight(0)
        row_height = 26
        widget.setFixedHeight(min(260, 4 + row_height * max(1, len(self._asset_items))))
        widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        widget.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        widget.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        widget.setToolTip(
            _tr_or("pawchive.post.non_previewable", "Non-previewable assets")
        )
        for item in self._asset_items:
            row = QListWidgetItem(self._asset_label(item))
            row.setData(Qt.ItemDataRole.UserRole, item.key)
            row.setToolTip(item.name or item.attachment_path)
            flags = Qt.ItemFlag.ItemIsEnabled
            if item.is_downloadable and item.key not in self._downloaded_keys:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            row.setFlags(flags)
            row.setCheckState(Qt.CheckState.Unchecked)
            widget.addItem(row)
        widget.itemChanged.connect(self._on_asset_item_changed)
        return widget

    @staticmethod
    def _asset_label(item: PawchiveCardItem) -> str:
        name = item.name or item.attachment_path.rsplit("/", 1)[-1]
        ext = (item.file_ext or "").upper()
        if ext and not name.upper().endswith(f".{ext}"):
            return f"{name}  [{ext}]"
        return name

    def _on_asset_item_changed(self, item: QListWidgetItem):
        key = item.data(Qt.ItemDataRole.UserRole)
        if key is None or not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        self.asset_toggled.emit(key, item.checkState() == Qt.CheckState.Checked)

    def asset_keys(self) -> list:
        return [item.key for item in self._asset_items]

    def _hidden_preview_count(self) -> int:
        return max(0, len(self._preview_items) - self.group.collapsed_limit)

    # ---- 卡片 ----
    def _visible_items(self) -> list:
        if self._expanded:
            return list(self._preview_items)
        return list(self._preview_items[: self.group.collapsed_limit])

    def _populate(self):
        for item in self._visible_items():
            self._add_card(item)
        self._refresh_inner_layout()

    def _add_card(self, item: PawchiveCardItem):
        key = item.key
        if key in self.cards:
            return
        # 全关键字传参：位置传参会把 parent 绑成 already_downloaded（01 A-3）。
        card = self._card_factory(
            item,
            parent=self.flow_host,
            metrics=self._card_metrics,
            already_downloaded=key in self._downloaded_keys,
        )
        self.cards[key] = card
        self.flow_layout.addWidget(card)
        # 卡高随缩略图真实宽高比变化 → 组要重排，且整页高度跟着变。
        notify = getattr(card, "layout_changed", None)
        if notify is not None:
            notify.connect(self.layout_dirty.emit)
        return card

    def add_hidden_cards(self) -> list:
        """展开剩余：逐张建卡并返回新增的 (key, card) 列表。"""
        added = []
        for item in self._preview_items[self.group.collapsed_limit:]:
            if item.key in self.cards:
                continue
            card = self._add_card(item)
            if card is not None:
                added.append((item.key, card))
        return added

    def remove_card(self, key: CardKey):
        card = self.cards.pop(key, None)
        if card is None:
            return
        self.flow_layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()

    def collapse(self):
        keep = {item.key for item in self._preview_items[: self.group.collapsed_limit]}
        for key in [k for k in self.cards if k not in keep]:
            self.remove_card(key)
        self._expanded = False
        self.expand_btn.setText(
            _tr_or("pawchive.grid.expand_post", "+{count}").format(
                count=self._hidden_preview_count()
            )
        )

    def _on_expand_clicked(self):
        self._expanded = not self._expanded
        if self._expanded:
            self.add_hidden_cards()
            self.expand_btn.setText(_tr_or("pawchive.grid.collapse_post", "−"))
        else:
            self.collapse()
        self.expand_toggled.emit(self.group.post_id, self._expanded)

    def set_expanded(self, expanded: bool) -> list:
        """程序化展开（06 徽标定位用）。返回新增的 (key, card) 列表。"""
        if expanded == self._expanded:
            return []
        self._expanded = bool(expanded)
        if self._expanded:
            added = self.add_hidden_cards()
            self.expand_btn.setText(_tr_or("pawchive.grid.collapse_post", "−"))
        else:
            self.collapse()
            added = []
        self.expand_toggled.emit(self.group.post_id, self._expanded)
        return added

    def is_expanded(self) -> bool:
        return self._expanded

    # ---- 键集合 ----
    def all_keys(self) -> list:
        return [item.key for item in self.group.items]

    def selectable_keys(self) -> set:
        """该帖可选键：可下载 **且未下载**（06 §1.5 的 D-7 漏洞修法）。"""
        return {
            item.key
            for item in self.group.items
            if item.is_downloadable and item.key not in self._downloaded_keys
        }

    def downloaded_count(self) -> int:
        return sum(1 for item in self.group.items if item.key in self._downloaded_keys)

    # ---- 组头同步 ----
    def sync_group_check(self, selected_keys: set):
        """三态勾 + 右列资产勾：两处都从同一个选中集回放。"""
        post_keys = set(self.all_keys())
        downloaded = post_keys & set(self._downloaded_keys)
        selectable = (post_keys - downloaded) & {
            item.key for item in self.group.items if item.is_downloadable
        }
        selected = set(selected_keys) & selectable
        self.group_check.sync(
            selectable=selectable,
            selected=selected,
            downloaded=downloaded,
            total=self.group.count,
        )
        self._sync_asset_checks(selected)

    def _sync_asset_checks(self, selected: set):
        widget = self.asset_list
        if widget is None:
            return
        downloaded = set(self._downloaded_keys)
        checkable = {
            item.key
            for item in self._asset_items
            if item.is_downloadable and item.key not in downloaded
        }
        widget.blockSignals(True)
        try:
            for index in range(widget.count()):
                row = widget.item(index)
                key = row.data(Qt.ItemDataRole.UserRole)
                flags = Qt.ItemFlag.ItemIsEnabled
                if key in checkable:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                row.setFlags(flags)
                checked = key in selected and key in checkable
                row.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
        finally:
            widget.blockSignals(False)

    def set_downloaded_count(self, hits: int, total: int):
        """组头「已下 N/M」：数据来自 `_downloaded_keys ∩ 帖全量 items`，与选中数解耦。"""
        if hits <= 0:
            self.count_label.setVisible(False)
            return
        self.count_label.setText(
            _tr_or("pawchive.grid.downloaded_progress", "{done}/{total}").format(
                done=hits, total=total
            )
        )
        self.count_label.setVisible(True)

    def set_highlight(self, highlighted: bool):
        self.header.setProperty("highlight", bool(highlighted))
        self.header.style().unpolish(self.header)
        self.header.style().polish(self.header)

    def _on_group_check(self, keys: set, checked: bool):
        self.select_requested.emit(set(keys), bool(checked))

    def _on_download_clicked(self):
        keys = self.selectable_keys()
        if keys:
            self.download_requested.emit(keys)

    # ---- 布局 ----
    def _refresh_inner_layout(self, width: t.Optional[int] = None):
        """只对内层 FlowLayout 调 setGeometry —— M5 判据只看这一层。"""
        if width is None:
            width = self._content_width()
        width = max(1, int(width) - self._asset_list_width())
        height = max(1, self.flow_layout.heightForWidth(width))
        self.flow_host.setMinimumWidth(0)
        self.flow_host.setFixedHeight(height)
        self.flow_host.resize(width, height)
        self.flow_layout.invalidate()
        self.flow_layout.setGeometry(QtCore.QRect(0, 0, width, height))

    def _asset_list_width(self) -> int:
        if self.asset_list is None:
            return 0
        return self.asset_list.width() + 6

    def _content_width(self) -> int:
        host = self.parentWidget()
        while host is not None and not isinstance(host, QScrollArea):
            host = host.parentWidget()
        if isinstance(host, QScrollArea):
            return max(1, host.viewport().width() - 4)
        return max(1, self.width())


class PawchiveGridWidget(QFrame):
    """L2 作品网格。选择模型接 ``PawchiveSelectionController``。"""

    selection_count_changed = Signal(int)
    request_next_page = Signal()
    request_open_detail = Signal(object)  # (CardKey)，差异 D-d
    request_close = Signal()
    retry_requested = Signal()
    download_requested = Signal(object)  # (set[CardKey])，06 消费
    retry_card_requested = Signal(object)  # (CardKey)，06 消费
    #: 帖头「详情」→ (PawchivePostGroup, anchor QWidget)；tab 复用同一个 textBrowser。
    group_detail_requested = Signal(object, object)

    def __init__(
        self,
        *,
        card_factory: t.Optional[t.Callable[..., t.Any]] = None,
        card_metrics=None,
        preview_fetcher: t.Optional[t.Callable[..., bytes]] = None,
        preview_loader: t.Optional[PawchivePreviewLoader] = None,
        flow_layout_factory: t.Optional[t.Callable[[QWidget], t.Any]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._card_factory = card_factory or default_card_factory
        self._flow_layout_factory = flow_layout_factory
        if card_metrics is None:
            from .style import DEFAULT_PAWCHIVE_CARD_METRICS

            card_metrics = DEFAULT_PAWCHIVE_CARD_METRICS
        self.card_metrics = card_metrics
        self.state = PawchiveGridState()
        self.card_widgets: dict = {}
        self.group_boxes: dict = {}
        self._post_groups: list = []
        # 三个键集合分开（05 §2.6）：全量可选 / 全量已下载 / 选中（在 state 里）。
        self._selectable_keys: set = set()
        #: ``CardKey → published``（"YYYY-MM-DD…"）。时间范围选择用，含折叠未渲染的卡。
        self._key_published: dict = {}
        self._downloaded_keys: set = set()
        #: zoom-pattern 脏集（06 §2.4）：后台 tab 下载完成只记账，激活时 flush。
        self._downloads_dirty: set = set()
        self._urls: dict = {}
        self._build_ui()
        self.selection_controller = PawchiveSelectionController(self)
        self.selection_controller.selection_count_changed.connect(
            self.selection_count_changed.emit
        )
        self.selection_controller.selection_count_changed.connect(
            lambda _n: self._sync_group_checks()
        )
        if preview_loader is not None:
            self.preview_loader = preview_loader
        else:
            fetcher = preview_fetcher or _missing_fetcher
            self.preview_loader = PawchivePreviewLoader(fetcher, parent=self)
        self.preview_loader.ready.connect(self._on_preview_ready)
        self.preview_loader.failed.connect(self._on_preview_failed)
        self.preview_loader.blocked.connect(self._on_preview_blocked)
        animated = getattr(self.preview_loader, "animated", None)
        if animated is not None:
            animated.connect(self._on_preview_animated)
        self._scroll_debounce = QtCore.QTimer(self)
        self._scroll_debounce.setSingleShot(True)
        self._scroll_debounce.setInterval(PREVIEW_SCROLL_DEBOUNCE_MS)
        self._scroll_debounce.timeout.connect(self._request_visible_previews)
        # 空白区右键：与卡片同一套 FluentMonkeyPatch（qfluentwidgets RoundMenu）。
        # 不再在实例里覆写 contextMenuEvent 手取坐标 —— PySide6 的
        # QContextMenuEvent 没有 globalPosition()（2026-11-19 现场 AttributeError），
        # 统一由 _exec_menu 走 globalPos()。
        FluentMonkeyPatch.rbutton_menu_widget(
            self, lambda _event: self._build_grid_context_menu()
        )

    # ---- UI ----
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 加载态 = 进度条，不是文字（2026-11-17 用户现场裁定：info 类不许用 label）。
        self.loading_bar = IndeterminateProgressBar(self, start=False)
        self.loading_bar.setObjectName("PawchiveGridLoadingBar")
        self.loading_bar.setFixedHeight(3)
        self.loading_bar.setVisible(False)
        layout.addWidget(self.loading_bar)

        # 空 / 错态 = Fluent InfoBar（可带重试按钮），不再是一行灰字。
        self.info_host = QWidget(self)
        self.info_host.setObjectName("PawchiveGridInfoHost")
        self.info_layout = QVBoxLayout(self.info_host)
        self.info_layout.setContentsMargins(8, 0, 8, 0)
        self.info_layout.setSpacing(6)
        self.info_host.setVisible(False)
        layout.addWidget(self.info_host)
        self._info_bar = None
        #: 最近一次创建的重试按钮（探针/兼容旧引用；InfoBar 关闭后失效）。
        self.empty_retry_btn = None
        self.error_retry_btn = None

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("PawchiveGridScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("PawchiveGridContent")
        self.outer_layout = QVBoxLayout(self.scroll_content)
        self.outer_layout.setContentsMargins(2, 2, 2, 2)
        self.outer_layout.setSpacing(10)
        self.outer_layout.addStretch(1)
        self.scroll_area.setWidget(self.scroll_content)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        layout.addWidget(self.scroll_area, 1)

    # ---- 空 / 错误态（InfoBar，不用 label） ----
    def _clear_info(self) -> None:
        bar = self._info_bar
        self._info_bar = None
        if bar is None:
            return
        try:
            self.info_layout.removeWidget(bar)
            bar.close()
        except RuntimeError:  # 已随父级销毁
            pass

    def _show_info(self, icon, text: str, *, retryable: bool = False, error: bool = False):
        self._clear_info()
        # 关键：必须用 NONE。InfoBar 是浮层控件，非 NONE 的 position 会注册进
        # InfoBarManager：manager 在 showEvent 里 installEventFilter 并强制
        # ``bar.move()``（TOP = 父控件顶部居中、y=24），与下面的 info_layout
        # 布局互相抢几何——布局把 info_host 撑到条的 sizeHint 高度，manager 又把
        # 条往下挪 24px，超出 info_host 底边的下半截被父子裁剪切掉（现场症状：
        # 「连不上站点」错误条只见半截）。NONE 不注册 manager，几何完全归布局。
        # 与宿主 _place_env_notices（env 提示条）同一类坑的同一解法。
        bar = InfoBar(
            icon,
            "",
            str(text or ""),
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.NONE,
            duration=-1,
            parent=self.info_host,
        )
        if retryable:
            retry_btn = TransparentPushButton(
                FIF.SYNC, _tr_or("pawchive.card.retry", "Retry"), bar
            )
            retry_btn.setFixedHeight(28)
            retry_btn.clicked.connect(self.retry_requested.emit)
            bar.addWidget(retry_btn)
            if error:
                self.error_retry_btn = retry_btn
            else:
                self.empty_retry_btn = retry_btn
        bar.closedSignal.connect(lambda: self.info_host.setVisible(False))
        self.info_layout.addWidget(bar)
        bar.show()
        self.info_host.setVisible(True)
        self._info_bar = bar

    def show_empty(self, text: str, *, retryable: bool = False):
        self.state.loading = False
        self._show_info(InfoBarIcon.INFORMATION, text, retryable=retryable)

    def show_empty_author(self):
        """S2 空作者：该作者 0 帖（``offset == 0`` 且无过滤词）。可重试 = 重发首屏。"""
        self.show_empty(
            _tr_or("pawchive.grid.empty_author", "No works for this author"),
            retryable=True,
        )

    def show_empty_filter(self):
        """S3 空过滤：过滤词非空但本地过滤后 0 条。不可重试（改过滤词即可）。"""
        self.show_empty(_tr_or("pawchive.grid.empty_filter", "No matching images"))

    def hide_empty(self):
        self.info_host.setVisible(False)

    def show_error(self, message: str):
        self.state.error = True
        self.state.loading = False
        self._show_info(InfoBarIcon.ERROR, message, retryable=True, error=True)

    def hide_error(self):
        self.state.error = False
        self.info_host.setVisible(False)

    def set_loading(self, loading: bool):
        self.state.loading = bool(loading)
        self.loading_bar.setVisible(bool(loading))
        if loading:
            self.info_host.setVisible(False)
            if not self.loading_bar.isStarted():
                self.loading_bar.start()
        else:
            self.loading_bar.stop()
            self.loading_bar.hide()

    def set_preview_active(self, active: bool) -> None:
        """只让当前可见 tab 拉缩略图；挂起时停止派发但保留队列。"""
        self.preview_loader.set_active(bool(active))
        if active:
            self._request_visible_previews()

    # ---- 分页 ----
    def append_page(
        self,
        groups: t.Sequence[PawchivePostGroup],
        *,
        has_more: bool,
        offset: int,
    ) -> int:
        """追加一页（50 的倍数 offset）。零附件的帖不建组框（IA §5.1 / D-15）。"""
        self.hide_empty()
        self.hide_error()
        self.state.has_loaded_once = True
        self.state.has_more_results = bool(has_more)
        self.state.offset = int(offset)
        inserted = 0
        new_keys: list = []
        for group in groups:
            if group.is_empty:
                continue
            if group.post_id in self.group_boxes:
                # 重放页幂等：同帖不重建组框（防覆盖闸 ①）。
                continue
            box = self._build_group_box(group)
            self.outer_layout.insertWidget(self.outer_layout.count() - 1, box)
            self.group_boxes[group.post_id] = box
            self._post_groups.append(group)
            self._selectable_keys.update(
                item.key for item in group.items if item.is_downloadable
            )
            # 时间范围选择需要"键 → 发布日期"；折叠未渲染的卡也要能命中，
            # 所以映射建在分页入口（全量 items），而不是渲染出来的卡上。
            for item in group.items:
                self._key_published[item.key] = str(getattr(item, "published", "") or "")
            self.state.image_count += group.count
            inserted += 1
            new_keys.extend(item.key for item in group.items)
        # 现盘 `post_count += len(groups)` 把零附件帖也算进去 → 改 `+= inserted`。
        self.state.post_count += inserted
        self._sync_group_checks()
        self._refresh_grid_layout()
        if new_keys:
            # 触发点 ①：首屏可见（构造期视口高度未定，showEvent 会再补一次）。
            self.preview_loader.request(new_keys, visible=self.visible_card_keys())
        return inserted

    def reset(self):
        for box in self.group_boxes.values():
            box.setParent(None)
            box.deleteLater()
        self.group_boxes.clear()
        self._post_groups.clear()
        self._selectable_keys.clear()
        self._key_published.clear()
        self._downloaded_keys.clear()
        self._urls.clear()
        for card in self.card_widgets.values():
            card.setParent(None)
            card.deleteLater()
        self.card_widgets.clear()
        self.state.reset()
        self.preview_loader.reset()
        if hasattr(self, "selection_controller"):
            self.selection_controller.clear()
        self._refresh_grid_layout()

    def _build_group_box(self, group: PawchivePostGroup) -> PawchivePostGroupBox:
        box = PawchivePostGroupBox(
            group,
            card_factory=self._make_card,
            card_metrics=self.card_metrics,
            downloaded_keys=self._downloaded_keys,
            flow_layout_factory=self._flow_layout_factory,
            parent=self.scroll_content,
        )
        for key, card in box.cards.items():
            self._register_card(key, card)
        box.expand_toggled.connect(self._on_group_expanded)
        box.select_requested.connect(self._on_group_select_requested)
        box.download_requested.connect(self.download_requested.emit)
        box.layout_dirty.connect(self._schedule_reflow)
        box.asset_toggled.connect(self._on_asset_toggled)
        box.detail_requested.connect(self.group_detail_requested.emit)
        return box

    def _schedule_reflow(self) -> None:
        """缩略图陆续到达会连续触发：合并成一次重排，避免每张图都全页抖动。

        60ms 足够把同一批解码回包并起来；用户空闲时几乎察觉不到。
        """
        timer = getattr(self, "_reflow_timer", None)
        if timer is None:
            timer = self._reflow_timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(60)
            timer.timeout.connect(self._refresh_grid_layout)
        timer.start()

    def _make_card(self, item: PawchiveCardItem, *, parent, metrics, already_downloaded=False):
        """卡片工厂调用口。**全关键字传参**（01 A-3：位置传参把 parent 绑成
        ``already_downloaded``）。

        兼容降级：06 §2.4 把工厂签名从 ``(item, parent, metrics)`` 扩成
        ``(..., *, already_downloaded=False)``；只接受旧签名的注入者（既有探针的
        stub lambda 就是这种）会 ``TypeError``。这里退回旧签名并把已下载态留给
        调用方后续 ``set_already_downloaded`` 补，而不是让整页建不出来。
        """
        try:
            return self._card_factory(
                item,
                parent=parent,
                metrics=metrics,
                already_downloaded=already_downloaded,
            )
        except TypeError:
            card = self._card_factory(item, parent=parent, metrics=metrics)
            if already_downloaded and hasattr(card, "set_already_downloaded"):
                card.set_already_downloaded(True)
            return card

    def _register_card(self, key: CardKey, card):
        # 防覆盖闸 ②：同帖同 path 重复的脏数据也兜住。
        if key in self.card_widgets:
            return
        self.card_widgets[key] = card
        self._urls[key] = getattr(card.post, "preview_file_url", None)
        self.preview_loader.set_url(key, self._urls[key])
        self.selection_controller.bind_card(card)
        card.open_detail_requested.connect(self.request_open_detail.emit)
        reload_signal = getattr(card, "reload_requested", None)
        if reload_signal is not None:
            reload_signal.connect(self._on_card_reload_requested)
        # 右键菜单统一由网格构造唯一的 RoundMenu（刷新 + 清空选择 + 下载）。
        # 卡片不覆写 contextMenuEvent 取坐标（QContextMenuEvent 没有
        # globalPosition()），菜单与坐标统一走 FluentMonkeyPatch。
        FluentMonkeyPatch.rbutton_menu_widget(
            card, lambda _event, _c=card: self._build_grid_context_menu(card=_c)
        )
        completed = getattr(card, "download_completed", None)
        if completed is not None:
            # 卡报"完成" → 写账本（幂等：apply_downloaded 只记新键）。
            completed.connect(lambda key, _k=key: self.apply_downloaded([_k]))

    def _on_group_expanded(self, post_id: str, expanded: bool):
        box = self.group_boxes.get(post_id)
        if box is None:
            return
        if expanded:
            new_keys: list = []
            for key, card in box.cards.items():
                if key not in self.card_widgets:
                    self._register_card(key, card)
                    new_keys.append(key)
        else:
            # 收起：已渲染键退出 card_widgets（D-11 折叠态 = 12），但**键仍留在
            # 选中集与 `_selectable_keys`**（否则勾 201 张一收就只剩 12）。
            keep = set(box.cards)
            new_keys = []
            for key in [k for k in self.card_widgets if k[0] == post_id and k not in keep]:
                self.card_widgets.pop(key, None)
                self.preview_loader.forget(key)
        self._sync_group_checks()
        self._refresh_grid_layout()
        if expanded and new_keys:
            # 触发点 ②：展开剩余（收起不回收已加载 pixmap，缓存留给再次展开）。
            self.preview_loader.request(new_keys, visible=self.visible_card_keys())

    def _on_group_select_requested(self, keys: set, checked: bool):
        current = set(self.state.selected_card_keys)
        target = (current | keys) if checked else (current - keys)
        self.selection_controller.set_selected_keys(target)
        self._sync_group_checks()

    def _on_asset_toggled(self, key: CardKey, checked: bool):
        """右列不可预览资产：多选勾直接并进同一个选中集（下载链零改动）。"""
        current = set(self.state.selected_card_keys)
        if checked:
            current.add(key)
        else:
            current.discard(key)
        self.selection_controller.set_selected_keys(current)
        self._sync_group_checks()

    def _on_scroll_changed(self, value: int):
        bar = self.scroll_area.verticalScrollBar()
        if (
            bar.maximum() - value < NEXT_PAGE_THRESHOLD_PX
            and not self.state.loading
            and self.state.has_more_results
        ):
            self.request_next_page.emit()
        # 触发点 ③：滚动进入视口（120ms 去抖；与触底加载共用槽但互不阻塞）。
        self._scroll_debounce.start()

    def _request_visible_previews(self):
        visible = self.visible_card_keys()
        if visible:
            self.preview_loader.request(visible, visible=visible)

    # ---- 缩略图回调 ----
    def _on_preview_ready(self, key: CardKey, pixmap: QPixmap):
        card = self.card_widgets.get(key)
        if card is None:
            return
        # 只换像素，不改几何 → 内层 FlowLayout 不重排（D-3）。
        card.set_preview_pixmap(pixmap)

    def _on_preview_animated(self, _key: CardKey, _raw: bytes):
        """动图回包：**故意不播**（瀑布流缩略图保持静态首帧，减负）。

        用户明确要求列表里的 GIF 不要动（几十张卡同时 QMovie 逐帧解码会吃满主线程）。
        首帧已由 ``_on_preview_ready`` 的 ``set_preview_pixmap`` 落位，这里保持 no-op。
        信号连接保留（``preview_loader.animated.connect``），将来要做"仅可见卡播放"
        只需在此按可见性恢复 ``card.set_preview_animation(raw)``，不用改 loader/viewer。
        大图 viewer 的 GIF 播放是独立链路，不受此处影响。
        """
        return

    def _on_preview_failed(self, key: CardKey, kind: str):
        card = self.card_widgets.get(key)
        if card is None:
            return
        if hasattr(card, "set_failed"):
            card.set_failed(True)

    def _on_card_reload_requested(self, key: CardKey):
        """右键「重新加载缩略图」：把键送回 loader 重取（含永久失败键）。"""
        reload = getattr(self.preview_loader, "reload", None)
        if callable(reload):
            reload(key)

    def _on_preview_blocked(self):
        """缩略图域 403/429：**只暂停派发，不弹任何 UI、不改卡态**。

        契约 §三.6 / 06 §2.5：403 只挂在下载提交上。缩略图域（``img.``）对 UA 不敏感，
        这里出现 403 说明是限速/风控而非链路失败；弹 infobar 会让用户以为"下载也挂了"
        （D-9b 正是要证明"缩略图正常 ≠ 链路正常"，反向同理）。暂停窗口由 loader 的
        ``_blocked_until`` 自己管，网格侧无事可做。
        """
        return

    # ---- 选择 ----
    def select_visible(self):
        """「全选本页可见」：视口内已渲染可选卡 + 可见的资产列表条目。"""
        visible = self.selection_controller.visible_selectable_keys() | self.visible_asset_keys()
        self.selection_controller.set_selected_keys(
            set(self.state.selected_card_keys) | visible
        )
        self._sync_group_checks()

    def visible_asset_keys(self) -> set:
        """视口内可见的「不可预览资产」列表条目（列表与组框相交即算可见）。"""
        viewport = self.scroll_area.viewport()
        viewport_rect = viewport.rect()
        keys: set = set()
        for box in self.group_boxes.values():
            widget = getattr(box, "asset_list", None)
            if widget is None or not widget.isVisible():
                continue
            rect = QtCore.QRect(
                widget.mapTo(viewport, QtCore.QPoint(0, 0)), widget.size()
            )
            if viewport_rect.intersects(rect):
                keys.update(box.asset_keys())
        return keys

    def select_date_range(self, start, end) -> int:
        """选中**已加载**帖子中发布日期落在 ``[start, end]`` 的图；返回新增选中数。

        没加载到范围内的帖子由宿主继续翻页后再调用一次（本方法幂等：重复调用只会
        把新来的帖子补进选择集，不会清掉用户已勾的其它卡）。

        ``start`` / ``end`` 接受 ``QDate`` 或 ``"YYYY-MM-DD"``；只比较日期前缀，
        时区与时分秒都不参与——用户选的是"哪一天"。
        """
        start_s = self._as_date_text(start)
        end_s = self._as_date_text(end)
        if not start_s or not end_s:
            return 0
        if start_s > end_s:
            start_s, end_s = end_s, start_s

        hits = {
            key
            for key in self._selectable_keys
            if start_s <= (self._key_published.get(key) or "")[:10] <= end_s
        }
        hits -= set(self._downloaded_keys)
        if not hits:
            return 0
        merged = set(self.selection_controller.selected_keys()) | hits
        self.selection_controller.set_selected_keys(merged)
        self._sync_group_checks()
        return len(hits)

    @staticmethod
    def _as_date_text(value) -> str:
        text = getattr(value, "toString", None)
        if callable(text):
            return value.toString("yyyy-MM-dd")
        return str(value or "")[:10]

    def oldest_loaded_date(self) -> str:
        """已加载帖子里最早的发布日期（"YYYY-MM-DD"）；用于判断还要不要继续翻页。"""
        dates = [text[:10] for text in self._key_published.values() if text]
        return min(dates) if dates else ""

    def select_post(self, post_id: str):
        """「全选本组」：全组可选键，**含折叠未渲染**（展开时 bind_card 回放）。"""
        box = self.group_boxes.get(post_id)
        if box is None:
            return
        self.selection_controller.set_selected_keys(box.selectable_keys())
        self._sync_group_checks()

    def clear_selection(self):
        self.selection_controller.clear()
        self._sync_group_checks()

    def selected_keys(self) -> set:
        return self.selection_controller.selected_keys()

    def all_selectable_keys(self) -> set:
        """全量可选键（含折叠未渲染）。公开名，selection 不得读 `_selectable_keys`。"""
        return set(self._selectable_keys)

    def selectable_keys(self) -> set:
        """可勾的键 = 全量可选 − 已下载（06 §2.4 的 D-7 修法）。"""
        return set(self._selectable_keys) - set(self._downloaded_keys)

    def visible_card_keys(self) -> set:
        """视口内**已渲染**的卡键（缩略图可见优先 / 全选可见共用口径）。"""
        viewport = self.scroll_area.viewport()
        viewport_rect = viewport.rect()
        visible: set = set()
        for key, card in self.card_widgets.items():
            rect = QtCore.QRect(card.mapTo(viewport, QtCore.QPoint(0, 0)), card.size())
            if viewport_rect.intersects(rect):
                visible.add(key)
        return visible

    def keys_of_post(self, post_id: str) -> list:
        box = self.group_boxes.get(post_id)
        return box.all_keys() if box is not None else []

    def group_of(self, key: CardKey) -> t.Optional[PawchivePostGroup]:
        for group in self._post_groups:
            if group.post_id == key[0]:
                return group
        return None

    def card_for_key(self, key: CardKey):
        return self.card_widgets.get(key)

    @property
    def downloaded_keys(self) -> frozenset:
        """已下载键（含折叠未渲染）—— 06 的唯一真源。"""
        return frozenset(self._downloaded_keys)

    def is_downloaded(self, key: CardKey) -> bool:
        return key in self._downloaded_keys

    def downloaded_counts(self, post_id: str) -> tuple:
        """``(已下, 该帖可下载总数)`` —— 06 §2.4.1 分子/分母口径。

        分子 = ``_downloaded_keys ∩ 帖全量 items``（含折叠未渲染）；
        分母 = 可下载附件数（**不是** ``group.count``：无 URL 的附件不进分母）。
        """
        box = self.group_boxes.get(post_id)
        if box is None:
            return (0, 0)
        downloadable_total = sum(
            1 for item in box.group.items if item.is_downloadable
        )
        return (box.downloaded_count(), downloadable_total)

    def note_downloaded(self, keys: t.Iterable[CardKey]):
        """grid 侧集合写入（selection 调用）。已渲染卡置灰，未渲染键只进集合。"""
        fresh = {k for k in keys if k not in self._downloaded_keys}
        self._downloaded_keys |= fresh
        for key in fresh:
            card = self.card_widgets.get(key)
            if card is not None and hasattr(card, "set_already_downloaded"):
                card.set_already_downloaded(True)

    def apply_downloaded(self, keys: t.Iterable[CardKey]) -> int:
        """已下载回填（06 §2.4 契约名）：并入集合、灰化**已渲染**卡、剔出选中集。

        返回新命中数。折叠未渲染的键只进集合，展开时由 `_add_card` 注入
        ``already_downloaded=True`` 补灰（D-7b）。
        """
        fresh = {k for k in keys if k not in self._downloaded_keys}
        if not fresh:
            return 0
        self._downloaded_keys |= fresh
        for key in fresh:
            card = self.card_widgets.get(key)
            if card is not None:
                if hasattr(card, "set_already_downloaded"):
                    card.set_already_downloaded(True)
                self.state.selected_card_keys.discard(key)
        self._sync_group_checks()
        self.selection_controller.sync_selection_count()
        return len(fresh)

    def mark_download_dirty(self, key: CardKey):
        """zoom-pattern：后台 tab 只记账，激活时 flush（与 danbooru tab 同形）。"""
        self._downloads_dirty.add(key)

    def flush_download_dirty(self) -> int:
        dirty = set(self._downloads_dirty)
        self._downloads_dirty.clear()
        return self.apply_downloaded(dirty) if dirty else 0

    def mark_downloaded(self, keys: t.Iterable[CardKey]):
        """已下载回灌入口（06 消费）：集合 + 已渲染卡 + 选中集全集剔除。"""
        keys = list(keys)
        if not keys:
            return
        self.apply_downloaded(keys)
        self.selection_controller.mark_downloaded(keys)
        self._sync_group_checks()

    def set_card_progress(self, key: CardKey, ratio: t.Optional[float]):
        """卡级进度（06 §2.2 的 L3 绘）：ratio=None → 不定式。"""
        card = self.card_widgets.get(key)
        if card is not None and hasattr(card, "show_progress"):
            card.show_progress(ratio)
        elif card is not None and hasattr(card, "set_progress"):
            card.set_progress(ratio)

    def set_card_download_state(self, key: CardKey, state: t.Any):
        card = self.card_widgets.get(key)
        if card is not None and hasattr(card, "set_download_state"):
            card.set_download_state(state)

    def set_card_failed(self, key: CardKey, error_message: str = ""):
        card = self.card_widgets.get(key)
        if card is not None and hasattr(card, "set_failed_download"):
            card.set_failed_download(True, error_message)

    def active_ring_keys(self) -> list:
        """自查用：当前常驻进度环的键（06 附清单第 1 项：活动环 ≤ 在途数）。"""
        return [
            key
            for key, card in self.card_widgets.items()
            if getattr(card, "has_ring", lambda: False)()
        ]

    def _sync_group_checks(self):
        """组头两个来源分开刷（06 §2.4 / G3）：三态勾归 05，已下计数归 06。"""
        selected = set(self.state.selected_card_keys)
        for post_id, box in self.group_boxes.items():
            box.sync_group_check(selected)
            box.set_downloaded_count(*self.downloaded_counts(post_id))

    def show_grid_context_menu(self, global_pos: QtCore.QPoint, *, card=None):
        """全项目**唯一**的右键菜单工厂（qfluentwidgets RoundMenu）。

        ``card`` 非空时顶部多一项「重新加载缩略图」（仅可预览卡才有）。
        「清空选择」始终可见、无选中时 disabled —— 用户要的是"能点到它、只是
        灰着"，而不是过去那样把取消选择整个删掉。空白区右键 ``card=None``，
        仍是"清空选择 + 下载选中"。
        """
        menu = self._build_grid_context_menu(card=card)
        menu.exec(global_pos)

    def _build_grid_context_menu(self, *, card=None) -> RoundMenu:
        """构造菜单本体（不 exec）；执行入口见 :meth:`show_grid_context_menu`。

        拆出来是给 ``FluentMonkeyPatch.rbutton_menu_widget`` 用的：那边由
        ``_exec_menu`` 统一从**事件**取 ``globalPos()`` 并 exec，菜单工厂只负责
        内容。刻意不走 ``QContextMenuEvent`` 的原生坐标 API —— PySide6 的
        ``QContextMenuEvent`` 没有 ``globalPosition()``，实例里覆写
        ``contextMenuEvent`` 手写坐标的写法 2026-11-19 现场炸过一次
        （AttributeError），右键菜单一律走 monkeypatch。
        """
        menu = RoundMenu(parent=self.scroll_area)
        # 1) 刷新缩略图：仅卡片右键、且该卡确有可渲染预览时给出。
        if card is not None and getattr(card.post, "has_renderable_preview_asset", False):
            reload_action = Action(
                FIF.SYNC,
                text=_tr_or("pawchive.card.reload_preview", "Reload thumbnail"),
                triggered=lambda _c=card: _c.reload_requested.emit(_c.key()),
            )
            menu.addAction(reload_action)
        # 2) 清空选择：始终在，未选中时 disabled（不是移除）。
        clear_action = Action(
            FIF.CANCEL,
            text=_tr_or("pawchive.grid.clear_selection", "Clear"),
            triggered=self.clear_selection,
        )
        clear_action.setEnabled(self.selection_controller.selection_count() > 0)
        menu.addAction(clear_action)
        # 3) 下载选中（排除已下载）。
        selected = set(self.state.selected_card_keys) - set(self._downloaded_keys)
        if selected:
            download_action = Action(
                FIF.DOWNLOAD,
                text=_tr_or("pawchive.download.selected_count", "{count}").format(
                    count=len(selected)
                ),
                triggered=lambda keys=frozenset(selected): self.download_requested.emit(keys),
            )
            menu.addAction(download_action)
        return menu

    # ---- 布局 ----
    def _refresh_grid_layout(self):
        """只刷新内层 FlowLayout；外层交给 QVBoxLayout 自己排（M5 判据口径）。"""
        content_width = max(1, self.scroll_area.viewport().width())
        for box in self.group_boxes.values():
            box._refresh_inner_layout(content_width)
        self.scroll_content.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_grid_layout()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_grid_layout()
        # 触发点 ①（补）：构造期视口高度尚未确定，append 时可见集可能为空。
        visible = self.visible_card_keys()
        if visible:
            self.preview_loader.request(visible, visible=visible)

    def apply_theme(self):
        """组头 + 三态条。卡片与拖选框由 `PawchiveGridTab.apply_theme` 一并刷。

        QSS 按 objectName 选择，设置在网格自身即对全部子孙生效（组头/标签都在
        scroll_content 之下），不需要逐控件 setStyleSheet。
        """
        from .style import build_grid_stylesheet

        self.setStyleSheet(build_grid_stylesheet(self._palette()))
        self.update()

    @staticmethod
    def _palette():
        from .style import PawchiveUiPalette

        return PawchiveUiPalette.current()

    def shutdown(self):
        self._scroll_debounce.stop()
        self.preview_loader.shutdown()


def _missing_fetcher(url: str) -> bytes:
    raise RuntimeError(f"pawchive preview fetcher is not injected: {url}")


__all__ = [
    "COLLAPSED_LIMIT",
    "PawchiveGridState",
    "PawchiveGridWidget",
    "PawchiveGroupCheck",
    "PawchivePostGroupBox",
    "default_card_factory",
]
