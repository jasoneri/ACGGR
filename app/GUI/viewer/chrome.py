"""详情浮窗的**唯一**壳层实现（danbooru / pawchive / 未来站点共用）。

壳层行为只改这个文件，子类只通过钩子注入差异（不要在各站点里各写一份，会漂移）。

与子类的契约（构造后必须存在的属性）：outer_layout / main_layout / view_column / top_bar /
previous_btn / next_btn / download_btn / _anchor_widget（可 None）/ _suppress_closed /
_native_window_warmed / left_panel（set_left_panel() 注入）。

子类可覆盖的钩子：wheel_media_widgets()（滚轮翻页只认这些矩形，空=不翻页）、
_ctrl_wheel_zoom(delta,pos)、_handle_custom_key(event)、_handle_extra_event_filter(obj,event)、
_chrome_width()/_chrome_height()（算术在 layout.chrome_extent()）、
_view_ratio()/_viewer_fit_result()/_viewer_fit_size()（fit 唯一入口，不要自算 max bounds）。
"""

from __future__ import annotations

import logging
import re
import typing as t

from PySide6.QtCore import QEvent, QMargins, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QWidget

from core import ori_path
from ui.core.theme import CustTheme, theme_mgr
from ui.core.theme.qss_template import (
    read_templated_qss_tokens,
    render_templated_qss_section,
)

from .layout import (
    ChromeExtent,
    ViewerFitResult,
    chrome_extent,
    clamp_into_screen,
    fit_calculator,
    media_bounds,
)

_LOGGER = logging.getLogger(__name__)

#: Shared viewer chrome object names. Both viewers MUST use these names for the
#: frame / image surface / hint / video surface / top bar — one QSS, one token
#: set, so light/dark can never diverge between sites.
VIEWER_CHROME_QSS_PATH = ori_path.joinpath("ui/core/theme/viewer.qss")
VIEWER_FRAME_OBJECT = "ViewerChromeFrame"
VIEWER_IMAGE_OBJECT = "ViewerChromeImage"
VIEWER_HINT_OBJECT = "ViewerChromeHint"
VIEWER_VIDEO_PANEL_OBJECT = "ViewerChromeVideoPanel"
VIEWER_VIDEO_SURFACE_OBJECT = "ViewerChromeVideoSurface"
VIEWER_TOPBAR_OBJECT = "ViewerChromeTopBar"

#: 中键「press → release」的最大有效间隔（毫秒）。超过就视为两次独立手势：
#: 磨损中键/浮窗吞掉 release 时，press 会一直「粘住」，下一次 release 会被误当成
#: 同一次关闭手势（用户现场："中间粘滞连关两个 tab"）。两层防护共用一个上限。
MIDDLE_PRESS_MAX_AGE_MS = 1000


def _viewer_theme_name() -> str:
    return "dark" if theme_mgr.get_theme() == CustTheme.DARK else "light"


def viewer_qss_tokens() -> dict[str, str]:
    """Shared viewer tokens for the current theme (empty dict on a broken file)."""
    try:
        return read_templated_qss_tokens(VIEWER_CHROME_QSS_PATH, _viewer_theme_name())
    except Exception:  # noqa: BLE001 - theme file missing must not kill the viewer
        _LOGGER.exception("viewer chrome tokens unreadable: %s", VIEWER_CHROME_QSS_PATH)
        return {}


def build_viewer_chrome_stylesheet() -> str:
    """One shared chrome stylesheet for every detail viewer.

    失败时返回空串（浮窗不能因主题文件坏掉而崩溃），但必须留痕：空串的可见后果是
    frame/图像面底色全消失 → 整窗透明、文字退回 app palette，真机上会透出底下的网格。
    """
    try:
        return render_templated_qss_section(
            VIEWER_CHROME_QSS_PATH, _viewer_theme_name(), "viewer"
        )
    except Exception:  # noqa: BLE001 - 主题文件坏掉不能连累浮窗，但必须留痕
        _LOGGER.exception("viewer chrome stylesheet render failed: %s", VIEWER_CHROME_QSS_PATH)
        return ""


