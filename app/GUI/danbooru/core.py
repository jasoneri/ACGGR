import asyncio
import time
import typing as t
from dataclasses import dataclass, field

from PySide6 import QtCore, QtGui
from PySide6.QtCore import Qt, qtTrId, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QPushButton, QRubberBand, QWidget
from qfluentwidgets import InfoBar

from app.GUI.log import script_logger as _script_logger
from app.GUI.viewer.chrome import MIDDLE_PRESS_MAX_AGE_MS
from core.config.qc import danbooru_cfg
from core.script.image.danbooru.constants import DANBOORU_BASE_URL, DANBOORU_PAGE_SIZE
from core.script.image.danbooru.download import DanbooruDownloadSubmitter
from core.script.image.danbooru.http import DanbooruChallengeRequired
from core.script.image.danbooru.models import DanbooruAutocompleteCandidate, DanbooruPost, DanbooruSearchQuery
from ui.async_task import TaskError

# preview_loader is a leaf module (PySide6 + danbooru.http + ui.async_task only);
# it must NOT import from .core at module level or this import becomes circular.
from .preview_loader import DanbooruPreviewLoader

if t.TYPE_CHECKING:
    from core.script.image.danbooru.client import DanbooruClient
    from . import DanbooruCardWidget, DanbooruInterface, DanbooruTabWidget

DANBOORU_CHALLENGE_ERROR_MARKER = "Danbooru request blocked by browser verification"
DANBOORU_SEARCH_ERROR_STATUS = "search failed"


def post_hidden_from_grid(post: DanbooruPost) -> bool:
    """Whether the "hide unpreviewable" switch currently hides this post.

    The predicate is the same one the viewer uses for its own source selection
    (``DanbooruPost.has_preview_source``), so the grid and previous/next walk
    cannot disagree about which posts are previewable.
    """
    return bool(danbooru_cfg.hideUnpreviewable.value) and not post.has_preview_source


def run_async(coro):
    return asyncio.run(coro)


def execute_danbooru_task(task_mgr, task, *, success_callback, error_callback, task_id: str):
    task_mgr.execute_simple_task(
        task,
        success_callback=success_callback, error_callback=error_callback, show_success_info=False, 
        show_error_info=False, show_tooltip=False, task_id=task_id,
    )


@dataclass(slots=True)
class DanbooruReqResult:
    value: t.Any = None
    challenge: t.Optional[DanbooruChallengeRequired] = None


def capture_danbooru_request(task: t.Callable[..., t.Any], /, *args: t.Any, **kwargs: t.Any) -> DanbooruReqResult:
    try:
        return DanbooruReqResult(value=task(*args, **kwargs))
    except DanbooruChallengeRequired as exc:
        return DanbooruReqResult(challenge=exc)


@dataclass(frozen=True, slots=True)
class DanbooruViewerFitResult:
    available_bounds: QtCore.QSize
    max_display_bounds: QtCore.QSize
    display_size: QtCore.QSize
    target_area: int
    axis_scale: float


class DanbooruViewerFitCalculator:
    """danbooru 侧薄适配层：fit 算法的**唯一**实现位于 ``app.GUI.viewer.layout``。

    保留类名与返回类型（``DanbooruViewerFitResult``）是为了不动站点内既有调用点，计算
    委托 :func:`app.GUI.viewer.layout.fit_calculator`。禁止再在站点里抄一份公式。
    """

    @classmethod
    def calculate(cls, available_bounds: QtCore.QSize, source_size: t.Optional[QtCore.QSize]) -> DanbooruViewerFitResult:
        # 函数内导入：danbooru.core 在 GUI 启动早期就被 import，顶层拉 app.GUI.viewer
        # 会把整包 __init__（chrome / theme / qfluentwidgets）提前载入，扩大启动依赖面。
        from app.GUI.viewer.layout import fit_calculator

        result = fit_calculator(available_bounds, source_size, danbooru_cfg.get_view_ratio())
        return DanbooruViewerFitResult(
            available_bounds=result.available_bounds,
            max_display_bounds=result.max_display_bounds,
            display_size=result.display_size,
            target_area=result.target_area,
            axis_scale=result.axis_scale,
        )


def fetch_image_bytes(request_client: "DanbooruClient", url: str, *, timeout: float = 20.0) -> bytes:
    """Download raw image bytes; decoding/scaling lives in the preview loader."""
    return request_client.fetch_remote_bytes(url, timeout=timeout)


