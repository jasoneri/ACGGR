from PySide6 import QtCore, QtGui
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QPushButton, QSizePolicy, QStyle, QStyleOptionButton, QStylePainter, QToolButton, QVBoxLayout, QWidget

from core.script.image.danbooru.models import DanbooruPost

from .style import (
    DanbooruCardMetrics,
    DanbooruCardTheme,
    DanbooruUiPalette,
    DEFAULT_CARD_METRICS,
    build_card_stylesheet,
    get_danbooru_card_theme,
    get_danbooru_qss_tokens,
    qcolor_from_css,
)

_CARD_SELECTOR_SIZE = 26
_CARD_SELECTOR_IDLE_INNER_RATIO = 0.7
_CARD_SELECTOR_HOVER_INNER_RATIO = 0.55
_CARD_CONTENT_MARGIN = 4
_CARD_PREVIEW_BORDER_INSET = 1
_CARD_PREVIEW_RADIUS = 16.0
_VIDEO_BADGE_H_PADDING = 6
_VIDEO_BADGE_V_PADDING = 3
_VIDEO_BADGE_RADIUS = 6.0
_VIDEO_BADGE_MARGIN = 8
_VIDEO_PLAY_TRIANGLE_SIZE = 5
#: 动图原文件扩展名（danbooru 支持集内的 gif/webp；apng 兜底）。
_ANIMATED_FILE_EXTS = frozenset({"gif", "webp", "apng"})
_PAGE_BAR_WIDTH = 4
_PAGE_BAR_MARGIN = 3


class _DanbooruCardSelectorButton(QToolButton):
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
        self.setObjectName("DanbooruCardSelector")
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setText("")
        self.setFixedSize(_CARD_SELECTOR_SIZE, _CARD_SELECTOR_SIZE)
        self.apply_theme()

    def apply_theme(self):
        tokens = get_danbooru_qss_tokens()
        self._gradient_start = qcolor_from_css(tokens["CARD_CHECKBOX_GRADIENT_START"])
        self._gradient_mid = qcolor_from_css(tokens["CARD_CHECKBOX_GRADIENT_MID"])
        self._gradient_end = qcolor_from_css(tokens["CARD_CHECKBOX_GRADIENT_END"])
        self._shadow_color = qcolor_from_css(tokens["CARD_CHECKBOX_SHADOW"])
        self._inner_color = qcolor_from_css(tokens["CARD_CHECKBOX_INNER"])
        self._idle_opacity = max(0.0, min(1.0, float(tokens["CARD_CHECKBOX_OPACITY"])))
        self._disabled_opacity = max(0.0, min(1.0, float(tokens["CARD_CHECKBOX_DISABLED_OPACITY"])))
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
        opacity = self._disabled_opacity if not self.isEnabled() else (1.0 if self._hovered or self.hasFocus() else self._idle_opacity)
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

        inner_ratio = _CARD_SELECTOR_HOVER_INNER_RATIO if self._hovered and self.isEnabled() else _CARD_SELECTOR_IDLE_INNER_RATIO
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


