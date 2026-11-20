import time
import typing as t
from dataclasses import dataclass

from PySide6 import QtCore, QtGui
from PySide6.QtCore import QT_TRANSLATE_NOOP, qtTrId
from PySide6.QtGui import QPixmap

from core.script.image.danbooru.http import DanbooruChallengeRequired
from core.script.image.danbooru.models import DanbooruPost

from .core import capture_danbooru_request, execute_danbooru_task, post_hidden_from_grid
from .preview_loader import DanbooruPreviewLoader
from app.GUI.log import script_logger as _script_logger
from .viewer import DanbooruImageViewer
from app.GUI.viewer.image_pipeline import throughput_of

# 原图/缩略图自适应网关：任一依赖缺席都降级为既有「原图优先」路径，不让浮窗炸在 import 期。
try:
    from app.GUI.viewer.source_engine import (
        SourceProbe, choose_source, speed_of, estimate_bytes,
    )
    from app.GUI.viewer.settings import viewer_source_mode
except Exception:  # noqa: BLE001 - 依赖缺席是合法降级路径
    SourceProbe = None  # type: ignore[assignment]
    choose_source = None  # type: ignore[assignment]
    speed_of = None  # type: ignore[assignment]
    estimate_bytes = None  # type: ignore[assignment]
    viewer_source_mode = None  # type: ignore[assignment]

if t.TYPE_CHECKING:
    from .interface import DanbooruInterface


@dataclass(frozen=True, slots=True)
class _DetailRequestSpec:
    task_prefix: str
    retry_prefix: str
    challenge_placeholder: str = ""
    discard_prefetch: bool = False


_PREFETCH_REQUEST = _DetailRequestSpec(task_prefix="danbooru-detail-prefetch", retry_prefix="detail-prefetch", discard_prefetch=True)
_PREVIEW_REQUEST = _DetailRequestSpec(task_prefix="danbooru-detail-preview", retry_prefix="detail-preview", challenge_placeholder="danbooru.search.verification_required")
#: auto 档的第二段：preview 秒出后后台升原图（与 pawchive 两阶段同口径）。
_UPGRADE_REQUEST = _DetailRequestSpec(task_prefix="danbooru-detail-upgrade", retry_prefix="detail-upgrade", challenge_placeholder="danbooru.search.verification_required")
_SIZE_REQUEST = _DetailRequestSpec(task_prefix="danbooru-detail-size", retry_prefix="detail-size")

#: 猜测「动图」的扩展名（仅喂给引擎做可观测性标记，不参与播放判定）。
_ANIMATED_EXTS = frozenset({"gif", "webp", "apng"})


class _DetailVideoPreview:
    def __init__(self, controller: "DetailPreviewController"):
        self.controller = controller
        self.viewer = controller.viewer
        self.video_proxy = controller.video_proxy
        self._url_cache: dict[int, str] = {}
        self.video_proxy.challenge_detected.connect(self.handle_challenge)
        self.video_proxy.request_failed.connect(self.handle_error)
        self.video_proxy.cache_progress.connect(self.handle_cache_progress)
        self.video_proxy.cache_ready.connect(self.handle_cache_ready)

    def cached_url(self, post_id: int) -> t.Optional[str]:
        return self._url_cache.get(post_id)

    def apply(self, post_id: int, local_proxy_url: str):
        self._url_cache[post_id] = local_proxy_url
        self.viewer.set_video(post_id, local_proxy_url)
        progress = self.video_proxy.progress_for_post(post_id)
        if progress is not None:
            self.apply_progress(progress)

    def handle_challenge(self, post_id: int, challenge: DanbooruChallengeRequired):
        post = self.controller._post_for_id(post_id)
        if post is None or self.controller.current_tab_id is None:
            return
        if self.controller.matches(post_id=post_id):
            self.viewer.show_error_infobar(qtTrId("danbooru.search.verification_required"))
        tab_id = self.controller.current_tab_id
        self.controller.interface.challenge_controller.submit(
            tab_id, challenge, retry_callback=lambda current_post_id=post_id: self.retry_playback(current_post_id),
            retry_key=f"detail-video-stream:{tab_id}:{post_id}",
        )

    def handle_error(self, post_id: int, error: str):
        post = self.controller._post_for_id(post_id)
        if post is None:
            return
        self.controller.log.error(f"[Danbooru] detail video stream failed post_id={post_id}: {error}")
        if self.controller.matches(post_id=post_id):
            self.viewer.show_error_infobar(self.controller._detail_preview_error_message(post, error))

    def retry_playback(self, post_id: int):
        local_proxy_url = self._url_cache.get(post_id)
        if local_proxy_url is None:
            post = self.controller._post_for_id(post_id)
            if post is None:
                raise ValueError(f"Danbooru video retry target not found: post_id={post_id}")
            local_proxy_url = self.video_proxy.register_post(post)
            self._url_cache[post_id] = local_proxy_url
        self.viewer.set_video(post_id, local_proxy_url)

    def handle_cache_progress(self, progress):
        self.apply_progress(progress)

    def handle_cache_ready(self, post_id: int):
        progress = self.video_proxy.progress_for_post(post_id)
        if progress is not None:
            self.apply_progress(progress)

    def apply_progress(self, progress):
        if not self.controller.matches(post_id=progress.post_id):
            return
        self.viewer.video_mgr.set_cache_progress(
            cached_bytes=progress.cached_bytes, total_bytes=progress.total_bytes, cached_ratio=progress.cached_ratio,
            active_segment_index=progress.active_segment_index, complete=progress.complete, play_bar=self.viewer.playBar,
        )


