"""共享 viewer 图像加载指示器（无文字、IndeterminateProgressBar）。

R6 收口：图像区不再用 QLabel 打「加载中」文字（铺满画布、可选中、无辨识度），
统一用居中的不确定进度条。danbooru / pawchive 两个 viewer 共用同一实现，
避免各造一套。视频缓冲仍走各站点 video_mgr 的 buffering indicator（带原因文字，
不在图像静态区），本组件只服务图像加载态。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout

from qfluentwidgets import IndeterminateProgressBar

#: 进度条固定宽度（px）：足够醒目又不铺满图像区。
INDICATOR_WIDTH = 180
#: 看门狗：加载态最多转这么久就强制停（ms）。防止任何路径遗漏 stop 导致
#: 进度条"只有开始没结束"——图没按时到达时宁可停掉让用户看到静态画布，
#: 也不能让用户对着一条永远转的条干等。
LOADING_TIMEOUT_MS = 15000


class ImageLoadingIndicator(QFrame):
    """居中的不确定进度条；``setActive(True)`` 显示并转动，False 隐藏停止。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ViewerImageLoadingIndicator")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(lambda: self.setActive(False))
        self._bar = IndeterminateProgressBar(self, start=False)
        self._bar.setFixedWidth(INDICATOR_WIDTH)
        # 默认 sizeHint 高度仅 4px，深色画布上几乎不可见；加高到 8px 并给
        # 浅色轨道底衬，让加载态在图像区有辨识度（R6 的核心诉求）。
        self._bar.setFixedHeight(8)
        self._bar.setStyleSheet(
            "IndeterminateProgressBar { background: rgba(255,255,255,28); border-radius: 4px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._bar, 0, Qt.AlignCenter)
        self.hide()

    def setActive(self, active: bool) -> None:
        if active:
            self.show()
            self.raise_()
            self._bar.start()
            # 每次开启都重置看门狗：无论回包丢失/路径遗漏/下载卡死，
            # 最多转 LOADING_TIMEOUT_MS 就强制停，绝不无限转。
            self._watchdog.start(LOADING_TIMEOUT_MS)
        else:
            self._watchdog.stop()
            self._bar.stop()
            self.hide()

    def isActive(self) -> bool:
        return self.isVisible()
