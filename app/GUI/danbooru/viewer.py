import sys
import typing as t
from dataclasses import dataclass

from PySide6 import QtCore
from PySide6.QtCore import Qt, qtTrId, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap, QMovie
from PySide6.QtWidgets import QApplication, QFrame, QGraphicsView, QHBoxLayout, QLabel, QVBoxLayout, QWidget, QSizePolicy
from qfluentwidgets import (
    BodyLabel, FluentIcon as FIF, IndeterminateProgressRing, PrimaryToolButton, PushButton, ScrollArea,
    Slider, ToolButton, TransparentToggleToolButton, TransparentToolButton,
)
from qfluentwidgets.multimedia import VideoWidget, SimpleMediaPlayBar
from qframelesswindow.utils import startSystemMove

from app.GUI.viewer import (
    ImageLoadingIndicator,
    ViewerChromeMixin,
    build_viewer_chrome_stylesheet,
    style_viewer_top_bar,
)

from core.config.qc import danbooru_cfg
from ui.core.timer import safe_single_shot
from ui.qfluent.components import FlexImageLabel
from ui.qfluent.components.icons import AcggrIcon
from core.script.image.danbooru.models import DanbooruPost
from core.script.image.danbooru.tag_prompt import TagPrompt

from .core import DanbooruViewerFitCalculator, DanbooruViewerFitResult, delete_flow_item as _delete_flow_item
from .style import DanbooruUiPalette, build_viewer_stylesheet, get_danbooru_qss_tokens, qcolor_from_css


@dataclass(frozen=True, slots=True)
class _PlaybackPolicy:
    autoplay: bool = True
    muted: bool = True
    loop: bool = False

    @classmethod
    def default(cls) -> "_PlaybackPolicy":
        return cls(autoplay=True, muted=True, loop=False)

class BufferedProgressSlider(Slider):
    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self._buffered_ratio = 0.0
        self.apply_theme()
        self.setObjectName("BufferedProgressSlider")

    def bufferedRatio(self) -> float:
        return self._buffered_ratio

    def setBufferedRatio(self, ratio: float):
        normalized = max(0.0, min(1.0, float(ratio)))
        if abs(normalized - self._buffered_ratio) < 0.001:
            return
        self._buffered_ratio = normalized
        self.update()

    def setBufferedRange(self, loaded: int, total: int):
        self.setBufferedRatio(0.0 if total <= 0 else loaded / total)

    def apply_theme(self):
        tokens = get_danbooru_qss_tokens()
        self._track_color = qcolor_from_css(tokens["VIEWER_PROGRESS_TRACK"])
        self._buffer_color = qcolor_from_css(tokens["VIEWER_PROGRESS_BUFFER"])
        self._played_color = qcolor_from_css(tokens["VIEWER_PROGRESS_PLAYED"])
        self.update()

    def _drawHorizonGroove(self, painter: QPainter):
        width, radius = self.width(), self.handle.width() / 2
        groove_width = max(0.0, width - radius * 2)
        groove = QtCore.QRectF(radius, radius - 2, groove_width, 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._track_color)
        painter.drawRoundedRect(groove, 2, 2)
        if groove_width <= 0:
            return
        buffered_width = groove_width * self._buffered_ratio
        if buffered_width > 0:
            painter.setBrush(self._buffer_color)
            painter.drawRoundedRect(QtCore.QRectF(radius, radius - 2, buffered_width, 4), 2, 2)
        total = self.maximum() - self.minimum()
        if total <= 0:
            return
        played_width = (self.value() - self.minimum()) / total * groove_width
        if played_width > 0:
            painter.setBrush(self._played_color)
            painter.drawRoundedRect(QtCore.QRectF(radius, radius - 2, played_width, 4), 2, 2)