def delete_flow_item(item):
    if item is None:
        return
    if isinstance(item, QWidget):
        item.deleteLater()
        return
    widget = item.widget() if hasattr(item, "widget") else None
    if widget is not None:
        widget.deleteLater()


@dataclass(slots=True)
class DanbooruTabState:
    tab_id: str
    title: str
    query: str = ""
    sort_mode: str = ""
    page_cursor: int = 1
    buffer_start_page: int = 1
    result_list: list[DanbooruPost] = field(default_factory=list)
    selected_post_ids: set[int] = field(default_factory=set)
    request_token: int = 0
    has_more_results: bool = True
    loading: bool = False
    has_loaded_once: bool = False
    total_count: t.Optional[int] = None
    count_query_key: str = ""

    def begin_request(self) -> int:
        self.request_token += 1
        return self.request_token

    def reset_results(self, *, query: t.Optional[str] = None, keep_count: bool = False):
        if query is not None:
            self.query = query
        self.page_cursor = 1
        self.buffer_start_page = 1
        self.result_list.clear()
        self.selected_post_ids.clear()
        self.has_more_results = True
        self.has_loaded_once = False
        if not keep_count:
            self.total_count = None
            self.count_query_key = ""

    def mark_loaded_page(self, posts: t.Sequence[DanbooruPost], page: int, *, replace: bool = False):
        if replace or not self.has_loaded_once or not self.result_list:
            self.buffer_start_page = page
        self.page_cursor = page
        self.has_loaded_once = True
        self.has_more_results = len(posts) >= DANBOORU_PAGE_SIZE

    def count_cache_key(self) -> str:
        return f"{self.query}\n{self.sort_mode}"

    def can_load_next_page(self) -> bool:
        return not self.loading and self.has_more_results and self.has_loaded_once

    def has_pages_before_current(self) -> bool:
        return self.page_cursor > self.buffer_start_page

    def trim_count_before_current_page(self) -> int:
        return (self.page_cursor - self.buffer_start_page) * DANBOORU_PAGE_SIZE

    def retain_current_page_as_buffer_start(self):
        self.buffer_start_page = self.page_cursor


@dataclass(frozen=True, slots=True)
class _DanbooruSearchDispatch:
    tab_id: str
    query: str
    order: str
    page: int
    token: int
    replace: bool
    task_prefix: str

    def task_id(self) -> str:
        return f"danbooru-{self.task_prefix}-{self.tab_id}-{self.token}"

    def challenge_retry_key(self) -> str:
        return f"search:{self.tab_id}:{self.page}:{int(self.replace)}"

    def retry_callback(self, controller: "DanbooruSearchController") -> t.Callable[[], None]:
        if self.task_prefix == "jump" or (self.replace and self.page > 1):
            return lambda dispatch=self: controller.jump_to_page(dispatch.tab_id, dispatch.page)
        if self.replace or self.page <= 1:
            return lambda dispatch=self: controller.start_search(dispatch.tab_id, dispatch.query, order=dispatch.order)
        return lambda dispatch=self: controller.load_next_page(dispatch.tab_id)


