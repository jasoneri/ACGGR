"""pawchive L3 详情浮窗（P3）。

- 无边框置顶浮窗 + 顶栏（置顶/翻页/下载/关闭）+ 大图画布 + 信息栏；顶栏与 danbooru 同构
  （VIEWER_TOPBAR_OBJECT、同按钮序列/间距/34px），无 − / + / FIT 按钮组，不设 WA_StyledBackground。
- 缩放入口两个：Ctrl+滚轮（以光标为锚点）、双击 fit ↔ 100%。锚点不变式 _pan = img_pt*new -
  cursor；缩放基准由共享 viewer 设置下发。
- 几何换算全走 app/GUI/viewer/layout.py（纯函数）+ 基类钩子 _view_ratio()/_viewer_fit_size()，
  本站无私有 magic number；布局落定后 _schedule_geometry_settle。
- 主题只在构造时渲染一次，刷新入口在宿主 PawchiveGridTab.apply_theme。
- 翻页/取图/下载归控制器 detail_preview；本模块只做状态与绘制，不 import danbooru、不带
  UA/HTTP 常量。视频面/动图面惰性创建。线程纪律：QPixmap/QImage/QMovie 只在主线程构造。
"""

import typing as t
from collections import OrderedDict
from dataclasses import dataclass

from PySide6 import QtCore, QtGui
from PySide6.QtCore import Qt, Signal, qtTrId
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon as FIF,
    PrimaryToolButton,
    TransparentToggleToolButton,
    TransparentToolButton,
)
from app.GUI.viewer import (
    ImageLoadingIndicator,
    ViewerChromeMixin,
    layout,
    style_viewer_top_bar,
    viewer_image_color,
)
from app.GUI.viewer.settings import viewer_default_pin
from ui.core.timer import safe_single_shot

from .post_adapter import VIDEO_EXTENSIONS, file_ext_from_name
from .style import build_viewer_stylesheet

#: 每档 25%（工程取值）。Ctrl+滚轮共用同一档位，触控板小 delta 走连续值。
_ZOOM_STEP = 1.25
#: 放大上限；再放大无信息增益，且内存按平方增长。
_ZOOM_MAX = 4.0
#: 浮点比较容差。
_ZOOM_EPS = 0.001
#: fit 基准面积比（view_ratio）与置顶初值没有站点私有常量：比例走基类
#: self._view_ratio()（转调共享 settings，与 danbooru 同一份 key），置顶初值走
#: viewer_default_pin()。画布/列宽常量来自 layout（UNKNOWN_SOURCE_CANVAS / LEFT_COLUMN_WIDTH）。
#: 已解码 pixmap 的 LRU 上限：1304×1848 的 32bpp 约 9.6 MB/张，6 张 ≈ 58 MB 峰值。
_PIXMAP_CACHE_LIMIT = 6
#: 缩放后画布图缓存（当前档的邻档），不跨图复用。
_SCALED_CACHE_LIMIT = 3
#: 视频扩展名：站点集合 + ``m4v``（H.264 容器常见别名，QMediaPlayer 同样能解）。
_VIDEO_EXTENSIONS = frozenset(VIDEO_EXTENSIONS | {"m4v"})
#: play bar 的空闲高度（顶栏在视频模式下按它增高）。
_PLAY_BAR_HEIGHT = 48

#: 本模块经 ``_tr_or`` 使用的全部 i18n 键。**这份元组不是装饰**：``_tr_or`` 用变量
#: 调 ``qtTrId``，而 ``res/i18n/build.py`` 的 AST 扫描只认字面量实参，或"赋值右侧
#: tuple/list/dict 里的 id 形字符串"这条间接通道。没有它，这些键在 ``--check`` 里
#: 会被判 stale，P4 一跑 ``--sync`` 就会把它们从表里删掉。新增/改名键时必须同步这里。
_VIEWER_I18N_KEYS = (
    "pawchive.card.download_only",
    "pawchive.card.downloading",
    "pawchive.card.preview_error",
    "pawchive.download.no_url",
    "pawchive.error.blocked",
    "pawchive.viewer.download",
    "pawchive.viewer.downloaded",
    "pawchive.viewer.loading",
    "pawchive.viewer.meta",
    "pawchive.viewer.position",
)


def _tr_or(key: str, fallback: str) -> str:
    """``qtTrId`` 缺键时回退成 id 本身；此时用更短的兜底串（id 串会撑变形顶栏）。"""
    text = qtTrId(key)
    return fallback if text == key else text


@dataclass(frozen=True, slots=True)
class PawchiveViewerItem:
    """浮窗当前项（视图模型，07 §2.1）。"""

    post: t.Any
    attachment: t.Any
    identity_key: str
    #: 0-based，同帖 merged 序号（信息栏「第 i/N 张」在喂值处 +1）。
    index: int
    #: ``len(service.attachments(post))``，恒 >= 1。
    total: int
    #: 0-based，作者页展开序列全局序号（翻页用）。
    seq: int
    #: 展开序列总长。
    seq_total: int