class _BufferedMediaPlayBar(SimpleMediaPlayBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        legacy_slider = self.progressSlider
        self.hBoxLayout.removeWidget(legacy_slider)
        legacy_slider.hide()
        self.progressSlider = BufferedProgressSlider(Qt.Horizontal, self)
        self.hBoxLayout.insertWidget(1, self.progressSlider, 1)
        self.player.durationChanged.connect(self.progressSlider.setMaximum)
        self.progressSlider.sliderMoved.connect(self.player.setPosition)
        self.progressSlider.clicked.connect(self.player.setPosition)
        self.progressSlider.setMaximum(max(0, self.player.duration()))
        self.progressSlider.setValue(max(0, self.player.position()))
        legacy_slider.deleteLater()

    def setBufferedRatio(self, ratio: float):
        self.progressSlider.setBufferedRatio(ratio)

    def setBufferedRange(self, loaded: int, total: int):
        self.progressSlider.setBufferedRange(loaded, total)

    def apply_theme(self):
        self.progressSlider.apply_theme()


class _BufferingIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DanbooruVideoBufferingIndicator")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.ring = IndeterminateProgressRing(self, start=False)
        self.ring.setFixedSize(42, 42)
        self.ring.setStrokeWidth(4)
        self.label = BodyLabel(qtTrId("danbooru.viewer.loading"), self)
        self.label.setObjectName("DanbooruVideoBufferingLabel")
        self.label.setAlignment(Qt.AlignCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.ring, 0, Qt.AlignCenter)
        layout.addWidget(self.label, 0, Qt.AlignCenter)
        self.hide()

    def setActive(self, active: bool, text: str = "Loading..."):
        self.label.setText(text)
        if active:
            self.show()
            self.raise_()
            self.ring.start()
            return
        self.ring.stop()
        self.hide()


class DanbooruImageViewer(ViewerChromeMixin, QWidget):
    tag_clicked = Signal(str)
    export_panel_requested = Signal()
    export_attach_requested = Signal()
    download_requested = Signal(object)
    previous_requested = Signal()
    next_requested = Signal()
    middle_click_closed = Signal()
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(None)
        self._anchor_widget = parent
        self.post: t.Optional[DanbooruPost] = None
        self._already_downloaded = False
        self._drag_offset: t.Optional[QtCore.QPoint] = None
        self._default_image_size = QtCore.QSize(520, 340)
        # 置顶的初始态来自设置页（默认 true）。会话内 pin 仍可自由切换，
        # 设置项只在每次新建 viewer（即软件启动后的首次打开）时读一次。
        self._keep_on_top = bool(danbooru_cfg.isDanbooruViewerDafaultPin.value)
        self._display_source_size: t.Optional[QtCore.QSize] = None
        self._loaded_pixmap_size: t.Optional[QtCore.QSize] = None
        self._last_fit_result: t.Optional[DanbooruViewerFitResult] = None
        self._applying_layout = False
        self._pending_loaded_settle_post_id: t.Optional[int] = None
        self._loaded_settle_revision = 0
        self._loaded_settle_scheduled = False
        self._current_media_kind = "image"
        self._page_loading = False
        self._status_hint_text = ""
        self._suppress_closed = False
        self._native_window_warmed = False
        self._video_backend_ready = False
        #: 动图面（gif/webp/apng）：QMovie 生命周期归共享 ViewerChromeMixin。
        self._init_animation_state()
        self.video_mgr = self._InnerVideoMgr(self)
        self._setup_ui()
        self.apply_theme()

    def _setup_ui(self):
        self.setObjectName("DanbooruImageViewer")
        self.setWindowFlags(self._window_flags(self._keep_on_top))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)

        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(12, 12, 12, 12)

        self.frame = QFrame(self)
        self.frame.setObjectName("DanbooruImageViewerFrame")
        self.outer_layout.addWidget(self.frame)

        self.main_layout = QHBoxLayout(self.frame)
        self.main_layout.setContentsMargins(14, 14, 14, 14)
        self.main_layout.setSpacing(14)

        self.left_column = QWidget(self.frame)
        self.left_column.setObjectName("DanbooruViewerLeftColumn")
        self.left_column.setFixedWidth(196)
        left_column_layout = QVBoxLayout(self.left_column)
        left_column_layout.setContentsMargins(0, 0, 0, 0)
        left_column_layout.setSpacing(6)

        self.tag_toolbar = QWidget(self.left_column)
        self.tag_toolbar.setObjectName("DanbooruTagToolbar")
        tag_toolbar_layout = QHBoxLayout(self.tag_toolbar)
        tag_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        tag_toolbar_layout.setSpacing(4)
        self.export_panel_btn = ToolButton(AcggrIcon.SCRIPT_TAG_EDIT, self.tag_toolbar)
        self.export_attach_btn = PrimaryToolButton(AcggrIcon.SCRIPT_TAG_ADD, self.tag_toolbar)
        self.export_panel_btn.setFixedHeight(34)
        self.export_panel_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.export_panel_btn.setToolTip(qtTrId("danbooru.viewer.open_export"))
        self.export_panel_btn.clicked.connect(self.export_panel_requested.emit)
        self.export_attach_btn.setFixedHeight(34)
        self.export_attach_btn.setToolTip(qtTrId("danbooru.viewer.attach_current"))
        self.export_attach_btn.clicked.connect(self.export_attach_requested.emit)
        self.export_attach_btn.hide()
        tag_toolbar_layout.addWidget(self.export_panel_btn)
        tag_toolbar_layout.addWidget(self.export_attach_btn)
        left_column_layout.addWidget(self.tag_toolbar, 0)

        self.tags_scroll = ScrollArea(self.left_column)
        self.tags_scroll.setWidgetResizable(True)
        self.tags_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tags_scroll.setStyleSheet("background: transparent; border: none;")
        self.tags_container = QWidget(self.tags_scroll)
        self.tags_container.setObjectName("DanbooruTagsContainer")
        self.tags_layout = QVBoxLayout(self.tags_container)
        self.tags_layout.setContentsMargins(0, 0, 0, 0)
        self.tags_layout.setSpacing(8)
        self.tags_scroll.setWidget(self.tags_container)
        left_column_layout.addWidget(self.tags_scroll, 1)
        self.main_layout.addWidget(self.left_column)

        self.right_panel_widget = QWidget(self.frame)
        self.right_panel_widget.setObjectName("DanbooruViewerRightPanel")
        self.right_panel_layout = QVBoxLayout(self.right_panel_widget)
        self.right_panel_layout.setContentsMargins(0, 0, 0, 0)
        self.right_panel_layout.setSpacing(12)

        self.top_bar = QWidget(self.frame)
        # 共享顶栏：objectName=ViewerChromeTopBar 由 chrome 统一设置，站点不再自命名
        # （R3「同对象名」）。该函数刻意不设 WA_StyledBackground；透明底 + 无边框的
        # 视觉规则只在 viewer.qss，两侧属性集合因此完全一致。
        style_viewer_top_bar(self.top_bar)
        self.top_bar.installEventFilter(self)
        top_bar = QHBoxLayout(self.top_bar)
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(6)
        self.previous_btn = TransparentToolButton(FIF.LEFT_ARROW, self.frame)
        self.previous_btn.setFixedSize(34, 34)
        self.previous_btn.clicked.connect(self.previous_requested.emit)
        self.next_btn = TransparentToolButton(FIF.RIGHT_ARROW, self.frame)
        self.next_btn.setFixedSize(34, 34)
        self.next_btn.clicked.connect(self.next_requested.emit)
        self.download_btn = PrimaryToolButton(FIF.DOWNLOAD, self.frame)
        self.download_btn.setFixedHeight(34)
        self.download_btn.clicked.connect(self._emit_download)
        self.close_btn = TransparentToolButton(FIF.CLOSE, self.frame)
        self.close_btn.setFixedSize(34, 34)
        self.close_btn.clicked.connect(self.hide)
        self.topHintBox = TransparentToggleToolButton(self.top_bar)
        self.topHintBox.setIcon(FIF.PIN)
        self.topHintBox.setChecked(self._keep_on_top)
        self.topHintBox.clicked.connect(self.keep_top_hint)
        # Play bar / VideoWidget pull QtMultimedia+FFmpeg; create only on first video use.
        self.playBar = None
        self._play_bar_host = QWidget(self.top_bar)
        self._play_bar_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._play_bar_host.hide()
        self.topBarSpacer = QWidget(self.top_bar)
        self.topBarSpacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        top_bar.addWidget(self.topHintBox)
        top_bar.addWidget(self._play_bar_host, 1)
        top_bar.addWidget(self.topBarSpacer, 1)
        top_bar.addWidget(self.previous_btn)
        top_bar.addWidget(self.next_btn)
        top_bar.addWidget(self.download_btn)
        top_bar.addWidget(self.close_btn)
        self._update_top_bar_height()
        self.right_panel_layout.addWidget(self.top_bar, 0, Qt.AlignTop)

        self.image_label = FlexImageLabel(self.frame)
        self.image_label.setObjectName("DanbooruImageLabel")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setWordWrap(True)
        self.image_label.setBorderRadius(16, 16, 16, 16)
        self.image_label.setFixedSize(self._default_image_size)
        self.image_label.installEventFilter(self)
        self.image_hint_label = QLabel("", self.image_label)
        self.image_hint_label.setObjectName("DanbooruImageHint")
        self.image_hint_label.setAlignment(Qt.AlignCenter)
        self.image_hint_label.setWordWrap(True)
        self.image_hint_label.installEventFilter(self)
        # R6：加载态用纯进度条、状态/错误用 InfoBar，图像区不再有文字 label。
        self.image_hint_label.hide()
        self.image_loading_indicator = ImageLoadingIndicator(self.image_label)
        self._sync_image_hint_geometry()
        self.image_label.mousePressEvent = self._image_mouse_press_event
        self.image_label.mouseMoveEvent = self._image_mouse_move_event
        self.image_label.mouseReleaseEvent = self._image_mouse_release_event
        self.right_panel_layout.addWidget(self.image_label, 0, Qt.AlignHCenter | Qt.AlignTop)
        self.right_panel_layout.addStretch(1)

        self.right_panel_widget.setFixedWidth(self._default_image_size.width())
        self.main_layout.addWidget(self.right_panel_widget, 0, Qt.AlignTop)
        self.setFocusPolicy(Qt.StrongFocus)
        for w in (self.previous_btn, self.next_btn, self.download_btn, self.tags_scroll,
                  self.close_btn, self.topHintBox, self.image_label, self.image_hint_label):
            w.setFocusPolicy(Qt.NoFocus)
        self._update_download_button()
        self._clear_tags()
        self.set_navigation_enabled(False, False)
        # 中键关窗也走 eventFilter：半透明窗口的边缘（12px 外扩 + 圆角）没有任何
        # 子控件接收事件，若不在这里拦下，mousedown/up 会落到下面的标签页网格上。
        # 必须等所有子控件建好后再装 self filter，否则构造期的事件会读到未定义属性。
        self.installEventFilter(self)

    def apply_theme(self):
        palette = DanbooruUiPalette.current()
        # 站点专属内容（tag chips）+ 共享 chrome（frame / 图片面 / 视频面 / 顶栏）。
        # chrome 必须来自 app.GUI.viewer 的单一实现，禁止在本文件再写一套颜色。
        self.setStyleSheet(
            build_viewer_stylesheet(palette) + build_viewer_chrome_stylesheet()
        )
        if self.playBar is not None:
            self.playBar.apply_theme()

    def _ensure_video_backend(self):
        """Create QtMultimedia surface/playbar on first video use (avoids FFmpeg cold load at Script open)."""
        if self._video_backend_ready:
            return
        self.playBar = _BufferedMediaPlayBar(self._play_bar_host)
        self.playBar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        host_layout = QHBoxLayout(self._play_bar_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)
        host_layout.addWidget(self.playBar, 1)
        # Insert video panel before the trailing stretch in the right column.
        stretch_index = max(0, self.right_panel_layout.count() - 1)
        self.video_mgr.setup_widgets(self.frame, self._default_image_size)
        self.right_panel_layout.insertWidget(
            stretch_index, self.video_mgr.frame, 0, Qt.AlignHCenter | Qt.AlignTop,
        )
        self.video_mgr.setup_backend(self.playBar)
        for widget in (self.video_mgr.frame, self.video_mgr.surface, self.video_mgr.hint_label):
            if widget is not None:
                widget.setFocusPolicy(Qt.NoFocus)
        self.playBar.apply_theme()
        self._video_backend_ready = True

    class _InnerVideoMgr:
        class _VideoWidget(VideoWidget):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._using_external_play_bar = False

            def bind_external_play_bar(self, play_bar: SimpleMediaPlayBar):
                if self._using_external_play_bar and self.playBar is play_bar:
                    return
                legacy_play_bar = self.playBar
                legacy_play_bar.hide()
                self.playBar = play_bar
                self._using_external_play_bar = True
                self.player.setVideoOutput(self.videoItem)
                legacy_play_bar.deleteLater()

            def enterEvent(self, e):
                if not self._using_external_play_bar:
                    super().enterEvent(e)

            def leaveEvent(self, e):
                if not self._using_external_play_bar:
                    super().leaveEvent(e)

            def resizeEvent(self, e):
                if not self._using_external_play_bar:
                    super().resizeEvent(e)
                    return
                QGraphicsView.resizeEvent(self, e)
                self.videoItem.setSize(QtCore.QSizeF(self.size()))
                self.fitInView(self.videoItem, Qt.KeepAspectRatio)

        def __init__(self, viewer: "DanbooruImageViewer"):
            self.viewer = viewer
            self.policy = self._initial_policy()
            self.player: t.Any = None
            self.frame: t.Optional[QFrame] = None
            self.surface: t.Optional[DanbooruImageViewer._InnerVideoMgr._VideoWidget] = None
            self.hint_label: t.Optional[QLabel] = None
            self.buffering_indicator: t.Optional[_BufferingIndicator] = None
            self.source_url = ""
            self.total_bytes = 0
            self.cached_bytes = 0
            self.cached_ratio = 0.0
            self.cache_complete = False
            self.active_segment_index = 0
            self.loading_visible = False
            self.loading_reason = ""
            self.buffer_guard_ms = 280

        @staticmethod
        def _player_state() -> dict:
            return danbooru_cfg.get_player()

        def _initial_policy(self) -> _PlaybackPolicy:
            player_state = self._player_state()
            default_policy = _PlaybackPolicy.default()
            return _PlaybackPolicy(autoplay=default_policy.autoplay, muted=player_state["muted"], loop=default_policy.loop)

        def setup_widgets(self, parent: QFrame, default_size: QtCore.QSize):
            self.frame = QFrame(parent)
            self.frame.setObjectName("DanbooruVideoPanel")
            self.frame.setFixedSize(default_size)
            layout = QVBoxLayout(self.frame)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            self.surface = self._VideoWidget(self.frame)
            self.surface.setObjectName("DanbooruVideoSurface")
            layout.addWidget(self.surface)
            self.hint_label = QLabel(qtTrId("danbooru.viewer.no_preview"), self.frame)
            self.hint_label.setObjectName("DanbooruVideoHint")
            self.hint_label.setAlignment(Qt.AlignCenter)
            self.hint_label.setWordWrap(True)
            self.frame.installEventFilter(self.viewer)
            self.surface.installEventFilter(self.viewer)
            self.buffering_indicator = _BufferingIndicator(self.frame)
            self.sync_hint_geometry()
            self.frame.hide()

        def setup_backend(self, play_bar: _BufferedMediaPlayBar):
            self.surface.bind_external_play_bar(play_bar)
            self.player = self.surface.player
            self.apply_saved_player_state()
            self.player.mediaStatusChanged.connect(self._on_media_status_changed)
            self.player.positionChanged.connect(self._on_position_changed)
            self.player.durationChanged.connect(self._on_duration_changed)
            self.player.volumeChanged.connect(self._on_volume_changed)
            self.player.mutedChanged.connect(self._on_muted_changed)

        def apply_saved_player_state(self):
            player_state = self._player_state()
            self.policy = _PlaybackPolicy(autoplay=self.policy.autoplay, muted=player_state["muted"], loop=self.policy.loop)
            if self.player is None:
                return
            self.player.setVolume(player_state["volume"])
            self.player.setMuted(player_state["muted"])

        def set_media_visible(self, media_kind: str, play_bar: _BufferedMediaPlayBar, top_bar_spacer: QWidget):
            is_video_mode = media_kind == "video" and self.player is not None
            if self.frame is not None:
                self.frame.setVisible(media_kind == "video")
            if play_bar is not None:
                play_bar.setVisible(is_video_mode)
                play_bar_host = play_bar.parentWidget()
                if play_bar_host is not None:
                    play_bar_host.setVisible(is_video_mode)
            top_bar_spacer.setVisible(not is_video_mode)

        def sync_hint_geometry(self):
            if self.frame is None or self.hint_label is None or self.buffering_indicator is None:
                return
            self.hint_label.setGeometry(0, 0, self.frame.width(), self.frame.height())
            self.buffering_indicator.setGeometry(0, 0, self.frame.width(), self.frame.height())

        def set_display_size(self, display_size: QtCore.QSize):
            if self.frame is None:
                return
            self.frame.setFixedSize(display_size)
            self.sync_hint_geometry()

        def set_hint(self, text: str, *, visible: bool = True):
            if self.hint_label is None:
                return
            self.hint_label.setText(text)
            self.hint_label.setVisible(visible)
            self.sync_hint_geometry()

        def clear_payload(self, play_bar: t.Optional[_BufferedMediaPlayBar]):
            self.source_url = ""
            self.total_bytes = 0
            self.cached_bytes = 0
            self.cached_ratio = 0.0
            self.cache_complete = False
            self.active_segment_index = 0
            if play_bar is not None:
                play_bar.setBufferedRatio(0.0)
            self._set_buffering_indicator(False, "")
            if self.player is not None:
                self.player.stop()
                self.player.setSource(QtCore.QUrl())
            self.hint_label.hide()

        def toggle_playback(self, media_kind: str) -> bool:
            if media_kind != "video":
                return False
            if self.player is None:
                raise RuntimeError("Danbooru viewer video player was not initialized")
            if self.player.isPlaying():
                self.player.pause()
            else:
                self.player.play()
            return True

        def set_video(self, post_id: int, source_url: str):
            self.source_url = str(source_url or "")
            if self.player is None:
                raise RuntimeError("Danbooru viewer video player was not initialized")
            if not self.source_url:
                raise ValueError(f"Danbooru viewer video source url is required: post_id={post_id}")
            self.player.setSource(QtCore.QUrl(self.source_url))
            self.apply_saved_player_state()
            if self.policy.autoplay:
                self.player.play()
            self.refresh_buffering_state(self.viewer._current_media_kind)

        def set_cache_progress(self, *, cached_bytes: int, total_bytes: int, cached_ratio: float,
                               active_segment_index: int, complete: bool, play_bar: _BufferedMediaPlayBar):
            self.cached_bytes = max(0, int(cached_bytes))
            self.total_bytes = max(0, int(total_bytes))
            self.cached_ratio = max(0.0, min(1.0, float(cached_ratio)))
            self.active_segment_index = max(0, int(active_segment_index))
            self.cache_complete = bool(complete)
            play_bar.setBufferedRatio(self.cached_ratio)
            self.refresh_buffering_state(self.viewer._current_media_kind)

        def is_click_target(self, obj) -> bool:
            frame = self.frame
            surface = self.surface
            return (frame is not None and obj is frame) or (surface is not None and obj is surface)

        def _on_volume_changed(self, volume: int):
            danbooru_cfg.save_player(volume=volume)

        def _on_muted_changed(self, muted: bool):
            danbooru_cfg.save_player(muted=muted)
            self.policy = _PlaybackPolicy(autoplay=self.policy.autoplay, muted=muted, loop=self.policy.loop)

        def _on_media_status_changed(self, status):
            if self.player is None or not self.source_url:
                return
            player_type = type(self.player)
            end_of_media = getattr(player_type.MediaStatus, "EndOfMedia", None)
            loaded_states = {
                getattr(player_type.MediaStatus, "LoadedMedia", None),
                getattr(player_type.MediaStatus, "BufferedMedia", None),
            }
            if status == end_of_media and self.policy.loop:
                self.player.setPosition(0)
                self.player.play()
                return
            if status in loaded_states:
                self.hint_label.hide()
                if self.policy.autoplay:
                    self.player.play()
                self.refresh_buffering_state(self.viewer._current_media_kind)

        def _on_position_changed(self, _position_ms: int):
            self.refresh_buffering_state(self.viewer._current_media_kind)

        def _on_duration_changed(self, _duration_ms: int):
            self.refresh_buffering_state(self.viewer._current_media_kind)

        def _set_buffering_indicator(self, visible: bool, reason: str):
            self.loading_visible = bool(visible)
            self.loading_reason = str(reason or "")
            if self.buffering_indicator is None:
                return
            self.buffering_indicator.setActive(self.loading_visible, self.loading_reason or "Loading...")

        def refresh_buffering_state(self, media_kind: str):
            if media_kind != "video" or self.player is None:
                self._set_buffering_indicator(False, "")
                return
            duration_ms = max(0, int(self.player.duration()))
            position_ms = max(0, int(self.player.position()))
            if duration_ms <= 0 or self.total_bytes <= 0:
                self._set_buffering_indicator(False, "")
                return
            playback_ratio = max(0.0, min(1.0, position_ms / duration_ms))
            projected_ratio = min(1.0, playback_ratio + (self.buffer_guard_ms / max(1, duration_ms)))
            waiting = (not self.cache_complete) and projected_ratio >= self.cached_ratio
            self._set_buffering_indicator(waiting, "Buffering...")

    def wheel_media_widgets(self) -> list:
        """滚轮翻页只认图片/视频区；标签列滚到边界的冒泡不算翻页。"""
        widgets = []
        if getattr(self, "image_label", None) is not None:
            widgets.append(self.image_label)
        video_frame = self.video_mgr.frame
        if video_frame is not None:
            widgets.append(video_frame)
        return widgets

    def _handle_extra_event_filter(self, obj, event) -> bool:
        """视频画面左键 = 播放/暂停（站点特有分支；壳层行为归基底）。"""
        if self.video_mgr.is_click_target(obj) and event.type() == QtCore.QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton and self._current_media_kind == "video":
                self.video_mgr.toggle_playback(self._current_media_kind)
                return True
        return False

    def _emit_download(self):
        if self.post is not None:
            self.download_requested.emit(self.post)

    def _sync_native_topmost(self, keep_on_top: bool):
        if sys.platform != "win32" or not self.isVisible():
            return
        try:
            import win32con
            import win32gui
        except ImportError:
            return
        hwnd = int(self.winId())
        insert_after = win32con.HWND_TOPMOST if keep_on_top else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(
            hwnd, insert_after, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED,
        )

    def _refresh_native_input_surface(self):
        """Resync Windows layered-window hit testing after first show / flag rebuild."""
        if sys.platform != "win32" or not self.isVisible():
            return
        try:
            import win32con
            import win32gui
        except ImportError:
            return
        hwnd = int(self.winId())
        win32gui.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
            | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW,
        )
        self.repaint()

    def _apply_window_mode(self):
        geometry = self.geometry()
        was_visible = self.isVisible()
        self._suppress_closed = True
        try:
            self.setWindowFlags(self._window_flags(self._keep_on_top))
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_NoSystemBackground, True)
            self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
            if was_visible:
                self.show()
                if geometry.isValid():
                    self.setGeometry(geometry)
                self._sync_native_topmost(self._keep_on_top)
                self._refresh_native_input_surface()
        finally:
            self._suppress_closed = False

    def keep_top_hint(self, _flag: bool = None):
        flag = _flag if _flag is not None else self.topHintBox.isChecked()
        self._keep_on_top = flag
        self.topHintBox.setChecked(flag)
        self._apply_window_mode()

    @staticmethod
    def _is_valid_size(size: t.Optional[QtCore.QSize]) -> bool:
        return bool(size is not None and size.width() > 0 and size.height() > 0)

    @staticmethod
    def _size_area(size: QtCore.QSize) -> int:
        return max(0, size.width()) * max(0, size.height())

    def _clone_size(self, size: QtCore.QSize) -> QtCore.QSize:
        return QtCore.QSize(size.width(), size.height())

    def _show_media_widget(self, media_kind: str):
        self._current_media_kind = media_kind
        if media_kind == "video":
            self._ensure_video_backend()
        self.image_label.setVisible(media_kind != "video")
        if self.playBar is not None:
            self.video_mgr.set_media_visible(media_kind, self.playBar, self.topBarSpacer)
        else:
            self.image_label.setVisible(True)
        self._update_top_bar_height()

    def _update_top_bar_height(self):
        play_bar_height = 0
        if self.playBar is not None and not self.playBar.isHidden():
            play_bar_height = self.playBar.height()
        self.top_bar.setFixedHeight(max(34, play_bar_height))

    def set_video_cache_progress(
        self, *, post_id: int, cached_bytes: int, total_bytes: int, cached_ratio: float, 
        active_segment_index: int, complete: bool,
    ):
        if self.post is None or self.post.post_id != int(post_id):
            return
        if self.playBar is None:
            return
        self.video_mgr.set_cache_progress(
            cached_bytes=cached_bytes, total_bytes=total_bytes, cached_ratio=cached_ratio,
            active_segment_index=active_segment_index, complete=complete, play_bar=self.playBar,
        )

    def _update_display_source_size(self, source_size: t.Optional[QtCore.QSize], *, replace: bool = False) -> bool:
        if not self._is_valid_size(source_size):
            return False
        normalized = self._clone_size(source_size)
        if replace or not self._is_valid_size(self._display_source_size):
            self._display_source_size = normalized
            return True
        assert self._display_source_size is not None
        if self._size_area(normalized) > self._size_area(self._display_source_size):
            self._display_source_size = normalized
            return True
        return False

    def _effective_display_source_size(self) -> t.Optional[QtCore.QSize]:
        if self._is_valid_size(self._display_source_size):
            assert self._display_source_size is not None
            return self._clone_size(self._display_source_size)
        post_hint = self._post_size_hint(self.post) if self.post is not None else None
        if self._is_valid_size(post_hint):
            assert post_hint is not None
            return self._clone_size(post_hint)
        if self._is_valid_size(self._loaded_pixmap_size):
            assert self._loaded_pixmap_size is not None
            return self._clone_size(self._loaded_pixmap_size)
        return None

    def _chrome_width(self) -> int:
        outer_margins = self.outer_layout.contentsMargins()
        main_margins = self.main_layout.contentsMargins()
        right_panel_margins = self.right_panel_layout.contentsMargins()
        return (
            outer_margins.left()
            + outer_margins.right()
            + main_margins.left()
            + main_margins.right()
            + self.left_column.width()
            + self.main_layout.spacing()
            + right_panel_margins.left()
            + right_panel_margins.right()
        )

    def _chrome_height(self) -> int:
        outer_margins = self.outer_layout.contentsMargins()
        main_margins = self.main_layout.contentsMargins()
        right_panel_margins = self.right_panel_layout.contentsMargins()
        return (
            outer_margins.top()
            + outer_margins.bottom()
            + main_margins.top()
            + main_margins.bottom()
            + right_panel_margins.top()
            + right_panel_margins.bottom()
            + self.top_bar.height()
            + self.right_panel_layout.spacing()
        )

    def _max_right_panel_width(self) -> int:
        geometry = self._screen_geometry()
        return max(1, geometry.width() - self._chrome_width())


    def _calculate_fit_result(self, source_size: t.Optional[QtCore.QSize]) -> DanbooruViewerFitResult:
        return DanbooruViewerFitCalculator.calculate(self._image_display_bounds(), source_size)

    def _fit_image_size(self, source_size: t.Optional[QtCore.QSize]) -> QtCore.QSize:
        return self._calculate_fit_result(source_size).display_size

    def _apply_image_display_size(self, display_size: QtCore.QSize):
        panel_width = min(self._max_right_panel_width(), max(display_size.width(), self._top_bar_min_width()))
        self.image_label.setFixedSize(display_size)
        self.video_mgr.set_display_size(display_size)
        self.right_panel_widget.setFixedWidth(panel_width)
        self._sync_image_hint_geometry()

    def _settle_viewer_layout(self, source_size: t.Optional[QtCore.QSize] = None):
        self._update_display_source_size(source_size)
        self._last_fit_result = self._calculate_fit_result(self._effective_display_source_size())
        self._applying_layout = True
        try:
            self._apply_image_display_size(self._last_fit_result.display_size)
            self._reposition_viewer()
        finally:
            self._applying_layout = False

    def _schedule_loaded_settlement(self):
        post_id = self._pending_loaded_settle_post_id
        if post_id is None or self._loaded_settle_scheduled:
            return
        revision = self._loaded_settle_revision
        self._loaded_settle_scheduled = True

        def _apply_loaded_settle():
            self._loaded_settle_scheduled = False
            if (
                self.post is None
                or self.post.post_id != post_id
                or self._pending_loaded_settle_post_id != post_id
                or revision != self._loaded_settle_revision
            ):
                return
            self._pending_loaded_settle_post_id = None
            self._settle_viewer_layout()

        safe_single_shot(0, _apply_loaded_settle)

    def _arm_loaded_settlement(self, post_id: int):
        self._pending_loaded_settle_post_id = post_id
        self._loaded_settle_revision += 1
        self._schedule_loaded_settlement()

    @staticmethod
    def _post_size_hint(post: DanbooruPost) -> t.Optional[QtCore.QSize]:
        width = post.image_width or post.preview_width
        height = post.image_height or post.preview_height
        if width > 0 and height > 0:
            return QtCore.QSize(width, height)
        return None

    def _clear_tags(self):
        while self.tags_layout.count():
            item = self.tags_layout.takeAt(0)
            _delete_flow_item(item)
        self.tags_layout.addStretch(1)

    def _sync_image_hint_geometry(self):
        self.image_hint_label.setGeometry(0, 0, self.image_label.width(), self.image_label.height())
        ind = self.image_loading_indicator
        ind.adjustSize()
        iw, ih = self.image_label.width(), self.image_label.height()
        ind.setGeometry((iw - ind.width()) // 2, (ih - ind.height()) // 2,
                        ind.width(), ind.height())

    def _has_image_pixmap(self) -> bool:
        pixmap = self.image_label.pixmap()
        return bool(pixmap is not None and not pixmap.isNull())

    def _display_media_kind_for_post(self, post: t.Optional[DanbooruPost]) -> str:
        return "video" if post is not None and post.uses_viewer_video else "image"

    def show_error_infobar(self, text: str):
        """错误反馈统一走 ``ViewerChromeMixin.show_error_infobar``（唯一实现）。"""
        return super().show_error_infobar(text)

    def set_placeholder(self, text: str):
        """换项初始占位：Loading 类→进度条，其余（不可看/错误）→InfoBar。

        R6：图像区不留文字 label。``_initial_placeholder_text`` 产出的多为
        "Loading..."/"Loading preview..." 等内部固定英文，按前缀识别为加载态；
        "ZIP archive, download only" 等不可看状态才用 InfoBar。
        """
        text = str(text or "")
        if self._current_media_kind == "video" and self.playBar is not None:
            self.video_mgr.clear_payload(self.playBar)
            if text and not text.startswith("Loading"):
                self.video_mgr.set_hint(text, visible=True)
            return
        self.image_label.setPixmap(QPixmap())
        self.image_hint_label.hide()
        if text.startswith("Loading"):
            self.image_loading_indicator.setActive(True)
        else:
            self.image_loading_indicator.setActive(False)
            if text:
                self.show_error_infobar(text)

    def set_status_hint(self, text: str):
        """状态提示（如 No more posts）：有文本→InfoBar，空→清提示。"""
        self._status_hint_text = str(text or "")
        if not self._status_hint_text:
            if self._current_media_kind == "video" and self._video_backend_ready:
                self.video_mgr.set_hint("", visible=False)
            self.image_hint_label.hide()
            return
        if self._current_media_kind == "video" and self._video_backend_ready:
            self.video_mgr.set_hint(self._status_hint_text, visible=True)
            return
        self.show_error_infobar(self._status_hint_text)

    def set_page_loading(self, loading: bool, text: str = "Loading..."):
        """图像翻页加载：居中进度条（无文字）；视频仍走 buffering indicator。"""
        self._page_loading = bool(loading)
        if self._current_media_kind == "video" and self._video_backend_ready:
            self.video_mgr._set_buffering_indicator(self._page_loading, text if self._page_loading else "")
        else:
            self.image_loading_indicator.setActive(self._page_loading)
            if self._page_loading:
                self.image_hint_label.hide()
        self.next_btn.setDisabled(self._page_loading)

    def set_placeholder_for_post(self, post: DanbooruPost, text: str):
        self._show_media_widget(self._display_media_kind_for_post(post))
        self._update_display_source_size(self._post_size_hint(post), replace=True)
        self._last_fit_result = self._calculate_fit_result(self._effective_display_source_size())
        self._apply_image_display_size(self._last_fit_result.display_size)
        self.set_placeholder(text)

    def set_placeholder_size(self, post_id: int, source_size: QtCore.QSize):
        if self.post is None or self.post.post_id != post_id or source_size.width() <= 0 or source_size.height() <= 0:
            return
        self._settle_viewer_layout(source_size)

    def _populate_tags(self, post: DanbooruPost):
        self._clear_tags()
        tail = self.tags_layout.takeAt(self.tags_layout.count() - 1)
        _delete_flow_item(tail)
        for section_label, tags in TagPrompt(post).groups:
            title = BodyLabel(section_label, self.tags_container)
            title.setObjectName("DanbooruTagSectionTitle")
            self.tags_layout.addWidget(title)
            for tag in tags:
                button = PushButton(tag, self.tags_container)
                button.setObjectName("DanbooruTagButton")
                button.setFocusPolicy(Qt.NoFocus)
                button.clicked.connect(lambda _=False, current_tag=tag: self.tag_clicked.emit(current_tag))
                self.tags_layout.addWidget(button)
        self.tags_layout.addStretch(1)

    def _update_download_button(self):
        downloadable = self.post is not None and self.post.is_downloadable and not self._already_downloaded
        self.download_btn.setDisabled(not downloadable)

    def set_download_state(self, downloaded: bool):
        self._already_downloaded = downloaded
        self._update_download_button()

    def set_navigation_enabled(self, has_previous: bool, has_next: bool):
        self.previous_btn.setEnabled(has_previous)
        self.next_btn.setEnabled(has_next)

    def _first_tag_from_group(self, tag_group: str) -> str:
        if self.post is None:
            return ""
        for label, tags in TagPrompt(self.post).groups:
            if label == tag_group:
                return tags[0] if tags else ""
        return ""

    def _open_first_group_tag(self, tag_group: str) -> bool:
        tag = self._first_tag_from_group(tag_group)
        if not tag:
            return False
        self.tag_clicked.emit(tag)
        return True

    def _keypad_tag_group_for_key(self, key: int, modifiers: Qt.KeyboardModifiers) -> t.Optional[str]:
        if not modifiers & Qt.KeypadModifier:
            return None
        tag_group_by_key = {
            Qt.Key_1: "Character",
            Qt.Key_End: "Character",
            Qt.Key_2: "Artist",
            Qt.Key_Down: "Artist",
            Qt.Key_3: "Copyright",
            Qt.Key_PageDown: "Copyright",
        }
        return tag_group_by_key.get(key)

    @staticmethod
    def _initial_placeholder_text(post: DanbooruPost) -> str:
        if post.is_video:
            return "Loading video..."
        if post.is_archive:
            if post.preview_asset_is_video:
                return "Loading video preview..."
            return "Loading preview asset..." if post.has_renderable_preview_asset else "ZIP archive, download original"
        if post.uses_viewer_video:
            return "Loading video preview..."
        if post.large_file_url or post.file_url or post.preview_file_url:
            return "Loading..."
        return "No Preview"

    def show_post(self, post: DanbooruPost, already_downloaded: bool):
        self.stop_animation()
        self.post = post
        self._already_downloaded = already_downloaded
        self._page_loading = False
        self._status_hint_text = ""
        self._display_source_size = None
        self._loaded_pixmap_size = None
        if self.playBar is not None:
            self.video_mgr.clear_payload(self.playBar)
        self._last_fit_result = None
        self._pending_loaded_settle_post_id = None
        self._loaded_settle_revision += 1
        self._loaded_settle_scheduled = False
        self._update_display_source_size(self._post_size_hint(post), replace=True)
        self._populate_tags(post)
        self._update_download_button()
        self.set_placeholder_for_post(post, self._initial_placeholder_text(post))
        first_real_show = not self.isVisible() and not self._native_window_warmed
        self._warm_native_window()
        self.show()
        self._settle_viewer_layout()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        if first_real_show or sys.platform == "win32":
            self._refresh_native_input_surface()

    def set_image(self, post_id: int, pixmap: QPixmap):
        if self.post is None or self.post.post_id != post_id:
            return
        if pixmap.width() == 0 or pixmap.height() == 0:
            return
        self._show_media_widget("image")
        if self.playBar is not None:
            self.video_mgr.clear_payload(self.playBar)
        pixmap_size = QtCore.QSize(pixmap.width(), pixmap.height())
        self._loaded_pixmap_size = self._clone_size(pixmap_size)
        self._update_display_source_size(pixmap_size)
        self.image_label.setPixmap(pixmap)
        self.image_hint_label.hide()
        self.image_loading_indicator.setActive(False)
        self._settle_viewer_layout()
        self._arm_loaded_settlement(post_id)

    def seed_pixmap(self, post_id: int, pixmap: QPixmap) -> bool:
        """0 网络首帧：把网格里已有的缩略图先贴进图像区，再等后台预览/原图替换。

        只做**显示**、不做尺寸结算：不写 ``_loaded_pixmap_size`` / 不调
        ``_update_display_source_size`` / ``_settle_viewer_layout``（否则窗口会先缩成
        缩略图尺寸再弹回）、不停 loading indicator（两阶段还在进行）。缩放按
        当前 image_label 视口保比例裁，不放大。
        """
        if self.post is None or self.post.post_id != post_id:
            return False
        if pixmap is None or pixmap.isNull() or pixmap.width() == 0:
            return False
        self._show_media_widget("image")
        view = self.image_label.size()
        shown = pixmap
        if view.isValid():
            fitted = pixmap.scaled(
                view, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
            if not fitted.isNull():
                shown = fitted
        self.image_label.setPixmap(shown)
        self.image_label.show()
        self.image_hint_label.hide()
        self.image_label.update()
        return True

    # ---- 动图面（gif / webp / apng）：生命周期走共享 ViewerChromeMixin ----
    def set_animated_image(self, post_id: int, raw: bytes) -> bool:
        """把原始字节接成 ``QMovie`` 动图（主线程建，buffer 保活）。

        movie 的创建/复用/释放归 ``ViewerChromeMixin``（``_build_movie`` / ``stop_animation``），
        本方法只做 danbooru 侧的挂载：静态首帧先落（无空窗），再把 movie 挂到
        ``image_label``（qfw ``ImageLabel`` 自带 ``frameChanged -> image -> update``）。
        """
        if self.post is None or self.post.post_id != post_id:
            return False
        if self._current_media_kind == "video":
            return False
        movie = self._build_movie(raw, post_id)
        if movie is None:
            return False
        self._show_media_widget("image")
        self.image_label.setMovie(movie)
        self.image_hint_label.hide()
        self.image_loading_indicator.setActive(False)
        frame = movie.currentImage()
        if not frame.isNull():
            size = QtCore.QSize(frame.width(), frame.height())
            self._loaded_pixmap_size = self._clone_size(size)
            self._update_display_source_size(size)
            self._settle_viewer_layout()
        if not self.isVisible():
            movie.stop()
        return True

    def set_video(self, post_id: int, source_url: str):
        if self.post is None or self.post.post_id != post_id:
            return
        self._ensure_video_backend()
        self._show_media_widget("video")
        self.video_mgr.clear_payload(self.playBar)
        self.image_label.setPixmap(QPixmap())
        self.image_hint_label.hide()
        self.video_mgr.set_video(post_id, source_url)
        self._settle_viewer_layout()

    def paintEvent(self, event):
        _ = event
        # On Windows a translucent top-level is a layered window and fully
        # transparent pixels are skipped by hit testing, so the 12px shadow ring
        # and the rounded frame corners would let clicks fall through to the tab
        # grid underneath. A 1/255 alpha fill is visually invisible but keeps the
        # whole viewer rect inside the window's input surface.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_image_hint_geometry()
        self.video_mgr.sync_hint_geometry()
        if self._applying_layout:
            return
        self._schedule_loaded_settlement()

    def _handle_custom_key(self, event) -> bool:
        """站点特有键：数字键跳标签组、视频空格播放。其余交基底统一键位。"""
        key = event.key()
        modifiers = event.modifiers()
        keypad_tag_group = self._keypad_tag_group_for_key(key, modifiers)
        if keypad_tag_group is not None:
            self._open_first_group_tag(keypad_tag_group)
            return True
        if key == Qt.Key_Space and self._current_media_kind == "video":
            self.video_mgr.toggle_playback(self._current_media_kind)
            return True
        return False

    def hideEvent(self, event):
        self._drag_offset = None
        self.stop_animation()
        if self.playBar is not None:
            self.video_mgr.clear_payload(self.playBar)
        # closed 信号由基底 hideEvent 统一发（_suppress_closed 语义一致）。
        super().hideEvent(event)

    def _image_mouse_press_event(self, event):
        QLabel.mousePressEvent(self.image_label, event)

    def _image_mouse_move_event(self, event):
        QLabel.mouseMoveEvent(self.image_label, event)

    def _image_mouse_release_event(self, event):
        self._drag_offset = None
        if self._is_close_gesture(event):
            self._close_from_middle_click()
            event.accept()
            return
        QLabel.mouseReleaseEvent(self.image_label, event)