_RGBA_RE = re.compile(
    r"rgba?\(\s*(?P<r>\d{1,3})\s*,\s*(?P<g>\d{1,3})\s*,\s*(?P<b>\d{1,3})"
    r"(?:\s*,\s*(?P<a>[\d.]+))?\s*\)",
    re.IGNORECASE,
)


def _css_color(value: str, fallback: str) -> QColor:
    """``rgba(r, g, b, a)`` / ``#rrggbb`` → QColor；QColor 本身不认 rgba()。"""
    color = QColor(value)
    if color.isValid():
        return color
    match = _RGBA_RE.fullmatch(str(value or "").strip())
    if match is None:
        return QColor(fallback)
    red = min(255, int(match.group("r")))
    green = min(255, int(match.group("g")))
    blue = min(255, int(match.group("b")))
    alpha_group = match.group("a")
    if alpha_group is None:
        return QColor(red, green, blue)
    alpha = float(alpha_group)
    if alpha <= 1:
        return QColor.fromRgbF(red / 255, green / 255, blue / 255, alpha)
    return QColor(red, green, blue, min(255, int(round(alpha))))


def viewer_image_color() -> QColor:
    """Background color of the image surface (for custom-painted canvases).

    A ``QWidget`` with an overridden ``paintEvent`` does not paint its QSS
    background; the canvas must fill this itself, otherwise the frame color
    shows through as a black halo in dark mode.
    """
    tokens = viewer_qss_tokens()
    return _css_color(tokens.get("VIEWER_IMAGE", ""), "#f2f2f2")


def style_viewer_top_bar(widget: QWidget) -> None:
    """给顶栏套共享 objectName；视觉规则全部在 ``viewer.qss``。

    这里刻意不设 WA_StyledBackground：Qt 在选择器匹配到 background 规则时会自动置位该属性，
    显式设置冗余；且自动置位是粘性的——objectName 漂移导致规则不再匹配时属性仍留在 widget
    上，Qt 会渲染出不透明底板。顶栏已无 border-bottom（边框只在 styled background 上绘制），
    故不设。danbooru / pawchive 都必须走本函数，保证两侧属性集合一致。
    """
    widget.setObjectName(VIEWER_TOPBAR_OBJECT)


