"""两个详情浮窗（danbooru / pawchive）共用的**快捷键语义**。

为什么单独立一个模块：两边的 `keyPressEvent` 各写一份的时候已经漂了——
danbooru 有 Keypad 修饰键守卫、有视频空格暂停；pawchive 没有，于是同一个 App 里
"←/→ 翻页、↓ 下载、Esc/↑ 关闭"在不同页面手感不一致。

这里只定义"哪个键做什么"，**不碰控件实现**：调用方只要提供
``previous_btn`` / ``next_btn`` / ``download_btn``（QAbstractButton，带
``isEnabled()`` 与 ``click()``），以及可选的 ``toggle_playback()`` 方法。
"""

from __future__ import annotations

import typing as t

from PySide6.QtCore import Qt


class ViewerKeys:
    """统一键位（两个 viewer 共用）。"""

    CLOSE = (Qt.Key_Escape, Qt.Key_Up)
    PREVIOUS = Qt.Key_Left
    NEXT = Qt.Key_Right
    DOWNLOAD = Qt.Key_Down
    TOGGLE_PLAYBACK = Qt.Key_Space


def _enabled_click(button: t.Any) -> bool:
    if button is None:
        return False
    try:
        if not button.isEnabled():
            return False
        button.click()
        return True
    except RuntimeError:  # 控件已销毁
        return False


def handle_viewer_key(viewer: t.Any, event) -> bool:
    """按统一键位处理一次按键；返回 True 表示已消费（调用方无需再处理）。

    调用方约定（鸭子类型，任一缺失都安全降级）：

    - ``previous_btn`` / ``next_btn`` / ``download_btn``：按钮；
    - ``toggle_playback()``：仅视频页面实现；
    - ``close()``：关闭浮窗。
    """
    key = event.key()
    modifiers = event.modifiers()

    # 小键盘也要能用方向键，但不能和 Ctrl/Shift 组合抢（例如 Shift+方向做选择）。
    if modifiers & Qt.KeyboardModifier.KeypadModifier:
        return False
    if modifiers & (
        Qt.KeyboardModifier.ControlModifier
        | Qt.KeyboardModifier.ShiftModifier
        | Qt.KeyboardModifier.AltModifier
    ):
        return False

    if key in ViewerKeys.CLOSE:
        close = getattr(viewer, "close", None)
        if callable(close):
            close()
            return True
        return False

    if key == ViewerKeys.TOGGLE_PLAYBACK:
        toggle = getattr(viewer, "toggle_playback", None)
        if callable(toggle):
            toggle()
            return True
        return False  # 非视频页面：让空格走默认（滚动等）

    if key == ViewerKeys.PREVIOUS:
        return _enabled_click(getattr(viewer, "previous_btn", None))

    if key == ViewerKeys.NEXT:
        return _enabled_click(getattr(viewer, "next_btn", None))

    if key == ViewerKeys.DOWNLOAD:
        return _enabled_click(getattr(viewer, "download_btn", None))

    return False


__all__ = ["ViewerKeys", "handle_viewer_key"]