class _ElidedLabel(QLabel):
    """单行省略标签：完整文本存起来，按当前宽度 ``ElideMiddle``，完整值进 tooltip。

    超长 ``attachment.name`` 不省略会把信息栏（固定 196）撑出可视区（07 §5-12）。
    """

    def __init__(self, parent=None, *, mode=Qt.ElideMiddle):
        super().__init__(parent)
        self._full_text = ""
        self._elide_mode = mode
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)

    def set_full_text(self, text: str, *, tooltip: str = ""):
        self._full_text = str(text or "")
        self.setToolTip(tooltip or self._full_text)
        self._sync_elide()


    def full_text(self) -> str:
        return self._full_text

    def _sync_elide(self):
        width = max(0, self.width())
        if width <= 0:
            self.setText(self._full_text)
            return
        metrics = self.fontMetrics()
        self.setText(metrics.elidedText(self._full_text, self._elide_mode, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_elide()


class PawchiveViewerSource:
    """取图 URL 解析（纯函数，无网络，07 §2.2）。"""

    @staticmethod
    def resolve(attachment: t.Any) -> t.Optional[t.Tuple[str, str]]:
        """``("original", url)`` / ``("thumbnail", url)`` / ``None``（不渲染）。

        **先原图、再缩略图，最后才认"没图可看"**。``kind`` 只决定能否拿到原图，
        不决定"能不能看"：

        现场事故：``attachment_kind()`` 把"没有 temp_url 但有 server URL"的图片标成
        ``archive``，viewer 于是直接弹「仅可下载」——可同一张图在网格里明明有缩略图。
        用户看到的是"列表有图、点开说没图"的反差。只要 ``path`` 能拼出 ``img.`` 域
        缩略图，就该把缩略图显示出来（下载按钮另行判定）。

        **不拼 ``file.`` 域**。
        """
        if PawchiveViewerSource.is_video_attachment(attachment):
            # 视频走真实媒体面（QtMultimedia），不能进 pixmap 取图链：把 mp4 整段
            # 读进内存再 ``loadFromData`` 只会得到「预览失败」。控制器据此跳过取图。
            return None
        temp_url = str(getattr(attachment, "temp_url", "") or "")
        if temp_url:
            return ("original", temp_url)
        # temp_url 为空 ≠ 只能看缩略图。服务层能由 server+path 拼出原图域（n3./data/...），
        # 而缩略图域对 GIF 只返静态 WebP 首帧—— viewer 若退到缩略图，动图就不动。
        # 先试原图域，拿不到再退缩略图。
        original = PawchiveViewerSource.media_url(attachment)
        if original:
            return ("original", original)
        thumb = PawchiveViewerSource.thumbnail_fallback(attachment)
        if thumb:
            return ("thumbnail", thumb)
        return None

    @staticmethod
    def is_video_attachment(attachment: t.Any) -> bool:
        """扩展名判定（``name`` 优先，缺失时看 ``path``）。"""
        name = str(getattr(attachment, "name", "") or "")
        path = str(getattr(attachment, "path", "") or "")
        return (file_ext_from_name(name) or file_ext_from_name(path)) in _VIDEO_EXTENSIONS

    @staticmethod
    def media_url(attachment: t.Any) -> t.Optional[str]:
        """媒体直链：``temp_url`` → ``{server}/data{path}?f={name}`` → ``None``。

        规则归服务层（``resolve_attachment_url``，与下载链同源）；服务层不可用时
        退回同字段顺序，浮窗不因配置缺失丢掉已押在附件里的 URL。
        """
        try:
            from core.website.pawchive.service import resolve_attachment_url

            resolved = resolve_attachment_url(attachment)
        except Exception:  # noqa: BLE001 - 服务层导入失败不挡住播放
            resolved = None
        if resolved:
            return str(resolved)
        temp_url = str(getattr(attachment, "temp_url", "") or "")
        if temp_url:
            return temp_url
        server = str(getattr(attachment, "server", "") or "")
        path = str(getattr(attachment, "path", "") or "")
        if server and path:
            name = str(getattr(attachment, "name", "") or "")
            query = f"?f={name}" if name else ""
            return f"{server.rstrip('/')}/data{path}{query}"
        return None

    @staticmethod
    def thumbnail_fallback(attachment: t.Any) -> t.Optional[str]:
        """缩略图域兜底（``img.`` 域：UA 不敏感、支持 HEAD/Range）。"""
        path = str(getattr(attachment, "path", "") or "")
        if not path:
            return None
        url = f"https://{PawchiveViewerSource._img_domain()}/thumbnail/data{path}"
        name = str(getattr(attachment, "name", "") or "")
        return f"{url}?f={name}" if name else url

    @staticmethod
    def is_downloadable(attachment: t.Any) -> bool:
        """按钮使能第一段：``kind`` 可下 **且** 字段能解析出地址（与 04 同源判定）。"""
        kind = str(getattr(attachment, "kind", "") or "")
        if kind not in ("image", "archive"):
            return False
        has_temp = bool(str(getattr(attachment, "temp_url", "") or ""))
        has_server = bool(str(getattr(attachment, "server", "") or ""))
        return bool(has_temp or has_server)

    @staticmethod
    def _img_domain() -> str:
        try:
            from core.website.pawchive import img_domain
        except Exception:  # noqa: BLE001 - 取不到域也不该崩浮窗
            return "img.pawchive.pw"
        return str(img_domain or "img.pawchive.pw")


class PawchiveZoomCanvas(QWidget):
    """大图画布：自绘（按 pan/zoom 裁源图）+ 自管鼠标（平移 / 双击 / 中键关闭）。

    **不持有 zoom 状态**：状态在 ``PawchiveImageViewer`` 上，画布只把交互翻译成
    信号，避免"翻页换图要同步两处状态"。
    """

    pan_delta_requested = Signal(object)  # (QPointF delta) 内容随鼠标走
    double_click_requested = Signal()
    close_gesture = Signal()

    def __init__(self, owner: "PawchiveImageViewer", parent=None):
        super().__init__(parent)
        self._owner = owner
        self._pan_anchor: t.Optional[QtCore.QPointF] = None
        self.setObjectName("PawchiveZoomCanvas")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # 源图未知时的中性占位：共享常量，且真正落尺寸前会夹进 media bounds。
        self.setFixedSize(layout.UNKNOWN_SOURCE_CANVAS)
        self.setFocusPolicy(Qt.NoFocus)

    # ---- 绘制 ----
    def paintEvent(self, event):
        _ = event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        # 自定义 paintEvent 不会画 QSS 背景：必须自己铺，否则深色主题下
        # 画布四角露出 frame 的近黑底色，看起来就是一圈"黑色边框"。
        painter.fillRect(self.rect(), viewer_image_color())
        image = self._owner.paint_payload(self.size())
        if image is not None and not image.isNull():
            painter.drawImage(0, 0, image)
            # 兜底：画布真的画出图了就停加载进度条。不依赖 set_image/set_animated_image
            # 的显式 stop（换项/事件时序可能让那些路径失效），只要图可见就绝不再转。
            # 唯一例外：两阶段动图升级窗（_image_upgrading）——静态首帧上屏但原图
            # 仍在下载，此时**不**停，让用户看到"正在变清晰"的进度条（1→2 过渡）。
            owner = self._owner
            if not getattr(owner, "_image_upgrading", False):
                ind = owner.image_loading_indicator
                if ind.isActive():
                    ind.setActive(False)

    # ---- 鼠标 ----
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._owner._zoom_at_fit():
            self._pan_anchor = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        # fit 态图片区左键无动作（移窗只在顶栏，与现盘手感一致）。
        event.ignore()

    def mouseMoveEvent(self, event):
        if self._pan_anchor is None:
            event.ignore()
            return
        position = event.position()
        self.pan_delta_requested.emit(position - self._pan_anchor)
        self._pan_anchor = position
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._pan_anchor is not None:
            self._pan_anchor = None
            self.unsetCursor()
            event.accept()
            return
        if event.button() == Qt.MiddleButton:
            self.close_gesture.emit()
            event.accept()
            return
        event.ignore()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        self.double_click_requested.emit()
        event.accept()


class _PawchiveVideoManager:
    """视频面 + 外部 play bar + 本地中继（全部惰性创建）。

    ``QtMultimedia`` / ``FFmpeg`` 的冷启动只发生在第一次显示视频项时；纯图片会话
    连 ``VideoWidget`` 都不会构造。视频源走 ``video_proxy``（本地 HTTP 中继），
    本地文件则直接给 ``QUrl.fromLocalFile``，不绕代理。
    """

    def __init__(self, viewer: "PawchiveImageViewer"):
        self.viewer = viewer
        self.frame: t.Optional[QFrame] = None
        self.surface: t.Any = None
        self.hint_label: t.Optional[QLabel] = None
        self.player: t.Any = None
        self.proxy: t.Any = None
        self._play_bar: t.Any = None
        self._active_key = ""
        self._active_source_url = ""
        self._user_paused = False
        self._media_status: t.Any = None
        self._playback_state: t.Any = None

    # ---- 构建 ----
    def setup_widgets(self, parent: QWidget, display_size: QtCore.QSize):
        from qfluentwidgets.multimedia import VideoWidget

        class _ExternalPlayBarVideoWidget(VideoWidget):
            """屏蔽自带悬浮 play bar：播控统一由顶栏的共享 play bar 承担。"""

            def __init__(self, surface_parent=None):
                super().__init__(surface_parent)
                self._using_external_play_bar = False

            def bind_external_play_bar(self, play_bar):
                if self._using_external_play_bar and self.playBar is play_bar:
                    return
                legacy_play_bar = self.playBar
                legacy_play_bar.hide()
                self.playBar = play_bar
                self._using_external_play_bar = True
                self.player.setVideoOutput(self.videoItem)
                legacy_play_bar.deleteLater()

            def enterEvent(self, event):
                if not self._using_external_play_bar:
                    super().enterEvent(event)

            def leaveEvent(self, event):
                if not self._using_external_play_bar:
                    super().leaveEvent(event)

            def resizeEvent(self, event):
                if not self._using_external_play_bar:
                    super().resizeEvent(event)
                    return
                QGraphicsView.resizeEvent(self, event)
                self.videoItem.setSize(QtCore.QSizeF(self.size()))
                self.fitInView(self.videoItem, Qt.KeepAspectRatio)

        self.frame = QFrame(parent)
        self.frame.setObjectName("PawchiveVideoPanel")
        frame_layout = QVBoxLayout(self.frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)
        self.surface = _ExternalPlayBarVideoWidget(self.frame)
        self.surface.setObjectName("PawchiveVideoSurface")
        self.surface.setFocusPolicy(Qt.NoFocus)
        frame_layout.addWidget(self.surface)
        self.hint_label = QLabel("", self.frame)
        self.hint_label.setObjectName("PawchiveVideoHint")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setWordWrap(True)
        self.hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.frame.hide()
        self.set_display_size(display_size)

    def setup_backend(self, play_bar):
        from PySide6.QtMultimedia import QMediaPlayer

        self._play_bar = play_bar
        self.surface.bind_external_play_bar(play_bar)
        self.player = self.surface.player
        self._media_status = QMediaPlayer.MediaStatus
        self._playback_state = QMediaPlayer.PlaybackState
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.errorOccurred.connect(self._on_player_error)
        # 预览默认静音自动播放：与 danbooru 浮窗同一套预览策略。
        self.player.setVolume(30)
        self.player.setMuted(True)

    # ---- 源管理 ----
    def set_source(self, identity_key: str, source_url: str) -> bool:
        local_path = _local_media_path(source_url)
        if local_path is not None:
            media_url = QtCore.QUrl.fromLocalFile(local_path)
            self._active_source_url = str(source_url)
        else:
            self._active_source_url = _strip_query(source_url)
            try:
                media_url = QtCore.QUrl(self._ensure_proxy().register_source(source_url))
            except Exception:  # noqa: BLE001 - 中继起不来时给占位，不炸浮窗
                self._active_key = ""
                self.set_hint(_tr_or("pawchive.card.preview_error", "Preview failed"), visible=True)
                return False
        self._active_key = str(identity_key)
        self._user_paused = False
        self.set_hint(_tr_or("pawchive.viewer.loading", "Loading…"), visible=True)
        self.player.setSource(media_url)
        self.player.play()
        return True

    def toggle_playback(self) -> bool:
        if self.player is None or not self._active_key:
            return False
        if self.player.playbackState() == self._playback_state.PlayingState:
            self.player.pause()
            self._user_paused = True
        else:
            self.player.play()
            self._user_paused = False
        return True

    def stop_playback(self):
        """停播并清源：换项 / 隐藏 / 关闭都走这里。"""
        # 先摘活动键再 stop/setSource：QMediaPlayer 可能同步回发
        # Loaded/Buffered 状态，若键还在就会把刚停下的播放器又 play() 回去。
        self._active_key = ""
        self._active_source_url = ""
        self._user_paused = False
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QtCore.QUrl())
        if self.hint_label is not None:
            self.hint_label.hide()

    def shutdown(self):
        self.stop_playback()
        proxy = self.proxy
        self.proxy = None
        if proxy is not None:
            proxy.close()

    # ---- 布局 ----
    def set_media_visible(self, media_kind: str, play_bar, top_bar_spacer: QWidget):
        is_video_mode = media_kind == "video"
        if self.frame is not None:
            self.frame.setVisible(is_video_mode)
        if play_bar is not None:
            play_bar.setVisible(is_video_mode)
            play_bar_host = play_bar.parentWidget()
            if play_bar_host is not None:
                play_bar_host.setVisible(is_video_mode)
        top_bar_spacer.setVisible(not is_video_mode)

    def set_display_size(self, display_size: QtCore.QSize):
        if self.frame is None:
            return
        self.frame.setFixedSize(display_size)
        self.sync_hint_geometry()

    def sync_hint_geometry(self):
        if self.frame is None or self.hint_label is None:
            return
        self.hint_label.setGeometry(0, 0, self.frame.width(), self.frame.height())

    def set_hint(self, text: str, *, visible: bool = True):
        if self.hint_label is None:
            return
        self.hint_label.setText(str(text or ""))
        self.hint_label.setVisible(bool(visible))
        if visible:
            self.hint_label.raise_()
        self.sync_hint_geometry()

    def is_click_target(self, obj) -> bool:
        return obj is self.frame or obj is self.surface

    # ---- 播放器回调 ----
    def _ensure_proxy(self):
        if self.proxy is not None:
            return self.proxy
        from .video_proxy import PawchiveVideoProxy

        self.proxy = PawchiveVideoProxy(self.viewer)
        self.proxy.request_failed.connect(self._on_proxy_failed)
        return self.proxy

    def _on_media_status_changed(self, status):
        if self.player is None or not self._active_key:
            return
        if status in (self._media_status.LoadedMedia, self._media_status.BufferedMedia):
            self.hint_label.hide()
            if not self._user_paused and self.player.playbackState() != self._playback_state.PlayingState:
                self.player.play()
            return
        if status == self._media_status.InvalidMedia:
            self.set_hint(_tr_or("pawchive.card.preview_error", "Preview failed"), visible=True)

    def _on_player_error(self, _error, _message):
        if not self._active_key:
            return
        self.set_hint(_tr_or("pawchive.card.preview_error", "Preview failed"), visible=True)

    def _on_proxy_failed(self, source_url: str, _message: str):
        if not self._active_key:
            return
        if source_url and self._active_source_url and source_url != self._active_source_url:
            return
        self.set_hint(_tr_or("pawchive.card.preview_error", "Preview failed"), visible=True)


def _strip_query(url: str) -> str:
    return str(url or "").split("?", 1)[0].split("#", 1)[0]


def _local_media_path(source_url: str) -> t.Optional[str]:
    text = str(source_url or "").strip()
    if not text:
        return None
    if text.lower().startswith("file:"):
        url = QtCore.QUrl(text)
        return url.toLocalFile() if url.isLocalFile() else None
    if "://" in text:
        return None
    try:
        from pathlib import Path

        return text if Path(text).is_file() else None
    except OSError:
        return None


class PawchiveImageViewer(ViewerChromeMixin, QWidget):
    """L3 详情浮窗。构造一次、会话内复用（``hide()`` 不销毁）。

    通用壳层（窗口 flags / 屏幕定位 / chrome 尺寸 / 滚轮翻页 / 中键关闭 / 键位）
    来自 `app/GUI/viewer/chrome.py::ViewerChromeMixin`——danbooru 浮窗将来接同一基底，
    两边只有"左列内容"（标签组 vs post 信息）与缩放实现不同。
    """

    previous_requested = Signal()
    next_requested = Signal()
    download_requested = Signal(object)  # (PawchiveViewerItem)
    closed = Signal()
    #: 中键关闭已发生。宿主据此武装防抖：磨损中键的第二次弹起会落在浮窗下面露出的
    #: 网格上，网格的中键语义是"关当前 tab"，不武装时间戳就会"关个图顺手把 tab 也关了"。
    middle_click_closed = Signal()

    def __init__(self, parent=None):
        super().__init__(None)
        self._anchor_widget = parent
        self._item: t.Optional[PawchiveViewerItem] = None
        self._current_key: str = ""
        self._current_image: t.Optional[QImage] = None
        self._source_size: t.Optional[QtCore.QSize] = None
        self._loaded_pixmap_size: t.Optional[QtCore.QSize] = None
        self._fit_factor: float = 1.0
        self._zoom_factor: t.Optional[float] = None
        self._pan = QtCore.QPointF(0.0, 0.0)
        self._already_downloaded = False
        self._download_pending = False
        self._download_available = False
        # 置顶初值与 danbooru 共用同一条 viewer 设置（不改 key、不搬文件）。
        self._keep_on_top = bool(viewer_default_pin())
        # W10 settle 守卫：代数（换项即作废上一遍 settle）+ 是否已排期。
        self._geometry_revision = 0
        self._geometry_settle_scheduled = False
        self._suppress_closed = False
        self._native_window_warmed = False
        self._pixmap_cache: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._scaled_cache: "OrderedDict[tuple, QImage]" = OrderedDict()
        #: 动图状态（QMovie 只能在主线程建）：movie + 支撑它的 buffer + 当前帧。
        #: 动图面（GIF/WebP/APNG）：movie/buffer 归共享 ViewerChromeMixin；
        #: canvas 自绘另需「当前帧」缓存（_animation_frame_image/_seq）。
        self._init_animation_state()
        self._animation_frame_image: t.Optional[QImage] = None
        self._animation_frame_seq = 0
        #: 已连接 ``_on_animation_frame`` 的 movie（复用时不再重复 disconnect/connect）。
        self._animation_frame_movie: t.Optional[QtGui.QMovie] = None
        self._current_media_kind = "image"
        #: 两阶段动图：缩略图静态首帧已上屏、原图（动画）仍在下载时为 True。
        #: 此状态下加载进度条**不因画布已有静态图而停**（1→2 过渡缺进度条的根因），
        #: 直到原图落地/失败由控制器 :meth:`set_upgrading` 清掉。
        self._image_upgrading = False
        self._video_backend_ready = False
        self._video_item_key = ""
        self.playBar = None
        self.video_mgr = _PawchiveVideoManager(self)
        self._setup_ui()
        self.apply_theme()

    # ---- 结构 ----
    def _setup_ui(self):
        self.setObjectName("PawchiveImageViewer")
        self.setWindowFlags(self._window_flags(self._keep_on_top))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setFocusPolicy(Qt.StrongFocus)

        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(12, 12, 12, 12)

        self.frame = QFrame(self)
        self.frame.setObjectName("PawchiveImageViewerFrame")
        self.outer_layout.addWidget(self.frame)

        self.main_layout = QHBoxLayout(self.frame)
        self.main_layout.setContentsMargins(14, 14, 14, 14)
        self.main_layout.setSpacing(14)

        self.view_column = QWidget(self.frame)
        self.view_column.setObjectName("PawchiveViewerColumn")
        column_layout = QVBoxLayout(self.view_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(12)
        self._view_column_layout = column_layout

        # 顶栏与 danbooru **同构**：同一个共享壳层 helper、同一组按钮顺序
        # （pin → [play bar] → spacer → 上一张 → 下一张 → 下载 → 关闭）、同样的
        # 6px 间距与 34px 高。站点私有按钮组（− / + / FIT）已删除：缩放基准改由
        # 共享 viewer 设置下发，浮窗只保留 Ctrl+滚轮与双击两个入口。
        self.top_bar = QWidget(self.view_column)
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
        # 下载按钮与 danbooru 一致用 PrimaryToolButton（强调色），只锁高度 34：
        # 宽度交给控件自身 sizeHint，顶栏间距才不会和基准图差一截。
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

        # play bar 与视频面都拉 QtMultimedia+FFmpeg：只在第一个视频项出现时创建。
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
        column_layout.addWidget(self.top_bar, 0, Qt.AlignTop)

        self.canvas = PawchiveZoomCanvas(self, self.view_column)
        self.canvas.installEventFilter(self)
        self.canvas.pan_delta_requested.connect(self._on_canvas_pan_delta)
        self.canvas.double_click_requested.connect(self._toggle_zoom)
        self.canvas.close_gesture.connect(self._close_from_middle_click)
        column_layout.addWidget(self.canvas, 0, Qt.AlignHCenter | Qt.AlignTop)

        self.image_hint_label = QLabel("", self.canvas)
        self.image_hint_label.setObjectName("PawchiveImageHint")
        self.image_hint_label.setAlignment(Qt.AlignCenter)
        self.image_hint_label.setWordWrap(True)
        self.image_hint_label.installEventFilter(self)
        # R6：加载态用纯进度条、错误态用 InfoBar，图像区不再有文字 label。
        # 保留控件（eventFilter/几何依赖）但初始隐藏、文本清空。
        self.image_hint_label.hide()
        self.image_loading_indicator = ImageLoadingIndicator(self.canvas)
        self._sync_hint_geometry()

        column_layout.addStretch(1)

        self.meta_panel = QWidget(self.frame)
        self.meta_panel.setObjectName("PawchiveViewerMetaPanel")
        self.meta_panel.setFixedWidth(layout.LEFT_COLUMN_WIDTH)
        # 左列注入基底：danbooru 那侧注入的是标签组，pawchive 是 post 信息。
        self.set_left_panel(self.meta_panel)
        meta_layout = QVBoxLayout(self.meta_panel)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setSpacing(8)

        # 信息栏四行用**裸 QLabel**，不用 qfluentwidgets 的 BodyLabel：
        # ``FluentLabelBase._init`` 会给自己 ``setCustomStyleSheet`` 一份
        # ``FluentLabelBase{color:black}`` / ``{color:white}``，控件级样式优先于祖先
        # （viewer）样式，于是 ``ui/core/theme/viewer.qss`` 里
        # ``QLabel#PawchiveViewer*`` 的 ${TEXT}/${MUTED_TEXT} 规则永远不生效——
        # dark→light 后 frame 还是深色而文字被染成黑色，"除了图片全看不见"。
        self.position_label = QLabel(self.meta_panel)
        self.position_label.setObjectName("PawchiveViewerPosition")
        self.position_label.setWordWrap(True)
        self.author_label = QLabel(self.meta_panel)
        self.author_label.setObjectName("PawchiveViewerAuthor")
        self.author_label.setWordWrap(True)
        self.filename_label = _ElidedLabel(self.meta_panel, mode=Qt.ElideMiddle)
        self.filename_label.setObjectName("PawchiveViewerFilename")
        self.meta_label = QLabel(self.meta_panel)
        self.meta_label.setObjectName("PawchiveViewerMeta")
        self.meta_label.setWordWrap(True)
        self.meta_label.setVisible(False)

        meta_layout.addWidget(self.position_label)
        meta_layout.addWidget(self.author_label)
        meta_layout.addWidget(self.filename_label)
        meta_layout.addWidget(self.meta_label)
        meta_layout.addStretch(1)
        # danbooru-viewer 样式：左列（信息/标签）在前、画布在后——信息面板在**左**。
        self.main_layout.addWidget(self.meta_panel, 0, Qt.AlignTop)
        self.main_layout.addWidget(self.view_column, 0, Qt.AlignTop)

        # 工具栏与信息栏不参与键盘焦点（Tab 不进、方向键直落浮窗）。
        for widget in (
            self.previous_btn,
            self.next_btn,
            self.download_btn,
            self.close_btn,
            self.topHintBox,
            self.canvas,
            self.image_hint_label,
            self.position_label,
            self.author_label,
            self.filename_label,
            self.meta_label,
        ):
            widget.setFocusPolicy(Qt.NoFocus)

        self.set_navigation_enabled(False, False)
        self._update_download_button()
        # 构造后立刻走一遍共享画布规则：源图未知 → ``layout.default_canvas_size``
        # （中性比例且夹进 media bounds），不再自己写 520×340。
        self._apply_canvas_size(self._canvas_size())

    # ---- 主题 ----
    def apply_theme(self):
        """按当前主题重渲染共享 chrome 样式（fresh render，可重复调用）。

        主题切换的入口**不在本类**：宿主 ``PawchiveGridTab.apply_theme()`` 在
        ``theme_mgr.subscribe`` 的回调链里显式转发到这里（与 danbooru
        ``DanbooruInterface._apply_theme`` → ``image_viewer.apply_theme()`` 同一条路径）。
        浮窗是会话级单例（hide 复用），漏掉那次转发就会一直停在旧档快照。
        """
        self.setStyleSheet(build_viewer_stylesheet())
        # 与 danbooru 同口径：已建好的 play bar 自带样式，随主题一起刷。
        if self.playBar is not None:
            self.playBar.apply_theme()

    # ---- 视频后端（惰性） ----
    def _ensure_video_backend(self):
        """第一个视频项出现时才建视频面/play bar（图片会话不付 FFmpeg 冷启动）。"""
        if self._video_backend_ready:
            return
        from qfluentwidgets.multimedia import SimpleMediaPlayBar

        self.playBar = SimpleMediaPlayBar(self._play_bar_host)
        self.playBar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        host_layout = QHBoxLayout(self._play_bar_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)
        host_layout.addWidget(self.playBar, 1)

        self.video_mgr.setup_widgets(self.view_column, self._canvas_size())
        canvas_index = self._view_column_layout.indexOf(self.canvas)
        self._view_column_layout.insertWidget(
            max(0, canvas_index), self.video_mgr.frame, 0, Qt.AlignHCenter | Qt.AlignTop
        )
        self.video_mgr.setup_backend(self.playBar)
        self.video_mgr.frame.installEventFilter(self)
        self.video_mgr.surface.installEventFilter(self)
        for widget in (
            self.video_mgr.frame,
            self.video_mgr.surface,
            self.video_mgr.hint_label,
            self.playBar,
        ):
            if widget is not None:
                widget.setFocusPolicy(Qt.NoFocus)
        self._video_backend_ready = True
        self._update_top_bar_height()

    def _show_media_widget(self, media_kind: str):
        self._current_media_kind = media_kind
        if media_kind == "video":
            self._ensure_video_backend()
        else:
            self._stop_video_playback()
        self.canvas.setVisible(media_kind != "video")
        if self._video_backend_ready:
            self.video_mgr.set_media_visible(media_kind, self.playBar, self.topBarSpacer)
        else:
            self.topBarSpacer.setVisible(True)
        self._update_top_bar_height()

    def _update_top_bar_height(self):
        play_bar_height = 0
        if self.playBar is not None and not self.playBar.isHidden():
            play_bar_height = self.playBar.height() or _PLAY_BAR_HEIGHT
        self.top_bar.setFixedHeight(max(34, play_bar_height))

    def _load_video_item(self, item: PawchiveViewerItem):
        media_url = PawchiveViewerSource.media_url(item.attachment)
        if not media_url:
            self._video_item_key = ""
            self.video_mgr.set_hint(
                _tr_or("pawchive.download.no_url", "No download url"), visible=True
            )
            return
        self._video_item_key = item.identity_key
        if not self.video_mgr.set_source(item.identity_key, media_url):
            self._video_item_key = ""

    def _stop_video_playback(self):
        self._video_item_key = ""
        if self._video_backend_ready:
            self.video_mgr.stop_playback()

    def toggle_playback(self) -> bool:
        """空格 / 画面左键：仅视频模式消费。"""
        if self._current_media_kind != "video" or not self._video_backend_ready:
            return False
        return bool(self.video_mgr.toggle_playback())

    # ---- 状态：打开 / 换图 ----
    def show_item(
        self,
        item: PawchiveViewerItem,
        *,
        downloaded: bool = False,
        author_name: str = "",
        source_size: t.Optional[QtCore.QSize] = None,
    ) -> None:
        """换项入口：复位缩放、重填信息栏、清空画面、置 LOADING。

        ``source_size``：**已经知道**的真实像素尺寸（本地缓存/预取项的文件头探针
        结果）。给了它，首帧就是最终 fit 尺寸，不会先出现占位窗口再跳；拿不到
        （冷开、要走网络）就留 ``None``，退回占位 + 解码后 fit 的既有路径。
        """
        self._geometry_revision += 1  # 换项即作废上一遍 settle
        self._item = item
        self._current_key = str(item.identity_key or "")
        self._current_image = None
        # 换项：上一项的"原图升级中"状态对新项无意义，必须复位，否则进度条会
        # 在新项上错误地"以为还在升级"而不随静态图落地停掉。
        self._image_upgrading = False
        # **保留**旧的 ``_source_size`` / ``_loaded_pixmap_size``：新项解码后由
        # ``set_image`` 覆盖。清空会让画布先塌到占位尺寸、解码到达再弹回——
        # 同尺寸连拍时就是"纵向塌 471px 再弹"（R3.3 x/y 猜不透的可见症状之一）。
        self._fit_factor = 1.0
        if self._is_valid_size(source_size):
            # 解码前就知道尺寸：直接按 fit 尺寸开窗（下面的 ``_canvas_size()`` 会用
            # 这个 ``_source_size``），首帧即终态。
            self._source_size = QtCore.QSize(source_size)
            self._fit_factor = layout.fit_scale(
                self._source_size, self._image_display_bounds(), self._view_ratio()
            )
        # 换项先停旧动图（键已变，旧帧再进来会被 set_animated_image 拒收，但表的
        # 解码线程不该继续跑）。
        self._stop_animation()
        self._already_downloaded = bool(downloaded)
        self._download_pending = False
        self._download_available = PawchiveViewerSource.is_downloadable(item.attachment)
        self._reset_zoom()
        self._apply_item_labels(item, author_name)
        self._update_download_button()
        # 预热必须在装入视频源之前：首次预热会 show/hide 一次，hideEvent 会停播。
        self._warm_native_window()
        # 任何换项先停掉上一段媒体：视频 -> 视频也要清源后再给新 URL。
        self._stop_video_playback()
        media_kind = "video" if PawchiveViewerSource.is_video_attachment(item.attachment) else "image"
        self._show_media_widget(media_kind)
        if media_kind == "video":
            self._load_video_item(item)
        else:
            self.set_loading(True)
        self._apply_canvas_size(self._canvas_size())
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        # 居中必须"先落实尺寸、再算位置"：_settle_geometry_now() 内含 activate()，
        # 这里读到的才是本次的 sizeHint，不会用上一次的高度算 y。解码到达后画布还会再变
        # 一次，由 deferred settle 再居中一遍（幂等）。
        self._settle_geometry_now()
        self._schedule_geometry_settle()

    def set_image(self, identity_key: str, pixmap: QPixmap) -> None:
        """主线程落图（worker 只搬字节）。同图回包**保留**当前 zoom/pan。"""
        if self._current_media_kind == "video":
            return
        if not identity_key or identity_key != self._current_key:
            return
        if pixmap is None or pixmap.isNull():
            return
        if pixmap.width() <= 0 or pixmap.height() <= 0:
            return
        # 静态图接管画面：旧动图先停表，避免下一帧把静态图顶掉。
        self._stop_animation()
        self._cache_pixmap(identity_key, pixmap)
        self._current_image = pixmap.toImage()
        self._loaded_pixmap_size = QtCore.QSize(pixmap.width(), pixmap.height())
        self._source_size = QtCore.QSize(pixmap.width(), pixmap.height())
        self._fit_factor = layout.fit_scale(
            self._source_size, self._image_display_bounds(), self._view_ratio()
        )
        self.image_hint_label.hide()
        # 两阶段动图：缩略图静态首帧上屏时原图仍在下载（_image_upgrading），
        # 此时**不能**停进度条——否则 1→2 过渡窗内没有加载指示（用户诉求）。
        if not self._image_upgrading:
            self.image_loading_indicator.setActive(False)
        self._update_meta_label()
        self._clamp_pan()
        self._apply_zoom()
        self._schedule_geometry_settle()

    @staticmethod
    def _is_valid_size(size: t.Optional[QtCore.QSize]) -> bool:
        return size is not None and size.width() > 0 and size.height() > 0

    def set_size_hint(self, identity_key: str, size: QtCore.QSize) -> bool:
        """解码前先按真实尺寸定画布（字节已到手、但还没解码时调用）。

        尺寸来自 preview_loader.probe_image_size()（只读文件头）。返回 True 表示已采纳：
        _source_size / _fit_factor 落好，画布随 _apply_zoom() 直接变成最终 fit 尺寸，随后
        set_image() 落图时尺寸不变，因此不会出现"图片先出现、窗口再跳"。

        两条守卫：identity_key != _current_key 的慢回包丢弃；视频项媒体面尺寸由 play bar 管。
        尺寸非法/缺失时返回 False，占位 → 解码后 fit 的原有路径仍然生效。
        """
        if self._current_media_kind == "video":
            return False
        if not identity_key or identity_key != self._current_key:
            return False
        if not self._is_valid_size(size):
            return False
        if self._source_size == size:
            return False  # 已是这个尺寸：不必再跑一遍缩放链
        self._source_size = QtCore.QSize(size)
        self._fit_factor = layout.fit_scale(
            self._source_size, self._image_display_bounds(), self._view_ratio()
        )
        self._clamp_pan()
        self._apply_zoom()
        # 尺寸**当场**落实（不等下一个事件循环）：调用方紧接着就 ``set_image`` 落图，
        # 窗口那时必须已是终态，否则图片会先按旧尺寸出现、再随 settle 跳一次
        # ——那正是本探针要消掉的那跳。``_apply_zoom`` 内部的 deferred settle 仍保留，
        # 用于解码后尺寸二次变化（如动图首帧）的情形。
        self._settle_geometry_now()
        return True

    # ---- 动图（GIF/WebP/APNG）：movie 生命周期走共享 ViewerChromeMixin ----
    def set_animated_image(self, identity_key: str, raw: bytes) -> bool:
        """把原始字节接成 ``QMovie`` 循环播放（主线程建；buffer 存成员保活）。

        movie 的创建/复用/释放归 ``ViewerChromeMixin``（``_build_movie`` / ``stop_animation``），
        本方法只做 pawchive 侧的挂载：canvas 自绘要的「当前帧缓存」在此维护，
        ``paint_payload`` 优先取 movie 当前帧，未就绪时回落到 ``self._current_image``。
        """
        if self._current_media_kind == "video":
            return False
        if not identity_key or identity_key != self._current_key:
            return False
        movie = self._build_movie(raw, str(identity_key))
        if movie is None:
            return False
        # 只在**新建** movie 时连帧回调：复用分支已连过，重复 disconnect/connect
        # 会触发 libpyside 的 "Failed to disconnect" RuntimeWarning。
        if movie is not self._animation_frame_movie:
            self._animation_frame_image = None
            self._animation_frame_seq = 0
            previous = self._animation_frame_movie
            if previous is not None:
                try:
                    previous.frameChanged.disconnect(self._on_animation_frame)
                except (RuntimeError, TypeError):
                    pass
            movie.frameChanged.connect(self._on_animation_frame)
            self._animation_frame_movie = movie
        # 先落第一帧（jumpToFrame 已在共享层做过）再 start：start() 首帧要走一次事件
        # 循环，期间画布会是空的（用户会读成"点开没图"）。
        self._on_animation_frame()
        # 静态首帧已落/动画已上表：非升级窗时停进度条（升级窗由控制器收口）。
        if not self._image_upgrading:
            self.image_loading_indicator.setActive(False)
        # 浮窗已关（回包晚到）：不要在后台上表解码，等 showEvent 再起。
        if self.isVisible():
            movie.start()
        return True

    def has_animation(self) -> bool:
        return self._animation_movie is not None

    def _current_display_image(self) -> t.Optional[QImage]:
        """绘制源头：动图当前帧优先，否则静态图。"""
        frame = self._animation_frame_image
        if self._animation_movie is not None and frame is not None and not frame.isNull():
            return frame
        return self._current_image

    def _on_animation_frame(self, _frame_number: int = 0) -> None:
        movie = self._animation_movie
        if movie is None:
            return
        try:
            pixmap = movie.currentPixmap()
        except RuntimeError:  # C++ 侧已销毁
            return
        if pixmap.isNull():
            return
        image = pixmap.toImage()
        if image.isNull():
            return
        self._animation_frame_image = image
        self._animation_frame_seq += 1
        self._current_image = image
        size = QtCore.QSize(image.width(), image.height())
        self._loaded_pixmap_size = size
        if self._source_size != size:
            # 首帧：按真实尺寸重算 fit / 画布（后续帧同尺寸，不重复跑缩放链）。
            self._source_size = size
            self._fit_factor = layout.fit_scale(
                size, self._image_display_bounds(), self._view_ratio()
            )
            self.image_hint_label.hide()
            self.image_loading_indicator.setActive(False)
            self._update_meta_label()
            self._clamp_pan()
            self._apply_zoom()
            return
        self.image_hint_label.hide()
        self.image_loading_indicator.setActive(False)
        self.canvas.update()

    def _stop_animation(self) -> None:
        """停表 + 释放 movie/buffer：共享实现 + 清本站帧缓存。

        movie/buffer 的释放归 ``ViewerChromeMixin.stop_animation``（先 movie 后 buffer）；
        这里只额外清 pawchive canvas 自绘用的当前帧缓存。
        """
        previous = self._animation_frame_movie
        if previous is not None:
            try:
                previous.frameChanged.disconnect(self._on_animation_frame)
            except (RuntimeError, TypeError):
                pass
        self._animation_frame_movie = None
        self._animation_frame_image = None
        self._animation_frame_seq = 0
        super().stop_animation()

    def show_error_infobar(self, text: str, *, duration: int = 4000):
        """错误反馈统一走 ``ViewerChromeMixin.show_error_infobar``（唯一实现）。"""
        return super().show_error_infobar(text, duration=duration)

    def set_placeholder(self, identity_key: str, text: str) -> None:
        """占位/失败/仅可下载：清像素、留文案，画布尺寸不变（不塌）。"""
        if not identity_key or identity_key != self._current_key:
            return
        if self._current_media_kind == "video":
            # 视频项的像素回包/取图失败不归画布管：源已交给 QtMultimedia 时静默，
            # 没有源（无 URL）才把文案落到视频提示位。
            if not self._video_item_key:
                self.video_mgr.set_hint(str(text or ""), visible=bool(text))
            return
        self._stop_animation()
        self._current_image = None
        self.canvas.update()
        self.show_status_hint(text)

    def set_loading(self, active: bool) -> None:
        """图像加载态：显示/停止居中进度条（R6：不再用文字 label）。

        视频的缓冲提示仍归 video_mgr（它不是图像静态区，且带原因文字）。
        """
        if self._current_media_kind == "video":
            self.video_mgr.set_hint(
                _tr_or("pawchive.viewer.loading", "Loading…") if active else "",
                visible=bool(active),
            )
            return
        self.image_loading_indicator.setActive(bool(active))
        if active:
            self.image_hint_label.hide()

    def set_upgrading(self, active: bool) -> None:
        """两阶段动图（缩略图静态帧 → 原图动画）的过渡指示。

        控制器在发出缩略图快取后置 True：即使画布已有静态首帧，进度条继续转，
        直到原图落地（:meth:`set_animated_image` / :meth:`set_image` 终态）或失败
        时由控制器置 False。视频面没有图像过渡，直接忽略。
        """
        if self._current_media_kind == "video":
            return
        self._image_upgrading = bool(active)
        if active:
            self.image_loading_indicator.setActive(True)
        else:
            self.image_loading_indicator.setActive(False)

    def show_status_hint(self, text: str) -> None:
        """状态/错误反馈：加载中→进度条，其余→InfoBar（不在图像区留文字 label）。"""
        text = str(text or "")
        loading_text = _tr_or("pawchive.viewer.loading", "Loading…")
        if text and text == loading_text:
            self.set_loading(True)
            return
        self.set_loading(False)
        if self._current_media_kind == "video":
            if text:
                self.video_mgr.set_hint(text, visible=True)
            return
        if text:
            self.show_error_infobar(text)

    def show_blocked_hint(self) -> None:
        """403：走 viewer 自己的 InfoBar（parent=self），不在图像区留文字。"""
        text = _tr_or("pawchive.error.blocked", "Request blocked")
        if self._current_media_kind == "video":
            self.video_mgr.set_hint(text, visible=True)
            return
        self.set_loading(False)
        self.show_error_infobar(text)

    # ---- 状态：导航 / 下载 ----
    def set_navigation_enabled(self, has_previous: bool, has_next: bool) -> None:
        self.previous_btn.setEnabled(bool(has_previous))
        self.next_btn.setEnabled(bool(has_next))

    def set_download_pending(self, pending: bool) -> None:
        self._download_pending = bool(pending)
        self._update_download_button()

    def set_download_state(self, downloaded: bool) -> None:
        self._already_downloaded = bool(downloaded)
        self._download_pending = False
        self._update_download_button()

    def _update_download_button(self) -> None:
        enabled = (
            bool(self._download_available)
            and not self._already_downloaded
            and not self._download_pending
        )
        self.download_btn.setEnabled(enabled)
        if self._already_downloaded:
            self.download_btn.setToolTip(_tr_or("pawchive.viewer.downloaded", "Downloaded"))
        elif self._download_pending:
            self.download_btn.setToolTip(_tr_or("pawchive.card.downloading", "Downloading"))
        elif not self._download_available:
            self.download_btn.setToolTip(_tr_or("pawchive.download.no_url", "No URL"))
        else:
            self.download_btn.setToolTip(_tr_or("pawchive.viewer.download", "Download"))

    def _emit_download(self) -> None:
        if self._item is not None:
            self.download_requested.emit(self._item)

    # ---- 缓存 ----
    def cache_pixmap(self, identity_key: str, pixmap: QPixmap) -> None:
        """预取落缓存（不显示）。"""
        self._cache_pixmap(identity_key, pixmap)

    def cached_pixmap(self, identity_key: str) -> t.Optional[QPixmap]:
        pixmap = self._pixmap_cache.get(identity_key)
        if pixmap is None:
            return None
        self._pixmap_cache.move_to_end(identity_key)
        return pixmap

    def drop_pixmap(self, identity_key: str) -> None:
        """丢弃某项的已落图（热切换档位时按新档重取，旧源字节不能再复用）。"""
        if identity_key:
            self._pixmap_cache.pop(identity_key, None)

    def seed_pixmap(self, identity_key: str, pixmap: QPixmap) -> bool:
        """0 网络首帧：把网格里已有的缩略图先贴上，再等后台 preview/original 替换。

        与 ``set_image`` 的差异是**只做显示、不做尺寸结算**：不写 ``_pixmap_cache``
        （终态图到来后由 ``_cache_pixmap`` 落库）、不动 ``_source_size``/``_fit_factor``
        （否则窗口会先缩成 208px 网格尺寸再弹回）、不停 loading indicator（两阶段
        还在进行）。``QPixmap → QImage`` 在主线程做，worker 纪律不破。
        """
        if self._current_media_kind == "video":
            return False
        if not identity_key or identity_key != self._current_key:
            return False
        if pixmap is None or pixmap.isNull():
            return False
        self._stop_animation()
        self._current_image = pixmap.toImage()
        self.image_hint_label.hide()
        self.canvas.update()
        return True

    def _cache_pixmap(self, identity_key: str, pixmap: QPixmap) -> None:
        if not identity_key or pixmap is None or pixmap.isNull():
            return
        self._pixmap_cache[identity_key] = QPixmap(pixmap)
        self._pixmap_cache.move_to_end(identity_key)
        while len(self._pixmap_cache) > _PIXMAP_CACHE_LIMIT:
            self._pixmap_cache.popitem(last=False)

    # ---- 信息栏（07 §2.5） ----
    def _apply_item_labels(self, item: PawchiveViewerItem, author_name: str) -> None:
        # 键值只允许裸占位符：1-based 在喂值处算（模板里做算术会在运行时 KeyError）。
        self.position_label.setText(
            _tr_or("pawchive.viewer.position", "{index} / {total}").format(
                index=item.index + 1, total=item.total
            )
        )
        name = str(getattr(item.attachment, "name", "") or "")
        self.filename_label.set_full_text(name)
        self.author_label.setText(self._author_text(item, author_name))
        self.meta_label.setText("")
        self.meta_label.setVisible(False)

    @staticmethod
    def _author_text(item: PawchiveViewerItem, author_name: str) -> str:
        """作者显示名：tab 注入优先 → ``creator_name`` → ``creator_id``（不查库）。"""
        for candidate in (
            author_name,
            getattr(item.post, "creator_name", ""),
            getattr(item.post, "creator_id", ""),
        ):
            text = str(candidate or "").strip()
            if text:
                return text
        return ""

    def _update_meta_label(self) -> None:
        """尺寸只取解码后 pixmap；未解码整行隐藏；**体积永不显示**。"""
        size = self._loaded_pixmap_size
        if self._item is None or size is None or size.width() <= 0 or size.height() <= 0:
            self.meta_label.setVisible(False)
            return
        published = str(getattr(self._item.post, "published", "") or "")[:10]
        service = str(getattr(self._item.post, "service", "") or "")
        self.meta_label.setText(
            _tr_or("pawchive.viewer.meta", "{service} · {published} · {width}×{height}").format(
                service=service,
                published=published,
                width=size.width(),
                height=size.height(),
            )
        )
        self.meta_label.setVisible(True)

    # ---- 缩放状态（07 §2.4.2） ----
    def _effective_factor(self) -> float:
        base = self._fit_factor if self._zoom_factor is None else self._zoom_factor
        return max(self._fit_factor, min(_ZOOM_MAX, base))

    def _zoom_at_fit(self) -> bool:
        return self._effective_factor() <= self._fit_factor + _ZOOM_EPS

    def _is_actual_pixels(self) -> bool:
        return abs(self._effective_factor() - 1.0) <= _ZOOM_EPS

    def _reset_zoom(self) -> None:
        self._zoom_factor = None
        self._pan = QtCore.QPointF(0.0, 0.0)

    def _toggle_zoom(self) -> None:
        """双击 fit ↔ 100%（锚点固定画布中心；小图两态同一则 no-op，不抖动）。"""
        if self._current_media_kind == "video" or self._source_size is None:
            return
        if abs(self._fit_factor - 1.0) <= _ZOOM_EPS:
            return
        if self._is_actual_pixels():
            self._zoom_factor = None
        else:
            self._zoom_factor = 1.0
        self._pan = QtCore.QPointF(0.0, 0.0)
        self._clamp_pan()
        self._apply_zoom()

    def _zoom_at_cursor(self, delta: float, cursor: QtCore.QPointF) -> bool:
        """Ctrl+滚轮：光标下的图像点在缩放前后落在同一屏幕点。"""
        if self._current_media_kind == "video":
            return False
        if self._source_size is None or not delta:
            return False
        old = self._effective_factor()
        new = max(self._fit_factor, min(_ZOOM_MAX, old * (_ZOOM_STEP ** (delta / 120.0))))
        if abs(new - old) <= _ZOOM_EPS:
            return False
        img_pt = (QtCore.QPointF(cursor) + self._pan) / old
        self._zoom_factor = new
        self._pan = img_pt * new - QtCore.QPointF(cursor)
        self._clamp_pan()
        self._apply_zoom(QtCore.QPointF(cursor))
        return True

    def _on_canvas_pan_delta(self, delta: QtCore.QPointF) -> None:
        if self._current_media_kind == "video" or self._zoom_at_fit():
            return
        self._pan = self._pan - delta
        self._clamp_pan()
        self.canvas.update()

    def _clamp_pan(self) -> None:
        f = self._effective_factor()
        if self._source_size is None or self._zoom_at_fit():
            self._pan = QtCore.QPointF(0.0, 0.0)
            return
        canvas = self._canvas_size()
        max_x = max(0.0, self._source_size.width() * f - canvas.width())
        max_y = max(0.0, self._source_size.height() * f - canvas.height())
        self._pan = QtCore.QPointF(
            min(max(self._pan.x(), 0.0), max_x),
            min(max(self._pan.y(), 0.0), max_y),
        )

    # ---- 画布几何与绘制 ----
    def _canvas_size(self) -> QtCore.QSize:
        """画布尺寸：全部走共享实现，本站不算 sqrt/int/ceil。

        - 源图未知：``layout.default_canvas_size``（中性比例，夹进 media bounds）。
        - fit 态：基类 ``self._viewer_fit_size(source)`` → 与 danbooru 的
          ``display_size`` 逐像素相同的**整数结果**；不用 factor 反投影
          （``round(x.5)`` + 浮点在临界点会差 1px，那是"猜不透"的 1px 来源之一）。
        - 放大态：``layout.zoom_canvas_size``（向上取整后夹 bounds，多 1px 由裁剪吃掉）。
        """
        bounds = self._image_display_bounds()
        source = self._source_size
        if source is None:
            return layout.default_canvas_size(bounds)
        if self._zoom_at_fit():
            return self._viewer_fit_size(source)
        return layout.zoom_canvas_size(source, self._effective_factor(), bounds)

    def paint_payload(self, size: QtCore.QSize) -> t.Optional[QImage]:
        """画布请求的「已缩到画布尺寸」的图（裁源图 → 缩放，带 3 项缓存）。

        动图活跃时源图换成 movie 当前帧；缓存键带上帧序号，避免上一帧的缩放结果
        被当成当前帧画出来。
        """
        image = self._current_display_image()
        if image is None or image.isNull() or self._source_size is None:
            return None
        width = max(1, size.width())
        height = max(1, size.height())
        f = self._effective_factor()
        key = (
            self._current_key,
            width,
            height,
            round(f, 3),
            int(self._pan.x()),
            int(self._pan.y()),
            self._animation_frame_seq,
        )
        cached = self._scaled_cache.get(key)
        if cached is not None:
            self._scaled_cache.move_to_end(key)
            return cached
        left = self._pan.x() / f
        top = self._pan.y() / f
        source = QtCore.QRectF(left, top, width / f, height / f).intersected(
            QtCore.QRectF(
                0.0, 0.0, float(self._source_size.width()), float(self._source_size.height())
            )
        )
        if source.width() <= 0.0 or source.height() <= 0.0:
            return None
        crop = image.copy(source.toRect())
        if crop.isNull():
            return None
        scaled = crop.scaled(
            width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
        )
        self._scaled_cache[key] = scaled
        while len(self._scaled_cache) > _SCALED_CACHE_LIMIT:
            self._scaled_cache.popitem(last=False)
        return scaled

    def _column_width_for(self, content_width: int) -> int:
        """承载列宽：不窄于顶栏、不宽出 media 可用区（共享 ``layout.column_width``）。"""
        return layout.column_width(
            content_width,
            self._top_bar_min_width(),
            self._image_display_bounds().width(),
        )

    def _apply_canvas_size(self, size: QtCore.QSize):
        self.canvas.setFixedSize(size)
        if self._video_backend_ready:
            self.video_mgr.set_display_size(size)
        self.view_column.setFixedWidth(self._column_width_for(size.width()))
        self._sync_hint_geometry()

    def _canvas_origin(self, size: QtCore.QSize) -> QtCore.QPointF:
        """画布在其列内的原点（解析式，不等布局跑完）：水平居中、顶部对齐。"""
        return QtCore.QPointF((self._column_width_for(size.width()) - size.width()) / 2.0, 0.0)

    def _apply_zoom(self, anchor: t.Optional[QtCore.QPointF] = None):
        """套用 zoom：改画布尺寸 → 修正原点位移 → 夹紧 → 重绘。

        ``anchor`` 非空时按「画布原点位移」补偿 ``_pan``：画布水平居中、顶部对齐，
        尺寸一变原点就动；不补偿的话光标锚点在低倍档（画布还在随内容增长）会漂。
        补偿量 = ``origin_new - origin_old``，补偿后 ``img_pt*new - cursor`` 的锚点
        不变式在画布坐标里成立（越界夹紧时按夹紧后位置为准）。
        """
        size = self._canvas_size()
        old_size = self.canvas.size()
        changed = size != old_size
        if anchor is not None and changed:
            delta = self._canvas_origin(size) - self._canvas_origin(old_size)
            self._pan = self._pan + delta
        self._apply_canvas_size(size)
        self._clamp_pan()
        self.canvas.update()
        # 居中统一交给 settle：此刻布局还没重算，直接 ``_reposition_viewer()`` 会用
        # 旧尺寸算窗口位置（见 ``_settle_geometry_now`` 的说明）。
        self._schedule_geometry_settle()

    def _settle_geometry_now(self) -> None:
        """把布局按当前 fixed 尺寸落实后居中 —— **唯一的窗口居中时机**。

        两步缺一不可：1) activate()：setFixedWidth/setFixedSize 只把布局标脏，sizeHint()
        仍是旧值，不 activate 窗口会保留"见过的最宽/最高尺寸"；2) _reposition_viewer()：
        共享实现要在 adjustSize() 之后才读 width/height，所以必须先让 hint 追上 fixed 尺寸。
        """
        self.main_layout.activate()
        self.outer_layout.activate()
        self._reposition_viewer()

    def _schedule_geometry_settle(self) -> None:
        """布局落定后（下一个事件循环）再做一次尺寸落实 + 居中。

        _apply_canvas_size 之后立刻读 sizeHint() 只能读到旧值，Qt 又只被动扩窗、从不主动缩窗，
        于是窗口会停在"见过的最大尺寸"上。settle 用代数守卫防串项：换项即作废。
        """
        if self._geometry_settle_scheduled:
            return
        self._geometry_settle_scheduled = True
        revision = self._geometry_revision

        def _settle() -> None:
            self._geometry_settle_scheduled = False
            if revision != self._geometry_revision:
                return  # 期间换过项：这一遍作废
            self._apply_canvas_size(self._canvas_size())
            self._settle_geometry_now()

        safe_single_shot(0, _settle)

    def _sync_hint_geometry(self):
        self.image_hint_label.setGeometry(0, 0, self.canvas.width(), self.canvas.height())
        # 进度条居中：固定宽度、高度取内容，位置在画布正中。
        ind = self.image_loading_indicator
        ind.adjustSize()
        cw, ch = self.canvas.width(), self.canvas.height()
        ind.setGeometry((cw - ind.width()) // 2, (ch - ind.height()) // 2,
                        ind.width(), ind.height())
        if self._video_backend_ready:
            self.video_mgr.sync_hint_geometry()

    # ---- 窗口：置顶 / 居中 / 移窗 ----
    def keep_top_hint(self, _flag: bool = None):
        flag = _flag if _flag is not None else self.topHintBox.isChecked()
        self._keep_on_top = bool(flag)
        self.topHintBox.setChecked(self._keep_on_top)
        geometry = self.geometry()
        was_visible = self.isVisible()
        self._suppress_closed = True
        try:
            self.setWindowFlags(self._window_flags(self._keep_on_top))
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_NoSystemBackground, True)
            if was_visible:
                self.show()
                if geometry.isValid():
                    self.setGeometry(geometry)
        finally:
            self._suppress_closed = False

    def _handle_extra_event_filter(self, obj, event) -> bool:
        """视频画面左键 = 播放/暂停（站点特有分支；壳层行为归基底）。"""
        if (
            self._current_media_kind == "video"
            and self._video_backend_ready
            and event.type() == QtCore.QEvent.MouseButtonPress
            and event.button() == Qt.LeftButton
            and self.video_mgr.is_click_target(obj)
        ):
            self.toggle_playback()
            return True
        return False

    # ---- 基底钩子 ----
    def wheel_media_widgets(self) -> list:
        """滚轮翻页只认画布/视频面；信息列滚到边界的冒泡不算翻页。"""
        widgets = [self.canvas]
        if self._video_backend_ready:
            widgets.append(self.video_mgr.frame)
        return widgets

    def _ctrl_wheel_zoom(self, delta: float, position: QtCore.QPointF) -> bool:
        """Ctrl+滚轮以光标为锚点缩放（基底只提供钩子，语义由本页决定）。"""
        return bool(self._zoom_at_cursor(delta, position))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_hint_geometry()

    def showEvent(self, event):
        super().showEvent(event)
        # 隐藏期间到货/被暂停的动图：重新显示时接着播（hideEvent 已清过表的不会进来）。
        movie = self._animation_movie
        if movie is None or not self.isVisible():
            return
        try:
            state = movie.state()
            if state == QtGui.QMovie.MovieState.Paused:
                movie.setPaused(False)
            elif state == QtGui.QMovie.MovieState.NotRunning:
                movie.start()
        except RuntimeError:  # C++ 侧已销毁
            pass

    def hideEvent(self, event):
        # 关闭/隐藏先停播并清源；再回到 fit，下次打开不继承上次的缩放与平移。
        # ``_suppress_closed`` 的内部隐藏（原生窗预热 / 置顶重设 flags 的 show-hide）
        # 不算关闭：那时停表会把正在播的动图无声掐掉。
        if not self._suppress_closed:
            self._stop_animation()
        self._stop_video_playback()
        self._reset_zoom()
        super().hideEvent(event)

    def closeEvent(self, event):
        # 浮窗是会话内复用的：这里只释放中继的 server/线程，视频面保留可再开。
        self._stop_animation()
        self.video_mgr.shutdown()
        super().closeEvent(event)


__all__ = [
    "PawchiveImageViewer",
    "PawchiveViewerItem",
    "PawchiveViewerSource",
    "PawchiveZoomCanvas",
]
