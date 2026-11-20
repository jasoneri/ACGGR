"""pawchive 卡片控件（P1）。

不要把它和 DanbooruCardWidget 合并：两者位置参数与 i18n / 主题语义不兼容。

正方形占位（D-4/D-3）：_preview_size() 恒返回 preview_edge × preview_edge，尺寸只由
metrics 决定，类里没有"读 pixmap 尺寸"分支；缩略图到达只换像素、不改几何。

唯一的 danbooru 依赖：已下载灰度共享 helper（DanbooruCardWidget._apply_partial_grayscale）。
依赖纪律：只依赖 PySide6 + qfluentwidgets + .style / .post_adapter / .preview_loader。
"""

import typing as t
from collections import OrderedDict

from PySide6 import QtCore, QtGui
from PySide6.QtCore import QEvent, QObject, Qt, Signal, qtTrId
from PySide6.QtGui import QMovie, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .preview_loader import animated_frame_size
from .style import (
    DEFAULT_PAWCHIVE_CARD_METRICS,
    PawchiveCardMetrics,
    PawchiveCardTheme,
    PawchiveUiPalette,
    build_card_stylesheet,
    get_pawchive_card_theme,
    get_pawchive_qss_tokens,
    qcolor_from_css,
)

_CARD_SELECTOR_SIZE = 26
_CARD_SELECTOR_IDLE_INNER_RATIO = 0.7
_CARD_SELECTOR_HOVER_INNER_RATIO = 0.55
_CARD_CONTENT_MARGIN = 4
#: 预览区高宽比（h/w）上下限。真实比例优先，但极端长条（1:4 竖图 / 4:1 横幅）
#: 会把瀑布流拉成一条缝或一座塔，故夹到这个区间；被夹时才退化为裁剪填充。
_ASPECT_MIN = 0.55
_ASPECT_MAX = 1.85
_CARD_PREVIEW_BORDER_INSET = 1
_CARD_PREVIEW_RADIUS = 14.0
_VIDEO_BADGE_H_PADDING = 6
_VIDEO_BADGE_V_PADDING = 3
_VIDEO_BADGE_RADIUS = 6.0
_VIDEO_BADGE_MARGIN = 8
_VIDEO_PLAY_TRIANGLE_SIZE = 5
_RING_DIAMETER = 30
_RING_STROKE = 2.5
_RING_MARGIN = 8
_RING_PLATE_ALPHA = 115

#: 已下载灰版 LRU：208² × 4B ≈ 173 KB/项 → 上限 ≈ 44 MB（06 §2.3）。
_GRAY_CACHE_MAX = 256
_GRAY_CACHE: "OrderedDict[tuple, QPixmap]" = OrderedDict()


def clear_gray_cache():
    """清灰版缓存。``grid.reset()`` / 主题切换时调用（令牌变了，旧灰版不该复用）。"""
    _GRAY_CACHE.clear()


#: 本模块经 ``_tr_or`` 使用的全部 i18n 键。**这份元组不是装饰**：``_tr_or`` 用变量
#: 调 ``qtTrId``，而 ``res/i18n/build.py`` 的 AST 扫描只认字面量实参，或"赋值右侧
#: tuple/list/dict 里的 id 形字符串"这条间接通道。没有它，这些键在 ``--check`` 里
#: 会被判 stale，P4 一跑 ``--sync`` 就会把它们从表里删掉。新增/改名键时必须同步这里。
_CARD_I18N_KEYS = (
    "pawchive.card.download_failed",
    "pawchive.card.download_only",
    "pawchive.card.preview_error",
    "pawchive.card.reload_preview",
    "pawchive.download.no_url",
)


def _tr_or(key: str, fallback: str) -> str:
    """``qtTrId`` 缺键时会回退成 id 本身；此时用更短的兜底串，界面不至于读出一串 id。

    06 §T3 允收缺键期间"难看但可用"，但 id 串（``pawchive.card.download_only``）
    比兜底文案长得多，卡片和工具栏会被撑变形。
    """
    text = qtTrId(key)
    return fallback if text == key else text


