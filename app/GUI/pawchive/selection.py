"""pawchive 网格选择模型（P1）。

键从 post_id: int 换成 CardKey = (post_id, attachment_path)（IA v4 §3.4），并新增
「帖级零附件不可选」的过滤（D-15）。

- mark_downloaded 对入参全集剔除（折叠/未渲染的已下载键必须一起离开选中集）。
- 私有名 _selectable_keys 改走 grid 公开契约 all_selectable_keys()。
- apply_theme 接上调用者（PawchiveGridTab.apply_theme），颜色从 palette 派生。

依赖：只依赖 PySide6 + qfluentwidgets + .post_adapter + viewer.chrome 的 MIDDLE_PRESS_MAX_AGE_MS。
网格对象按鸭子类型访问：card_widgets / scroll_area / scroll_content / state.selected_card_keys /
all_selectable_keys / show_grid_context_menu / request_close / note_downloaded。
"""

import time
import typing as t

from PySide6 import QtCore
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QPushButton, QRubberBand, QWidget

from app.GUI.viewer.chrome import MIDDLE_PRESS_MAX_AGE_MS

from .post_adapter import CardKey


class PawchiveSelectionController(QtCore.QObject):
    selection_count_changed = Signal(int)

    def __init__(self, grid: "t.Any"):
        super().__init__(grid)
        self.grid = grid
        self._selection_band = QRubberBand(QRubberBand.Rectangle, grid.scroll_area.viewport())
        self._selection_band.hide()
        self._drag_select_origin: t.Optional[QtCore.QPoint] = None
        self._drag_select_active = False
        self._drag_select_source: t.Optional[QWidget] = None
        self._drag_select_seed: set = set()
        #: 中键 press 的时间戳（0 = 没有未消费的 press）。用时间戳而不是布尔：
        #: release 被浮窗/弹层吃掉时，布尔会永久「粘住」，下一次 release 就被误判成
        #: 同一次手势（用户现场："中间粘滞连关两个 tab"）。超过
        #: ``MIDDLE_PRESS_MAX_AGE_MS`` 的旧 press 一律作废。
        self._middle_press_at: float = 0.0
        self._install_drag_select_source(grid.scroll_area.viewport())
        self._install_drag_select_source(grid.scroll_content)

    # ---- 主题 ----
    def apply_theme(self, *, selection_border: str, selection_background: str):
        self._selection_band.setStyleSheet(
            f"border: 2px dashed {selection_border}; background: {selection_background};"
        )

    # ---- 绑定 / 读写 ----
    def bind_card(self, card):
        card.selection_changed.connect(self._on_card_selection_changed)
        self._apply_card_selection(
            card,
            self.key_for_card(card) in self.grid.state.selected_card_keys,
            emit_count=False,
        )
        self._install_drag_select_source(card)
        self._install_drag_select_source(card.preview_frame)
        self._install_drag_select_source(card.preview_button)

    @staticmethod
    def key_for_card(card) -> CardKey:
        """从卡片反推 CardKey。两段直接取，不反解 sha。"""
        return (str(card.post.post_id), str(card.post.attachment_path))

    def set_card_selected(self, card, selected: bool):
        self._apply_card_selection(card, selected)

    def sync_selection_count(self):
        self.selection_count_changed.emit(len(self.grid.state.selected_card_keys))

    def selection_count(self) -> int:
        return len(self.grid.state.selected_card_keys)

    def has_selection(self) -> bool:
        return bool(self.grid.state.selected_card_keys)

    def selected_keys(self) -> set:
        return set(self.grid.state.selected_card_keys)

    def clear(self):
        self._reset_drag_select_state()
        self._set_selected_card_keys(set())

    def select_visible(self):
        """「全选本页可见」：视口内所有可选卡的键取并集（IA §4.4）。"""
        target = set(self.grid.state.selected_card_keys) | self.visible_selectable_keys()
        self._set_selected_card_keys(target)

    def set_selected_keys(self, keys: t.Iterable[CardKey]):
        """组头勾走的通路：整体设置选中集（不可选/未渲染/已下载的键会被过滤）。"""
        self._set_selected_card_keys(keys)

    def mark_downloaded(self, keys: t.Iterable[CardKey]):
        """已下载回灌：先写 grid 的账本，再对**入参全集**剔除。

        渲染与否是视觉问题，不是账本问题：折叠/未渲染的键也要离开选中集
        （现盘 ``difference_update(hit)`` 只剔已渲染键，是 D-7 违例）。
        """
        keys = list(keys)
        if not keys:
            return
        if hasattr(self.grid, "note_downloaded"):
            self.grid.note_downloaded(keys)
        for card_key in keys:
            card = self.grid.card_widgets.get(card_key)
            if card is not None and hasattr(card, "set_already_downloaded"):
                card.set_already_downloaded(True)
        self.grid.state.selected_card_keys.difference_update(keys)
        self.sync_selection_count()

    # ---- 内部 ----
    def visible_selectable_keys(self) -> set:
        viewport = self.grid.scroll_area.viewport()
        viewport_rect = viewport.rect()
        visible: set = set()
        for card_key, card in self.grid.card_widgets.items():
            if not card.checkbox.isEnabled() or not card.checkbox.isVisible():
                continue
            if viewport_rect.intersects(self._card_rect_in(viewport, card)):
                visible.add(card_key)
        return visible

    @staticmethod
    def _card_rect_in(viewport, card) -> QtCore.QRect:
        return QtCore.QRect(card.mapTo(viewport, QtCore.QPoint(0, 0)), card.size())

    def _apply_card_selection(
        self, card, selected: bool, *, sync_widget: bool = True, emit_count: bool = True
    ):
        if not card.checkbox.isEnabled():
            # D-14：不可下载 / 已下载的卡永不进选中集。卡片自己的 set_selected 有
            # isEnabled() 守卫会跳过 setChecked，因此禁用态下被外部改过的小方框要
            # 在这里强制复位，否则读回 isChecked() 会把它当成已选中。
            selected = False
            if card.checkbox.isChecked():
                card.checkbox.blockSignals(True)
                card.checkbox.setChecked(False)
                card.checkbox.blockSignals(False)
                card.set_selected(False)
        elif sync_widget:
            card.set_selected(selected)
            selected = card.checkbox.isChecked()
        card_key = self.key_for_card(card)
        if selected:
            self.grid.state.selected_card_keys.add(card_key)
        else:
            self.grid.state.selected_card_keys.discard(card_key)
        if emit_count:
            self.sync_selection_count()

    def _on_card_selection_changed(self, card, selected: bool):
        self._apply_card_selection(card, selected, sync_widget=False)

    def _known_selectable_keys(self) -> set:
        """全量可选键：走 grid 的**公开**契约，不读 ``_selectable_keys``。"""
        getter = getattr(self.grid, "all_selectable_keys", None)
        if callable(getter):
            return set(getter())
        return set(self.grid.card_widgets)

    def _downloaded_keys(self) -> set:
        getter = getattr(self.grid, "downloaded_keys", None)
        if getter is None:
            return set()
        try:
            return set(getter)
        except TypeError:
            return set()

    def _set_selected_card_keys(self, card_keys: t.Iterable[CardKey], *, emit_count: bool = True):
        # 可达集 = 已渲染的可选卡 ∪ 全量索引里的可选键（折叠未渲染的卡也要能选中）。
        known = set(self.grid.card_widgets) | self._known_selectable_keys()
        downloaded = self._downloaded_keys()
        selectable = {
            key
            for key in known
            if key not in downloaded
            and (
                (card := self.grid.card_widgets.get(key)) is None
                or card.checkbox.isEnabled()
            )
        }
        target = {key for key in card_keys if key in selectable}
        current = set(self.grid.state.selected_card_keys)
        if current == target:
            return
        for card_key in current - target:
            card = self.grid.card_widgets.get(card_key)
            if card is None:
                self.grid.state.selected_card_keys.discard(card_key)
                continue
            self._apply_card_selection(card, False, emit_count=False)
        for card_key in target - current:
            card = self.grid.card_widgets.get(card_key)
            if card is None:
                # 折叠未渲染的卡：没有 widget 可同步，直接落进选中集；
                # 展开时 bind_card 会按选中集回放（_apply_card_selection 的读侧）。
                self.grid.state.selected_card_keys.add(card_key)
                continue
            self._apply_card_selection(card, True, emit_count=False)
        if emit_count:
            self.sync_selection_count()

    # ---- 拖选 ----
    def _install_drag_select_source(self, widget: QWidget):
        widget.setProperty("pawchiveDragSelectSource", True)
        widget.installEventFilter(self)

    def _viewport_point_from_global(self, global_pos: QtCore.QPoint) -> QtCore.QPoint:
        viewport = self.grid.scroll_area.viewport()
        rect = viewport.rect()
        point = viewport.mapFromGlobal(global_pos)
        return QtCore.QPoint(
            min(max(point.x(), rect.left()), rect.right()),
            min(max(point.y(), rect.top()), rect.bottom()),
        )

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
            QtCore.QRect(
                self._drag_select_origin,
                self._viewport_point_from_global(global_pos),
            ).normalized()
        )

    def _selected_keys_for_rect(self, selection_rect: QtCore.QRect) -> set:
        if selection_rect.width() < 5 and selection_rect.height() < 5:
            return set(self._drag_select_seed)
        viewport = self.grid.scroll_area.viewport()
        hit = set(self._drag_select_seed)
        for card_key, card in self.grid.card_widgets.items():
            if not card.checkbox.isEnabled():
                continue
            if selection_rect.intersects(self._card_rect_in(viewport, card)):
                hit.add(card_key)
        return hit

    def _preview_drag_selection(self, selection_rect: QtCore.QRect):
        self._set_selected_card_keys(self._selected_keys_for_rect(selection_rect))

    def eventFilter(self, obj, event):
        if not bool(getattr(obj, "property", lambda *_a: False)("pawchiveDragSelectSource")):
            return super().eventFilter(obj, event)
        event_type = event.type()
        if event_type == QtCore.QEvent.MouseButtonPress:
            if event.button() == Qt.MiddleButton:
                # 记住"这次 press 到达过网格"：浮窗中键关闭后弹起的第二次 release
                # 也会落到网格上，但没有对应 press —— 不是真实的关 tab 手势。
                self._middle_press_at = time.monotonic()
                return super().eventFilter(obj, event)
            self._middle_press_at = 0.0
            if event.button() != Qt.LeftButton:
                return super().eventFilter(obj, event)
            self._drag_select_origin = self._viewport_point_from_global(
                event.globalPosition().toPoint()
            )
            self._drag_select_active = False
            self._drag_select_source = obj
            self._drag_select_seed = set(self.grid.state.selected_card_keys)
            return False
        if event_type == QtCore.QEvent.MouseMove:
            if self._drag_select_origin is None:
                return super().eventFilter(obj, event)
            current_point = self._viewport_point_from_global(event.globalPosition().toPoint())
            if not self._drag_select_active:
                if (
                    current_point - self._drag_select_origin
                ).manhattanLength() < QApplication.startDragDistance():
                    return False
                self._begin_drag_select()
            self._update_drag_select_band(event.globalPosition().toPoint())
            self._preview_drag_selection(self._selection_band.geometry())
            return True
        if event_type == QtCore.QEvent.MouseButtonRelease:
            if event.button() == Qt.MiddleButton:
                pressed_at = self._middle_press_at
                self._middle_press_at = 0.0
                # 没有 press，或 press 已过期（release 曾被浮窗/弹层吃掉）→ 不是
                # 同一次关 tab 手势，丢弃。时间戳而不是布尔，避免「粘滞」误关第二个 tab。
                if pressed_at <= 0.0:
                    return True
                if (time.monotonic() - pressed_at) * 1000.0 > MIDDLE_PRESS_MAX_AGE_MS:
                    return True
                self.grid.request_close.emit()
                return True
            if self._drag_select_origin is None:
                return super().eventFilter(obj, event)
            selection_rect = self._selection_band.geometry()
            drag_was_active = self._drag_select_active
            target = self._selected_keys_for_rect(selection_rect) if drag_was_active else set()
            self._reset_drag_select_state()
            if drag_was_active:
                self._set_selected_card_keys(target)
                return True
            return False
        return super().eventFilter(obj, event)


__all__ = ["PawchiveSelectionController"]
