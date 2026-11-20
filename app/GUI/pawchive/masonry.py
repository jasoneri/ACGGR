"""瀑布流布局（masonry）：等高列、按最短列依次摆放，卡片保持各自宽高比。

不用 qfluentwidgets FlowLayout（按行排列，行高取该行最高，图片高矮不一时留空白带）。

约束：
- 列宽由卡片自身 sizeHint().width() 决定（缩放档位改的是它）；
- 列数 = 能在给定宽度里放下几列；
- 新卡片放进当前最矮的一列（同高取最左），保证 heightForWidth(w) 每次一致；
- heightForWidth(w) 返回整块内容高度，供 flow_host.setFixedHeight() 钉死。
"""

from __future__ import annotations

import typing as t

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidgetItem


class PawchiveMasonryLayout(QLayout):
    """列式瀑布流。接口与 ``FlowLayout`` 对齐（``addWidget`` / ``heightForWidth`` / ``setGeometry``）。"""

    def __init__(self, parent: t.Optional[QWidget] = None, *, spacing: int = 4):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)
        # PySide 的 QLayout.spacing 在不同绑定里既是属性又是方法，直接存一份整数更稳。
        self._gap = int(spacing)
        self._items: list[QLayoutItem] = []

    # ---- QLayout 必备 ----
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt 命名
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> t.Optional[QLayoutItem]:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> t.Optional[QLayoutItem]:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def sizeHint(self) -> QSize:  # noqa: N802
        """不换行时的尺寸（与 FlowLayout 同口径：交给外部用 heightForWidth 定高）。"""
        width = 0
        height = 0
        for item in self._items:
            hint = item.sizeHint()
            if not hint.isValid():
                continue
            width += hint.width() + self._gap
            height = max(height, hint.height())
        margins = self.contentsMargins()
        return QSize(
            width + margins.left() + margins.right(),
            height + margins.top() + margins.bottom(),
        )

    def minimumSize(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    # ---- 摆放 ----
    def _column_count(self, width: int, card_width: int) -> int:
        if card_width <= 0:
            return 1
        usable = max(0, width - self.contentsMargins().left() - self.contentsMargins().right())
        step = card_width + self._gap
        if step <= 0:
            return 1
        return max(1, (usable + self._gap) // step)

    def _visible_items(self) -> list[QLayoutItem]:
        return [item for item in self._items if item.widget() is None or not item.widget().isHidden()]

    def _placements(self, width: int) -> tuple[list[tuple[int, int, int, int]], int, int, int]:
        """返回 ([(x, y, w, h)], 内容宽, 内容高, 列宽)。纯计算，不改控件。"""
        margins = self.contentsMargins()
        items = self._visible_items()
        if not items:
            return [], 0, 0, 0

        card_width = 0
        for item in items:
            hint = item.sizeHint()
            if hint.isValid() and hint.width() > 0:
                card_width = max(card_width, hint.width())
        if card_width <= 0:
            return [], 0, 0, 0

        columns = self._column_count(width, card_width)
        heights = [0] * columns
        out: list[tuple[int, int, int, int]] = []
        for item in items:
            hint = item.sizeHint()
            card_h = hint.height() if hint.isValid() else 0
            col = min(range(columns), key=lambda i: (heights[i], i))
            x = margins.left() + col * (card_width + self._gap)
            y = margins.top() + heights[col]
            out.append((x, y, card_width, card_h))
            heights[col] += card_h + self._gap

        content_w = columns * card_width + (columns - 1) * self._gap
        content_h = max(0, max(heights) - self._gap) if heights else 0
        return out, content_w, content_h, card_width

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        _, _, content_h, _ = self._placements(width)
        return content_h + self.contentsMargins().top() + self.contentsMargins().bottom()

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        placements, _, _, _ = self._placements(rect.width())
        for item, (x, y, w, h) in zip(self._visible_items(), placements):
            widget = item.widget()
            if widget is not None:
                widget.setGeometry(x, y, w, h)
            else:
                item.setGeometry(QRect(x, y, w, h))


def masonry_item(widget: QWidget) -> QLayoutItem:
    """测试辅助：把一个 widget 当成 layout item 估尺寸。"""
    return QWidgetItem(widget)