class PawchiveCardSelectorButton(QToolButton):
    """右上角圆形勾选点：自绘渐变 + 内圆 + hover 收缩（视觉照 danbooru 卡，代码自立）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hovered = False
        self._shadow_color = QtGui.QColor("#ffbeb8")
        self._inner_color = QtGui.QColor("#ffffff")
        self._gradient_start = QtGui.QColor("#acb1cb")
        self._gradient_mid = QtGui.QColor("#d1aacf")
        self._gradient_end = QtGui.QColor("#a99f8d")
        self._idle_opacity = 0.25
        self._disabled_opacity = 0.25
        self.setObjectName("PawchiveCardSelector")
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setText("")
        self.setFixedSize(_CARD_SELECTOR_SIZE, _CARD_SELECTOR_SIZE)
        self.apply_theme()

    def apply_theme(self):
        tokens = get_pawchive_qss_tokens()
        self._gradient_start = qcolor_from_css(tokens["CARD_CHECKBOX_GRADIENT_START"])
        self._gradient_mid = qcolor_from_css(tokens["CARD_CHECKBOX_GRADIENT_MID"])
        self._gradient_end = qcolor_from_css(tokens["CARD_CHECKBOX_GRADIENT_END"])
        self._shadow_color = qcolor_from_css(tokens["CARD_CHECKBOX_SHADOW"])
        self._inner_color = qcolor_from_css(tokens["CARD_CHECKBOX_INNER"])
        self._idle_opacity = max(0.0, min(1.0, float(tokens["CARD_CHECKBOX_OPACITY"])))
        self._disabled_opacity = max(
            0.0, min(1.0, float(tokens["CARD_CHECKBOX_DISABLED_OPACITY"]))
        )
        self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        _ = event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        opacity = (
            self._disabled_opacity
            if not self.isEnabled()
            else (1.0 if self._hovered or self.hasFocus() else self._idle_opacity)
        )
        painter.setOpacity(opacity)

        shadow_rect = QtCore.QRectF(1.0, 4.0, self.width() - 2.0, self.height() - 2.0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._shadow_color)
        painter.drawEllipse(shadow_rect)

        circle_rect = QtCore.QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        gradient = QtGui.QLinearGradient(circle_rect.topLeft(), circle_rect.bottomRight())
        gradient.setColorAt(0.0, self._gradient_start)
        gradient.setColorAt(0.46, self._gradient_mid)
        gradient.setColorAt(1.0, self._gradient_end)
        painter.setBrush(QtGui.QBrush(gradient))
        painter.drawEllipse(circle_rect)

        inner_ratio = (
            _CARD_SELECTOR_HOVER_INNER_RATIO
            if self._hovered and self.isEnabled()
            else _CARD_SELECTOR_IDLE_INNER_RATIO
        )
        inner_size = self.width() * inner_ratio
        inner_offset = (self.width() - inner_size) / 2.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._inner_color)
        painter.drawEllipse(QtCore.QRectF(inner_offset, inner_offset, inner_size, inner_size))

        if self.hasFocus():
            painter.setOpacity(1.0)
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 72), 2.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(circle_rect.adjusted(-2.0, -2.0, 2.0, 2.0))


class PawchiveCardPreviewButton(QPushButton):
    """预览面：圆角裁剪自绘，客户端居中裁切（服务端只给长边 800 的缩略图）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preview_pixmap = QPixmap()

    def set_preview_pixmap(self, pixmap: QPixmap):
        self._preview_pixmap = QPixmap(pixmap)
        self.update()

    def clear_preview_pixmap(self):
        self._preview_pixmap = QPixmap()
        self.update()

    def preview_pixmap(self) -> QPixmap:
        return QPixmap(self._preview_pixmap)

    def paintEvent(self, event):
        if self._preview_pixmap.isNull():
            super().paintEvent(event)
            return

        _ = event
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.icon = QtGui.QIcon()
        option.text = ""

        painter = QStylePainter(self)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        painter.drawControl(QStyle.CE_PushButtonBevel, option)

        target_rect = self.rect().adjusted(
            _CARD_PREVIEW_BORDER_INSET,
            _CARD_PREVIEW_BORDER_INSET,
            -_CARD_PREVIEW_BORDER_INSET,
            -_CARD_PREVIEW_BORDER_INSET,
        )
        if target_rect.width() <= 0 or target_rect.height() <= 0:
            return

        path = QtGui.QPainterPath()
        path.addRoundedRect(
            QtCore.QRectF(target_rect), _CARD_PREVIEW_RADIUS, _CARD_PREVIEW_RADIUS
        )
        painter.save()
        painter.setClipPath(path)
        if self._preview_pixmap.size() == target_rect.size():
            painter.drawPixmap(target_rect.topLeft(), self._preview_pixmap)
        else:
            scaled = self._preview_pixmap.scaled(
                target_rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            source_x = max(0, (scaled.width() - target_rect.width()) // 2)
            source_y = max(0, (scaled.height() - target_rect.height()) // 2)
            source_rect = QtCore.QRect(
                source_x, source_y, target_rect.width(), target_rect.height()
            )
            painter.drawPixmap(target_rect, scaled, source_rect)
        painter.restore()


class _IndeterminateSpin:
    """不定式环的**全局**自旋驱动：一个 20ms 定时器 + 一个共享角度。

    06 §2.2：不定式（服务层只给 status 不给 total）不新增每卡定时器；所有不定式环
    共用一个角度变量，无环时停表。
    """

    _instance: t.Optional["_IndeterminateSpin"] = None

    def __init__(self):
        self._angle = 0
        self._rings: set = set()
        self._timer = QtCore.QTimer()
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._tick)

    @classmethod
    def instance(cls) -> "_IndeterminateSpin":
        if cls._instance is None:
            cls._instance = _IndeterminateSpin()
        return cls._instance

    @property
    def angle(self) -> int:
        return self._angle

    def add(self, ring: "DownloadRingOverlay"):
        self._rings.add(ring)
        if not self._timer.isActive():
            self._timer.start()

    def remove(self, ring: "DownloadRingOverlay"):
        self._rings.discard(ring)
        if not self._rings:
            self._safe_stop()

    def _tick(self):
        self._angle = (self._angle + 12) % 360
        for ring in list(self._rings):
            try:
                ring.update()
            except RuntimeError:  # C++ 侧已销毁
                self._rings.discard(ring)
        if not self._rings:
            self._timer.stop()

    def _safe_stop(self):
        try:
            self._timer.stop()
        except RuntimeError:  # 单例随进程收尾时的边缘态
            pass


class DownloadRingOverlay(QWidget):
    """卡右下角进度环（overlay 控件，**不烤进 pixmap**）。

    烤进 pixmap 意味着每次进度事件都重跑"缩放 + 裁剪 + 灰度 + 视频徽标"全链；
    overlay 零改 danbooru、零重烤，深色底板顺带盖住视频徽标（06 §2.2）。
    懒创建：首次 ``set_progress`` 才建，448 个格子不会常驻 448 个子控件。

    频率三层节流（L1 收 / L2 批 / L3 绘）的最后一层就在这里：值判等后相同直接
    ``return``，不 ``update()``。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self._progress: t.Optional[float] = None
        self._failed = False
        self._spinning = False
        self.setFixedSize(_RING_DIAMETER, _RING_DIAMETER)

    def set_progress(self, value: t.Optional[float]):
        rounded = None if value is None else round(max(0.0, min(1.0, float(value))), 2)
        if rounded == self._progress:
            return
        self._progress = rounded
        self._sync_spin()
        self.update()

    def set_failed(self, failed: bool):
        failed = bool(failed)
        if failed == self._failed:
            return
        self._failed = failed
        self._sync_spin()
        self.update()

    def _sync_spin(self):
        want = (self._progress is None) and (not self._failed) and self.isVisible()
        if want == self._spinning:
            return
        self._spinning = want
        spin = _IndeterminateSpin.instance()
        if want:
            spin.add(self)
        else:
            spin.remove(self)

    def paintEvent(self, event):
        _ = event
        theme = get_pawchive_card_theme(False)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)

        plate = qcolor_from_css(theme.progress_plate)
        plate.setAlpha(_RING_PLATE_ALPHA)
        painter.setPen(Qt.NoPen)
        painter.setBrush(plate)
        painter.drawEllipse(self.rect())

        inset = _RING_STROKE / 2.0 + 3.0
        arc_rect = QtCore.QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)

        track = qcolor_from_css(theme.progress_track)
        painter.setPen(
            QtGui.QPen(track, _RING_STROKE, Qt.SolidLine, Qt.RoundCap)
        )
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(arc_rect, 0, 360 * 16)

        arc_color = qcolor_from_css(
            theme.progress_failed if self._failed else theme.progress_arc
        )
        painter.setPen(QtGui.QPen(arc_color, _RING_STROKE, Qt.SolidLine, Qt.RoundCap))
        if self._failed:
            painter.drawArc(arc_rect, 0, 360 * 16)
        elif self._progress is None:
            # 不定式：90° 长弧绕圈转（共享角度，非每卡计时器）。
            start = (_IndeterminateSpin.instance().angle + 90) * 16
            painter.drawArc(arc_rect, start, 90 * 16)
        else:
            painter.drawArc(arc_rect, 90 * 16, -int(self._progress * 360 * 16))

        painter.setPen(QtGui.QColor(255, 255, 255, 235))
        font = painter.font()
        font.setPointSize(8)
        font.setWeight(QtGui.QFont.Weight.DemiBold)
        if self._failed:
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, "!")
        elif self._progress is not None:
            percent = int(self._progress * 100)
            # 06 §2.2：value >= 10 才画整数百分比（8pt DemiBold）。30px 圆扣描边约 20px
            # 可用宽：两位数 8pt 正好，三位数会被裁成半截字，按字宽阶梯降到 6pt；
            # 连 6pt 都放不下就只留满环（满环即"完成"）。
            if percent >= 10:
                text = str(percent)
                inner = self.width() - (_RING_STROKE + 4) * 2
                for size in (8, 6):
                    font.setPointSize(size)
                    painter.setFont(font)
                    if painter.fontMetrics().horizontalAdvance(text) <= inner:
                        painter.drawText(self.rect(), Qt.AlignCenter, text)
                        break

    def hideEvent(self, event):
        spin = _IndeterminateSpin.instance()
        spin.remove(self)
        self._spinning = False
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_spin()


class PawchiveCardWidget(QFrame):
    """一格一图的作品卡。尺寸只由 ``metrics`` 决定，绝不读 pixmap。"""

    open_detail_requested = Signal(object)  # (CardKey)
    selection_changed = Signal(object, bool)  # (card, selected)
    progress_changed = Signal(object, float)  # (CardKey, ratio) —— P2 消费
    #: 卡片高度因缩略图真实宽高比而变化：外层瀑布流需要重排（否则会压在下一张上）。
    layout_changed = Signal()
    #: 卡自己观察到"下载完成"时报告（06 §2.8 状态机终态）。网格据此写已下载账本，
    #: 保证"卡变灰"与"账本记上"不会只有一半生效。
    download_completed = Signal(object)  # (CardKey)
    #: 右键 →「重新加载缩略图」（CardKey）。网格/宿主接 ``PawchivePreviewLoader.reload``。
    reload_requested = Signal(object)  # (CardKey)

    def __init__(
        self,
        item,
        *,
        metrics: PawchiveCardMetrics = DEFAULT_PAWCHIVE_CARD_METRICS,
        already_downloaded: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.post = item
        self.item = item
        self.metrics = metrics
        self.already_downloaded = bool(already_downloaded)
        self._card_theme = get_pawchive_card_theme(self.already_downloaded)
        self._preview_pixmap = QPixmap()
        #: 预览区高宽比（h/w）。1.0 = 正方形占位；缩略图解出来后按真实比例改写。
        self._aspect = 1.0
        self._failed = False
        self._progress: t.Optional[float] = None
        self._ring: t.Optional[DownloadRingOverlay] = None
        #: 动图（QMovie 必须在主线程建；buffer 要跟着 movie 一起活着）。
        self._preview_movie: t.Optional[QMovie] = None
        self._preview_movie_buffer: t.Optional[QtCore.QBuffer] = None
        self._preview_movie_source: bytes = b""
        self._preview_movie_frame_size = QtCore.QSize()
        self._rendering_movie_frame = False
        self.setObjectName("PawchiveCardWidget")
        self._setup_ui()
        self.apply_theme()

    # ---- 结构 ----
    def _setup_ui(self):
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setProperty("downloaded", self.already_downloaded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            _CARD_CONTENT_MARGIN, _CARD_CONTENT_MARGIN, _CARD_CONTENT_MARGIN, _CARD_CONTENT_MARGIN
        )
        layout.setSpacing(0)

        self.preview_frame = QFrame(self)
        self.preview_frame.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        preview_layout = QVBoxLayout(self.preview_frame)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)

        self.preview_button = PawchiveCardPreviewButton(self.preview_frame)
        self.preview_button.setObjectName("PawchiveCardPreview")
        self.preview_button.setProperty("unsupported", not self.post.is_supported)
        self.preview_button.setCursor(Qt.PointingHandCursor)
        self.preview_button.clicked.connect(
            lambda: self.open_detail_requested.emit(self.key())
        )
        self.preview_button.setText(self._initial_preview_text())
        preview_layout.addWidget(self.preview_button)

        self._preview_glow = QGraphicsDropShadowEffect(self.preview_frame)
        self._preview_glow.setOffset(0.0, 0.0)
        self._preview_glow.setBlurRadius(0.0)
        self._preview_glow.setEnabled(False)
        self.preview_frame.setGraphicsEffect(self._preview_glow)

        self.checkbox = PawchiveCardSelectorButton(self.preview_frame)
        self.checkbox.raise_()
        self.checkbox.toggled.connect(self._on_checkbox_toggled)
        layout.addWidget(self.preview_frame, 0, Qt.AlignLeft | Qt.AlignTop)

        self._sync_geometry()
        self._apply_checkbox_availability()
        self._position_overlay_widgets()
        self._sync_selection_state(self.checkbox.isChecked())

    def key(self):
        """``CardKey = (post_id, attachment_path)``，两段皆 str。"""
        return (str(self.post.post_id), str(self.post.attachment_path))

    # ---- 尺寸：只由 metrics 决定 ----
    def _preview_size(self) -> QtCore.QSize:
        """预览区尺寸：宽来自缩放档，高按**真实宽高比**算（瀑布流的关键）。

        高度必须跟着图走，否则只能靠正方形裁剪来统一 —— 那就是"竖图被裁成方块"。
        """
        return QtCore.QSize(self.preview_width, self.preview_height)

    @property
    def aspect_ratio(self) -> float:
        return float(self._aspect)

    def apply_aspect_ratio(self, width: int, height: int) -> bool:
        """按真实像素尺寸更新卡片高度；返回是否真的变了（变了外层要重排）。"""
        if width <= 0 or height <= 0:
            return False
        ratio = max(_ASPECT_MIN, min(_ASPECT_MAX, float(height) / float(width)))
        if abs(ratio - self._aspect) < 0.01:
            return False
        self._aspect = ratio
        self._sync_geometry()
        self._position_overlay_widgets()
        if not self._preview_pixmap.isNull():
            self._apply_preview_icon()
        self.updateGeometry()
        self.layout_changed.emit()
        return True

    @property
    def preview_width(self) -> int:
        return max(1, self.metrics.preview_edge)

    @property
    def preview_height(self) -> int:
        return max(1, int(round(self.metrics.preview_edge * self._aspect)))

    def sizeHint(self) -> QtCore.QSize:
        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else QtCore.QMargins()
        return QtCore.QSize(
            margins.left() + self.preview_width + margins.right(),
            margins.top() + self.preview_height + margins.bottom(),
        )

    def minimumSizeHint(self) -> QtCore.QSize:
        return self.sizeHint()

    def _sync_geometry(self):
        size = self._preview_size()
        self.preview_frame.setFixedSize(size)
        self.preview_button.setFixedSize(size)
        self.setFixedSize(self.sizeHint())
        self._sync_preview_movie_scale()

    def apply_metrics(self, metrics: PawchiveCardMetrics, *, refresh_preview: bool = True):
        self.metrics = metrics
        self._sync_geometry()
        self._position_overlay_widgets()
        self._sync_preview_movie_scale()
        if refresh_preview and not self._preview_pixmap.isNull():
            self._apply_preview_icon()
        self.updateGeometry()

    def _position_overlay_widgets(self):
        tokens = get_pawchive_qss_tokens()
        max_x = max(0, self.preview_width - self.checkbox.width())
        max_y = max(0, self.preview_height - self.checkbox.height())
        self.checkbox.move(
            max(0, min(max_x, int(round(float(tokens["CARD_CHECKBOX_OFFSET_X"]))))),
            max(0, min(max_y, int(round(float(tokens["CARD_CHECKBOX_OFFSET_Y"]))))),
        )
        if self._ring is not None:
            self._ring.move(
                max(0, self.preview_frame.width() - _RING_DIAMETER - _RING_MARGIN),
                max(0, self.preview_frame.height() - _RING_DIAMETER - _RING_MARGIN),
            )

    # ---- 主题 ----
    def apply_theme(self):
        self._card_theme = get_pawchive_card_theme(self.already_downloaded)
        self.setStyleSheet(
            build_card_stylesheet(PawchiveUiPalette.current(), self.already_downloaded)
        )
        self.checkbox.apply_theme()
        self._sync_preview_glow()
        if not self._preview_pixmap.isNull():
            self._apply_preview_icon()
        if self._ring is not None:
            self._ring.update()

    def _sync_preview_glow(self):
        self._preview_glow.setColor(qcolor_from_css(self._card_theme.glow_color))
        self._preview_glow.setBlurRadius(26.0)
        self._preview_glow.setEnabled(self._is_selected())

    # ---- 选中 ----
    def _is_selected(self) -> bool:
        return bool(self.property("selected"))

    def _sync_selection_state(self, selected: bool):
        self.setProperty("selected", selected)
        self.setProperty("downloaded", self.already_downloaded)
        self.style().unpolish(self)
        self.style().polish(self)
        self._sync_preview_glow()
        if not self._preview_pixmap.isNull():
            self._apply_preview_icon()
        self.update()

    def _on_checkbox_toggled(self, checked: bool):
        self._sync_selection_state(checked)
        self.selection_changed.emit(self, checked)

    def set_selected(self, selected: bool):
        if self.checkbox.isEnabled():
            self.checkbox.blockSignals(True)
            self.checkbox.setChecked(selected)
            self.checkbox.blockSignals(False)
        self._sync_selection_state(self.checkbox.isChecked())

    def _apply_checkbox_availability(self):
        self.checkbox.setVisible(not self.already_downloaded)
        self.checkbox.setDisabled((not self.post.is_downloadable) or self.already_downloaded)
        if not self.post.is_downloadable:
            self.setToolTip(_tr_or("pawchive.download.no_url", self.post.file_ext.upper()))

    def set_already_downloaded(self, downloaded: bool):
        self.already_downloaded = bool(downloaded)
        if self.already_downloaded:
            self.checkbox.blockSignals(True)
            self.checkbox.setChecked(False)
            self.checkbox.blockSignals(False)
        self._apply_checkbox_availability()
        self.apply_theme()
        if not self._preview_pixmap.isNull():
            self._apply_preview_icon()
        self._sync_selection_state(self.checkbox.isChecked())

    # ---- 预览像素 ----
    def set_preview_pixmap(self, pixmap: QPixmap):
        """收到缩略图：先按**真实宽高比**定卡高（瀑布流），再刷新像素。

        改了几何是有意的：正方形裁剪统一不了高度，只能让卡跟着图长。
        高度变了会发 ``layout_changed``，外层合并成一次重排。
        """
        self._preview_pixmap = QPixmap(pixmap)
        self._failed = False
        if not self._preview_pixmap.isNull():
            self.apply_aspect_ratio(
                self._preview_pixmap.width(), self._preview_pixmap.height()
            )
        self._apply_preview_icon()

    def set_preview_animation(self, raw: bytes) -> bool:
        """把缩略图的原始字节接成 ``QMovie`` 播放（真动图，不再只显示首帧）。

        静态首帧仍由 ``set_preview_pixmap`` 落好：动画未起/暂停/被系统停表时卡上
        仍有画面。``QMovie`` 与 ``QBuffer`` 都只在主线程构造，buffer 存成成员——
        movie 只持有设备指针，buffer 一被回收就停帧。

        同一份字节重复回包直接复用现有 movie（防重复创建导致闪烁/双份解码）。
        返回是否已有一段可播放的动画。
        """
        data = bytes(raw or b"")
        if not data or not self.post.has_renderable_preview_asset:
            return False
        if (
            self._preview_movie is not None
            and self._preview_movie_source == data
        ):
            self._resume_preview_animation()
            return True
        self._stop_preview_animation()
        buffer = QtCore.QBuffer(self)
        buffer.setData(QtCore.QByteArray(data))
        if not buffer.open(QtCore.QIODevice.ReadOnly):
            buffer.deleteLater()
            return False
        movie = QMovie(buffer, parent=self)
        if not movie.isValid():
            movie.deleteLater()
            buffer.close()
            buffer.deleteLater()
            return False
        self._preview_movie = movie
        self._preview_movie_buffer = buffer
        self._preview_movie_source = data
        frame_size = animated_frame_size(data)
        self._preview_movie_frame_size = QtCore.QSize(frame_size)
        if frame_size.width() > 0 and frame_size.height() > 0:
            self.apply_aspect_ratio(frame_size.width(), frame_size.height())
        self._sync_preview_movie_scale()
        movie.frameChanged.connect(self._on_preview_movie_frame)
        # 先落第一帧再 start：start() 的首帧回包要走一次事件循环，期间卡会空白。
        movie.jumpToFrame(0)
        self._on_preview_movie_frame()
        if self.isVisible():
            movie.start()
        return True

    def has_preview_animation(self) -> bool:
        return self._preview_movie is not None

    def _sync_preview_movie_scale(self):
        """把 movie 的解码尺寸贴到当前预览区（缩放换档/比例变化都要重贴）。"""
        movie = self._preview_movie
        frame_size = self._preview_movie_frame_size
        if movie is None or frame_size.width() <= 0 or frame_size.height() <= 0:
            return
        target = self._preview_target_size()
        scaled = frame_size.scaled(target, Qt.KeepAspectRatioByExpanding)
        try:
            movie.setScaledSize(scaled)
        except RuntimeError:  # C++ 侧已销毁
            return

    def _on_preview_movie_frame(self, _frame_number: int = 0):
        movie = self._preview_movie
        if movie is None or self._rendering_movie_frame:
            return
        try:
            frame = movie.currentPixmap()
        except RuntimeError:  # C++ 侧已销毁
            return
        if frame.isNull():
            return
        # 逐帧走既有像素链（裁剪 + 已下载灰度 + 选中渐变 + 视频徽标），保证与静态帧同观感。
        self._rendering_movie_frame = True
        try:
            self._preview_pixmap = QPixmap(frame)
            self._failed = False
            self._apply_preview_icon()
        finally:
            self._rendering_movie_frame = False

    def _pause_preview_animation(self):
        movie = self._preview_movie
        if movie is None:
            return
        try:
            if movie.state() == QMovie.MovieState.Running:
                movie.setPaused(True)
        except RuntimeError:  # C++ 侧已销毁
            pass

    def _resume_preview_animation(self):
        movie = self._preview_movie
        if movie is None or not self.isVisible():
            return
        try:
            state = movie.state()
            if state == QMovie.MovieState.Paused:
                movie.setPaused(False)
            elif state == QMovie.MovieState.NotRunning:
                movie.start()
        except RuntimeError:  # C++ 侧已销毁
            pass

    def _stop_preview_animation(self):
        """停表 + 释放 movie/buffer。**顺序不能反**：movie 先删，buffer 后删。"""
        movie = self._preview_movie
        buffer = self._preview_movie_buffer
        self._preview_movie = None
        self._preview_movie_buffer = None
        self._preview_movie_source = b""
        self._preview_movie_frame_size = QtCore.QSize()
        if movie is not None:
            try:
                movie.stop()
                movie.frameChanged.disconnect()
            except (RuntimeError, TypeError):
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

    # ---- 右键：由网格用 FluentMonkeyPatch 挂菜单，卡片自己不碰坐标 ----
    def showEvent(self, event):
        super().showEvent(event)
        self._resume_preview_animation()

    def hideEvent(self, event):
        self._pause_preview_animation()
        super().hideEvent(event)

    def event(self, event):
        if event.type() == QEvent.DeferredDelete:
            self._stop_preview_animation()
        return super().event(event)

    def deleteLater(self):
        self._stop_preview_animation()
        super().deleteLater()

    def _preview_target_size(self) -> QtCore.QSize:
        inset = _CARD_PREVIEW_BORDER_INSET * 2
        return QtCore.QSize(
            max(1, self.preview_width - inset),
            max(1, self.preview_height - inset),
        )

    def _default_preview_text(self) -> str:
        ext = (self.post.file_ext or "").upper()
        if self.post.has_renderable_preview_asset:
            return ""
        if self.post.is_downloadable:
            # 有 URL、能下、不能预览（archive / 视频无缩略图）：专用键，禁与 no_url 混用。
            return _tr_or("pawchive.card.download_only", ext or "FILE")
        return _tr_or("pawchive.download.no_url", ext or "—")

    def _initial_preview_text(self) -> str:
        if self.post.has_renderable_preview_asset:
            return ""
        return self._default_preview_text()

    def _build_preview_icon(self) -> QPixmap:
        target_size = self._preview_target_size()
        if self._preview_pixmap.isNull():
            return QPixmap()
        scaled = self._preview_pixmap.scaled(
            target_size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )
        source_x = max(0, (scaled.width() - target_size.width()) // 2)
        source_y = max(0, (scaled.height() - target_size.height()) // 2)
        preview = scaled.copy(
            source_x, source_y, target_size.width(), target_size.height()
        )
        if self.already_downloaded:
            preview = self._apply_downloaded_preview_effect(preview)
        if self._is_selected():
            preview = self._apply_selection_overlay(preview, self._card_theme)
        if self.post.uses_viewer_video:
            preview = self._apply_video_badge(
                preview, self._card_theme, self.post.viewer_video_media_ext
            )
        return preview

    def _apply_preview_icon(self):
        preview = self._build_preview_icon()
        self.preview_button.set_preview_pixmap(preview)
        self.preview_button.setText("" if not preview.isNull() else self._preview_text())

    def _preview_text(self) -> str:
        if self._failed:
            return _tr_or("pawchive.card.preview_error", "!")
        return self._default_preview_text()

    def refresh_preview_icon(self):
        if not self._preview_pixmap.isNull():
            self._apply_preview_icon()

    def set_failed(self, failed: bool):
        """缩略图加载失败：占位文案，尺寸不变（正方形占位不塌）。"""
        self._failed = bool(failed)
        if self._failed:
            self._stop_preview_animation()
            self.preview_button.clear_preview_pixmap()
        self.preview_button.setText(self._preview_text())

    # ---- 进度（P2 挂点，P1 先建结构） ----
    def _ensure_ring(self) -> DownloadRingOverlay:
        """懒创建环。**必须 raise_**：preview_button 铺满 preview_frame，布局重新
        polish 时会把它抬到同层兄弟之上，环（创建更晚）不显式 raise 就被整块盖住。"""
        if self._ring is None:
            self._ring = DownloadRingOverlay(self.preview_frame)
            self._position_overlay_widgets()
            self._ring.show()
        self._ring.raise_()
        return self._ring

    def set_progress(self, ratio: t.Optional[float]):
        ring = self._ensure_ring()
        self._progress = None if ratio is None else max(0.0, min(1.0, float(ratio)))
        ring.set_progress(self._progress)
        self.progress_changed.emit(self.key(), -1.0 if self._progress is None else self._progress)

    #: 06 §2.1 契约名 —— ``show_progress(pct | None)``：None = 不定式（服务层只给
    #: status 不给 total 时的退化分支，见 06 §5-3）。
    def show_progress(self, pct: t.Optional[float]):
        self.set_progress(pct)

    #: 06 §2.1 契约名 —— ``set_download_state(state)``：
    #: ``queued`` / ``active`` / ``complete`` / ``error``（state 枚举归 04/06）。
    def set_download_state(self, state: t.Any):
        value = str(getattr(state, "value", state) or "").strip().lower()
        if value in ("queued", "pending"):
            self.set_progress(None)
        elif value in ("active", "running", "downloading"):
            if self._progress is None and self._ring is None:
                self.set_progress(None)
        elif value in ("complete", "completed", "done"):
            self.clear_progress()
            self.set_already_downloaded(True)
            # 卡自己也要把"完成"报告出去：账本（grid._downloaded_keys）归网格管，
            # 卡只知道自己；两边同时更新才不会出现"卡灰了但还能被全选本组选中"。
            self.download_completed.emit(self.key())
        elif value in ("error", "failed", "failure"):
            self.set_failed_download(True)

    def set_failed_download(self, failed: bool, error_message: str = ""):
        """失败态：红环 + tooltip。**两层文案分工不同**：

        - 短标签用 ``pawchive.card.download_failed``（08 §2.1 沿用态表把「单卡下载
          失败」映射到它）：卡上的失败是**卡级**事实，不该复用批量下载的
          ``download.failed``（后者带 ``{error}`` 占位，缺 error 时会出现"下载失败："空尾巴）。
        - 有具体错误再另起一行拼 ``\\n{error_message}``，无则不拼。
        """
        ring = self._ensure_ring()
        ring.set_failed(bool(failed))
        if failed:
            label = _tr_or("pawchive.card.download_failed", "Download failed")
            detail = str(error_message or "").strip()
            self.setToolTip(f"{label}\n{detail}" if detail else label)
        else:
            self._apply_checkbox_availability()
            if self.post.is_downloadable:
                self.setToolTip("")

    def clear_progress(self):
        if self._ring is not None:
            self._ring.hide()
            self._ring.deleteLater()
            self._ring = None
        self._progress = None

    def has_ring(self) -> bool:
        """自查用：环是否常驻（06 附清单第 1 项：活动环 ≤ 在途文件数）。"""
        return self._ring is not None

    # ---- 像素效果（与 danbooru/card.py 同源，改动需两边同步） ----
    @staticmethod
    def _apply_opacity(pixmap: QPixmap, opacity: float) -> QPixmap:
        clamped = max(0.0, min(1.0, opacity))
        if clamped >= 1.0 or pixmap.isNull():
            return QPixmap(pixmap)
        faded = QPixmap(pixmap.size())
        faded.fill(Qt.transparent)
        painter = QtGui.QPainter(faded)
        painter.setOpacity(clamped)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return faded

    def _apply_downloaded_preview_effect(self, pixmap: QPixmap) -> QPixmap:
        """已下载态像素入口：灰度 + 降透明度。

        **不实现灰度本体**：委托 danbooru 的共享 helper（契约 §三.7 / 06 §2.3），
        一处实现两处受益。这是 pawchive 侧唯一允许的 ``app.GUI.danbooru.*`` 依赖。
        """
        amount = self._card_theme.preview_downloaded_grayscale
        if self._rendering_movie_frame:
            # 动图逐帧灰度：缓存键不含帧内容，命中会把首帧的灰版复用给所有帧
            # （表现为"灰掉的动图不动"），所以动画帧只算不缓存。
            from app.GUI.danbooru.card import DanbooruCardWidget  # 05 §2.4 灰化挂点

            gray = DanbooruCardWidget._apply_partial_grayscale(pixmap, amount)
            return self._apply_opacity(gray, self._card_theme.preview_downloaded_opacity)
        key = (
            str(getattr(self.post, "attachment_path", "")),
            pixmap.width(),
            pixmap.height(),
            round(amount, 2),
        )
        gray = _GRAY_CACHE.get(key)
        if gray is None:
            from app.GUI.danbooru.card import DanbooruCardWidget  # 05 §2.4 灰化挂点

            gray = DanbooruCardWidget._apply_partial_grayscale(pixmap, amount)
            _GRAY_CACHE[key] = gray
            while len(_GRAY_CACHE) > _GRAY_CACHE_MAX:
                _GRAY_CACHE.popitem(last=False)
        else:
            _GRAY_CACHE.move_to_end(key)
        return self._apply_opacity(gray, self._card_theme.preview_downloaded_opacity)

    @staticmethod
    def _apply_selection_overlay(pixmap: QPixmap, card_theme: PawchiveCardTheme) -> QPixmap:
        image = pixmap.toImage().convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
        painter = QtGui.QPainter(image)
        gradient = QtGui.QLinearGradient(0.0, 0.0, float(image.width()), float(image.height()))
        gradient.setColorAt(0.0, qcolor_from_css(card_theme.preview_selected_overlay_start))
        gradient.setColorAt(1.0, qcolor_from_css(card_theme.preview_selected_overlay_end))
        painter.fillRect(image.rect(), gradient)
        painter.end()
        return QPixmap.fromImage(image)

    @staticmethod
    def _apply_video_badge(
        pixmap: QPixmap, card_theme: PawchiveCardTheme, file_ext: str = ""
    ) -> QPixmap:
        ext = str(file_ext or "").strip().lower().lstrip(".").upper() or "VIDEO"
        image = pixmap.toImage().convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
        painter = QtGui.QPainter(image)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        font = painter.font()
        font.setPointSize(8)
        font.setWeight(QtGui.QFont.Weight.Bold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(ext)
        text_height = metrics.height()
        badge_w = text_width + _VIDEO_BADGE_H_PADDING * 2 + _VIDEO_PLAY_TRIANGLE_SIZE + 4
        badge_h = text_height + _VIDEO_BADGE_V_PADDING * 2
        badge_x = image.width() - badge_w - _VIDEO_BADGE_MARGIN
        badge_y = image.height() - badge_h - _VIDEO_BADGE_MARGIN
        badge_rect = QtCore.QRectF(badge_x, badge_y, badge_w, badge_h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(qcolor_from_css(card_theme.video_badge_bg))
        painter.drawRoundedRect(badge_rect, _VIDEO_BADGE_RADIUS, _VIDEO_BADGE_RADIUS)
        text_color = qcolor_from_css(card_theme.video_badge_text)
        painter.setPen(text_color)
        tri_cx = badge_x + _VIDEO_BADGE_H_PADDING + _VIDEO_PLAY_TRIANGLE_SIZE / 2
        tri_cy = badge_y + badge_h / 2
        tri = QtGui.QPolygonF([
            QtCore.QPointF(
                tri_cx - _VIDEO_PLAY_TRIANGLE_SIZE / 2, tri_cy - _VIDEO_PLAY_TRIANGLE_SIZE / 2
            ),
            QtCore.QPointF(tri_cx + _VIDEO_PLAY_TRIANGLE_SIZE / 2, tri_cy),
            QtCore.QPointF(
                tri_cx - _VIDEO_PLAY_TRIANGLE_SIZE / 2, tri_cy + _VIDEO_PLAY_TRIANGLE_SIZE / 2
            ),
        ])
        painter.setBrush(text_color)
        painter.drawPolygon(tri)
        text_x = badge_x + _VIDEO_BADGE_H_PADDING + _VIDEO_PLAY_TRIANGLE_SIZE + 2
        text_y = badge_y + (badge_h - text_height) / 2
        painter.drawText(QtCore.QPointF(text_x, text_y + metrics.ascent()), ext)
        painter.end()
        return QPixmap.fromImage(image)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_overlay_widgets()


def default_card_factory(
    item, *, parent=None, metrics=DEFAULT_PAWCHIVE_CARD_METRICS, already_downloaded=False
) -> PawchiveCardWidget:
    """网格默认工厂：**全关键字传参**，禁位置（01 A-3 的位置参数陷阱）。"""
    return PawchiveCardWidget(
        item,
        metrics=metrics,
        already_downloaded=already_downloaded,
        parent=parent,
    )


__all__ = [
    "DownloadRingOverlay",
    "PawchiveCardPreviewButton",
    "PawchiveCardSelectorButton",
    "PawchiveCardWidget",
    "clear_gray_cache",
    "default_card_factory",
]