class _DanbooruCardPreviewButton(QPushButton):
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
        path.addRoundedRect(QtCore.QRectF(target_rect), _CARD_PREVIEW_RADIUS, _CARD_PREVIEW_RADIUS)
        painter.save()
        painter.setClipPath(path)
        if self._preview_pixmap.size() == target_rect.size():
            painter.drawPixmap(target_rect.topLeft(), self._preview_pixmap)
        else:
            scaled = self._preview_pixmap.scaled(target_rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            source_x = max(0, (scaled.width() - target_rect.width()) // 2)
            source_y = max(0, (scaled.height() - target_rect.height()) // 2)
            source_rect = QtCore.QRect(source_x, source_y, target_rect.width(), target_rect.height())
            painter.drawPixmap(target_rect, scaled, source_rect)
        painter.restore()


class _PageBarAnchor(QObject):
    """Tracks card widget position and repositions page bar via mapTo."""
    def __init__(self, card: "DanbooruCardWidget", bar: QFrame, container: QWidget):
        super().__init__(bar)
        self._card = card
        self._bar = bar
        self._container = container
        card.installEventFilter(self)
        parent = card.parentWidget()
        if parent:
            parent.installEventFilter(self)

    def eventFilter(self, obj, e):
        if obj is self._card and e.type() in (QEvent.Hide, QEvent.Show):
            self._reposition()
            return False
        if e.type() in (QEvent.Resize, QEvent.Move):
            self._reposition()
        return False

    def _reposition(self):
        # The page bar belongs to the *page's first card*, which may be filtered
        # out by the hide-unpreviewable switch; don't leave the marker floating
        # at a stale position when that happens.
        self._bar.setVisible(self._card.isVisible())
        if not self._card.isVisible():
            return
        top_left = self._card.mapTo(self._container, QtCore.QPoint(0, 0))
        bar_x = top_left.x() - _PAGE_BAR_MARGIN - _PAGE_BAR_WIDTH
        bar_y = top_left.y()
        self._bar.move(bar_x, bar_y)
        self._bar.setFixedHeight(self._card.height())

    def detach(self):
        self._card.removeEventFilter(self)
        parent = self._card.parentWidget()
        if parent:
            parent.removeEventFilter(self)


class DanbooruCardWidget(QFrame):
    open_detail_requested = Signal(object)
    selection_changed = Signal(object, bool)

    def __init__(
        self,
        post: DanbooruPost,
        already_downloaded: bool = False,
        parent=None,
        metrics: DanbooruCardMetrics = DEFAULT_CARD_METRICS,
    ):
        super().__init__(parent)
        self.post = post
        self.already_downloaded = already_downloaded
        self.metrics = metrics
        self._card_theme = get_danbooru_card_theme(self.already_downloaded)
        self._preview_pixmap = QPixmap()
        self._preview_size = self._derive_preview_size()
        self.preview_height = self._preview_size.height()
        self._page_bar: QFrame | None = None
        self._page_bar_anchor: _PageBarAnchor | None = None
        self.setObjectName(f"DanbooruCard_{post.post_id}")
        self._setup_ui()
        self.apply_theme()

    def _setup_ui(self):
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setProperty("downloaded", self.already_downloaded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_CARD_CONTENT_MARGIN, _CARD_CONTENT_MARGIN, _CARD_CONTENT_MARGIN, _CARD_CONTENT_MARGIN)
        layout.setSpacing(0)

        self.preview_frame = QFrame(self)
        self.preview_frame.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        preview_layout = QVBoxLayout(self.preview_frame)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)

        self.preview_button = _DanbooruCardPreviewButton(self.preview_frame)
        self.preview_button.setObjectName("DanbooruCardPreview")
        self.preview_button.setProperty("unsupported", not self.post.is_supported)
        self.preview_button.setCursor(Qt.PointingHandCursor)
        self.preview_button.clicked.connect(lambda: self.open_detail_requested.emit(self.post))
        self.preview_button.setText(self._initial_preview_text())
        preview_layout.addWidget(self.preview_button)

        self._preview_glow = QGraphicsDropShadowEffect(self.preview_frame)
        self._preview_glow.setOffset(0.0, 0.0)
        self._preview_glow.setBlurRadius(0.0)
        self._preview_glow.setEnabled(False)
        self.preview_frame.setGraphicsEffect(self._preview_glow)

        self.checkbox = _DanbooruCardSelectorButton(self.preview_frame)
        self.checkbox.raise_()
        self.checkbox.toggled.connect(self._on_checkbox_toggled)
        layout.addWidget(self.preview_frame, 0, Qt.AlignLeft | Qt.AlignTop)

        self._sync_geometry()
        self._apply_checkbox_availability()
        self._position_overlay_widgets()
        self._sync_selection_state(self.checkbox.isChecked())

    def apply_theme(self):
        self._card_theme = get_danbooru_card_theme(self.already_downloaded)
        self.setStyleSheet(build_card_stylesheet(DanbooruUiPalette.current(), self.already_downloaded))
        self.checkbox.apply_theme()
        self._sync_preview_glow()
        if not self._preview_pixmap.isNull():
            self._apply_preview_icon()

    def _source_preview_size(self) -> QtCore.QSize:
        if not self._preview_pixmap.isNull():
            return self._preview_pixmap.size()
        preview_width = self.post.preview_width or self.post.image_width or 0
        preview_height = self.post.preview_height or self.post.image_height or 0
        if preview_width > 0 and preview_height > 0:
            return QtCore.QSize(preview_width, preview_height)
        return QtCore.QSize(max(1, self.metrics.preview_content_width), max(1, self.metrics.preview_base_height))

    def _derive_preview_size(self) -> QtCore.QSize:
        source_size = self._source_preview_size()
        bounds = QtCore.QSize(
            max(1, self.metrics.preview_content_width),
            max(1, self.metrics.preview_max_height),
        )
        if source_size.width() <= 0 or source_size.height() <= 0:
            return QtCore.QSize(bounds.width(), max(1, self.metrics.preview_base_height))
        fitted = source_size.scaled(bounds, Qt.KeepAspectRatio)
        return QtCore.QSize(max(1, fitted.width()), max(1, fitted.height()))

    @property
    def preview_width(self) -> int:
        return max(1, self._preview_size.width())

    def preview_fetch_width(self) -> int:
        return self.preview_width

    def _position_overlay_widgets(self):
        tokens = get_danbooru_qss_tokens()
        max_x = max(0, self.preview_width - self.checkbox.width())
        max_y = max(0, self.preview_height - self.checkbox.height())
        self.checkbox.move(
            max(0, min(max_x, int(round(float(tokens["CARD_CHECKBOX_OFFSET_X"]))))),
            max(0, min(max_y, int(round(float(tokens["CARD_CHECKBOX_OFFSET_Y"]))))),
        )

    def _apply_checkbox_availability(self):
        self.checkbox.setVisible(not self.already_downloaded)
        self.checkbox.setDisabled((not self.post.is_downloadable) or self.already_downloaded)

    def _is_selected(self) -> bool:
        return bool(self.property("selected"))

    def _sync_preview_glow(self):
        self._preview_glow.setColor(qcolor_from_css(self._card_theme.glow_color))
        self._preview_glow.setBlurRadius(26.0)
        self._preview_glow.setEnabled(self._is_selected())

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

    def set_already_downloaded(self, downloaded: bool):
        self.already_downloaded = downloaded
        if downloaded:
            self.checkbox.blockSignals(True)
            self.checkbox.setChecked(False)
            self.checkbox.blockSignals(False)
        self._apply_checkbox_availability()
        self.apply_theme()
        if not self._preview_pixmap.isNull():
            self._apply_preview_icon()
        self._sync_selection_state(self.checkbox.isChecked())

    def set_preview_pixmap(self, pixmap: QPixmap):
        self._preview_pixmap = QPixmap(pixmap)
        self._sync_geometry()
        self._apply_preview_icon()

    def _preview_target_size(self) -> QtCore.QSize:
        inset = _CARD_PREVIEW_BORDER_INSET * 2
        return QtCore.QSize(max(1, self.preview_width - inset), max(1, self.preview_height - inset))

    def _default_preview_text(self) -> str:
        if self.post.is_archive:
            if self.post.preview_asset_is_video:
                return "ZIP original, video preview available"
            return "ZIP original, preview available" if self.post.has_renderable_preview_asset else "ZIP archive, download original"
        if not self.post.is_supported:
            ext = DanbooruPost.normalize_file_ext(self.post.file_ext).upper() or "UNKNOWN"
            if self.post.has_renderable_preview_asset:
                return f"{ext} original, preview available"
            return f"Non-renderable preview: {ext}"
        return "Loading..." if self.post.preview_file_url else "No Preview"

    def _initial_preview_text(self) -> str:
        if self.post.has_renderable_preview_asset:
            return "Loading..."
        return self._default_preview_text()

    def _badge_text(self) -> tuple[str, bool]:
        """瀑布流卡的格式徽章文案（空串=不出徽章）+ 是否动图原文件。

        对齐 mp4 播放徽章，补齐"点开到底是啥"的可辨识度：zip/gif/webp 原文件
        现在都有标识；普通可渲染静态 jpg/png **不标**（它就是它，不误导成 zip）。
        """
        post = self.post
        if post.uses_viewer_video:
            return (post.viewer_video_media_ext.upper() or "VIDEO", False)
        if post.is_archive:
            return ("ZIP", False)
        ext = DanbooruPost.normalize_file_ext(post.file_ext)
        if post.is_supported and ext in _ANIMATED_FILE_EXTS:
            return (ext.upper(), True)
        if not post.is_supported and post.has_renderable_preview_asset:
            return (ext.upper() or "FILE", False)
        return ("", False)

    def _build_preview_icon(self) -> QPixmap:
        target_size = self._preview_target_size()
        if self._preview_pixmap.isNull():
            return QPixmap()
        scaled = self._preview_pixmap.scaled(target_size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        source_x = max(0, (scaled.width() - target_size.width()) // 2)
        source_y = max(0, (scaled.height() - target_size.height()) // 2)
        preview = scaled.copy(source_x, source_y, target_size.width(), target_size.height())
        if self.already_downloaded:
            preview = self._apply_downloaded_preview_effect(preview, self._card_theme)
        if self._is_selected():
            preview = self._apply_selection_overlay(preview, self._card_theme)
        badge_text, badge_animated = self._badge_text()
        if badge_text:
            preview = self._apply_type_badge(
                preview, self._card_theme, badge_text,
                animated=badge_animated,
                play_triangle=self.post.uses_viewer_video,
            )
        return preview

    def _apply_preview_icon(self):
        preview = self._build_preview_icon()
        self.preview_button.set_preview_pixmap(preview)
        self.preview_button.setText("" if not preview.isNull() else self._default_preview_text())

    def refresh_preview_icon(self):
        if not self._preview_pixmap.isNull():
            self._apply_preview_icon()

    @staticmethod
    def _apply_partial_grayscale(pixmap: QPixmap, amount: float) -> QPixmap:
        """部分灰度：``out = orig*(1-a) + luma*a``，alpha 原样保留。

        实现口径 = V-B（Rec.601 保真）：pillow ``convert("L")`` 取灰度、``Image.blend`` 插值；
        QImage↔PIL 直接字节桥（``Format_ARGB32`` 小端即 PIL ``BGRA``，``stride`` 传
        ``bytesPerLine()``，不假设 ``4*width``）。缓存键须带 ``w/h`` 与 ``amount``，用
        ``pixmap.cacheKey()`` 区分内容。pillow 不可用时退回 Qt ``Format_Grayscale8``。
        """
        clamped = max(0.0, min(1.0, amount))
        if clamped <= 0 or pixmap.isNull():
            return QPixmap(pixmap)

        src = pixmap.toImage().convertToFormat(QtGui.QImage.Format_ARGB32)
        width, height = src.width(), src.height()
        if width <= 0 or height <= 0:
            return QPixmap(pixmap)

        cache = DanbooruCardWidget._apply_partial_grayscale.__dict__.setdefault("_cache", {})
        cache_key = (pixmap.cacheKey(), width, height, round(clamped, 2))
        cached = cache.get(cache_key)
        if cached is not None:
            return QPixmap(cached)

        out_image = None
        try:
            from PIL import Image, ImageOps

            stride = src.bytesPerLine()
            pil = Image.frombytes(
                "RGBA", (width, height), src.constBits().tobytes(), "raw", "BGRA", stride, 1
            )
            luma = ImageOps.grayscale(pil).convert("RGBA")
            if clamped >= 1.0:
                gray = luma
            else:
                gray = Image.blend(pil, luma, clamped)
            # blend 会连 alpha 一起插值；旧实现的 alpha 是原样保留的，这里补回。
            gray.putalpha(pil.getchannel("A"))
            out_image = QtGui.QImage(
                gray.tobytes("raw", "BGRA"), width, height, 4 * width,
                QtGui.QImage.Format_ARGB32,
            ).copy()
        except (ImportError, ValueError, OSError):
            out_image = None

        if out_image is None:
            # V-A 兜底：Grayscale8 丢 alpha，用原图 alpha 遮罩补回。
            gray = src.convertToFormat(QtGui.QImage.Format_Grayscale8).convertToFormat(
                QtGui.QImage.Format_ARGB32
            )
            merged = QtGui.QImage(src.size(), QtGui.QImage.Format_ARGB32_Premultiplied)
            merged.fill(0)
            painter = QtGui.QPainter(merged)
            painter.drawImage(0, 0, gray)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_DestinationIn)
            painter.drawImage(0, 0, src)
            painter.end()
            if clamped >= 1.0:
                out_image = merged
            else:
                blended = QtGui.QImage(src.size(), QtGui.QImage.Format_ARGB32_Premultiplied)
                blended.fill(0)
                painter = QtGui.QPainter(blended)
                painter.drawImage(0, 0, src)
                painter.setOpacity(clamped)
                painter.drawImage(0, 0, merged)
                painter.end()
                out_image = blended

        result = QPixmap.fromImage(out_image)
        # FIFO 上限 256 项（208² × 4B ≈ 173KB/项 → ≈ 44MB），键位含 w/h 与 amount。
        cache[cache_key] = result
        while len(cache) > 256:
            cache.pop(next(iter(cache)))
        return QPixmap(result)

    @staticmethod
    def _apply_downloaded_preview_effect(pixmap: QPixmap, card_theme: DanbooruCardTheme) -> QPixmap:
        def _apply_opacity(pixmap: QPixmap) -> QPixmap:
            clamped = max(0.0, min(1.0, card_theme.preview_downloaded_opacity))
            if clamped >= 1.0 or pixmap.isNull():
                return QPixmap(pixmap)
            faded = QPixmap(pixmap.size())
            faded.fill(Qt.transparent)
            painter = QtGui.QPainter(faded)
            painter.setOpacity(clamped)
            painter.drawPixmap(0, 0, pixmap)
            painter.end()
            return faded
        return _apply_opacity(DanbooruCardWidget._apply_partial_grayscale(pixmap, card_theme.preview_downloaded_grayscale))

    @staticmethod
    def _apply_selection_overlay(pixmap: QPixmap, card_theme: DanbooruCardTheme) -> QPixmap:
        image = pixmap.toImage().convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
        painter = QtGui.QPainter(image)
        gradient = QtGui.QLinearGradient(0.0, 0.0, float(image.width()), float(image.height()))
        gradient.setColorAt(0.0, qcolor_from_css(card_theme.preview_selected_overlay_start))
        gradient.setColorAt(1.0, qcolor_from_css(card_theme.preview_selected_overlay_end))
        painter.fillRect(image.rect(), gradient)
        painter.end()
        return QPixmap.fromImage(image)

    @staticmethod
    def _apply_type_badge(
        pixmap: QPixmap,
        card_theme: DanbooruCardTheme,
        text: str,
        *,
        animated: bool = False,
        play_triangle: bool = False,
    ) -> QPixmap:
        """右下角格式徽章：视频带播放三角，zip/gif/webp 等只出扩展名标签。

        字用扩展名（``GIF``/``ZIP``/``MP4``），**不走 i18n**；样式复用视频徽章的
        ``CARD_VIDEO_BADGE_*`` token 与 ``_VIDEO_BADGE_*`` 几何常量，不新增 token。
        """
        label = str(text or "").strip().upper()
        if not label:
            return QPixmap(pixmap)
        image = pixmap.toImage().convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
        painter = QtGui.QPainter(image)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        font = painter.font()
        font.setPointSize(8)
        font.setWeight(QtGui.QFont.Weight.Bold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(label)
        text_height = metrics.height()
        leading = (_VIDEO_PLAY_TRIANGLE_SIZE + 4) if play_triangle else 0
        badge_w = text_width + _VIDEO_BADGE_H_PADDING * 2 + leading
        badge_h = text_height + _VIDEO_BADGE_V_PADDING * 2
        badge_x = image.width() - badge_w - _VIDEO_BADGE_MARGIN
        badge_y = image.height() - badge_h - _VIDEO_BADGE_MARGIN
        badge_rect = QtCore.QRectF(badge_x, badge_y, badge_w, badge_h)
        bg_color = qcolor_from_css(card_theme.video_badge_bg)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(badge_rect, _VIDEO_BADGE_RADIUS, _VIDEO_BADGE_RADIUS)
        text_color = qcolor_from_css(card_theme.video_badge_text)
        text_x = badge_x + _VIDEO_BADGE_H_PADDING
        if play_triangle:
            painter.setPen(text_color)
            tri_cx = badge_x + _VIDEO_BADGE_H_PADDING + _VIDEO_PLAY_TRIANGLE_SIZE / 2
            tri_cy = badge_y + badge_h / 2
            tri = QtGui.QPolygonF([
                QtCore.QPointF(tri_cx - _VIDEO_PLAY_TRIANGLE_SIZE / 2, tri_cy - _VIDEO_PLAY_TRIANGLE_SIZE / 2),
                QtCore.QPointF(tri_cx + _VIDEO_PLAY_TRIANGLE_SIZE / 2, tri_cy),
                QtCore.QPointF(tri_cx - _VIDEO_PLAY_TRIANGLE_SIZE / 2, tri_cy + _VIDEO_PLAY_TRIANGLE_SIZE / 2),
            ])
            painter.setBrush(text_color)
            painter.drawPolygon(tri)
            text_x = badge_x + _VIDEO_BADGE_H_PADDING + _VIDEO_PLAY_TRIANGLE_SIZE + 2
        painter.setPen(text_color)
        text_y = badge_y + (badge_h - text_height) / 2
        painter.drawText(QtCore.QPointF(text_x, text_y + metrics.ascent()), label)
        if animated:
            # 动图原文件：右上角一个小圆点，提示"这张会动"（不占额外 token）。
            dot_r = 2.5
            painter.setBrush(text_color)
            painter.drawEllipse(QtCore.QPointF(badge_x + badge_w - dot_r - 2, badge_y + dot_r + 2), dot_r, dot_r)
        painter.end()
        return QPixmap.fromImage(image)

    @staticmethod
    def _apply_video_badge(pixmap: QPixmap, card_theme: DanbooruCardTheme, file_ext: str = "") -> QPixmap:
        ext = DanbooruPost.normalize_file_ext(file_ext).upper() or "VIDEO"
        return DanbooruCardWidget._apply_type_badge(
            pixmap, card_theme, ext, play_triangle=True,
        )

    def _sync_geometry(self):
        self._preview_size = self._derive_preview_size()
        self.preview_height = self._preview_size.height()
        self.preview_frame.setFixedSize(self._preview_size)
        self.preview_button.setFixedSize(self._preview_size)
        self.setFixedSize(self.sizeHint())

    def apply_metrics(self, metrics: DanbooruCardMetrics, *, refresh_preview: bool = True):
        self.metrics = metrics
        self._sync_geometry()
        if refresh_preview and not self._preview_pixmap.isNull():
            self._apply_preview_icon()
        self._position_overlay_widgets()
        self.adjustSize()
        self.updateGeometry()

    def sizeHint(self) -> QtCore.QSize:
        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else QtCore.QMargins()
        return QtCore.QSize(
            margins.left() + self.preview_width + margins.right(),
            margins.top() + self.preview_height + margins.bottom(),
        )

    def minimumSizeHint(self) -> QtCore.QSize:
        return self.sizeHint()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_overlay_widgets()
        if self._page_bar_anchor:
            self._page_bar_anchor._reposition()

    def set_pagination_bar(self, is_first: bool):
        if is_first:
            if self._page_bar is not None:
                return
            container = self.parentWidget()
            if container is None:
                return
            palette = DanbooruUiPalette.current()
            bar = QFrame(container)
            bar.setFixedWidth(_PAGE_BAR_WIDTH)
            bar.setStyleSheet(
                f"QFrame {{ background: {palette.page_border_color}; border: none; border-radius: 2px; }}"
            )
            bar.show()
            self._page_bar = bar
            self._page_bar_anchor = _PageBarAnchor(self, bar, container)
            self._page_bar_anchor._reposition()
        else:
            if self._page_bar_anchor:
                self._page_bar_anchor.detach()
                self._page_bar_anchor = None
            if self._page_bar:
                self._page_bar.deleteLater()
                self._page_bar = None