class DanbooruTabSelectionController(QtCore.QObject):
    selection_count_changed = Signal(int)

    def __init__(self, tab: "DanbooruTabWidget"):
        super().__init__(tab)
        self.tab = tab
        self._selection_band = QRubberBand(QRubberBand.Rectangle, self.tab.scroll_area.viewport())
        self._selection_band.hide()
        self._drag_select_origin: t.Optional[QtCore.QPoint] = None
        self._drag_select_active = False
        self._drag_select_source: t.Optional[QWidget] = None
        self._drag_select_seed: set[int] = set()
        #: 中键 press 时间戳（0 = 没有未消费的 press）；用时间戳而不是布尔，
        #: 避免 release 被吞后布尔永久「粘住」、下一次 release 误关第二个 tab。
        self._middle_press_at: float = 0.0
        self._install_drag_select_source(self.tab.scroll_area.viewport())
        self._install_drag_select_source(self.tab.scroll_content)

    def apply_theme(self, *, selection_border: str, selection_background: str):
        self._selection_band.setStyleSheet(f"border: 2px dashed {selection_border}; background: {selection_background};")

    def bind_card(self, card: "DanbooruCardWidget"):
        card.selection_changed.connect(self._on_card_selection_changed)
        self._apply_card_selection(card, card.post.post_id in self.tab.state.selected_post_ids, emit_count=False)
        self._install_drag_select_source(card)
        self._install_drag_select_source(card.preview_frame)
        self._install_drag_select_source(card.preview_button)

    def set_card_selected(self, card: "DanbooruCardWidget", selected: bool):
        self._apply_card_selection(card, selected)

    def sync_selection_count(self):
        self.selection_count_changed.emit(len(self.tab.state.selected_post_ids))

    def selection_count(self) -> int:
        return len(self.tab.state.selected_post_ids)

    def has_selection(self) -> bool:
        return bool(self.tab.state.selected_post_ids)

    def clear(self):
        self._reset_drag_select_state()
        self._middle_press_at = 0.0
        self._set_selected_post_ids(set())

    def mark_downloaded(self, md5_value: str):
        downloaded_post_ids = set()
        for card in self.tab.card_widgets.values():
            if card.post.md5 != md5_value:
                continue
            card.set_already_downloaded(True)
            downloaded_post_ids.add(card.post.post_id)
        self.tab.state.selected_post_ids.difference_update(downloaded_post_ids)
        self.sync_selection_count()

    def _apply_card_selection(self, card: "DanbooruCardWidget", selected: bool, *, sync_widget: bool = True, emit_count: bool = True):
        if sync_widget:
            card.set_selected(selected)
            selected = card.checkbox.isChecked()
        if selected:
            self.tab.state.selected_post_ids.add(card.post.post_id)
        else:
            self.tab.state.selected_post_ids.discard(card.post.post_id)
        if emit_count:
            self.sync_selection_count()

    def _on_card_selection_changed(self, card: "DanbooruCardWidget", selected: bool):
        self._apply_card_selection(card, selected, sync_widget=False)

    def _install_drag_select_source(self, widget: QWidget):
        widget.setProperty("danbooruDragSelectSource", True)
        widget.installEventFilter(self)

    def _set_selected_post_ids(self, post_ids: t.Iterable[int], *, emit_count: bool = True):
        target_post_ids = {
            post_id
            for post_id in post_ids
            if (card := self.tab.card_widgets.get(post_id)) is not None
            and not card.isHidden() and card.checkbox.isEnabled()
        }
        current_post_ids = set(self.tab.state.selected_post_ids)
        if current_post_ids == target_post_ids:
            return
        for post_id in current_post_ids - target_post_ids:
            card = self.tab.card_widgets.get(post_id)
            if card is None:
                self.tab.state.selected_post_ids.discard(post_id)
                continue
            self._apply_card_selection(card, False, emit_count=False)
        for post_id in target_post_ids - current_post_ids:
            card = self.tab.card_widgets.get(post_id)
            if card is not None:
                self._apply_card_selection(card, True, emit_count=False)
        if emit_count:
            self.sync_selection_count()

    def _viewport_point_from_global(self, global_pos: QtCore.QPoint) -> QtCore.QPoint:
        viewport = self.tab.scroll_area.viewport()
        rect = viewport.rect()
        point = viewport.mapFromGlobal(global_pos)
        return QtCore.QPoint(min(max(point.x(), rect.left()), rect.right()), min(max(point.y(), rect.top()), rect.bottom()))

    def _reset_drag_select_state(self):
        was_active = self._drag_select_active
        self._drag_select_origin = None
        self._drag_select_active = False
        self._drag_select_source = None
        self._drag_select_seed.clear()
        self._selection_band.hide()
        if was_active:
            QApplication.restoreOverrideCursor()

    def _begin_drag_select(self):
        if self._drag_select_active or self._drag_select_origin is None:
            return
        self._drag_select_active = True
        self._selection_band.setGeometry(QtCore.QRect(self._drag_select_origin, QtCore.QSize()))
        self._selection_band.show()
        if isinstance(self._drag_select_source, QPushButton):
            self._drag_select_source.setDown(False)
        QApplication.setOverrideCursor(Qt.CrossCursor)

    def _update_drag_select_band(self, global_pos: QtCore.QPoint):
        if self._drag_select_origin is None:
            return
        self._selection_band.setGeometry(
            QtCore.QRect(self._drag_select_origin, self._viewport_point_from_global(global_pos)).normalized()
        )

    def _selected_post_ids_for_rect(self, selection_rect: QtCore.QRect) -> set[int]:
        if selection_rect.width() < 5 and selection_rect.height() < 5:
            return set(self._drag_select_seed)
        viewport = self.tab.scroll_area.viewport()
        hit_post_ids = set(self._drag_select_seed)
        for card in self.tab.card_widgets.values():
            if card.isHidden() or not card.checkbox.isEnabled():
                continue
            card_rect = QtCore.QRect(card.mapTo(viewport, QtCore.QPoint(0, 0)), card.size())
            if selection_rect.intersects(card_rect):
                hit_post_ids.add(card.post.post_id)
        return hit_post_ids

    def _preview_drag_selection(self, selection_rect: QtCore.QRect):
        self._set_selected_post_ids(self._selected_post_ids_for_rect(selection_rect))

    def eventFilter(self, obj, event):
        if not bool(getattr(obj, "property", lambda *_args: False)("danbooruDragSelectSource")):
            return super().eventFilter(obj, event)
        event_type = event.type()
        if event_type == QtCore.QEvent.MouseButtonPress:
            if event.button() == Qt.MiddleButton:
                self._middle_press_at = time.monotonic()
                return super().eventFilter(obj, event)
            self._middle_press_at = 0.0
            if event.button() != Qt.LeftButton:
                return super().eventFilter(obj, event)
            self._drag_select_origin = self._viewport_point_from_global(event.globalPosition().toPoint())
            self._drag_select_active = False
            self._drag_select_source = obj
            self._drag_select_seed = set(self.tab.state.selected_post_ids)
            return False
        if event_type == QtCore.QEvent.MouseMove:
            if self._drag_select_origin is None:
                return super().eventFilter(obj, event)
            current_point = self._viewport_point_from_global(event.globalPosition().toPoint())
            if not self._drag_select_active:
                if (current_point - self._drag_select_origin).manhattanLength() < QApplication.startDragDistance():
                    return False
                self._begin_drag_select()
            self._update_drag_select_band(event.globalPosition().toPoint())
            self._preview_drag_selection(self._selection_band.geometry())
            return True
        if event_type == QtCore.QEvent.MouseButtonRelease:
            if event.button() == Qt.RightButton:
                self.tab.show_grid_context_menu(event.globalPosition().toPoint())
                return True
            if event.button() == Qt.MiddleButton:
                pressed_at = self._middle_press_at
                self._middle_press_at = 0.0
                # A bounced release can reach the grid after the viewer consumed the
                # matching press and hid itself. A release with no recent press (or a
                # press older than the max age) is not a real tab-close gesture.
                if pressed_at <= 0.0:
                    return True
                if (time.monotonic() - pressed_at) * 1000.0 > MIDDLE_PRESS_MAX_AGE_MS:
                    return True
                self.tab.request_close.emit()
                return True
            if self._drag_select_origin is None:
                return super().eventFilter(obj, event)
            selection_rect = self._selection_band.geometry()
            drag_was_active = self._drag_select_active
            target_post_ids = self._selected_post_ids_for_rect(selection_rect) if drag_was_active else set()
            self._reset_drag_select_state()
            if drag_was_active:
                self._set_selected_post_ids(target_post_ids)
                return True
            return False
        return super().eventFilter(obj, event)


