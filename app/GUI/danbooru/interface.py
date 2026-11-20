import gc
import re
import shutil
import time
import typing as t
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

from PySide6 import QtCore
from PySide6.QtCore import QSize, Qt, qtTrId, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLineEdit, QPlainTextEdit, QStackedWidget, QTextEdit, QVBoxLayout, QWidget
from qfluentwidgets import PlainTextEdit as FluentPlainTextEdit
from qfluentwidgets import (
    Action, ComboBox, FluentIcon as FIF, FlyoutAnimationType, InfoBar, InfoBarPosition,
    PrimaryToolButton, ProgressBar, RoundMenu, StrongBodyLabel, SubtitleLabel, TabBar,
    TabCloseButtonDisplayMode, TeachingTipTailPosition, ToolButton, TransparentToolButton,
)

from deploy import curr_os
from ui.core.theme import theme_mgr
from ui.async_task import AsyncTaskManager, summarize_error_message
from ui.qfluent import MonkeyPatch as FluentMonkeyPatch
from ui.qfluent.components import BusyRingHost, CountBadge, CustomFlyout, CustomInfoBar, CustomTeachingTip
from ui.qfluent.components.icons import AcggrIcon
from core.script.image.danbooru.client import DanbooruClient
from core.config.qc import danbooru_cfg
from core.script.image.danbooru.constants import DANBOORU_SAVE_TYPE_SEARCH_TAG, DANBOORU_SQL_TABLE
from core.script.image.danbooru.models import DanbooruRuntimeConfig, DanbooruSearchQuery
from core.script import folder_sub
from core.sql import SqlRecorder

from .challenge import DanbooruChallengeController
from .core import DanbooruDownloadController, DanbooruSearchController, DanbooruTabState
from .detail_preview import DetailPreviewController
from app.GUI.log import script_logger
from .page_nav import PageNavController, PageNavState
from .pin_flyout import DanbooruPinTabsFlyout
from .preview_loader import DanbooruPreviewLoader
from .style import (
    CARD_ZOOM_METRICS, DEFAULT_TAB_STATUS_CLASS, DanbooruCardMetrics, DanbooruUiPalette, default_tab_status_text,
    build_interface_stylesheet, build_page_nav_stylesheet, build_tip_line_stylesheet, build_title_label_stylesheet,
    format_tip_rich_text as _format_tip_rich_text, get_danbooru_qss_tokens, qcolor_from_css,
)
from .tab import DanbooruTabWidget
from .tag import (
    build_favorite_groups_state,
    FavoriteTagTranslateController,
    DanbooruTagActionController,
)
from .video_proxy import VideoProxy
from .viewer import DanbooruImageViewer


# A worn middle button bounces one click into two releases (~100ms apart). One bounce
# can close the detail viewer first and land on the tab grid next, so the viewer close
# and the tab close share one debounce timestamp.
MIDDLE_CLICK_CLOSE_DEBOUNCE_MS = 400


class _ViewerSpeedProbe(QtCore.QRunnable):
    """一次性后台测速：把同步 ``ensure_speed`` 挪出 GUI 线程。"""

    def __init__(self, ensure_speed, site: str, url: str, fetcher):
        super().__init__()
        self.setAutoDelete(True)
        self._ensure_speed = ensure_speed
        self._site = site
        self._url = url
        self._fetcher = fetcher

    def run(self):
        try:
            self._ensure_speed(self._site, self._url, self._fetcher)
        except Exception:  # noqa: BLE001 - 测速失败静默，引擎内部也会落负缓存
            pass


def _schedule_viewer_speed_probe(interface: "DanbooruInterface"):
    """首次打开 danbooru 页时测一次下载速度（每进程每站点一次，ensure_speed 自带去重）。

    ``ensure_speed`` 是同步的（首次测速会真的执行 fetcher），所以
    ``QTimer.singleShot(0)`` 只把"提交后台任务"挪出当前事件处理，真正的网络 IO
    在 ``QThreadPool`` 工作线程里跑，绝不卡界面。探针用最小 API 往返、走现有
    request_client 取字节；失败由引擎负缓存静默收口，不打扰用户。
    """
    try:
        from app.GUI.viewer.source_engine import ensure_speed
    except Exception:  # noqa: BLE001 - 引擎缺席是合法降级路径
        return
    probe_url = f"{DanbooruClient.base_url.rstrip('/')}/posts.json?limit=1"

    def _submit():
        QtCore.QThreadPool.globalInstance().start(
            _ViewerSpeedProbe(ensure_speed, "danbooru", probe_url, interface.request_client.fetch_remote_bytes)
        )

    QtCore.QTimer.singleShot(0, _submit)


class DanbooruFuncs:
    def __init__(self, parentInterface: "DanbooruInterface"):
        self.parentInterface = parentInterface
        self.func_menu = None
        self._merge_floder_tip = None

    def open_menu(self):
        func_menu = RoundMenu(parent=self.parentInterface.funcBtn)
        merge_floder_action = Action(AcggrIcon.SCRIPT_MERGE_FLODER.icon(), text=qtTrId("danbooru.action.merge_folders"))
        merge_floder_action.triggered.connect(lambda *_: self.merge_floder())
        func_menu.addAction(merge_floder_action)
        self.func_menu = func_menu
        func_menu.exec(self.parentInterface.funcBtn.mapToGlobal(self.parentInterface.funcBtn.rect().bottomLeft()))

    def merge_floder(self):
        merge_floder_queue = self._build_merge_floder_queue()
        if not merge_floder_queue:
            return
        progress_bar = ProgressBar(self.parentInterface)
        progress_bar.setRange(0, len(merge_floder_queue))
        progress_bar.setValue(0)
        if self._merge_floder_tip is not None:
            self._merge_floder_tip.close()
        tip = CustomTeachingTip.create(
            [progress_bar], target=self.parentInterface.funcBtn, parent=self.parentInterface, tailPosition=TeachingTipTailPosition.RIGHT
        )
        self._merge_floder_tip = tip
        tip.destroyed.connect(lambda *_args, current_tip=tip: self._clear_merge_floder_tip(current_tip))

        def update_progress(value: str):
            done = int(value)
            progress_bar.setValue(done)
            if done >= progress_bar.maximum():
                progress_bar.setCustomBarColor(light="#00ff00", dark="#00cc00")

        self.parentInterface.task_mgr.execute_simple_task(
            self._process_merge_floder_queue,
            progress_callback=update_progress, show_success_info=False, show_error_info=True, show_tooltip=False,
            task_id="danbooru-merge-floder", merge_floder_queue=merge_floder_queue,
        )

    def _clear_merge_floder_tip(self, tip):
        if self._merge_floder_tip is tip:
            self._merge_floder_tip = None

    def _build_merge_floder_queue(self) -> list[tuple[Path, Path]]:
        base_path = Path(self.parentInterface._runtime_config.save_path)
        search_extras = [
            folder_sub.sub("-", danbooru_cfg.canonicalize_term(extra))
            for extra in danbooru_cfg.get_search_extra()
            if danbooru_cfg.canonicalize_term(extra)
        ]
        suffixes = sorted({f" {extra}" for extra in search_extras if extra}, key=len, reverse=True)
        merge_floder_queue = []
        for source_path in base_path.iterdir():
            if not source_path.is_dir():
                continue
            for suffix in suffixes:
                if source_path.name.endswith(suffix):
                    target_name = source_path.name[:-len(suffix)]
                    if target_name:
                        merge_floder_queue.append((source_path, base_path.joinpath(target_name)))
                    break
        return merge_floder_queue

    def _process_merge_floder_queue(self, merge_floder_queue: list[tuple[Path, Path]], progress_callback=None) -> int:
        done = 0
        target_locks = {target_path: Lock() for _, target_path in merge_floder_queue}

        def merge_one(source_path: Path, target_path: Path) -> None:
            with target_locks[target_path]:
                self._merge_one_floder(source_path, target_path)

        with ThreadPoolExecutor(max_workers=min(8, len(merge_floder_queue))) as executor:
            futures = [
                executor.submit(merge_one, source_path, target_path)
                for source_path, target_path in merge_floder_queue
            ]
            for future in as_completed(futures):
                future.result()
                done += 1
                if progress_callback is not None:
                    progress_callback(str(done))
        return done

    @staticmethod
    def _merge_one_floder(source_path: Path, target_path: Path) -> None:
        target_path.mkdir(parents=True, exist_ok=True)
        for child in list(source_path.iterdir()):
            target_child = target_path.joinpath(child.name)
            if target_child.exists():
                if target_child.is_dir():
                    shutil.rmtree(target_child)
                else:
                    target_child.unlink()
            shutil.move(str(child), str(target_child))
        source_path.rmdir()