class DetailPreviewController(QtCore.QObject):
    def __init__(self, interface: "DanbooruInterface", viewer: DanbooruImageViewer):
        super().__init__(interface)
        self.interface = interface
        self.log = _script_logger()
        self.viewer = viewer
        self.video_proxy = interface.video_proxy
        self._tab_id: t.Optional[str] = None
        self._pixmap_cache: dict[int, QPixmap] = {}
        self._size_cache: dict[int, QtCore.QSize] = {}
        self._prefetching_post_ids: set[int] = set()
        self._pending_next_page_tab_id: t.Optional[str] = None
        self._pending_next_page_anchor_post_id: t.Optional[int] = None
        self._pending_next_page_token: int = 0
        self._preview_loader: t.Optional[DanbooruPreviewLoader] = None
        #: 键 -> (tab_id, post, spec, tier)；tier = "preview" / "original"。
        self._detail_inflight: dict[str, tuple[str, DanbooruPost, _DetailRequestSpec, str]] = {}
        #: 已落地的最高档位（post_id -> "preview"/"original"），防止 preview 后到覆盖原图。
        self._applied_tier: dict[int, str] = {}
        #: 换项代号：翻页/换档后旧回包按代号丢弃，避免旧图落到新项。
        self._generation: int = 0
        #: 上一次换项时刻：用于快速连翻时的 300ms 防抖（正常节奏不额外等待）。
        self._last_show_at: float = 0.0
        #: 本次换项是否发生在上一项 300ms 内（快速连翻才延迟发起）。
        self._rapid_nav: bool = False
        self.video_preview = _DetailVideoPreview(self)
        # 档位热切换：设置里改 自动/开/关 后，活着的浮窗按新档重出当前图。
        try:
            from app.GUI.viewer.settings import subscribe_source_mode_changed
        except Exception:  # noqa: BLE001 - 共享层缺席是合法降级
            pass
        else:
            subscribe_source_mode_changed(self._on_source_mode_changed)

    def _on_source_mode_changed(self, _value: str = "") -> None:
        """设置侧改档后回调：丢弃当前图旧源缓存，按新档重取（无则静默）。

        视频项不随档位变；当前无 post / 非当前 tab 时不动。清掉 ``_pixmap_cache``
        里这张的缩略图缓存，再走一次正常的 ``_load_preview``——这样 ``open_viewer``
        那种「命中 post_id 缓存就直接 set_image」的短路不会再把旧缩略图顶回来
        （用户现场"改成原图再点还是缩略图"的第二个根因就是缓存没失效）。
        """
        post = self.viewer.post
        if post is None or self._tab_id is None:
            return
        if self._uses_video_preview(post):
            return
        self._pixmap_cache.pop(post.post_id, None)
        self._load_preview(self._tab_id, post)

    def bind_preview_loader(self, loader: DanbooruPreviewLoader):
        """Wire the shared preview loader into this controller's slots.

        Idempotent: safe to call multiple times, connections are made once.
        """
        if self._preview_loader is not None:
            return
        self._preview_loader = loader
        loader.ready.connect(self._on_loader_ready)
        loader.failed.connect(self._on_loader_failed)
        loader.challenge.connect(self._on_loader_challenge)

    @property
    def current_tab_id(self) -> t.Optional[str]:
        return self._tab_id

    def clear_context(self):
        self._tab_id = None
        self.cancel_page_continuation()

    def matches(self, *, post_id: t.Optional[int] = None, md5: t.Optional[str] = None) -> bool:
        viewer_post = self.viewer.post
        if viewer_post is None:
            return False
        if post_id is not None:
            return viewer_post.post_id == post_id
        if md5 is not None:
            return viewer_post.md5 == md5
        return False

    def open_viewer(self, tab_id: str, post: DanbooruPost):
        self._clear_pending_next_page()
        tab = self.interface.tabs.get(tab_id)
        card = tab.card_widgets.get(post.post_id) if tab is not None else None
        already_downloaded = card.already_downloaded if card is not None else self.interface.sql_recorder.check_dupe(post.md5)
        now = time.monotonic()
        self._rapid_nav = self._last_show_at > 0 and (now - self._last_show_at) < 0.3
        self._last_show_at = now
        self._generation += 1
        self._cancel_detail_inflight()
        self._tab_id = tab_id
        self._apply_cached_size(post)
        self.viewer.show_post(post, already_downloaded)
        self.sync_navigation()
        if self._uses_video_preview(post):
            cached_video = self.video_preview.cached_url(post.post_id)
            if cached_video is not None:
                return self.viewer.set_video(post.post_id, cached_video)
        generation = self._generation
        # 0 网络首帧：网格卡已有缩略图就先贴进图像区（不写缓存、不做尺寸结算）。
        cached_pixmap = self._pixmap_cache.get(post.post_id)
        if (cached_pixmap is None or cached_pixmap.isNull()) and card is not None and not self._uses_video_preview(post):
            seed = getattr(card, "preview_pixmap", None)
            if callable(seed):
                seed_pixmap = seed()
                if seed_pixmap is not None and not seed_pixmap.isNull():
                    self.viewer.seed_pixmap(post.post_id, seed_pixmap)
        if cached_pixmap is not None and not cached_pixmap.isNull():
            self.viewer.set_image(post.post_id, cached_pixmap)
            # 预取/上次看过的缓存可能只是 preview 档：仍按档位补一次原图升级。
            self._maybe_schedule_upgrade(tab_id, post)
            return self._preload_next(post)
        if post.is_supported and DanbooruImageViewer._post_size_hint(post) is None and tab is not None:
            self._probe_size(tab_id, post)
        # 快速连翻时延迟发起真正的取图（seed 已即时贴上，延迟只为不发请求风暴）。
        def run_if_current():
            if self._generation != generation or self.viewer.post is None or self.viewer.post.post_id != post.post_id:
                return
            self._load_preview(tab_id, post)
        if self._rapid_nav:
            QtCore.QTimer.singleShot(300, run_if_current)
        else:
            run_if_current()

    def _cancel_detail_inflight(self) -> None:
        """换项时摘掉旧的在飞 detail key。

        两处都要摘：``loader.cancel_keys`` 让同键可重新入队（清 requested/params/queue），
        控制器 ``_detail_inflight`` 弹出则让**在飞** worker 的旧回包到达时
        ``_pop_detail_inflight`` 返回 None 直接丢弃（回包无法真正中断线程，只能弃收）。
        """
        doomed = [key for key, entry in self._detail_inflight.items() if entry[2] in (_PREVIEW_REQUEST, _UPGRADE_REQUEST)]
        if not doomed:
            return
        for key in doomed:
            self._detail_inflight.pop(key, None)
        loader = self._preview_loader
        if loader is not None:
            loader.cancel_keys(doomed)

    def sync_navigation(self):
        posts = self._current_posts()
        index = self._current_post_index(posts)
        self.viewer.set_navigation_enabled(index > 0, 0 <= index < len(posts) - 1 or self._can_load_next_page_from_viewer(index, posts))

    def open_adjacent(self, step: int):
        posts = self._current_posts()
        index = self._current_post_index(posts)
        target_index = index + step
        if index < 0 or target_index < 0:
            return
        if target_index >= len(posts):
            if step > 0 and self._can_load_next_page_from_viewer(index, posts):
                self._request_next_page_from_viewer(posts[index])
            return
        assert self._tab_id is not None
        self.open_viewer(self._tab_id, posts[target_index])

    def handle_page_appended(self, tab_id: str, posts: list[DanbooruPost]):
        if not self._is_pending_next_page(tab_id):
            return
        self._clear_pending_next_page()
        # The filter may hide every card of the freshly appended page; walk to
        # the first post the grid actually shows, otherwise the viewer would
        # open a placeholder for a card the user cannot see.
        target = next((post for post in posts if not post_hidden_from_grid(post)), None)
        if target is None:
            if self._continue_past_hidden_page(tab_id, posts):
                return
            self.viewer.set_page_loading(False)
            self.viewer.set_status_hint("No more posts")
            self.sync_navigation()
            return
        self.open_viewer(tab_id, target)

    def _continue_past_hidden_page(self, tab_id: str, posts: list[DanbooruPost]) -> bool:
        """Whole appended page hidden: try one more page, bounded by has_more_results."""
        state = self.interface.tab_states.get(tab_id)
        if state is None or not posts or not state.has_more_results:
            return False
        anchor = self.viewer.post
        if anchor is None:
            return False
        self._pending_next_page_tab_id = tab_id
        self._pending_next_page_anchor_post_id = anchor.post_id
        # ``load_next_page`` calls ``begin_request()`` before dispatching, so the
        # page that lands next will carry ``request_token + 1`` (same arithmetic
        # as ``_request_next_page_from_viewer``).
        self._pending_next_page_token = state.request_token + 1
        self.viewer.set_page_loading(True, f"Loading page {state.page_cursor + 1}...")
        if self.interface.search_controller.load_next_page(tab_id):
            return True
        self._clear_pending_next_page()
        return False

    def on_viewer_hidden_post(self):
        """The hide-unpreviewable switch hid the post currently in the viewer."""
        if self.viewer.post is not None and post_hidden_from_grid(self.viewer.post):
            self.viewer.hide()

    def handle_page_load_empty(self, tab_id: str):
        if not self._is_pending_next_page(tab_id):
            return
        self._clear_pending_next_page()
        self.viewer.set_page_loading(False)
        self.viewer.set_status_hint("No more posts")
        self.sync_navigation()

    def handle_page_load_failed(self, tab_id: str, message: str):
        if not self._is_pending_next_page(tab_id):
            return
        self._clear_pending_next_page()
        self.viewer.set_page_loading(False)
        self.viewer.set_status_hint(message)
        self.sync_navigation()

    def cancel_page_continuation(self, tab_id: t.Optional[str] = None):
        if tab_id is not None and self._pending_next_page_tab_id != tab_id:
            return
        if self._pending_next_page_tab_id is None:
            return
        self._clear_pending_next_page()
        self.viewer.set_page_loading(False)
        self.viewer.set_status_hint("")
        self.sync_navigation()

    def _can_load_next_page_from_viewer(self, index: int, posts: t.Sequence[DanbooruPost]) -> bool:
        if not self._tab_id or index < 0 or index != len(posts) - 1:
            return False
        state = self.interface.tab_states.get(self._tab_id)
        return bool(state is not None and state.can_load_next_page())

    def _request_next_page_from_viewer(self, anchor: DanbooruPost):
        assert self._tab_id is not None
        state = self.interface.tab_states.get(self._tab_id)
        if state is None:
            return
        self._pending_next_page_tab_id = self._tab_id
        self._pending_next_page_anchor_post_id = anchor.post_id
        self._pending_next_page_token = state.request_token + 1
        self.viewer.set_page_loading(True, f"Loading page {state.page_cursor + 1}...")
        if self.interface.search_controller.load_next_page(self._tab_id):
            self.sync_navigation()
            return
        self._clear_pending_next_page()
        self.viewer.set_page_loading(False)
        self.sync_navigation()

    def _is_pending_next_page(self, tab_id: str) -> bool:
        if self._pending_next_page_tab_id != tab_id:
            return False
        state = self.interface.tab_states.get(tab_id)
        return bool(state is not None and state.request_token == self._pending_next_page_token)

    def _clear_pending_next_page(self):
        self._pending_next_page_tab_id = None
        self._pending_next_page_anchor_post_id = None
        self._pending_next_page_token = 0

    def _current_posts(self) -> list[DanbooruPost]:
        if not self._tab_id:
            return []
        state = self.interface.tab_states.get(self._tab_id)
        if state is None:
            return []
        # previous/next must walk exactly what the grid shows; hidden cards are
        # skipped here so the viewer cannot land on an item that has no card.
        return [post for post in state.result_list if not post_hidden_from_grid(post)]

    def _post_for_id(self, post_id: int) -> t.Optional[DanbooruPost]:
        viewer_post = self.viewer.post
        if viewer_post is not None and viewer_post.post_id == post_id:
            return viewer_post
        return next((post for post in self._current_posts() if post.post_id == post_id), None)

    def _current_post_index(self, posts: t.Sequence[DanbooruPost]) -> int:
        viewer_post = self.viewer.post
        if viewer_post is None:
            return -1
        return next((index for index, post in enumerate(posts) if post.post_id == viewer_post.post_id), -1)

    def _preload_next(self, current_post: t.Optional[DanbooruPost] = None):
        if not self._tab_id:
            return
        posts = self._current_posts()
        if not posts:
            return
        anchor = current_post or self.viewer.post
        if anchor is None:
            return
        current_index = next((index for index, post in enumerate(posts) if post.post_id == anchor.post_id), -1)
        if current_index < 0:
            return
        # 前后各 1 张都暖（原来只暖下一张；向上翻页得重下）；越界各自跳过。
        for offset in (-1, 1):
            target_index = current_index + offset
            if 0 <= target_index < len(posts):
                self._prefetch(self._tab_id, posts[target_index])

    @staticmethod
    def _detail_preview_url(post: DanbooruPost) -> t.Optional[str]:
        return post.large_file_url or post.file_url or post.preview_file_url

    @staticmethod
    def _video_preview_url(post: DanbooruPost) -> t.Optional[str]:
        return post.viewer_video_source_url or None

    @staticmethod
    def _image_preview_url(post: DanbooruPost) -> t.Optional[str]:
        if post.is_supported:
            return post.large_file_url or post.file_url or post.preview_file_url
        if post.has_renderable_preview_asset and not post.preview_asset_is_video:
            return post.preview_file_url
        return None

    @staticmethod
    def _thumbnail_url(post: DanbooruPost) -> t.Optional[str]:
        return post.preview_file_url or None

    @staticmethod
    def _original_url(post: DanbooruPost) -> t.Optional[str]:
        return post.large_file_url or post.file_url or None

    @classmethod
    def _select_preview_url(cls, post: DanbooruPost) -> t.Optional[str]:
        """按「显示清晰度」档位决定取原图还是缩略图 URL（只影响 url，不改 viewer 显示逻辑）。

        video（mp4）分支短路，不走网关；on/off 直接用对应源，auto 用引擎决策。
        目标 URL 取不到则回退到既有 ``_image_preview_url``，绝不空手。
        """
        image_url = cls._image_preview_url(post)
        if cls._uses_video_preview(post) or choose_source is None or SourceProbe is None:
            return image_url
        mode = str(viewer_source_mode()) if viewer_source_mode is not None else "auto"
        if mode == "on":
            want = "original"
        elif mode == "off":
            want = "thumbnail"
        else:
            ext = DanbooruPost.normalize_file_ext(post.file_ext)
            # auto 档下动图直接走原图：缩略图对 gif/webp 只给静态首帧，动图走缩略图
            # 必然不动（与 pawchive 同口径）。冷开无尺寸时 est=0 会被速度网关误判成
            # 缩略图，因此动图短路原图，静态图才按速度网关决策。
            if ext in _ANIMATED_EXTS:
                want = "original"
            else:
                # speed_of 只读缓存、非阻塞：测速由 interface 首次打开时丢后台完成；
                # 未测/测速中返回 -1.0 → 引擎按「速率未知」保守走缩略图。
                # 主输入是**真实图片吞吐**（EWMA），JSON 探针 speed_of 只作回退。
                bpm = throughput_of("danbooru") if throughput_of is not None else 0.0
                if bpm <= 0:
                    try:
                        bpm = float(speed_of("danbooru")) if speed_of is not None else 0.0
                    except Exception:  # noqa: BLE001
                        bpm = 0.0
                # 优先用 API 给的原图真实字节数 file_size（压缩后大小）估下载耗时；
                # 拿不到（旧缓存/字段缺失为 0）才退到像素面积 ×4 粗估。旧实现恒用
                # w*h*4（解码后 RGBA），把一张 300KB 的 jpg 读成约 4MB，auto 档于是
                # 一律误判成缩略图——正是"300KB 原图也给我用缩略图"的根因。
                try:
                    est = int(getattr(post, "file_size", 0) or 0)
                except (TypeError, ValueError):
                    est = 0
                if est <= 0:
                    size = QtCore.QSize(
                        post.image_width or post.preview_width,
                        post.image_height or post.preview_height,
                    )
                    est = int(estimate_bytes(0, size)) if estimate_bytes is not None else 0
                probe = SourceProbe(
                    bytes_per_ms=bpm,
                    estimated_bytes=est,
                    is_animated=False,
                )
                try:
                    want = str(choose_source(mode, probe))
                except Exception:  # noqa: BLE001
                    want = "original"
        if want == "thumbnail":
            return cls._thumbnail_url(post) or image_url
        return cls._original_url(post) or image_url

    @staticmethod
    def _uses_video_preview(post: DanbooruPost) -> bool:
        return post.uses_viewer_video

    @staticmethod
    def _detail_preview_error_message(post: DanbooruPost, error: str) -> str:
        first_line = (error or "").splitlines()[0].strip()
        ext = DanbooruPost.normalize_file_ext(post.file_ext)
        if DetailPreviewController._uses_video_preview(post):
            media_hint = post.viewer_video_media_ext.upper() or "VIDEO"
            if first_line:
                return qtTrId("danbooru.preview.failed_detail").format(media_hint=media_hint, first_line=first_line)
            return qtTrId("danbooru.preview.failed").format(media_hint=media_hint)
        if post.is_archive:
            if post.has_renderable_preview_asset and first_line != "no preview url":
                return qtTrId("danbooru.preview.zip_original")
            return "ZIP archive, download original"
        if not post.is_supported:
            media_hint = ext.upper() if ext else "UNKNOWN"
            return qtTrId("danbooru.preview.not_renderable").format(media_hint=media_hint)
        if not DetailPreviewController._detail_preview_url(post):
            return qtTrId("danbooru.preview.unavailable")
        if first_line == "invalid image data":
            media_hint = ext.upper() if ext else qtTrId("danbooru.preview.unknown_format")
            return qtTrId("danbooru.preview.not_an_image").format(media_hint=media_hint)
        if first_line:
            return qtTrId("danbooru.preview.failed_label_detail").format(first_line=first_line)
        return qtTrId("danbooru.preview.failed_label")

    def _apply_cached_size(self, post: DanbooruPost):
        cached_size = self._size_cache.get(post.post_id)
        if cached_size is None:
            return
        post.preview_width = cached_size.width()
        post.preview_height = cached_size.height()

    def _prefetch(self, tab_id: str, post: DanbooruPost):
        if not (post.is_supported or post.has_renderable_preview_asset) or post.post_id in self._pixmap_cache or \
            post.post_id in self._prefetching_post_ids or not self._image_preview_url(post):
            return
        # 体积上限：file_size > 8MB 的邻图跳过（防内存/带宽被预取吃掉）。
        try:
            if int(getattr(post, "file_size", 0) or 0) > 8 * 1024 * 1024:
                return
        except (TypeError, ValueError):
            pass
        self._prefetching_post_ids.add(post.post_id)
        self._execute_request(_PREFETCH_REQUEST, tab_id, post)

    def _maybe_schedule_upgrade(self, tab_id: str, post: DanbooruPost) -> None:
        """缓存命中/预取命中时，按档位补一次原图升级（否则会停在 preview 档）。

        - ``off``   → 不升级；
        - ``auto`` / ``on`` → 若尚未落地 original，则请求原图（只升不降）。
        """
        if self._uses_video_preview(post) or self._is_animated_post(post):
            return
        if self._applied_tier.get(post.post_id) == "original":
            return
        mode = str(viewer_source_mode()) if viewer_source_mode is not None else "auto"
        if mode == "off":
            return
        original_url = post.file_url or post.large_file_url or self._image_preview_url(post)
        if not original_url:
            return
        self._execute_request(_UPGRADE_REQUEST, tab_id, post, force_url=original_url, tier="original")

    def _is_animated_post(self, post: DanbooruPost) -> bool:
        if self._uses_video_preview(post):
            return False
        ext = DanbooruPost.normalize_file_ext(post.file_ext)
        return bool(post.is_supported and ext in _ANIMATED_EXTS)

    def _select_tier_sources(self, post: DanbooruPost) -> tuple[t.Optional[str], t.Optional[str], bool]:
        """两阶段选源：``(preview_url, original_url, upgrade)``。

        - ``off``  → preview 即终态（不升级）；
        - ``on``   → 直接 original，无 preview；
        - ``auto`` → preview 秒开 + 后台升 original（大图在本机 1s 内，不该一票否决停缩略）；
        - 动图/视频 → 短路原图（各自专链），不升级。
        """
        #: 缩略图档（off 档终态 / seed 兜底）
        thumb_url = self._thumbnail_url(post) or self._image_preview_url(post)
        #: 显示档（danbooru 的 large sample，比 256 缩略图清楚；作为两阶段第一段）
        display_url = post.large_file_url or thumb_url
        #: 真原图（file_url 才是动图/未缩放原文件；large 可能是静态样本）
        true_original_url = post.file_url or post.large_file_url or thumb_url
        mode = str(viewer_source_mode()) if viewer_source_mode is not None else "auto"
        if self._is_animated_post(post):
            return (true_original_url, None, False)
        if mode == "on":
            return (true_original_url, None, False)
        if mode == "off":
            return (thumb_url, None, False)
        upgrade = bool(true_original_url and display_url and true_original_url != display_url)
        return (display_url, true_original_url, upgrade)

    def _load_preview(self, tab_id: str, post: DanbooruPost):
        preview_url = self._video_preview_url(post) if self._uses_video_preview(post) else self._image_preview_url(post)
        if not preview_url:
            if self.matches(post_id=post.post_id):
                self.viewer.show_error_infobar(self._detail_preview_error_message(post, "no preview url"))
            return
        if not post.is_supported and not self._uses_video_preview(post) and not post.has_renderable_preview_asset:
            if self.matches(post_id=post.post_id):
                self.viewer.show_error_infobar(self._detail_preview_error_message(post, "non-renderable preview"))
            return
        if self._is_animated_post(post):
            self._load_animated_preview(tab_id, post)
            return
        preview_url, original_url, upgrade = self._select_tier_sources(post)
        if not preview_url:
            preview_url = self._image_preview_url(post)
        if not preview_url:
            return
        self._execute_request(_PREVIEW_REQUEST, tab_id, post, force_url=preview_url, tier="preview")
        if upgrade and original_url:
            self._execute_request(_UPGRADE_REQUEST, tab_id, post, force_url=original_url, tier="original")

    def _load_animated_preview(self, tab_id: str, post: DanbooruPost) -> None:
        """动图（gif/webp/apng）：直接取原图 raw，主线程建 QMovie（与 pawchive 同口径）。

        走 loader 的 QImage 路径只能得到静态首帧，所以这里自己取 raw 字节；
        回包在主线程调 ``viewer.set_animated_image``（B 若缺席则回退静态首帧）。
        """
        url = post.file_url or post.large_file_url or self._image_preview_url(post)
        if not url:
            return
        request_client = self.interface.request_client
        key = f"detail:{tab_id}:{post.post_id}:anim"

        def task(_url=url):
            started = time.perf_counter()
            payload = capture_danbooru_request(request_client.fetch_remote_bytes, _url)
            raw = payload.value if payload is not None else b""
            data = bytes(raw or b"") if isinstance(raw, (bytes, bytearray)) else b""
            if data:
                from app.GUI.viewer.image_pipeline import record_throughput
                record_throughput("danbooru", len(data), (time.perf_counter() - started) * 1000.0)
            return payload

        self._detail_inflight[key] = (tab_id, post, _PREVIEW_REQUEST, "original")
        execute_danbooru_task(
            self.interface.task_mgr, task,
            success_callback=lambda payload, _tab=tab_id, _post=post, _key=key: self._apply_animated_payload(_tab, _post, _key, payload),
            error_callback=lambda error, _spec=_PREVIEW_REQUEST, _post=post, _key=key: self._handle_animated_error(_key, _post, error),
            task_id=f"danbooru-detail-anim-{tab_id}-{post.post_id}",
        )

    def _apply_animated_payload(self, tab_id: str, post: DanbooruPost, key: str, payload) -> None:
        self._detail_inflight.pop(key, None)
        if payload is None:
            return
        challenge = getattr(payload, "challenge", None)
        if challenge is not None:
            self._handle_request_challenge(_PREVIEW_REQUEST, tab_id, post, challenge)
            return
        raw = getattr(payload, "value", b"")
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            self._handle_animated_error(key, post, "empty animated bytes")
            return
        data = bytes(raw)
        if self.matches(post_id=post.post_id):
            setter = getattr(self.viewer, "set_animated_image", None)
            if callable(setter) and setter(post.post_id, data):
                self._applied_tier[post.post_id] = "original"
                return
            # B 缺席 / 建 movie 失败：退回静态首帧（不让动图整张看不到）。
            from app.GUI.viewer.image_pipeline import decode_scaled
            image = decode_scaled(data, self._viewport_edge())
            if image is not None and not image.isNull():
                self._apply_preview(post.post_id, QPixmap.fromImage(image), post)

    def _handle_animated_error(self, key: str, post: DanbooruPost, error) -> None:
        self._detail_inflight.pop(key, None)
        if self.matches(post_id=post.post_id):
            self.viewer.show_error_infobar(self._detail_preview_error_message(post, str(error)))

    def _probe_size(self, tab_id: str, post: DanbooruPost):
        if not post.is_supported or not self._image_preview_url(post):
            return
        self._execute_request(_SIZE_REQUEST, tab_id, post)

    def _viewport_edge(self) -> int:
        """按当前视口算 preview 档解码边（最长边×DPR，clamp [512,2048]）。

        detail 不再 ``width=0`` 全尺寸解码——大图整图解码是主线程卡死主因之一；
        解到视口档位即可看清，放大时再升档。取不到视口（未 show）时退回 1024。
        """
        try:
            longest = max(int(self.viewer.width()), int(self.viewer.height()))
            dpr = float(self.viewer.devicePixelRatioF())
        except Exception:  # noqa: BLE001
            return 1024
        return max(512, min(int(longest * max(1.0, dpr)), 2048))

    def _execute_request(
        self,
        spec: _DetailRequestSpec,
        tab_id: str,
        post: DanbooruPost,
        *,
        force_url: t.Optional[str] = None,
        tier: str = "preview",
    ):
        if force_url is not None:
            preview_url = force_url
        elif self._uses_video_preview(post):
            preview_url = self._video_preview_url(post)
        elif spec is _SIZE_REQUEST:
            # 尺寸探针只读原图文件头（拿到的是原图尺寸，供 fit 用），不参与源档位决策。
            preview_url = self._image_preview_url(post)
        else:
            preview_url = self._select_preview_url(post)
        if not preview_url:
            raise ValueError(f"Danbooru detail request missing preview url: post_id={post.post_id}")
        if spec is not _SIZE_REQUEST and not self._uses_video_preview(post):
            loader = self._preview_loader
            if loader is None:
                # No fallback thread: an unbound loader is an interface wiring bug.
                self.log.error(f"[Danbooru] preview loader not bound, drop detail request post_id={post.post_id}")
                return
            suffix = ":orig" if spec is _UPGRADE_REQUEST else ""
            key = f"detail:{tab_id}:{post.post_id}{suffix}"
            self._detail_inflight[key] = (tab_id, post, spec, tier)
            # detail 也按视口边解码（不再 width=0 全尺寸）：大图整图解码是主线程卡死主因；
            # 预览档足够看清，放大时由 viewer 侧升档。
            enqueued = loader.request(DanbooruPreviewLoader.KIND_DETAIL, preview_url, width=self._viewport_edge(), key=key)
            if not enqueued:
                # False paths: shutdown (no further emission) or permanent-failure
                # re-emit / dedup with a still-pending duplicate (both emit for
                # this key later), so the inflight entry always gets popped by
                # the corresponding slot.
                self.log.warning(f"[Danbooru] detail preview request not enqueued post_id={post.post_id}")
                if loader._shutting_down:
                    self._detail_inflight.pop(key, None)
            return
        request_client = self.interface.request_client
        if spec is _SIZE_REQUEST:
            task = lambda url=preview_url: capture_danbooru_request(request_client.fetch_remote_image_size, url)
        else:
            task = lambda current_post=post: capture_danbooru_request(self.video_proxy.register_post, current_post)
        execute_danbooru_task(
            self.interface.task_mgr, task,
            success_callback=lambda payload, current_spec=spec, current_tab_id=tab_id, current_post=post, current_url=preview_url: (
                self._handle_request_result(current_spec, current_tab_id, current_post, current_url, payload)
            ),
            error_callback=lambda error, current_spec=spec, current_post=post: self._handle_request_error(
                current_spec, current_post, error,
            ),
            task_id=f"{spec.task_prefix}-{tab_id}-{post.post_id}",
        )

    def _handle_request_result(self, spec: _DetailRequestSpec, tab_id: str, post: DanbooruPost, preview_url: str, payload):
        if payload.challenge is not None:
            return self._handle_request_challenge(spec, tab_id, post, payload.challenge)
        if spec is _SIZE_REQUEST:
            return self._apply_preview_size(post.post_id, payload.value)
        if self._uses_video_preview(post):
            return self.video_preview.apply(post.post_id, payload.value)

    def _handle_request_challenge(self, spec: _DetailRequestSpec, tab_id: str, post: DanbooruPost, challenge: DanbooruChallengeRequired):
        if spec.discard_prefetch:
            self._prefetching_post_ids.discard(post.post_id)
        if spec.challenge_placeholder and self.matches(post_id=post.post_id):
            self.viewer.show_error_infobar(qtTrId("danbooru.search.verification_required"))
        if spec is _PREFETCH_REQUEST:
            retry_callback = lambda current_tab_id=tab_id, current_post=post: self._prefetch(current_tab_id, current_post)
        if spec is _PREVIEW_REQUEST or spec is _UPGRADE_REQUEST:
            retry_callback = lambda current_tab_id=tab_id, current_post=post: self._load_preview(current_tab_id, current_post)
        else:
            retry_callback = lambda current_tab_id=tab_id, current_post=post: self._probe_size(current_tab_id, current_post)
        self.interface.challenge_controller.submit(
            tab_id, challenge, retry_callback, retry_key=f"{spec.retry_prefix}:{tab_id}:{post.post_id}",
        )

    @staticmethod
    def _is_detail_loader_key(key) -> bool:
        # Detail keys are "detail:<tab_id>:<post_id>" strings; thumb keys are
        # (tab_id, post_id) tuples owned by DanbooruSearchController.
        return isinstance(key, str) and key.startswith("detail:")

    def _pop_detail_inflight(self, key) -> t.Optional[tuple[str, DanbooruPost, _DetailRequestSpec, str]]:
        return self._detail_inflight.pop(key, None) if self._is_detail_loader_key(key) else None

    def _on_loader_ready(self, key, image: QtGui.QImage):
        entry = self._pop_detail_inflight(key)
        if entry is None:
            return
        tab_id, post, spec, tier = entry
        # 显示路径换项后不得再把旧回包画上去；预取只暖缓存、允许后到。
        if spec in (_PREVIEW_REQUEST, _UPGRADE_REQUEST) and not self.matches(post_id=post.post_id):
            return
        # 只升不降：preview 档不得覆盖已落地的 original 档。
        if tier != "original" and self._applied_tier.get(post.post_id) == "original":
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            # The loader classifies decode failures via failed(); a null image
            # here would be an invariant break, so log it loudly.
            self.log.error(f"[Danbooru] detail preview decoded to empty image post_id={post.post_id}")
        else:
            self._apply_preview(post.post_id, pixmap, post if spec in (_PREVIEW_REQUEST, _UPGRADE_REQUEST) else None)
            if tier == "original":
                self._applied_tier[post.post_id] = "original"
            elif self._applied_tier.get(post.post_id) != "original":
                self._applied_tier[post.post_id] = "preview"
        if spec is _PREFETCH_REQUEST:
            self._prefetching_post_ids.discard(post.post_id)

    def _on_loader_failed(self, key, error):
        entry = self._pop_detail_inflight(key)
        if entry is None:
            return
        tab_id, post, spec, tier = entry
        self._handle_request_error(spec, post, error)

    def _on_loader_challenge(self, key, challenge: DanbooruChallengeRequired):
        entry = self._pop_detail_inflight(key)
        if entry is None:
            return
        tab_id, post, spec, tier = entry
        self._handle_request_challenge(spec, tab_id, post, challenge)

    def _handle_request_error(self, spec: _DetailRequestSpec, post: DanbooruPost, error: str):
        if spec is _PREFETCH_REQUEST:
            self._prefetching_post_ids.discard(post.post_id)
            self.log.warning(f"[Danbooru] detail prefetch failed post_id={post.post_id}: {error}")
            return
        if spec is _PREVIEW_REQUEST or spec is _UPGRADE_REQUEST:
            self.log.error(f"[Danbooru] detail preview failed post_id={post.post_id}: {error}")
            if self.matches(post_id=post.post_id):
                self.viewer.show_error_infobar(self._detail_preview_error_message(post, error))
            return
        self.log.warning(f"[Danbooru] detail size probe failed post_id={post.post_id}: {error}")

    def _apply_preview_size(self, post_id: int, size: t.Optional[tuple[int, int]]):
        if not size:
            return
        qsize = QtCore.QSize(*size)
        self._store_size(post_id, qsize)
        self.viewer.set_placeholder_size(post_id, qsize)

    def _apply_preview(self, post_id: int, pixmap: QPixmap, current_post: t.Optional[DanbooruPost] = None):
        if pixmap.isNull():
            return
        self._pixmap_cache[post_id] = pixmap
        self._store_size(post_id, QtCore.QSize(pixmap.width(), pixmap.height()))
        is_current_post = self.matches(post_id=post_id)
        self.viewer.set_image(post_id, pixmap)
        if is_current_post:
            self._preload_next(current_post)

    def _store_size(self, post_id: int, size: QtCore.QSize):
        cached_size = self._size_cache.get(post_id)
        if cached_size is None or (size.width() * size.height()) > (cached_size.width() * cached_size.height()):
            self._size_cache[post_id] = size