class ViewerChromeMixin:
    """浮窗外壳与导航（唯一实现，见模块 docstring）。"""

    _suppress_closed: bool = False
    _native_window_warmed: bool = False
    _anchor_widget: t.Any = None
    left_panel: t.Optional[QWidget] = None

    # ---- 左列注入 ----
    def set_left_panel(self, widget: t.Optional[QWidget]) -> None:
        """注入左列内容（danbooru=标签组，pawchive=post 信息面板）。

        宽度由调用方 ``setFixedWidth``；基类只用 ``left_panel.width()`` 参与
        chrome 尺寸换算，不替内容做布局决策。
        """
        self.left_panel = widget

    def wheel_media_widgets(self) -> list:
        """滚轮翻页的有效落点。默认空 = 不翻页；子类必须给出媒体区。"""
        return []

    # ---- 窗口 flags / 定位 ----
    @staticmethod
    def _window_flags(keep_on_top: bool):
        flags = Qt.Window | Qt.FramelessWindowHint
        if keep_on_top:
            flags |= Qt.WindowStaysOnTopHint
        return flags

    def _screen_geometry(self) -> QRect:
        """锚点窗口所在屏幕的可用区域；取不到退主屏 / 兜底尺寸。"""
        center = None
        anchor = self._anchor_widget if isinstance(self._anchor_widget, QWidget) else None
        if anchor is not None and anchor.isVisible():
            center = anchor.mapToGlobal(anchor.rect().center())
        elif self.isVisible():
            center = self.mapToGlobal(self.rect().center())
        screen = QGuiApplication.screenAt(center) if center is not None else None
        if screen is not None:
            return screen.availableGeometry()
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return QRect(0, 0, 1280, 720)
        return screen.availableGeometry()

    def _reposition_viewer(self) -> None:
        """居中到锚点所在屏幕；**四边钳制**保证整窗不越出可用区。

        换项后窗口可能变矮再长高，若本次算 y 仍用旧高度，窗口会从偏下位置往上长、下半截
        飞出屏幕底边。钳制在 layout.clamp_into_screen()（纯函数、可单测）。

        **前置条件（全局不变量）**：调用方必须确保布局已 activate（self.size() 已是最终尺寸）。
        本方法只用当下尺寸做钳制，不会把陈旧尺寸变正确；这里刻意不加 activate。
        """
        geometry = self._screen_geometry()
        self.adjustSize()
        x, y = clamp_into_screen(self.size(), geometry)
        self.move(x, y)

    def _warm_native_window(self) -> None:
        """预热 HWND/DWM：Windows 分层窗口首次真显示才收得到鼠标命中。"""
        if self._native_window_warmed or self.isVisible():
            self._native_window_warmed = True
            return
        self._native_window_warmed = True
        self._suppress_closed = True
        try:
            self.setAttribute(Qt.WA_DontShowOnScreen, True)
            self.show()
            self.winId()
            self.hide()
        finally:
            self.setAttribute(Qt.WA_DontShowOnScreen, False)
            self._suppress_closed = False

    # ---- chrome 尺寸（画布可用区域 = 屏幕 - 外壳）----
    def _left_panel_width(self) -> int:
        panel = self.left_panel
        if panel is None:
            # 兼容 danbooru 的历史形状：左列建好了却没走 set_left_panel()。
            # 基类只读宽度，不替子类做布局决策。
            panel = getattr(self, "left_column", None)
        if panel is None:
            return 0
        return panel.width() if not panel.isHidden() else 0

    def _chrome_column_layout(self) -> t.Any:
        """承载媒体那一列的布局（margins/spacing 参与外壳换算）。

        pawchive 用 ``view_column``；danbooru 的图像列是 ``right_panel_widget``。
        两列的边距都是 0、间距都是 12，所以换算法线前后数值一致。
        """
        column = getattr(self, "view_column", None)
        if isinstance(column, QWidget) and column.layout() is not None:
            return column.layout()
        return getattr(self, "right_panel_layout", None)

    def _chrome_extent(self) -> ChromeExtent:
        column_layout = self._chrome_column_layout()
        column_margins = (
            column_layout.contentsMargins() if column_layout is not None else QMargins()
        )
        column_spacing = column_layout.spacing() if column_layout is not None else 0
        return chrome_extent(
            outer_margins=self.outer_layout.contentsMargins(),
            main_margins=self.main_layout.contentsMargins(),
            column_margins=column_margins,
            left_panel_width=self._left_panel_width(),
            main_spacing=self.main_layout.spacing(),
            column_spacing=column_spacing,
            top_bar_height=self.top_bar.height(),
        )

    def _chrome_width(self) -> int:
        return self._chrome_extent().width

    def _chrome_height(self) -> int:
        return self._chrome_extent().height

    def _image_display_bounds(self) -> QSize:
        return media_bounds(self._screen_geometry(), self._chrome_width(), self._chrome_height())

    # ---- fit（唯一实现；站点只喂 source_size）----
    def _view_ratio(self) -> float:
        """面积比（0..1），来自共享 viewer 设置。

        站点**不要**覆盖成常量（会让同一张图在两个站点显示尺寸不同）。导入放在函数里：
        ``settings`` 依赖 qfluentwidgets，本模块要保持"只被 import 也不炸"。
        """
        from .settings import viewer_view_ratio

        return viewer_view_ratio()

    def _viewer_fit_result(self, source_size: t.Optional[QSize] = None) -> ViewerFitResult:
        """媒体可用区域 + 源图尺寸 → fit 全部中间量（``layout.fit_calculator``）。"""
        return fit_calculator(self._image_display_bounds(), source_size, self._view_ratio())

    def _viewer_fit_size(self, source_size: t.Optional[QSize] = None) -> QSize:
        """fit 后的图像面尺寸（danbooru 的 ``display_size`` 同义）。"""
        return self._viewer_fit_result(source_size).display_size

    def _top_bar_min_width(self) -> int:
        return max(
            self.top_bar.sizeHint().width(),
            self.top_bar.minimumSizeHint().width(),
            self.top_bar.minimumWidth(),
        )

    # ---- 中键关闭（含防连击 guard，2026-11-16 danbooru 修复上移）----
    @staticmethod
    def _is_close_gesture(event) -> bool:
        return (
            event.type() == QEvent.MouseButtonRelease
            and event.button() == Qt.MiddleButton
        )

    def _close_from_middle_click(self) -> None:
        """先武装 tab 管理器的共享中键防抖，再隐藏。

        磨损中键一次点击弹成两次 release：浮窗在第一次 release 就隐藏后，第二次 release
        落到浮窗下面的网格/标签页上，会被当成新中键点击把 tab 关掉。所以隐藏前必须先
        emit ``middle_click_closed``（tab 层据此记时间戳），再 ``hide()``。
        """
        signal = getattr(self, "middle_click_closed", None)
        if signal is not None:
            signal.emit()
        self.hide()

    # ---- 滚轮：只认媒体区（2026-11-16 danbooru 修复上移）----
    def _wheel_targets_media_area(self, event) -> bool:
        """滚轮翻页只认媒体区（图片/视频/画布）。

        左列（标签组/信息面板）是滚动区：滚到顶/底时 Qt 把未接受的 wheel 冒泡给
        浮窗，旧的无条件处理会把"滚到头"读成"上一张/下一张"。限制落点后，左列
        与外壳的滚轮保持滚自己该滚的东西。
        """
        global_position = event.globalPosition().toPoint()
        return any(
            widget.isVisible()
            and widget.rect().contains(widget.mapFromGlobal(global_position))
            for widget in self.wheel_media_widgets()
        )

    def _handle_navigation_wheel(self, event) -> bool:
        if not self._wheel_targets_media_area(event):
            return False
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if not delta:
            return False
        if delta < 0 and self.next_btn.isEnabled():
            self.next_requested.emit()
        elif delta > 0 and self.previous_btn.isEnabled():
            self.previous_requested.emit()
        else:
            return False
        event.accept()
        return True

    def _ctrl_wheel_zoom(self, delta: float, position: QPointF) -> bool:
        """Ctrl+滚轮缩放钩子。默认不处理（没有缩放能力的浮窗不吞事件）。"""
        return False

    def _wheel_zoom_anchor(self) -> QPointF:
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return QPointF(self.width() / 2.0, self.height() / 2.0)
        return QPointF(canvas.width() / 2.0, canvas.height() / 2.0)

    # ---- 子类扩展点 ----
    def _handle_custom_key(self, event) -> bool:
        """站点特有键（先于统一键位）。返回 True 表示已消费。"""
        return False

    def _handle_extra_event_filter(self, obj, event) -> bool:
        """站点特有的 eventFilter 分支（如视频点击播放）。返回 True 表示已消费。"""
        return False

    # ---- 事件入口（唯一实现）----
    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        event_type = event.type()
        if event_type == QEvent.Wheel:
            # 顺序固定「先 Ctrl 后翻页」：Ctrl+滚轮只缩放，不翻页。
            if event.modifiers() & Qt.ControlModifier:
                delta = event.angleDelta().y() or event.pixelDelta().y()
                if delta and self._ctrl_wheel_zoom(float(delta), event.position()):
                    event.accept()
                    return True
                return False
            if self._handle_navigation_wheel(event):
                return True
        if obj is getattr(self, "top_bar", None) and event_type == QEvent.MouseButtonPress:
            # 顶栏空白处拖动窗口：无边框窗口没有系统标题栏，必须自己接管。
            if event.button() == Qt.LeftButton and obj.childAt(event.position().toPoint()) is None:
                try:
                    from qframelesswindow.utils import startSystemMove

                    startSystemMove(self, event.globalPosition().toPoint())
                    return True
                except Exception:  # noqa: BLE001 - 平台不支持就退回默认鼠标处理
                    pass
        if self._is_close_gesture(event):
            self._close_from_middle_click()
            event.accept()
            return True
        if self._handle_extra_event_filter(obj, event):
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def wheelEvent(self, event):  # noqa: N802
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta and self._ctrl_wheel_zoom(float(delta), self._wheel_zoom_anchor()):
                event.accept()
            return
        if self._handle_navigation_wheel(event):
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        """站点特有键 → 统一键位（本包 ``shortcuts``）→ 默认。"""
        if self._handle_custom_key(event):
            event.accept()
            return
        from .shortcuts import handle_viewer_key

        if handle_viewer_key(self, event):
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):  # noqa: N802
        if not self._suppress_closed:
            closed = getattr(self, "closed", None)
            if closed is not None:
                closed.emit()
        super().hideEvent(event)

    # ---- 错误反馈（唯一实现；两站共用，禁止各抄一份） ----
    def show_error_infobar(self, text: str, *, duration: int = 4000):
        """错误信息用 InfoBar 反馈（``parent=self``：viewer 前置激活时覆盖在 viewer 上）。

        错误文案不再叠到图像区 label（可选中 QLabel、无辨识度）；Loading / No more posts
        等临时状态仍走各站 ``set_placeholder`` / ``set_status_hint``。
        ``AcggrIcon`` 无 error 语义且 ``path()`` 返回资源 key 而非 qrc 路径，
        故用 ``InfoBar.error`` 自带的 ``InfoBarIcon.ERROR``。
        """
        from qfluentwidgets import InfoBar, InfoBarPosition

        try:
            return InfoBar.error(
                title="", content=str(text or ""), orient=Qt.Horizontal,
                isClosable=True, position=InfoBarPosition.TOP,
                duration=int(duration), parent=self,
            )
        except RuntimeError:  # viewer 已销毁（晚到的错误回包）
            return None

    # ---- 动图（QMovie）生命周期（唯一实现；两站共用） ----
    #: 子类在 ``__init__`` 里调用 ``self._init_animation_state()`` 后即可用。
    def _init_animation_state(self) -> None:
        self._animation_movie: t.Optional["QMovie"] = None
        self._animation_buffer: t.Optional[t.Any] = None
        self._animation_token: t.Any = None
        self._animation_source: bytes = b""

    def _build_movie(self, raw: bytes, token: t.Any) -> t.Optional["QMovie"]:
        """raw → 已 ``jumpToFrame(0)`` 的 ``QMovie``（主线程）；失败返回 ``None``。

        同一 ``(token, raw)`` 复用现有 movie（防重复解码/帧号回跳）。
        """
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QMovie

        data = bytes(raw or b"")
        if not data:
            return None
        if (
            self._animation_movie is not None
            and self._animation_token == token
            and self._animation_source == data
        ):
            return self._animation_movie
        self.stop_animation()
        buffer = QBuffer(self)
        buffer.setData(QByteArray(data))
        if not buffer.open(QIODevice.ReadOnly):
            buffer.deleteLater()
            return None
        movie = QMovie(buffer, parent=self)
        if not movie.isValid():
            movie.deleteLater()
            buffer.close()
            buffer.deleteLater()
            return None
        movie.jumpToFrame(0)
        self._animation_movie = movie
        self._animation_buffer = buffer
        self._animation_token = token
        self._animation_source = data
        return movie

    def stop_animation(self) -> None:
        """停表 + 释放 movie/buffer（先 movie 后 buffer，避免 C++ 对象先删）。"""
        movie = getattr(self, "_animation_movie", None)
        buffer = getattr(self, "_animation_buffer", None)
        self._animation_movie = None
        self._animation_buffer = None
        self._animation_token = None
        self._animation_source = b""
        if movie is not None:
            try:
                movie.stop()
            except RuntimeError:
                pass
            try:
                movie.setParent(None)
                movie.deleteLater()
            except RuntimeError:
                pass
        if buffer is not None:
            try:
                buffer.close()
                buffer.deleteLater()
            except RuntimeError:
                pass

    def has_animation(self) -> bool:
        return getattr(self, "_animation_movie", None) is not None


__all__ = ["MIDDLE_PRESS_MAX_AGE_MS", "ViewerChromeMixin"]