class DanbooruSearchController:
    def __init__(self, interface: "DanbooruInterface"):
        self.interface = interface
        self.log = _script_logger()
        self._preview_loader: t.Optional["DanbooruPreviewLoader"] = None
        self._preview_loader_bound = False

    def bind_preview_loader(self, loader: "DanbooruPreviewLoader"):
        """Wire the shared preview loader into this controller's slots.

        Idempotent: safe to call multiple times, connections are made once.
        """
        if self._preview_loader_bound:
            return
        self._preview_loader_bound = True
        self._preview_loader = loader
        loader.ready.connect(self._on_preview_ready)
        loader.failed.connect(self._on_preview_failed)
        loader.challenge.connect(self._on_preview_challenge)

    def start_search(self, tab_id: str, query: str, *, order: str | None = None):
        tab = self.interface.tabs.get(tab_id)
        state = self.interface.tab_states.get(tab_id)
        if tab is None or state is None:
            return
        canonical_term = DanbooruSearchQuery.normalize(query)
        token = state.begin_request()
        tab.clear_results(query=canonical_term)
        state.total_count = None
        state.count_query_key = ""
        self.interface.detail_preview_controller.cancel_page_continuation(tab_id)
        if order is not None:
            state.sort_mode = str(order or "")
        tab.set_loading(True)
        self.interface.sync_page_nav(tab_id)
        self._submit_search_request(
            tab_id=tab_id, query=state.query, order=state.sort_mode, page=1,
            token=token, replace=True, task_prefix="search",
        )

    def jump_to_page(self, tab_id: str, page: int) -> bool:
        tab = self.interface.tabs.get(tab_id)
        state = self.interface.tab_states.get(tab_id)
        if tab is None or state is None or state.loading:
            return False
        target_page = max(1, int(page or 1))
        if target_page == state.page_cursor and state.has_loaded_once:
            return False
        token = state.begin_request()
        keep_count = state.count_query_key == state.count_cache_key() and state.total_count is not None
        tab.clear_results(query=state.query, keep_count=keep_count)
        if not keep_count:
            state.total_count = None
            state.count_query_key = ""
        state.page_cursor = target_page
        state.buffer_start_page = target_page
        self.interface.detail_preview_controller.cancel_page_continuation(tab_id)
        tab.set_loading(True)
        self.interface.tab_mgr.set_tip(tab_id, f"jump page {target_page}...", cls="theme-tip")
        self.interface.sync_page_nav(tab_id)
        self._submit_search_request(
            tab_id=tab_id, query=state.query, order=state.sort_mode, page=target_page,
            token=token, replace=True, task_prefix="jump",
        )
        return True

    def load_next_page(self, tab_id: str) -> bool:
        tab = self.interface.tabs.get(tab_id)
        state = self.interface.tab_states.get(tab_id)
        if tab is None or state is None or not state.can_load_next_page():
            return False
        token = state.begin_request()
        next_page = state.page_cursor + 1
        tab.set_loading(True)
        self.interface.tab_mgr.set_tip(tab_id, f"loading page {next_page}...", cls="theme-tip")
        self.interface.sync_page_nav(tab_id)
        self._submit_search_request(
            tab_id=tab_id, query=state.query, order=state.sort_mode, page=next_page,
            token=token, replace=False, task_prefix="page",
        )
        return True

    def _submit_search_request(
        self,
        *,
        tab_id: str,
        query: str,
        order: str,
        page: int,
        token: int,
        replace: bool,
        task_prefix: str,
    ) -> None:
        dispatch = _DanbooruSearchDispatch(
            tab_id=tab_id, query=query, order=order, page=page,
            token=token, replace=replace, task_prefix=task_prefix,
        )
        self.interface._log_search_request(dispatch.tab_id, dispatch.query, dispatch.order, dispatch.page, DANBOORU_PAGE_SIZE)
        execute_danbooru_task(
            self.interface.task_mgr,
            lambda current=dispatch, interface=self.interface: capture_danbooru_request(
                interface.request_client.search_posts, current.query, order=current.order, page=current.page
            ),
            success_callback=lambda result, current=dispatch: self.handle_search_result(current, result),
            error_callback=lambda err, current=dispatch: self.handle_search_error(current, err), task_id=dispatch.task_id(),
        )

    def handle_search_result(self, dispatch: _DanbooruSearchDispatch, result: DanbooruReqResult):
        if result.challenge is not None:
            self.interface.detail_preview_controller.handle_page_load_failed(dispatch.tab_id, "Need browser verification")
            self.handle_search_challenge(dispatch, result.challenge)
            return
        self.handle_search_success(dispatch, result.value or [])

    def handle_search_success(self, dispatch: _DanbooruSearchDispatch, posts: list[DanbooruPost]):
        tab = self.interface.tabs.get(dispatch.tab_id)
        state = self.interface.tab_states.get(dispatch.tab_id)
        if tab is None or state is None or dispatch.token != state.request_token:
            return
        state.mark_loaded_page(posts, dispatch.page, replace=dispatch.replace)
        self.interface.tab_mgr.update_title(dispatch.tab_id, state.query)
        self.interface.tab_mgr.set_httpx_status(dispatch.tab_id, f"httpx 200/{len(posts)}", cls="theme-success")
        if dispatch.replace:
            self._schedule_count_fetch(dispatch)
        if not posts and dispatch.replace:
            tab.set_loading(False)
            self.interface.tab_mgr.set_httpx_status(dispatch.tab_id, "httpx 200/0", cls="theme-tip")
            self.interface.sync_page_nav(dispatch.tab_id)
            return
        if not posts:
            tab.set_loading(False)
            self.interface.detail_preview_controller.handle_page_load_empty(dispatch.tab_id)
            self.interface.tab_mgr.set_tip(dispatch.tab_id, "empty", cls="theme-err")
            self.interface.sync_page_nav(dispatch.tab_id)
            return
        self.interface.tab_mgr.set_tip(dispatch.tab_id, f"rendering {len(posts)} posts...", cls="theme-tip")
        QtCore.QTimer.singleShot(0, lambda current=dispatch, payload=list(posts): self._append_search_success(current, payload))

    def _append_search_success(self, dispatch: _DanbooruSearchDispatch, posts: list[DanbooruPost]):
        tab = self.interface.tabs.get(dispatch.tab_id)
        state = self.interface.tab_states.get(dispatch.tab_id)
        if tab is None or state is None or dispatch.token != state.request_token:
            return
        downloaded_md5s = self.interface.sql_recorder.batch_check_dupe([post.md5 for post in posts if post.md5])
        appended_cards = tab.append_results(posts, downloaded_md5s)
        tab.set_loading(False)
        danbooru_cfg.add_history(state.query)
        self.interface._refresh_completer(tab)
        self.interface.detail_preview_controller.handle_page_appended(dispatch.tab_id, posts)
        self.queue_card_previews(tab, appended_cards)
        if not state.has_more_results:
            self.interface.tab_mgr.set_tip(dispatch.tab_id, "empty", cls="theme-err")
        else:
            self.interface.tab_mgr.set_httpx_status(dispatch.tab_id, f"httpx 200/{len(posts)}", cls="theme-success")
        self.interface.sync_page_nav(dispatch.tab_id)

    def _schedule_count_fetch(self, dispatch: _DanbooruSearchDispatch):
        state = self.interface.tab_states.get(dispatch.tab_id)
        if state is None:
            return
        query_key = f"{dispatch.query}\n{dispatch.order}"
        if state.count_query_key == query_key and state.total_count is not None:
            self.interface.sync_page_nav(dispatch.tab_id)
            return
        token = state.request_token
        execute_danbooru_task(
            self.interface.task_mgr,
            lambda current=dispatch, interface=self.interface: capture_danbooru_request(
                interface.request_client.count_posts, current.query, order=current.order
            ),
            success_callback=lambda result, current=dispatch, request_token=token: self.handle_count_result(
                current, request_token, result
            ),
            error_callback=lambda _err, current=dispatch, request_token=token: self.handle_count_error(
                current, request_token
            ),
            task_id=f"danbooru-count-{dispatch.tab_id}-{token}",
        )

    def handle_count_result(self, dispatch: _DanbooruSearchDispatch, request_token: int, result: DanbooruReqResult):
        state = self.interface.tab_states.get(dispatch.tab_id)
        if state is None or request_token != state.request_token:
            return
        if result.challenge is not None:
            return
        try:
            total_count = int(result.value)
        except (TypeError, ValueError):
            return
        state.total_count = max(0, total_count)
        state.count_query_key = f"{dispatch.query}\n{dispatch.order}"
        self.interface.sync_page_nav(dispatch.tab_id)

    def handle_count_error(self, dispatch: _DanbooruSearchDispatch, request_token: int):
        state = self.interface.tab_states.get(dispatch.tab_id)
        if state is None or request_token != state.request_token:
            return
        self.interface.sync_page_nav(dispatch.tab_id)

    def handle_search_challenge(self, dispatch: _DanbooruSearchDispatch, challenge: DanbooruChallengeRequired):
        tab = self.interface.tabs.get(dispatch.tab_id)
        state = self.interface.tab_states.get(dispatch.tab_id)
        if tab is None or state is None or dispatch.token != state.request_token:
            return
        tab.set_loading(False)
        self.interface.sync_page_nav(dispatch.tab_id)
        self.interface.challenge_controller.submit(
            dispatch.tab_id, challenge, dispatch.retry_callback(self), retry_key=dispatch.challenge_retry_key(),
        )

    def handle_search_error(self, dispatch: _DanbooruSearchDispatch, error: str):
        tab = self.interface.tabs.get(dispatch.tab_id)
        state = self.interface.tab_states.get(dispatch.tab_id)
        if tab is None or state is None or dispatch.token != state.request_token:
            return
        tab.set_loading(False)
        self.interface.sync_page_nav(dispatch.tab_id)
        if DANBOORU_CHALLENGE_ERROR_MARKER in str(error or ""):
            self.interface.detail_preview_controller.handle_page_load_failed(dispatch.tab_id, "Need browser verification")
            self.handle_search_challenge(dispatch, DanbooruChallengeRequired(verify_url=DANBOORU_BASE_URL, status_code=403))
            return
        self.interface.tab_mgr.set_tip(dispatch.tab_id, DANBOORU_SEARCH_ERROR_STATUS, cls="theme-err")
        self.interface.detail_preview_controller.handle_page_load_failed(dispatch.tab_id, DANBOORU_SEARCH_ERROR_STATUS)
        self.interface._show_task_error(error, 6000)

    def load_card_preview(self, tab: "DanbooruTabWidget", card: "DanbooruCardWidget"):
        if card.isHidden():
            # Filtered-out cards are not on screen; fetching their thumbnail is
            # pure waste (they also stay hidden until the preference changes).
            return
        if card.post.preview_asset_is_video:
            card.preview_button.setText(qtTrId("danbooru.card.video_preview"))
            return
        preview_url = card.post.preview_file_url or card.post.file_url
        if not preview_url:
            return
        loader = self._preview_loader
        if loader is None:
            self.log.warning(
                "[Danbooru] preview loader not bound, drop card preview request "
                f"tab_id={tab.state.tab_id} post_id={card.post.post_id}"
            )
            return
        loader.request(
            DanbooruPreviewLoader.KIND_THUMB, preview_url,
            width=max(card.preview_fetch_width(), 280), key=(tab.state.tab_id, card.post.post_id),
        )

    def queue_card_previews(self, tab: "DanbooruTabWidget", cards: list["DanbooruCardWidget"]):
        visible_ids = tab.visible_card_post_ids()
        ordered_post_ids = [card.post.post_id for card in cards if card.post.post_id in visible_ids] + [
            card.post.post_id for card in cards if card.post.post_id not in visible_ids
        ]
        self._queue_card_preview_batch(tab.state.tab_id, ordered_post_ids, 0)

    def _queue_card_preview_batch(self, tab_id: str, post_ids: list[int], index: int):
        tab = self.interface.tabs.get(tab_id)
        if tab is None or index >= len(post_ids):
            return
        end = min(len(post_ids), index + 6)
        for post_id in post_ids[index:end]:
            card = tab.card_for_post(post_id)
            if card is not None:
                self.load_card_preview(tab, card)
        if end < len(post_ids):
            QtCore.QTimer.singleShot(16, lambda tid=tab_id, ids=post_ids, i=end: self._queue_card_preview_batch(tid, ids, i))

    @staticmethod
    def _unpack_card_preview_key(key) -> t.Optional[tuple[str, int]]:
        # Thumb keys are ``(tab_id, post_id)`` tuples; detail keys are
        # ``"detail:..."`` strings, so a non-tuple key belongs to another
        # controller and must be ignored here.
        if not isinstance(key, tuple) or len(key) != 2:
            return None
        tab_id, post_id = key
        return str(tab_id), int(post_id)

    def _on_preview_ready(self, key, image: QtGui.QImage):
        unpacked = self._unpack_card_preview_key(key)
        if unpacked is None:
            return
        tab_id, post_id = unpacked
        tab = self.interface.tabs.get(tab_id)
        if tab is None:
            return
        card = tab.card_for_post(post_id)
        if card is None:
            return
        self.handle_preview_image(card, image)

    def _on_preview_failed(self, key, error: TaskError):
        unpacked = self._unpack_card_preview_key(key)
        if unpacked is None:
            return
        tab_id, post_id = unpacked
        tab = self.interface.tabs.get(tab_id)
        if tab is None:
            return
        card = tab.card_for_post(post_id)
        if card is None:
            return
        self.log.warning(f"[Danbooru] card preview failed tab_id={tab_id} post_id={post_id}: {error.debug_text or str(error)}")
        card.preview_button.setText(qtTrId("danbooru.card.preview_error"))

    def _on_preview_challenge(self, key, challenge: DanbooruChallengeRequired):
        unpacked = self._unpack_card_preview_key(key)
        if unpacked is None:
            return
        tab_id, post_id = unpacked
        tab = self.interface.tabs.get(tab_id)
        if tab is None:
            return
        card = tab.card_for_post(post_id)
        if card is None:
            return
        card.preview_button.setText(qtTrId("danbooru.search.verification_required"))
        self.interface.challenge_controller.submit(
            tab_id, challenge, lambda tid=tab_id, pid=post_id: self.retry_card_preview(tid, pid),
            retry_key=f"card-preview:{tab_id}:{post_id}",
        )

    def retry_card_preview(self, tab_id: str, post_id: int):
        tab = self.interface.tabs.get(tab_id)
        if tab is None:
            return
        card = tab.card_for_post(post_id)
        if card is None:
            return
        self.load_card_preview(tab, card)

    @staticmethod
    def handle_preview_image(card: "DanbooruCardWidget", image: QtGui.QImage):
        pixmap = QPixmap.fromImage(image)
        if not pixmap.isNull():
            card.set_preview_pixmap(pixmap)

    def convert_term(self, tab_id: str):
        tab = self.interface.tabs.get(tab_id)
        if tab is None:
            return
        term = DanbooruSearchQuery.normalize(tab.search_edit.text())
        if not term:
            return
        execute_danbooru_task(
            self.interface.task_mgr,
            lambda current_term=term, interface=self.interface: capture_danbooru_request(
                interface.request_client.autocomplete_tags, current_term,
            ),
            success_callback=lambda payload, tid=tab_id: self.handle_conversion_task_result(tid, payload),
            error_callback=lambda err: self.interface._show_task_error(err, 6000), task_id=f"danbooru-convert-{tab_id}",
        )

    def handle_conversion_task_result(self, tab_id: str, payload: DanbooruReqResult):
        if payload.challenge is not None:
            self.handle_conversion_challenge(tab_id, payload.challenge)
            return
        self.handle_conversion_result(tab_id, payload.value)

    def handle_conversion_challenge(self, tab_id: str, challenge: DanbooruChallengeRequired):
        tab = self.interface.tabs.get(tab_id)
        if tab is None:
            return
        self.interface.challenge_controller.submit(
            tab_id, challenge, lambda tid=tab_id: self.convert_term(tid), retry_key=f"convert:{tab_id}",
        )

    def _search_converted_candidate(self, tab_id: str, candidate: DanbooruAutocompleteCandidate):
        self.start_search(tab_id, candidate.value)

    def handle_conversion_result(self, tab_id: str, result):
        tab = self.interface.tabs.get(tab_id)
        if tab is None:
            return
        if result.is_single_match:
            self._search_converted_candidate(tab_id, result.matches[0])
            return
        if result.has_matches:
            self.interface.tab_mgr.set_tip(tab_id, f"? {len(result.matches)}", cls="theme-tip")
            tab.show_conversion_candidates(
                result.matches,
                on_selected=lambda candidate, tid=tab_id: self._search_converted_candidate(tid, candidate),
            )
            return
        content = "unknown"
        self.interface.tab_mgr.set_tip(tab_id, content, cls="theme-err")
        self.interface._show_info(InfoBar.warning, content, 4000)


