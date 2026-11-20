"""pinTabsGroup 左上角 pin 图标的飞出列表。

用途：pin 组被横向滚动挤下时，点一下角落的 pin 图标就能直接跳到任意 pinned tab
（比反复点组内左右箭头快）。视觉沿用 qfluentwidgets 的 ``FlyoutViewBase``，
由 ``CustomFlyout.make(..., DROP_DOWN)`` 挂在按钮下方。
"""
from __future__ import annotations

import typing as t

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    FlyoutViewBase, IconWidget, PushButton, StrongBodyLabel,
)

from ui.qfluent.components.icons import AcggrIcon


class DanbooruPinTabsFlyout(FlyoutViewBase):
    """pin tab 一览：每行一个 tab 标题，点击即切到该 tab 的瀑布流。"""

    closed = Signal()
    tab_selected = Signal(str)

    def __init__(self, entries: t.Sequence[tuple[str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        pin_icon = IconWidget(AcggrIcon.SCRIPT_PIN, self)
        pin_icon.setFixedSize(16, 16)
        header.addWidget(pin_icon, 0, Qt.AlignVCenter)
        header.addWidget(StrongBodyLabel(f"{len(entries)}", self), 1)
        layout.addLayout(header)

        for tab_id, title in entries:
            row = PushButton(title, self)
            row.setMinimumWidth(160)
            row.clicked.connect(lambda _=False, tid=tab_id: self._pick(tid))
            layout.addWidget(row)

    def _pick(self, tab_id: str):
        self.tab_selected.emit(tab_id)
        self.closed.emit()