class DanbooruInterface(QFrame):
    download_result_signal = Signal(str, bool)
    favorites_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.parent_window = parent
        self.setObjectName("DanbooruInterface")
        self.task_mgr = AsyncTaskManager(self)
        self.funcs = DanbooruFuncs(self)
        self.tabs: dict[str, DanbooruTabWidget] = {}
        self.tab_states: dict[str, DanbooruTabState] = {}
        self._runtime_config = DanbooruRuntimeConfig.from_conf()
        self.request_client = DanbooruClient(runtime_config=self._runtime_config)
        self.sql_recorder = SqlRecorder(table=DANBOORU_SQL_TABLE)
        self.video_proxy = VideoProxy(self.request_client, self)
        self.image_viewer = DanbooruImageViewer(parent)
        # viewer 是常驻实例（construct 一次、整个会话复用），所以只在
        # DanbooruImageViewer.__init__ 里读一次配置，用户在设置页拨开关时当前会话
        # 看不到任何变化。这里把「默认置顶」接到 pin 本身：配置变了就同步按钮勾选、
        # Qt flag 和 Win32 TOPMOST；用户手动点 pin 仍是即时覆盖，不回写配置。
        danbooru_cfg.isDanbooruViewerDafaultPin.valueChanged.connect(
            self._on_viewer_default_pin_changed)
        self.detail_preview_controller = DetailPreviewController(self, self.image_viewer)
        self.search_controller = DanbooruSearchController(self)
        self.download_controller = DanbooruDownloadController(self)
        self.challenge_controller = DanbooruChallengeController(self)
        self.page_nav = PageNavController(self)
        self.favorite_translate = FavoriteTagTranslateController(self)
        self.tag_action_controller = DanbooruTagActionController(
            self, browser_opener=self._open_imgpalace_browser,
        )
        self.zoom_mgr = self._ZoomMgr(self)
        self.tab_mgr = self._TabMgr(self)
        self._gc_generation = 0
        # 预览加载器：网格缩略图与详情大图共用一个实例（跨 tab url 去重、双层 LRU）。
        # 归 interface 所有，生命周期与本界面一致。
        self.preview_loader = DanbooruPreviewLoader(self.request_client, parent=self)
        self.search_controller.bind_preview_loader(self.preview_loader)
        self.detail_preview_controller.bind_preview_loader(self.preview_loader)
        self.image_viewer.middle_click_closed.connect(self.tab_mgr.note_middle_click_close)
        self._infobars_by_key = {}
        self._favorites_state_cache = None
        self.download_result_signal.connect(self.download_controller.on_download_result)
        self.image_viewer.tag_clicked.connect(self._open_tag_jump_tab)
        self.image_viewer.export_panel_requested.connect(self._open_tag_export_panel)
        self.image_viewer.export_attach_requested.connect(self._attach_tag_export)
        self.image_viewer.download_requested.connect(self.download_controller.submit_single)
        self.image_viewer.previous_requested.connect(lambda: self.detail_preview_controller.open_adjacent(-1))
        self.image_viewer.next_requested.connect(lambda: self.detail_preview_controller.open_adjacent(1))
        self.image_viewer.closed.connect(self.detail_preview_controller.clear_context)
        self._install_key_event_filter()
        theme_mgr.subscribe(self._apply_theme)
        self.setupUi()
        self._apply_theme()
        self.tab_mgr.create()
        # pin tab 懒加载：danbooru 页（AcggrMainWindow 懒工厂）首次构造时才恢复，
        # 每个 pin tab 都要真建 widget 并真正发起一次搜索（懒加载 = 推迟 IO 到本页，
        # 不是不加载）。
        self.tab_mgr.restore_pin_tabs()

    def server_mode_switch_blockers(self) -> list[str]:
        if self.task_mgr.get_running_tasks():
            return ["danbooru script"]
        return []

    def _on_viewer_default_pin_changed(self, enabled):
        """设置页「默认置顶」变更 → detail viewer 的 pin 立即跟随。

        走 ``keep_top_hint`` 这一条既有路径：按钮勾选态、Qt ``WindowStaysOnTopHint``
        与 Win32 ``HWND_TOPMOST`` 三者一起变，和用户手点 pin 完全同一套逻辑。
        """
        self.image_viewer.keep_top_hint(bool(enabled))

    def _install_key_event_filter(self):
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)

    def _install_tab_context_menu_filter(self, tab_bar: TabBar):
        """tab 右键菜单走 ``MonkeyPatch.rbutton_menu_tabBar``（Fluent
        ``RoundMenu``）。``TabItem`` 把鼠标事件 forward 给 ``TabBar.view``，
        右键到不了 viewport，不能用 eventFilter。"""
        def build_menu(route_key: str) -> RoundMenu:
            is_pin_bar = tab_bar is self.pin_tab_bar
            menu = RoundMenu(parent=tab_bar)
            if is_pin_bar:
                pin_action = Action(AcggrIcon.SCRIPT_PIN.icon(), text=qtTrId("danbooru.tab.unpin"))
                pin_action.triggered.connect(lambda *_: self.tab_mgr.unpin_tab(route_key))
            else:
                pin_action = Action(AcggrIcon.SCRIPT_PIN.icon(), text=qtTrId("danbooru.tab.pin"))
                pin_action.triggered.connect(lambda *_: self.tab_mgr.pin_tab(route_key))
            menu.addAction(pin_action)
            close_action = Action(FIF.CLOSE, text=qtTrId("danbooru.tab.close"))
            close_action.triggered.connect(lambda *_: self.tab_mgr.close_by_id(route_key))
            menu.addAction(close_action)
            return menu

        FluentMonkeyPatch.rbutton_menu_tabBar(tab_bar, build_menu)

    def _open_pin_flyout(self):
        """pin 组左上角图标 → 飞出 pinned tab 列表；点任一行切到该 tab。"""
        entries = [
            (tab_id, self.tab_states[tab_id].title)
            for tab_id in self.tab_mgr.pin_order
            if tab_id in self.tab_states
        ]
        view = DanbooruPinTabsFlyout(entries, self)
        view.tab_selected.connect(self.tab_mgr.set_current)
        CustomFlyout.make(view, self.pin_badge_btn, self.window(), aniType=FlyoutAnimationType.DROP_DOWN)

    def eventFilter(self, obj, event):
        if self._handle_interface_key_press(obj, event):
            return True
        return super().eventFilter(obj, event)

    def setupUi(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(14, 12, 14, 14)
        self.main_layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 20, 0)
        title_row.setSpacing(4)
        self.title_block = QWidget(self)
        self.title_block.setObjectName("DanbooruTitleBlock")
        title_block_layout = QHBoxLayout(self.title_block)
        title_block_layout.setContentsMargins(16, 12, 16, 12)
        title_block_layout.setSpacing(16)
        self.title_label = SubtitleLabel("Danbooru", self)
        self.tip_line = StrongBodyLabel("", self)
        self.tip_line.setTextFormat(Qt.RichText)
        self.tip_line.setObjectName("DanbooruTipLine")
        title_block_layout.addWidget(self.title_label)
        title_block_layout.addWidget(self.tip_line, 1)
        zoomBtnGroup = QVBoxLayout()
        zoomBtnGroup.setContentsMargins(2,0,2,0)
        zoomBtnGroup.setSpacing(0)
        self.zoomIn = ToolButton(QIcon(':/zoomin.svg'))
        self.zoomIn.setMaximumHeight(22)
        self.zoomOut = ToolButton(QIcon(':/zoomout.svg'))
        self.zoomOut.setMaximumHeight(22)
        zoomBtnGroup.addWidget(self.zoomIn)
        zoomBtnGroup.addWidget(self.zoomOut)
        self.funcBtn = ToolButton(FIF.APPLICATION, self)
        self.funcBtn.setIconSize(QSize(20, 20))
        self.funcBtn.setMinimumHeight(50)
        self.favMgrBtn = ToolButton(AcggrIcon.SCRIPT_FAV_MGR, self)
        self.favMgrBtn.setObjectName("FavMgrBtn")
        self.favMgrBtn.setIconSize(QSize(20, 20))
        self.favMgrBtn.setMinimumHeight(50)
        # 收藏翻译有确定进度（done/total）：双环变种，busy 时取代 favMgrBtn 槽位。
        self.favTranslateRingHost = BusyRingHost.make(self, self.favMgrBtn, determinate=True,
                                                      object_name="FavTranslateRingHost")
        self.favTranslateRingHost.setToolTip(qtTrId("danbooru.tag.translating"))
        self.openBtn = ToolButton(FIF.FOLDER)
        self.openBtn.setMinimumHeight(50)
        self.batch_download_btn = PrimaryToolButton(FIF.DOWNLOAD, self)
        self.batch_download_btn.setMinimumHeight(50)
        self.batch_download_btn.setMinimumWidth(80)
        self.batch_download_btn.setIconSize(QtCore.QSize(20, 20))
        self.batch_download_btn.setDisabled(True)
        self.batch_download_badge = CountBadge(parent=self, target=self.batch_download_btn)
        self.batch_download_badge.hide()
        title_row.addWidget(self.title_block, 1)
        title_row.addLayout(zoomBtnGroup)
        title_row.addWidget(self.funcBtn)
        title_row.addWidget(self.favMgrBtn)
        title_row.addWidget(self.favTranslateRingHost, 0, Qt.AlignVCenter)
        title_row.addWidget(self.openBtn)
        title_row.addWidget(self.batch_download_btn)
        self.main_layout.addLayout(title_row)
        self._title_row_layout = title_row

        self.pivot_shell = QFrame(self)
        self.pivot_shell.setObjectName("DanbooruPivotShell")
        self.pivot_shell.setMinimumHeight(34)
        # 单行双组：左 pinTabsGroup / 右 NormalTabsGroup，**各自一张独立圆角卡片**。
        # 外层 shell 透明无框（边框只归组容器，杜绝「大框套小框」），组间 spacing 10
        # 露出底层窗口背景；每组内部 = 左箭头 + tabs + 右箭头（两套独立横向滚动区）。
        pivot_shell_layout = QHBoxLayout(self.pivot_shell)
        pivot_shell_layout.setContentsMargins(0, 0, 0, 0)
        pivot_shell_layout.setSpacing(10)

        # ---- pinTabsGroup（左组，独立卡片）----
        self.pin_tabs_group = QFrame(self.pivot_shell)
        self.pin_tabs_group.setObjectName("DanbooruPinTabsGroup")
        pin_group_layout = QHBoxLayout(self.pin_tabs_group)
        pin_group_layout.setContentsMargins(8, 2, 8, 2)
        pin_group_layout.setSpacing(6)
        # 滑动容器左上角的 pin 标记：点一下飞出 pinned tab 列表（组被滚挤时的出口）
        self.pin_badge_btn = TransparentToolButton(AcggrIcon.SCRIPT_PIN, self.pin_tabs_group)
        self.pin_badge_btn.setObjectName("DanbooruPinBadgeButton")
        self.pin_badge_btn.setFixedSize(16, 16)
        self.pin_badge_btn.setIconSize(QtCore.QSize(14, 14))
        self.pin_badge_btn.setToolTip(qtTrId("danbooru.tab.pin_list"))
        self.pin_badge_btn.clicked.connect(self._open_pin_flyout)
        self.pin_back_btn = TransparentToolButton(FIF.LEFT_ARROW, self.pin_tabs_group)
        self.pin_back_btn.setObjectName("DanbooruPivotScrollButton")
        self.pin_back_btn.setFixedSize(18, 18)
        self.pin_back_btn.setIconSize(QtCore.QSize(14, 14))
        self.pin_tab_bar = TabBar(self.pin_tabs_group)
        self.pin_tab_bar.setObjectName("DanbooruPinScrollArea")
        self.pin_tab_bar.view.setObjectName("DanbooruPinTabBarView")
        self.pin_tab_bar.setAddButtonVisible(False)
        self.pin_tab_bar.setCloseButtonDisplayMode(TabCloseButtonDisplayMode.ON_HOVER)
        self.pin_tab_bar.setTabShadowEnabled(False)
        self.pin_tab_bar.setTabMaximumWidth(148)
        self.pin_tab_bar.setTabMinimumWidth(96)
        self.pin_tab_bar.enableTransparentBackground()
        self.pin_tab_bar.itemLayout.setContentsMargins(0, 5, 0, 5)
        self.pin_tab_bar.itemLayout.setSpacing(6)
        self.pin_tab_bar.setFixedHeight(40)
        self.pin_forward_btn = TransparentToolButton(FIF.RIGHT_ARROW, self.pin_tabs_group)
        self.pin_forward_btn.setObjectName("DanbooruPivotScrollButton")
        self.pin_forward_btn.setFixedSize(18, 18)
        self.pin_forward_btn.setIconSize(QtCore.QSize(14, 14))
        pin_group_layout.addWidget(self.pin_badge_btn, 0, Qt.AlignTop)
        pin_group_layout.addWidget(self.pin_back_btn, 0, Qt.AlignVCenter)
        pin_group_layout.addWidget(self.pin_tab_bar, 1)
        pin_group_layout.addWidget(self.pin_forward_btn, 0, Qt.AlignVCenter)
        self.pin_tabs_group.hide()
        pivot_shell_layout.addWidget(self.pin_tabs_group, 0)

        # ---- NormalTabsGroup（右组，独立卡片）----
        self.normal_tabs_group = QFrame(self.pivot_shell)
        self.normal_tabs_group.setObjectName("DanbooruNormalTabsGroup")
        normal_group_layout = QHBoxLayout(self.normal_tabs_group)
        normal_group_layout.setContentsMargins(8, 2, 8, 2)
        normal_group_layout.setSpacing(6)
        self.pivot_back_btn = TransparentToolButton(FIF.LEFT_ARROW, self.normal_tabs_group)
        self.pivot_back_btn.setObjectName("DanbooruPivotScrollButton")
        self.pivot_back_btn.setFixedSize(18, 18)
        self.pivot_back_btn.setIconSize(QtCore.QSize(14, 14))
        self.tab_bar = TabBar(self.normal_tabs_group)
        self.tab_bar.setObjectName("DanbooruPivotScrollArea")
        self.tab_bar.view.setObjectName("DanbooruPivotTabBarView")
        self.tab_bar.setAddButtonVisible(False)
        self.tab_bar.setCloseButtonDisplayMode(TabCloseButtonDisplayMode.ON_HOVER)
        self.tab_bar.setTabShadowEnabled(False)
        self.tab_bar.setTabMaximumWidth(148)
        self.tab_bar.setTabMinimumWidth(96)
        self.tab_bar.enableTransparentBackground()
        self.tab_bar.itemLayout.setContentsMargins(0, 5, 0, 5)
        self.tab_bar.itemLayout.setSpacing(6)
        self.pivot_scroll = self.tab_bar
        self.pivot_forward_btn = TransparentToolButton(FIF.RIGHT_ARROW, self.normal_tabs_group)
        self.pivot_forward_btn.setObjectName("DanbooruPivotScrollButton")
        self.pivot_forward_btn.setFixedSize(18, 18)
        self.pivot_forward_btn.setIconSize(QtCore.QSize(14, 14))
        normal_group_layout.addWidget(self.pivot_back_btn, 0, Qt.AlignVCenter)
        normal_group_layout.addWidget(self.tab_bar, 1)
        normal_group_layout.addWidget(self.pivot_forward_btn, 0, Qt.AlignVCenter)
        pivot_shell_layout.addWidget(self.normal_tabs_group, 1)

        pivot_scroll_bar = self.pivot_scroll.horizontalScrollBar()
        pin_scroll_bar = self.pin_tab_bar.horizontalScrollBar()

        self.content_shell = QFrame(self)
        self.content_shell.setObjectName("DanbooruContentShell")
        content_shell_layout = QVBoxLayout(self.content_shell)
        content_shell_layout.setContentsMargins(12, 12, 12, 12)
        content_shell_layout.setSpacing(0)
        self.stacked_widget = QStackedWidget(self.content_shell)
        content_shell_layout.addWidget(self.stacked_widget)
        # binding
        self.zoomIn.clicked.connect(self.zoom_mgr.zoom_in)
        self.zoomOut.clicked.connect(self.zoom_mgr.zoom_out)
        self.funcBtn.clicked.connect(self.funcs.open_menu)
        self.favMgrBtn.clicked.connect(self._open_favorite_manager)
        self.openBtn.clicked.connect(self._open_save_path)
        self.batch_download_btn.clicked.connect(self.download_controller.submit_selected)
        # tabBarClicked 每次点击必发（currentChanged 在 index==currentIndex 时会被
        # qfluentwidgets 吞掉：pin 组建 tab 时就已把 _currentIndex 置 0，点第一个
        # pin tab 永远不发 → 瀑布流切不过去）。激活一律走 tabBarClicked。
        self.tab_bar.tabBarClicked.connect(
            lambda index, bar=self.tab_bar: self.tab_mgr.on_tabbar_index_changed(index, bar))
        self.pin_tab_bar.tabBarClicked.connect(
            lambda index, bar=self.pin_tab_bar: self.tab_mgr.on_tabbar_index_changed(index, bar))
        self.pivot_back_btn.clicked.connect(lambda: self.tab_mgr.scroll_tabs(-1))
        self.pivot_forward_btn.clicked.connect(lambda: self.tab_mgr.scroll_tabs(1))
        self.pin_back_btn.clicked.connect(lambda: self.tab_mgr.scroll_tabs(-1, self.pin_tab_bar))
        self.pin_forward_btn.clicked.connect(lambda: self.tab_mgr.scroll_tabs(1, self.pin_tab_bar))
        self.tab_bar.tabCloseRequested.connect(
            lambda index, bar=self.tab_bar: self.tab_mgr.on_tab_close_requested(index, bar))
        self.pin_tab_bar.tabCloseRequested.connect(
            lambda index, bar=self.pin_tab_bar: self.tab_mgr.on_tab_close_requested(index, bar))
        pivot_scroll_bar.rangeChanged.connect(lambda *_args: self.tab_mgr.sync_scroll_controls())
        pivot_scroll_bar.valueChanged.connect(lambda *_args: self.tab_mgr.sync_scroll_controls())
        pin_scroll_bar.rangeChanged.connect(lambda *_args: self.tab_mgr.sync_pin_scroll_controls())
        pin_scroll_bar.valueChanged.connect(lambda *_args: self.tab_mgr.sync_pin_scroll_controls())
        self.stacked_widget.currentChanged.connect(self.tab_mgr.on_current_tab_changed)
        self._install_tab_context_menu_filter(self.tab_bar)
        self._install_tab_context_menu_filter(self.pin_tab_bar)
        
        self.main_layout.addWidget(self.pivot_shell)
        self.main_layout.addWidget(self.content_shell, 1)
        self.page_nav.attach(self)
        self.page_nav.page_jump_requested.connect(self._on_page_nav_jump)
        self.page_nav.jump_rejected.connect(self._on_page_nav_rejected)
        self.sync_page_nav()

    def sync_page_nav(self, tab_id: t.Optional[str] = None):
        active_tab_id = tab_id or self.tab_mgr.active_tab_id()
        state = self.tab_states.get(active_tab_id) if active_tab_id else None
        if state is None:
            self.page_nav.set_state(
                PageNavState.from_counts(current_page=1, total_count=None, query_is_empty=True, page_size=30)
            )
            return
        total_count = state.total_count if state.count_query_key == state.count_cache_key() else None
        self.page_nav.set_state(
            PageNavState.from_counts(
                current_page=state.page_cursor,
                total_count=total_count,
                query_is_empty=not bool(state.query),
                page_size=30,
                loading=state.loading,
            )
        )

    def _on_page_nav_jump(self, page: int):
        tab_id = self.tab_mgr.active_tab_id()
        if not tab_id:
            return
        self.search_controller.jump_to_page(tab_id, int(page))

    def _on_page_nav_rejected(self, reason: str, target_page: object):
        if reason == "home_soft_cap":
            self._show_info(
                InfoBar.warning,
                qtTrId("danbooru.pagination.soft_cap").format(target_page=target_page),
                4500,
            )
            return
        if reason == "above_cap":
            self._show_info(InfoBar.warning, qtTrId("danbooru.pagination.out_of_range").format(target_page=target_page), 3500)
            return
        if reason == "same_page":
            return
        if reason == "below_min" or reason == "invalid":
            self._show_info(InfoBar.warning, qtTrId("danbooru.pagination.invalid"), 3000)

    def _apply_theme(self, *_args):
        # Intentional exception to the global-push pattern (invariant 7): theme switches
        # are low-frequency, user-triggered, one-shot, and require whole-page consistency.
        palette = DanbooruUiPalette.current()
        self.setStyleSheet(build_interface_stylesheet(palette))
        self.title_label.setStyleSheet(build_title_label_stylesheet(palette))
        self.tip_line.setStyleSheet(build_tip_line_stylesheet(palette))
        self.tab_bar.setTabShadowEnabled(False)
        selected_color = qcolor_from_css(palette.pivot_selected)
        self.tab_bar.setTabSelectedBackgroundColor(selected_color, selected_color)
        self.pin_tab_bar.setTabShadowEnabled(False)
        self.pin_tab_bar.setTabSelectedBackgroundColor(selected_color, selected_color)
        self.image_viewer.apply_theme()
        for tab in self.tabs.values():
            tab.apply_theme()
        self.page_nav.apply_theme(
            stylesheet=build_page_nav_stylesheet(palette),
            shadow_color=qcolor_from_css(get_danbooru_qss_tokens()["PAGE_NAV_FAB_SHADOW"]),
        )
        self.tab_mgr.update_chrome()
        self.tab_mgr.sync_tip_line()
        self.refresh_runtime_settings()
        self.zoom_mgr.sync_buttons()
        self.tab_mgr.sync_bar_width()
        self.page_nav.reposition()
        self.sync_page_nav()

    def eventFilter(self, obj, event):
        if self._handle_interface_key_press(obj, event):
            return True
        return super().eventFilter(obj, event)

    def _handle_interface_key_press(self, obj, event) -> bool:
        if event.type() != QtCore.QEvent.KeyPress:
            return False
        if not self._is_active_interface_key_target():
            return False
        if self._is_editable_key_target(obj):
            return False
        if not event.modifiers() & Qt.KeypadModifier:
            return False
        active_tab = self._active_tab_widget()
        if active_tab is None:
            return False
        key = event.key()
        if key in (Qt.Key_4, Qt.Key_Left):
            active_tab.apply_first_extra_search()
        elif key in self._keypad_num5_keys():
            if active_tab.favorite_btn.isEnabled():
                active_tab.favorite_btn.click()
        elif key in (Qt.Key_6, Qt.Key_Right):
            active_tab.apply_score_sort_search()
        else:
            return False
        event.accept()
        return True

    def _is_active_interface_key_target(self) -> bool:
        if not self.isVisible():
            return False
        active_window = QApplication.activeWindow()
        return active_window is not None and active_window is self.window()

    @staticmethod
    def _is_editable_key_target(obj) -> bool:
        editable_widgets = (QLineEdit, QTextEdit, QPlainTextEdit, FluentPlainTextEdit)
        focus_widget = QApplication.focusWidget()
        return isinstance(focus_widget, editable_widgets) or isinstance(obj, editable_widgets)

    def _active_tab_widget(self) -> t.Optional[DanbooruTabWidget]:
        tab_id = self.tab_mgr.active_tab_id()
        return self.tabs.get(tab_id) if tab_id else None

    @staticmethod
    def _keypad_num5_keys() -> set:
        keys = {Qt.Key_5}
        key_clear = getattr(Qt, "Key_Clear", None)
        if key_clear is not None:
            keys.add(key_clear)
        return keys

    class _ZoomMgr:
        def __init__(self, interface: "DanbooruInterface"):
            self.interface = interface
            self.zoom_index = int(danbooru_cfg.zoom_index.value)
            self.generation = 0

        def current_metrics(self) -> DanbooruCardMetrics:
            return CARD_ZOOM_METRICS[self.zoom_index]

        def zoom_in(self):
            if self.zoom_index >= len(CARD_ZOOM_METRICS) - 1:
                return
            self.zoom_index += 1
            danbooru_cfg.zoom_index.value = self.zoom_index
            danbooru_cfg.save()
            self.apply_current()
            self.sync_buttons()

        def zoom_out(self):
            if self.zoom_index <= 0:
                return
            self.zoom_index -= 1
            danbooru_cfg.zoom_index.value = self.zoom_index
            danbooru_cfg.save()
            self.apply_current()
            self.sync_buttons()

        def sync_buttons(self):
            self.interface.zoomIn.setEnabled(self.zoom_index < len(CARD_ZOOM_METRICS) - 1)
            self.interface.zoomOut.setEnabled(self.zoom_index > 0)

        def apply_current(self, *, active_tab_id: t.Optional[str] = None):
            self.generation += 1
            metrics = self.current_metrics()
            active_tab_id = self.interface.tab_mgr.active_tab_id() if active_tab_id is None else active_tab_id
            for tab_id, tab in self.interface.tabs.items():
                if tab_id == active_tab_id:
                    tab.zoom_mgr.apply_active(metrics=metrics)
                else:
                    tab.zoom_mgr.mark_hidden_target(metrics=metrics)

        def sync_tab(self, tab_id: str):
            tab = self.interface.tabs.get(tab_id)
            if tab is None:
                return
            tab.zoom_mgr.sync_to(metrics=self.current_metrics())

        def forget(self, tab_id: str):
            return

        def shutdown(self):
            return

    class _TabMgr:
        def __init__(self, interface: "DanbooruInterface"):
            self.interface = interface
            self.counter = 0
            self.tips: dict[str, tuple[str, str]] = {}
            self.httpx_status: dict[str, str] = {}
            self.activation_order: list[str] = []
            self._zoom_sync_generation = 0
            self._last_middle_click_at = 0.0
            #: pinTabsGroup 的 tab_id 顺序表（= qconfig tabPinTabs 的内存镜像）。
            self.pin_order: list[str] = []
            #: restore_pin_tabs 批量建 tab 时置 True：跳过逐 tab 持久化与 chrome 抖动。
            self._restoring_pin_tabs = False
            #: sync_pin_group 同步重入闸：setFixedWidth → 布局 → 滚动条信号 → 再进本方法。
            self._syncing_pin_group = False

        def create(self, initial_query: str = "", auto_search: bool = False) -> str:
            self.counter += 1
            tab_id = f"danbooru-tab-{self.counter}"
            state = DanbooruTabState(
                tab_id=tab_id, title=self.display_title_for_query(initial_query, self.counter),
                query=DanbooruSearchQuery.normalize(initial_query),
            )
            tab = DanbooruTabWidget(state, self.interface)
            tab.setObjectName(tab_id)
            tab.zoom_mgr.apply_immediate(metrics=self.interface.zoom_mgr.current_metrics(), refresh_preview=True)
            tab.request_search.connect(lambda query, tid=tab_id: self.interface.search_controller.start_search(tid, query))
            tab.request_conversion.connect(lambda tid=tab_id: self.interface.search_controller.convert_term(tid))
            tab.request_single_download.connect(lambda post, tid=tab_id: self.interface.download_controller.submit_single(post, tid))
            tab.request_tag_jump.connect(self.interface._open_tag_jump_tab)
            tab.request_next_page.connect(lambda tid=tab_id: self.interface.search_controller.load_next_page(tid))
            tab.request_close.connect(self.close_current)
            tab.request_extra_search.connect(lambda term, tid=tab_id: self.interface._on_extra_search(tid, term))
            tab.detail_opened.connect(lambda post, tid=tab_id: self.interface.detail_preview_controller.open_viewer(tid, post))
            tab.selection_count_changed.connect(lambda _count, tid=tab_id: self.interface._update_batch_button(tid))
            tab.favorite_btn.clicked.connect(lambda _=False, tid=tab_id: self.interface._toggle_favorite(tid))
            self.interface.favorites_changed.connect(tab.mark_favorites_dirty)
            self.interface.tabs[tab_id] = tab
            self.interface.tab_states[tab_id] = state
            self.tips[tab_id] = (default_tab_status_text(), DEFAULT_TAB_STATUS_CLASS)
            self.httpx_status[tab_id] = ""
            self.interface.stacked_widget.addWidget(tab)
            self.interface.tab_bar.addTab(routeKey=tab_id, text=state.title)
            self.sync_bar_width()
            self.set_current(tab_id)
            self.update_chrome()
            self.interface._refresh_completer(tab)
            if state.query:
                tab.search_edit.setText(state.query)
                if auto_search:
                    self.interface.search_controller.start_search(tab_id, state.query)
            if self.active_tab_id() == tab_id:
                self.sync_tip_line(tab_id)
            return tab_id

        # ---- pinTabsGroup（左组）----
        def bar_of_tab(self, tab_id: str) -> t.Optional[TabBar]:
            """tab 当前挂在哪个栏（pin 栏优先判断，与 TabBar.itemMap 实况对齐）。"""
            if self.interface.pin_tab_bar.tab(tab_id) is not None:
                return self.interface.pin_tab_bar
            if self.interface.tab_bar.tab(tab_id) is not None:
                return self.interface.tab_bar
            return None

        def bar_for_tab(self, tab_id: str) -> t.Optional[TabBar]:
            return self.bar_of_tab(tab_id)

        def pin_tab(self, tab_id: str):
            state = self.interface.tab_states.get(tab_id)
            if state is None or not DanbooruSearchQuery.normalize(state.query):
                return
            if tab_id in self.pin_order:
                return
            self.interface.tab_bar.removeTabByKey(tab_id)
            self.interface.pin_tab_bar.addTab(routeKey=tab_id, text=state.title)
            self.pin_order.append(tab_id)
            self.persist_pin_tabs()
            self.sync_pin_group()
            self.set_current(tab_id)
            self.update_chrome()

        def unpin_tab(self, tab_id: str):
            if tab_id not in self.pin_order:
                return
            self.interface.pin_tab_bar.removeTabByKey(tab_id)
            self.interface.tab_bar.addTab(routeKey=tab_id, text=self.interface.tab_states[tab_id].title)
            self.pin_order.remove(tab_id)
            self.persist_pin_tabs()
            self.sync_pin_group()
            self.set_current(tab_id)
            self.update_chrome()

        def persist_pin_tabs(self):
            if self._restoring_pin_tabs:
                return
            danbooru_cfg.save_pin_tabs(
                [self.interface.tab_states[tab_id].query for tab_id in self.pin_order]
            )

        def restore_pin_tabs(self):
            """进入 danbooru 页时**懒加载**所有 pin tab。

            「懒」= 推迟到本页首次构造才做（不是不加载）：每个 pin tab 都要
            建 widget **并真正发起一次搜索**，否则用户看到的是一个空壳 tab。
            """
            self._restoring_pin_tabs = True
            try:
                for query in danbooru_cfg.get_pin_tabs():
                    tab_id = self.create(initial_query=query, auto_search=True)
                    self.pin_tab(tab_id)
            finally:
                self._restoring_pin_tabs = False
            self.sync_pin_group()
            self.update_chrome()
            self.sync_bar_width()

        @staticmethod
        def _tab_items_width(tab_bar: TabBar) -> int:
            """tab 真实内容宽 = 各 TabItem 宽之和 + item 间距 + 布局边距。

            不能用 ``tab_bar.view.size().width()``：TabBar 是 ScrollArea，``view`` 被
            widgetResizable 撑到视口宽，量出来恒等于可用宽。只能按 item 逐个累加。
            """
            items = tab_bar.items
            if not items:
                return 0
            spacing = tab_bar.itemLayout.spacing()
            margins = tab_bar.itemLayout.contentsMargins()
            return (
                sum(item.sizeHint().width() for item in items)
                + spacing * max(len(items) - 1, 0)
                + margins.left() + margins.right()
            )

        def sync_pin_group(self):
            """pinTabsGroup 宽度：真实 tab 内容宽夹在 [2.5 tab-min, 父宽 50%]。

            下界保证 2.5 个 tab 可见；上界是父组件（pin + normal 同行）宽的 50%，
            超出部分交给组内横向滚动条；中间**贴内容宽**，所以 tab 少时不会
            无脑吃满 50%。空 pin 组整组隐藏。
            重入闸：``setFixedWidth`` 布局期间滚动条信号可能同步回调进来，直接丢弃。
            """
            pin_group = self.interface.pin_tabs_group
            if not self.pin_order:
                pin_group.hide()
                return
            if self._syncing_pin_group:
                return
            self._syncing_pin_group = True
            try:
                pin_bar = self.interface.pin_tab_bar
                pin_bar.updateGeometry()
                pin_group.show()
                chrome = 2 + 16 + 16 + 3 * 6 + 2 * 18   # 边框 + margins + pin 标记 + 3 处间距 + 一对箭头
                minimum_width = int(2.5 * pin_bar.tabMinimumWidth()) + chrome
                maximum_width = max(minimum_width, self.interface.pivot_shell.width() // 2)
                content_width = self._tab_items_width(pin_bar) + chrome
                pin_group.setFixedWidth(max(minimum_width, min(content_width, maximum_width)))
                pin_bar.horizontalScrollBar().setValue(0)
                self.sync_pin_scroll_controls()
            finally:
                self._syncing_pin_group = False

        def close_current(self):
            # Timestamp is shared across tabs: the bounced release lands on whichever tab
            # became current after the first close, so per-tab state cannot catch it.
            now = time.monotonic()
            if (now - self._last_middle_click_at) * 1000.0 < MIDDLE_CLICK_CLOSE_DEBOUNCE_MS:
                return
            self._last_middle_click_at = now
            self.close_by_id(self.active_tab_id())

        def note_middle_click_close(self):
            # The viewer hides on the first release of a middle click. A worn button's
            # bounced second release is delivered to the grid now visible underneath,
            # so the viewer must arm the same guard the grid uses.
            self._last_middle_click_at = time.monotonic()

        def close_by_id(self, tab_id: t.Optional[str]):
            if len(self.interface.tabs) <= 1 or not tab_id:
                return
            is_active_tab = self.active_tab_id() == tab_id
            next_tab_id = self.previous_live_tab_id(tab_id) if is_active_tab else None
            tab = self.interface.tabs.pop(tab_id, None)
            self.interface.tab_states.pop(tab_id, None)
            self.tips.pop(tab_id, None)
            self.httpx_status.pop(tab_id, None)
            self.drop_activation(tab_id)
            self.interface.zoom_mgr.forget(tab_id)
            if tab is None:
                return
            if self.interface.detail_preview_controller.current_tab_id == tab_id and self.interface.image_viewer.isVisible():
                self.interface.image_viewer.hide()
                self.interface.detail_preview_controller.clear_context()
            if tab_id in self.pin_order:
                self.pin_order.remove(tab_id)
                self.interface.pin_tab_bar.removeTabByKey(tab_id)
                self.persist_pin_tabs()
                self.sync_pin_group()
            else:
                self.interface.tab_bar.removeTabByKey(tab_id)
            self.interface.stacked_widget.removeWidget(tab)
            tab.deleteLater()
            self.interface.preview_loader.cancel_tab(tab_id)
            self.interface.schedule_deferred_gc()
            if is_active_tab and next_tab_id:
                self.set_current(next_tab_id)
            elif self.interface.stacked_widget.count():
                fallback_widget = self.interface.stacked_widget.currentWidget() or self.interface.stacked_widget.widget(0)
                if fallback_widget is not None:
                    self.set_current(fallback_widget.objectName())
            self.update_chrome()
            self.sync_bar_width()

        def set_current(self, tab_id: str):
            tab = self.interface.tabs.get(tab_id)
            if tab is None:
                return
            previous_widget = self.interface.stacked_widget.currentWidget()
            previous_tab_id = previous_widget.objectName() if previous_widget is not None else None
            if previous_tab_id and previous_tab_id != tab_id:
                previous_tab = self.interface.tabs.get(previous_tab_id)
                if previous_tab is not None:
                    previous_tab.zoom_mgr.suspend_hidden()
            self.interface.stacked_widget.setCurrentWidget(tab)
            self.activate_tab_chrome(tab_id)
            self.record_activation(tab_id)
            self.interface._update_batch_button(tab_id)
            self.interface.sync_page_nav(tab_id)

        def activate_tab_chrome(self, tab_id: str):
            """把「当前 tab」的选中态落到它所在的栏（pin 栏/常规栏互不抢 current）。

            选中态跨栏全局唯一：另一栏里残留的选中 item 要手动摘掉 ——
            ``setCurrentTab`` 只会反选本栏内的旧 item，跨栏切换会留下双选中。
            """
            for tab_bar in (self.interface.pin_tab_bar, self.interface.tab_bar):
                if tab_bar.tab(tab_id) is not None:
                    tab_bar.setCurrentTab(tab_id)
                for item in tab_bar.items:
                    if item.isSelected and item.routeKey() != tab_id:
                        item.setSelected(False)

        def record_activation(self, tab_id: str):
            if tab_id in self.activation_order:
                self.activation_order.remove(tab_id)
            self.activation_order.append(tab_id)

        def drop_activation(self, tab_id: str):
            if tab_id in self.activation_order:
                self.activation_order.remove(tab_id)

        def previous_live_tab_id(self, closing_tab_id: str) -> t.Optional[str]:
            for candidate in reversed(self.activation_order):
                if candidate != closing_tab_id and candidate in self.interface.tabs:
                    return candidate
            return None

        def on_current_tab_changed(self, _index: int):
            widget = self.interface.stacked_widget.currentWidget()
            if widget is None:
                return
            tab_id = widget.objectName()
            owning_bar = self.bar_of_tab(tab_id)
            if owning_bar is None or owning_bar.currentTab() != tab_id:
                self.activate_tab_chrome(tab_id)
            self.interface._update_batch_button(tab_id)
            self.update_chrome()
            self.sync_tip_line(tab_id)
            self.schedule_zoom_sync(tab_id)
            tab = self.interface.tabs.get(tab_id)
            if tab is not None:
                if tab.favorites_dirty:
                    self.interface._refresh_favorites_tab(tab)
                tab.flush_download_dirty()
            self.interface.sync_page_nav(tab_id)

        def schedule_zoom_sync(self, tab_id: str):
            self._zoom_sync_generation += 1
            generation = self._zoom_sync_generation
            QtCore.QTimer.singleShot(0, lambda tid=tab_id, gen=generation: self._run_deferred_zoom_sync(tid, gen))

        def _run_deferred_zoom_sync(self, tab_id: str, generation: int):
            if generation != self._zoom_sync_generation or self.active_tab_id() != tab_id:
                return
            self.interface.zoom_mgr.sync_tab(tab_id)

        def on_tabbar_index_changed(self, index: int, tab_bar: t.Optional[TabBar] = None):
            tab_id = self.tab_id_at(index, tab_bar=tab_bar)
            if tab_id:
                self.set_current(tab_id)

        def on_tab_close_requested(self, index: int, tab_bar: t.Optional[TabBar] = None):
            self.close_by_id(self.tab_id_at(index, tab_bar=tab_bar))

        def tab_id_at(self, index: int, tab_bar: t.Optional[TabBar] = None) -> t.Optional[str]:
            if tab_bar is None:
                tab_bar = self.interface.tab_bar
            if not 0 <= index < tab_bar.count():
                return None
            return tab_bar.tabItem(index).routeKey()

        def tab_index(self, tab_id: str) -> int:
            for tab_bar in (self.interface.pin_tab_bar, self.interface.tab_bar):
                item = tab_bar.tab(tab_id)
                if item is not None:
                    return tab_bar.items.index(item)
            return -1

        @staticmethod
        def display_title_for_query(query: str, tab_index: int) -> str:
            canonical = DanbooruSearchQuery.normalize(query)
            if not canonical:
                return qtTrId("danbooru.workspace.label").format(tab_index=tab_index)
            if len(canonical) <= 18:
                return canonical
            return canonical[:16].rstrip() + ".."

        def update_title(self, tab_id: str, query: str):
            state = self.interface.tab_states.get(tab_id)
            if state is None:
                return
            title = self.display_title_for_query(query, int(tab_id.rsplit("-", 1)[-1]))
            if title == state.title:
                return
            state.title = title
            tab_bar = self.bar_for_tab(tab_id)
            index = self.tab_index(tab_id)
            if tab_bar is not None and index >= 0:
                tab_bar.setTabText(index, state.title)
            self.update_chrome()
            self.sync_bar_width()

        def update_chrome(self):
            palette = DanbooruUiPalette.current()
            current_tab_id = self.active_tab_id()
            active_text_color = qcolor_from_css(palette.text)
            inactive_text_color = qcolor_from_css(palette.muted_text)
            closable = len(self.interface.tabs) > 1
            self.interface.tab_bar.setTabsClosable(closable)
            self.interface.pin_tab_bar.setTabsClosable(closable)
            for tab_bar in (self.interface.tab_bar, self.interface.pin_tab_bar):
                for index in range(tab_bar.count()):
                    item = tab_bar.tabItem(index)
                    if item is None:
                        continue
                    tab_id = item.routeKey()
                    item.setBorderRadius(12)
                    tab_bar.setTabTextColor(index, active_text_color if tab_id == current_tab_id else inactive_text_color)

        def active_tab_id(self) -> t.Optional[str]:
            widget = self.interface.stacked_widget.currentWidget()
            return widget.objectName() if widget is not None else None

        def set_tip(self, tab_id: str, text: str, cls: str = DEFAULT_TAB_STATUS_CLASS):
            self.tips[tab_id] = (text, cls or DEFAULT_TAB_STATUS_CLASS)
            if self.active_tab_id() == tab_id:
                self.interface.tip_line.setText(_format_tip_rich_text(*self.tips[tab_id]))

        def set_httpx_status(self, tab_id: str, status: str, cls: str = DEFAULT_TAB_STATUS_CLASS):
            self.httpx_status[tab_id] = str(status or "")
            self.set_tip(tab_id, self.httpx_status[tab_id], cls=cls)

        def sync_tip_line(self, tab_id: t.Optional[str] = None):
            effective_tab_id = tab_id or self.active_tab_id()
            text, cls = self.tips.get(effective_tab_id, (default_tab_status_text(), DEFAULT_TAB_STATUS_CLASS))
            self.interface.tip_line.setText(_format_tip_rich_text(text, cls))

        def scroll_tabs(self, direction: int, tab_bar: t.Optional[TabBar] = None):
            target = tab_bar if tab_bar is not None else self.interface.pivot_scroll
            bar = target.horizontalScrollBar()
            if bar.maximum() <= bar.minimum():
                return
            step = max(72, int(target.viewport().width() * 0.72))
            bar.setValue(bar.value() + direction * step)

        @staticmethod
        def _sync_group_scroll_controls(tab_bar: TabBar, back_btn, forward_btn):
            bar = tab_bar.horizontalScrollBar()
            has_overflow = bar.maximum() > bar.minimum()
            back_btn.setEnabled(has_overflow and bar.value() > bar.minimum())
            forward_btn.setEnabled(has_overflow and bar.value() < bar.maximum())

        def sync_scroll_controls(self):
            """NormalTabsGroup 自己那对箭头。"""
            self._sync_group_scroll_controls(
                self.interface.pivot_scroll,
                self.interface.pivot_back_btn,
                self.interface.pivot_forward_btn,
            )

        def sync_pin_scroll_controls(self):
            """pinTabsGroup 自己那对箭头。"""
            self._sync_group_scroll_controls(
                self.interface.pin_tab_bar,
                self.interface.pin_back_btn,
                self.interface.pin_forward_btn,
            )

        def sync_bar_width(self):
            self.interface.tab_bar.view.adjustSize()
            self.interface.tab_bar.updateGeometry()
            QtCore.QTimer.singleShot(0, self.sync_scroll_controls)

    def _open_save_path(self):
        base_path = Path(self._runtime_config.save_path)
        tab_id = self.tab_mgr.active_tab_id()
        state = self.tab_states.get(tab_id) if tab_id else None
        open_path = base_path
        if state is not None and self._runtime_config.save_type == DANBOORU_SAVE_TYPE_SEARCH_TAG:
            folder_term = DanbooruSearchQuery(state.query).folder_term
            if folder_term:
                tab_save_path = base_path.joinpath(folder_sub.sub("-", folder_term))
                if tab_save_path.exists():
                    open_path = tab_save_path
        curr_os.open_folder(open_path)

    def _update_batch_button(self, tab_id: str):
        if self.tab_mgr.active_tab_id() != tab_id:
            return
        state = self.tab_states.get(tab_id)
        count = len(state.selected_post_ids) if state else 0
        self.batch_download_btn.setDisabled(count == 0)
        if count <= 0:
            self.batch_download_badge.hide()
            return
        self.batch_download_badge.set_count(count)
        self.batch_download_badge.show()

    def apply_downloaded_post(self, md5_value: str):
        # Zoom-pattern push: mark every tab dirty (O(1)/tab); only the active tab
        # applies immediately; hidden tabs rescan on activation.
        for tab in self.tabs.values():
            tab.mark_download_dirty(md5_value)
        active_tab = self._active_tab_widget()
        if active_tab is not None:
            active_tab.flush_download_dirty()
        if self.detail_preview_controller.matches(md5=md5_value):
            self.image_viewer.set_download_state(True)
        self.detail_preview_controller.sync_navigation()

    def _show_task_error(self, error: str, duration: int = 6000):
        script_logger().error(error)
        summary = summarize_error_message(error)
        self._show_info(InfoBar.error, f"✕ {summary}", duration)

    def schedule_deferred_gc(self) -> None:
        """gc.collect() 出用户操作关键路径（zoom-pattern 机制 3：generation 合并延迟）。

        连续关 tab / 清前页只触发一次全堆 GC：close 与 clear 路径每卡 deleteLater
        已即时回收 C++ 侧，Python 侧残余由 2s 后的单次 GC 统一清扫。
        """
        self._gc_generation += 1
        generation = self._gc_generation
        QtCore.QTimer.singleShot(2000, lambda: self._run_deferred_gc(generation))

    def _run_deferred_gc(self, generation: int) -> None:
        if generation != self._gc_generation:
            return
        gc.collect()

    def _log_search_request(self, tab_id: str, query: str, order: str, page: int, limit: int):
        params = DanbooruSearchQuery(query, order).params(page=page, limit=limit)
        stub_endpoint = self._runtime_config.stub_dns_endpoint() or "disabled"
        dns_summary = f"DoH={self._runtime_config.doh_url}" if self._runtime_config.is_doh_enabled() else "system"
        script_logger().info(f"[Danbooru] GET /posts.json tab={tab_id} params={params} dns={dns_summary} stub={stub_endpoint}")

    def refresh_runtime_settings(self):
        self._runtime_config = DanbooruRuntimeConfig.from_conf()
        self.request_client.set_runtime_config(self._runtime_config)

    def _favorite_groups_state(self):
        if self._favorites_state_cache is None:
            self._favorites_state_cache = build_favorite_groups_state(
                danbooru_cfg.searchFavorites.value,
                canonicalize_term=danbooru_cfg.canonicalize_term,
            )
        return self._favorites_state_cache

    def _invalidate_favorites(self):
        self._favorites_state_cache = None
        self.favorites_changed.emit()
        active_tab = self._active_tab_widget()
        if active_tab is not None:
            self._refresh_favorites_tab(active_tab)

    def _save_favorite_groups_state(self, groups_state):
        danbooru_cfg.fav.save_payload(groups_state.to_payload())
        self._favorites_state_cache = groups_state

    def _refresh_completer(self, tab: DanbooruTabWidget):
        history = danbooru_cfg.get_history()
        favorites_state = self._favorite_groups_state()
        favorites = sorted(favorites_state.all_terms() - set(history))
        tab.update_completer(history + favorites)

    def _refresh_favorites_tab(self, tab: DanbooruTabWidget):
        # Keep FluentMonkeyPatch.rbutton_menu_lineEdit path; only rebuild when needed.
        tab.set_search_menu()
        self._refresh_completer(tab)
        tab.sync_favorite_button_state()
        tab.clear_favorites_dirty()

    def apply_unpreviewable_filter(self):
        """Settings toggled hide-unpreviewable: re-filter cards + viewer sequence."""
        viewer_post = self.image_viewer.post
        for tab in self.tabs.values():
            tab.apply_unpreviewable_filter()
        # Navigation sync must come after the cards are re-filtered: previous/next
        # walk the grid, and the viewer may need to close or re-enable buttons.
        if viewer_post is not None:
            self.detail_preview_controller.on_viewer_hidden_post()
        if self.image_viewer.isVisible():
            self.detail_preview_controller.sync_navigation()

    def _open_tag_export_panel(self):
        self.tag_action_controller.open_export_panel(self.image_viewer)

    def _attach_tag_export(self):
        self.tag_action_controller.attach_from_viewer(self.image_viewer)

    def _open_imgpalace_browser(self, service_name: str, url: str | None):
        script_window = self.parent_window
        jsoneri_interface = getattr(script_window, "jsoneriPalacesProbeInterface", None)
        if jsoneri_interface is None:
            self._show_info(InfoBar.warning, qtTrId("danbooru.interface.jsoneri_unavailable"), 3500)
            return
        browser_controller = getattr(jsoneri_interface, "browser_controller", None)
        if browser_controller is None:
            raise RuntimeError("jsoneriPalacesProbeInterface.browser_controller is required")
        known_url = str(url or "").strip()
        if known_url and hasattr(browser_controller, "open_url"):
            browser_controller.open_url(known_url, service_name=service_name)
            return
        browser_controller.open_service(service_name, url=known_url or None)

    def _show_info(self, factory, content: str, duration: int = 3000):
        key = (getattr(factory, "__name__", repr(factory)), str(content or ""))
        existing = self._infobars_by_key.get(key)
        if existing is not None:
            return existing
        infobar = factory(
            title="", content=content, orient=Qt.Horizontal, isClosable=True, position=InfoBarPosition.TOP, duration=duration, parent=self,
        )
        if infobar is None:
            return None
        self._infobars_by_key[key] = infobar
        infobar.closedSignal.connect(lambda bar=infobar, current_key=key: self._forget_infobar(current_key, bar))
        return infobar

    def _show_custom_info(self, *, content: str, _type: str, duration: int, widgets: list, key_suffix: str = ""):
        key = (f"custom:{_type}", f"{content}|{key_suffix}")
        existing = self._infobars_by_key.get(key)
        if existing is not None:
            existing.close()
        infobar = CustomInfoBar.show_custom(
            title="", content=content, parent=self, _type=_type,
            ib_pos=InfoBarPosition.TOP, duration=duration, widgets=widgets,
        )
        if infobar is None:
            return None
        self._infobars_by_key[key] = infobar
        infobar.closedSignal.connect(lambda bar=infobar, current_key=key: self._forget_infobar(current_key, bar))
        return infobar

    def _forget_infobar(self, key, infobar):
        if self._infobars_by_key.get(key) is infobar:
            self._infobars_by_key.pop(key, None)

    def _on_extra_search(self, tab_id: str, extra_term: str):
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        current = DanbooruSearchQuery.normalize(tab.search_edit.text())
        combined = f"{current} {extra_term}".strip() if current else extra_term
        tab.search_edit.setText(combined)
        self.search_controller.start_search(tab_id, combined)

    def _toggle_favorite(self, tab_id: str):
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        term = DanbooruSearchQuery.normalize(tab.search_edit.text())
        if not term:
            tab.sync_favorite_button_state()
            return
        favorites_state = self._favorite_groups_state()
        is_favorited = favorites_state.toggle(term)
        self._save_favorite_groups_state(favorites_state)
        if not is_favorited and term not in favorites_state.all_terms():
            danbooru_cfg.drop_translate_keys([term])
        self._invalidate_favorites()
        content = f"★ {term}" if is_favorited else f"☆ {term}"
        if not is_favorited:
            return self._show_info(InfoBar.error, content)
        custom_groups = favorites_state.custom_groups
        if not custom_groups:
            return self._show_info(InfoBar.success, content)
        tmpFavMgrBtn = PrimaryToolButton(AcggrIcon.SCRIPT_FAV_MGR)
        first_ib = self._show_custom_info(
            content=content, _type="SUCCESS", duration=3000, widgets=[tmpFavMgrBtn], key_suffix="fav",
        )
        def _open_group_picker():
            if first_ib is not None:
                first_ib.close()
            combo = ComboBox()
            group_names = [group.name for group in custom_groups]
            combo.addItems(group_names)
            combo.setMinimumWidth(100)
            match = re.search("|".join(re.escape(group_name) for group_name in group_names), term)
            if match is not None:
                combo.setCurrentIndex(group_names.index(match.group()))
            accept_btn = PrimaryToolButton(FIF.ACCEPT)
            picker_ib = self._show_custom_info(
                content=qtTrId("danbooru.favorites.move_to"), _type="INFORMATION", duration=-1, widgets=[combo, accept_btn],
                key_suffix=f"move:{term}",
            )
            def _move_tag():
                group_name = combo.currentText()
                move_state = self._favorite_groups_state()
                move_state.move_to_group(term, group_name)
                self._save_favorite_groups_state(move_state)
                self._invalidate_favorites()
                if picker_ib is not None:
                    picker_ib.close()
                self._show_info(InfoBar.success, f"moved to「{group_name}」")
            accept_btn.clicked.connect(_move_tag)
        tmpFavMgrBtn.clicked.connect(_open_group_picker)

    def _open_favorite_manager(self):
        from .tag import DanbooruFavoriteManagerDialog

        dialog = DanbooruFavoriteManagerDialog(self._favorite_groups_state().copy(), self)
        if dialog.exec():
            self._save_favorite_groups_state(dialog.groups_state)
            self._invalidate_favorites()

    def _open_tag_jump_tab(self, tag: str):
        self.image_viewer.hide()
        self.detail_preview_controller.clear_context()
        canonical_tag = DanbooruSearchQuery.normalize(tag)
        if not canonical_tag:
            return
        for tab_id, state in self.tab_states.items():
            if DanbooruSearchQuery.normalize(state.query) == canonical_tag:
                self.tab_mgr.set_current(tab_id)
                tab = self.tabs.get(tab_id)
                if tab is not None and not state.result_list and not state.loading:
                    self.search_controller.start_search(tab_id, canonical_tag)
                return
        active_tab_id = self.tab_mgr.active_tab_id()
        active_state = self.tab_states.get(active_tab_id) if active_tab_id else None
        if active_tab_id and active_state and not active_state.query and not active_state.result_list and not active_state.loading:
            active_state.query = canonical_tag
            self.tabs[active_tab_id].search_edit.setText(canonical_tag)
            self.search_controller.start_search(active_tab_id, canonical_tag)
            return
        self.tab_mgr.create(initial_query=canonical_tag, auto_search=True)

    def notify_download_result(self, md5_value: str, success: bool):
        self.download_result_signal.emit(md5_value, success)

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_viewer_speed_probe_scheduled", False):
            self._viewer_speed_probe_scheduled = True
            _schedule_viewer_speed_probe(self)
        self.page_nav.reposition()
        self.page_nav.schedule_reposition()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.tab_mgr.sync_bar_width()
        self.tab_mgr.sync_pin_group()
        self.page_nav.reposition()

    def closeEvent(self, event):
        try:
            theme_mgr.unsubscribe(self._apply_theme)
            self.image_viewer.hide()
            self.video_proxy.close()
            self.sql_recorder.close()
            self.zoom_mgr.shutdown()
        finally:
            super().closeEvent(event)