class DanbooruDownloadController:
    def __init__(self, interface: "DanbooruInterface"):
        self.interface = interface

    def selected_posts_for_tab(self, tab_id: str) -> list[DanbooruPost]:
        state = self.interface.tab_states.get(tab_id)
        if state is None:
            return []
        return [post for post in state.result_list if post.post_id in state.selected_post_ids]

    def submit_selected(self):
        tab_id = self.interface.tab_mgr.active_tab_id()
        if not tab_id:
            return
        posts = self.selected_posts_for_tab(tab_id)
        if not posts:
            return
        self._submit_posts(
            list(posts),
            success_callback=lambda plan, tid=tab_id: self.handle_submission_result(tid, plan, True),
            task_id=f"danbooru-batch-submit-{tab_id}",
        )

    def submit_single(self, post: DanbooruPost, tab_id: t.Optional[str] = None):
        effective_tab_id = tab_id or self.interface.detail_preview_controller.current_tab_id or self.interface.tab_mgr.active_tab_id()
        if self.interface.sql_recorder.check_dupe(post.md5):
            self.interface.apply_downloaded_post(post.md5)
            return
        self._submit_posts(
            [post],
            success_callback=lambda plan, tid=effective_tab_id or "viewer": self.handle_submission_result(tid, plan, False),
            task_id=f"danbooru-single-submit-{effective_tab_id or 'viewer'}-{post.post_id}",
        )

    def _submit_posts(self, posts: list[DanbooruPost], *, success_callback, task_id: str):
        def submit_task(progress_callback=None, payload=list(posts)):
            return run_async(
                DanbooruDownloadSubmitter().submit(
                    payload,
                    completion_callback=self.interface.notify_download_result, progress_callback=progress_callback,
                )
            )

        execute_danbooru_task(
            self.interface.task_mgr,
            submit_task, success_callback=success_callback,
            error_callback=lambda err: self.interface._show_task_error(err, 6000), task_id=task_id,
        )

    def handle_submission_result(self, tab_id: str, plan, batch: bool):
        content = f"{len(plan.deduped_skipped)} skipped, {len(plan.to_submit)} completed, {len(plan.failed_pre_submit)} failed"
        self.interface.tab_mgr.set_tip(tab_id, content, cls="theme-success")
        if getattr(plan, "submission_errors", None):
            self.interface._show_info(InfoBar.warning, plan.submission_errors[0], 6000)
        for post in plan.deduped_skipped:
            self.interface.apply_downloaded_post(post.md5)
        if batch:
            self.interface._update_batch_button(tab_id)

    def on_download_result(self, md5_value: str, success: bool):
        if success:
            self.interface.sql_recorder.add(md5_value)
            self.interface.apply_downloaded_post(md5_value)
