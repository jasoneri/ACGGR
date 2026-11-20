"""视频预览本地中继的**唯一**实现（danbooru / pawchive / 未来站点共用）。

QtMultimedia 只认系统代理，不读应用的代理/DoH 配置；把附件 URL 包成
http://127.0.0.1:<port>/player/video/<token>，上游请求统一走各站点自己的 HTTP 传输。

必须只有一份：缓存 / 分段预取 / Range 解析 / HTTP 响应 / 进度事件全部在本文件；站点子类
只提供 _stream()（怎么发一个上游请求）和 key 的语义。
设计：建会话即起后台线程拉起播段（前 1/3）+ 元数据尾部窗口，再顺序补段；已读字节写进临时
文件（_cached_ranges 维护连续区间）；客户端先等缓存（wait_for_range），命中即本地读；
cache_progress / cache_ready 由独立线程节流发出。

线程纪律：QPixmap / 控件只能在主线程；本文件只碰字节、文件与 HTTP。
"""

from __future__ import annotations

import logging
import math
import secrets
import tempfile
import threading
import typing as t
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PySide6 import QtCore

from core.network.socket_errors import is_client_disconnect_error

_LOG = logging.getLogger(__name__)

PROXY_PATH_PREFIX = "/player/video/"
STREAM_TIMEOUT = 90.0
STREAM_CHUNK_SIZE = 64 * 1024
DEFAULT_CONTENT_TYPE = "application/octet-stream"
RANGE_PROBE_HEADER = "bytes=0-0"

#: 起播阈值 = 总长的 1/3；总长未知时用下面的兜底字节数。
SEGMENT_COUNT = 3
FIRST_SEGMENT_FALLBACK_BYTES = 2 * 1024 * 1024
#: 元数据尾部窗口（MP4 moov 常在文件尾）：下载它才算"可起播"。
TAIL_METADATA_WINDOW_BYTES = 512 * 1024
#: 进度线程节流间隔。
PROGRESS_TICK_MS = 120


@dataclass(frozen=True, slots=True)
class VideoCacheProgress:
    """一条视频流的缓存进度（跨站点同构）。"""

    post_id: t.Any
    cached_bytes: int
    total_bytes: int
    active_segment_index: int
    ready_to_play: bool
    complete: bool

    @property
    def cached_ratio(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return max(0.0, min(1.0, self.cached_bytes / self.total_bytes))


@dataclass(frozen=True, slots=True)
class VideoRoute:
    post_id: t.Any
    source_url: str
    cache: "VideoCacheSession"


class ClientDisconnected(Exception):
    """客户端（Qt）主动断开：静默结束请求，不算失败。"""


class RangeNotSatisfiable(Exception):
    pass


def safe_proxy_url(url: str) -> str:
    """剥 query / fragment：签名 URL 不得进日志或错误提示。"""
    parts = urlsplit(str(url or ""))
    if not parts.scheme and not parts.netloc:
        return str(url or "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def redacted_error_message(exc: BaseException, source_url: str) -> str:
    """异常文案里若带完整源 URL（含签名 query），替换成剥签名的版本。"""
    message = str(exc)
    if source_url:
        message = message.replace(str(source_url), safe_proxy_url(source_url))
    return message


class VideoCacheSession:
    """一条上游视频流：分段后台预取 + 连续区间缓存 + 进度。

    子类不继承它，而是通过 ``stream_factory`` 注入"怎么发请求"：

    ``stream_factory(url, method=..., headers=..., timeout=...)`` 必须返回一个
    ``contextmanager``，yield 已 ``raise_for_status`` 过的流式响应对象
    （``.status_code`` / ``.headers`` / ``.iter_bytes()``）。
    """

    def __init__(
        self,
        *,
        post_id: t.Any,
        source_url: str,
        stream_factory: t.Callable[..., t.ContextManager[t.Any]],
        cache_prefix: str = "video",
    ):
        self.post_id = post_id
        self.source_url = str(source_url)
        self._stream_factory = stream_factory
        self._condition = threading.Condition()
        self._closed = False
        self._complete = False
        self._error: t.Optional[BaseException] = None
        self._content_type = DEFAULT_CONTENT_TYPE
        self._total_bytes = 0
        self._cached_bytes = 0
        self._cached_ranges: list[tuple[int, int]] = []
        self._metadata_required_ranges: list[tuple[int, int]] = []
        self._ready_to_play = False
        self._last_emitted: t.Optional[VideoCacheProgress] = None
        cache_file = tempfile.NamedTemporaryFile(
            prefix=f"{cache_prefix}-{post_id}-", suffix=".cache", delete=False
        )
        self._cache_path = Path(cache_file.name)
        cache_file.close()
        self._thread = threading.Thread(
            target=self._download_loop,
            name=f"{cache_prefix.capitalize()}VideoCache-{post_id}",
            daemon=True,
        )
        self._thread.start()

    # ---- 生命周期 ----
    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._thread.join(timeout=2.0)
        try:
            self._cache_path.unlink(missing_ok=True)
        except OSError:
            _LOG.debug("failed to remove video cache file %s", self._cache_path, exc_info=True)

    # ---- 对外（主线程 / HTTP 线程都可调）----
    def progress(self) -> VideoCacheProgress:
        with self._condition:
            return self._progress_locked()

    def next_progress_event(self) -> t.Optional[VideoCacheProgress]:
        """进度变了才返回（NULL 表示本轮无变化，调用方不必发信号）。"""
        with self._condition:
            progress = self._progress_locked()
            if progress == self._last_emitted:
                return None
            self._last_emitted = progress
            return progress

    def wait_until_ready(self, *, timeout: float = 30.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._closed or self._ready_to_play or self._error is not None,
                timeout=timeout,
            )

    def wait_until_described(self, *, timeout: float = 30.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._closed
                or self._error is not None
                or self._total_bytes > 0
                or self._complete,
                timeout=timeout,
            )

    def describe(self) -> tuple[int, str]:
        with self._condition:
            if self._error is not None:
                raise self._error
            return self._total_bytes, self._content_type

    def wait_for_range(self, start: int, end: int, *, timeout: float = STREAM_TIMEOUT) -> bool:
        expected_start = max(0, int(start))
        expected_end = max(expected_start, int(end))
        with self._condition:
            return self._condition.wait_for(
                lambda: self._closed
                or self._error is not None
                or self._complete
                or self._is_range_cached_locked(expected_start, expected_end + 1),
                timeout=timeout,
            )

    def read_range(self, start: int, end: int) -> bytes:
        request_start = max(0, int(start))
        request_end = max(request_start, int(end))
        with self._condition:
            if self._error is not None:
                raise self._error
            available_end_exclusive = min(
                request_end + 1, self._covered_end_for_offset_locked(request_start)
            )
            if available_end_exclusive <= request_start:
                return b""
        with self._cache_path.open("rb") as handle:
            handle.seek(request_start)
            return handle.read(available_end_exclusive - request_start)

    # ---- 下载线程 ----
    def _download_loop(self):
        try:
            self._probe_metadata()
            with self._condition:
                total_bytes = self._total_bytes
            if total_bytes > 0:
                self._download_range_segments(total_bytes)
            else:
                self._download_full_stream()
        except BaseException as exc:  # noqa: BLE001 - 失败转成 describe/read 抛
            with self._condition:
                self._error = exc
                self._condition.notify_all()

    def _probe_metadata(self):
        try:
            with self._stream_factory(
                self.source_url, method="HEAD", headers={"Accept": "*/*"}, timeout=STREAM_TIMEOUT
            ) as response:
                self._apply_response_metadata(response)
                return
        except Exception as exc:  # noqa: BLE001 - 405/不支持 HEAD 时退回 Range 探测
            if not self._is_method_not_allowed(exc):
                raise
        self._probe_range_metadata()

    @staticmethod
    def _is_method_not_allowed(exc: BaseException) -> bool:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(exc, "status", None)
        try:
            return int(status or 0) == int(HTTPStatus.METHOD_NOT_ALLOWED)
        except (TypeError, ValueError):
            return False

    def _probe_range_metadata(self):
        with self._stream_factory(
            self.source_url,
            method="GET",
            headers={"Accept": "*/*", "Range": RANGE_PROBE_HEADER},
            timeout=STREAM_TIMEOUT,
        ) as response:
            total_bytes = self._resolve_total_bytes(response)
            if total_bytes <= 0:
                self._download_response_body(
                    response, start=0, total_bytes=0, complete_when_done=True
                )
                return
            self._apply_response_metadata(response, total_bytes=total_bytes)
            supports_ranges = int(getattr(response, "status_code", 0)) == int(
                HTTPStatus.PARTIAL_CONTENT
            )
            self._download_response_body(
                response,
                start=0,
                total_bytes=total_bytes,
                complete_when_done=not supports_ranges,
            )

    def _download_range_segments(self, total_bytes: int):
        for start, end in self._priority_ranges(total_bytes):
            with self._condition:
                if self._closed or self._complete:
                    return
                if self._is_range_cached_locked(start, end + 1):
                    continue
            with self._stream_factory(
                self.source_url,
                method="GET",
                headers={"Accept": "*/*", "Range": f"bytes={start}-{end}"},
                timeout=STREAM_TIMEOUT,
            ) as response:
                if int(getattr(response, "status_code", 0)) != int(HTTPStatus.PARTIAL_CONTENT):
                    # 上游不支持 Range：只能整段重下。
                    self._download_response_body(
                        response, start=0, total_bytes=total_bytes, complete_when_done=True
                    )
                    return
                self._apply_response_metadata(response, total_bytes=total_bytes)
                self._download_response_body(
                    response, start=start, total_bytes=total_bytes, complete_when_done=False
                )
        with self._condition:
            fully_cached = self._is_range_cached_locked(0, total_bytes)
        if fully_cached:
            self._mark_complete(total_bytes, total_bytes=total_bytes)

    def _download_full_stream(self):
        with self._stream_factory(
            self.source_url, method="GET", headers={"Accept": "*/*"}, timeout=STREAM_TIMEOUT
        ) as response:
            total_bytes = self._resolve_total_bytes(response)
            self._apply_response_metadata(response, total_bytes=total_bytes)
            self._download_response_body(
                response, start=0, total_bytes=total_bytes, complete_when_done=True
            )

    def _download_response_body(
        self, response, *, start: int, total_bytes: int, complete_when_done: bool
    ):
        offset = int(start)
        with self._cache_path.open("r+b") as handle:
            if total_bytes > 0:
                handle.truncate(total_bytes)
            for chunk in response.iter_bytes(chunk_size=STREAM_CHUNK_SIZE):
                if not chunk:
                    continue
                with self._condition:
                    if self._closed:
                        return
                chunk_start = offset
                handle.seek(offset)
                handle.write(chunk)
                handle.flush()
                offset += len(chunk)
                self._mark_cached_range(chunk_start, offset)
        if complete_when_done:
            self._mark_complete(offset, total_bytes=total_bytes)

    # ---- 状态更新（全部持锁）----
    def _apply_response_metadata(self, response, *, total_bytes: t.Optional[int] = None):
        resolved_total = (
            self._resolve_total_bytes(response) if total_bytes is None else int(total_bytes)
        )
        content_type = response.headers.get("content-type") or DEFAULT_CONTENT_TYPE
        with self._condition:
            if resolved_total > 0:
                self._total_bytes = resolved_total
                self._metadata_required_ranges = self._build_metadata_required_ranges_locked(
                    resolved_total
                )
                self._ready_to_play = self._is_ready_to_play_locked()
            self._content_type = content_type
            self._condition.notify_all()

    def _mark_cached_range(self, start: int, end_exclusive: int):
        with self._condition:
            self._add_cached_range_locked(int(start), int(end_exclusive))
            self._ready_to_play = self._is_ready_to_play_locked()
            self._condition.notify_all()

    def _mark_complete(self, offset: int, *, total_bytes: int):
        with self._condition:
            resolved_total = int(total_bytes) if int(total_bytes) > 0 else int(offset)
            if self._total_bytes <= 0 and resolved_total > 0:
                self._total_bytes = resolved_total
                self._metadata_required_ranges = self._build_metadata_required_ranges_locked(
                    self._total_bytes
                )
            if self._total_bytes > 0:
                self._complete = self._is_range_cached_locked(0, self._total_bytes)
            else:
                self._complete = True
            self._ready_to_play = self._is_ready_to_play_locked()
            self._condition.notify_all()

    # ---- 区间算法（全部持锁调用）----
    def _progress_locked(self) -> VideoCacheProgress:
        return VideoCacheProgress(
            post_id=self.post_id,
            cached_bytes=self._cached_bytes,
            total_bytes=self._total_bytes,
            active_segment_index=self._active_segment_index_locked(),
            ready_to_play=self._ready_to_play,
            complete=self._complete,
        )

    def _startup_threshold_locked(self) -> int:
        if self._total_bytes > 0:
            return max(1, math.ceil(self._total_bytes / SEGMENT_COUNT))
        return FIRST_SEGMENT_FALLBACK_BYTES

    def _active_segment_index_locked(self) -> int:
        if self._total_bytes <= 0:
            return 0
        segment_size = max(1, math.ceil(self._total_bytes / SEGMENT_COUNT))
        front_cached_bytes = self._front_cached_bytes_locked()
        if front_cached_bytes <= 0:
            return 0
        return min(SEGMENT_COUNT - 1, max(0, (front_cached_bytes - 1) // segment_size))

    def _priority_ranges(self, total_bytes: int) -> list[tuple[int, int]]:
        segment_size = max(1, math.ceil(total_bytes / SEGMENT_COUNT))
        ranges: list[tuple[int, int]] = []
        startup_end = min(total_bytes, self._startup_threshold_locked())
        if startup_end > 0:
            ranges.append((0, startup_end - 1))
        with self._condition:
            metadata_ranges = list(
                self._metadata_required_ranges
                or self._build_metadata_required_ranges_locked(total_bytes)
            )
        ranges.extend((start, end - 1) for start, end in metadata_ranges)
        for start in range(0, total_bytes, segment_size):
            end_exclusive = min(total_bytes, start + segment_size)
            ranges.append((start, end_exclusive - 1))
        return self._dedupe_ranges(ranges)

    def _add_cached_range_locked(self, start: int, end_exclusive: int):
        if end_exclusive <= start:
            return
        normalized_start = max(0, int(start))
        normalized_end = int(end_exclusive)
        if self._total_bytes > 0:
            normalized_end = min(normalized_end, self._total_bytes)
        if normalized_end <= normalized_start:
            return
        merged: list[tuple[int, int]] = []
        left = normalized_start
        right = normalized_end
        inserted = False
        for current_start, current_end in self._cached_ranges:
            if current_end < left:
                merged.append((current_start, current_end))
                continue
            if right < current_start:
                if not inserted:
                    merged.append((left, right))
                    inserted = True
                merged.append((current_start, current_end))
                continue
            left = min(left, current_start)
            right = max(right, current_end)
        if not inserted:
            merged.append((left, right))
        self._cached_ranges = merged
        self._cached_bytes = sum(
            max(0, range_end - range_start) for range_start, range_end in self._cached_ranges
        )
        if self._total_bytes > 0:
            self._cached_bytes = min(self._cached_bytes, self._total_bytes)

    def _is_range_cached_locked(self, start: int, end_exclusive: int) -> bool:
        if end_exclusive <= start:
            return True
        target_start = max(0, int(start))
        target_end = int(end_exclusive)
        for current_start, current_end in self._cached_ranges:
            if current_end <= target_start:
                continue
            if current_start > target_start:
                return False
            return current_end >= target_end
        return False

    def _covered_end_for_offset_locked(self, offset: int) -> int:
        normalized_offset = max(0, int(offset))
        for current_start, current_end in self._cached_ranges:
            if current_end <= normalized_offset:
                continue
            if current_start > normalized_offset:
                return normalized_offset
            return current_end
        return normalized_offset

    def _front_cached_bytes_locked(self) -> int:
        if not self._cached_ranges:
            return 0
        first_start, first_end = self._cached_ranges[0]
        if first_start > 0:
            return 0
        return max(0, first_end)

    def _build_metadata_required_ranges_locked(self, total_bytes: int) -> list[tuple[int, int]]:
        if total_bytes <= 0:
            return []
        threshold = self._startup_threshold_locked()
        tail_window = min(total_bytes, max(1, TAIL_METADATA_WINDOW_BYTES))
        tail_start = max(0, total_bytes - tail_window)
        if tail_start < threshold:
            tail_start = threshold
        if tail_start >= total_bytes:
            return []
        return [(tail_start, total_bytes)]

    def _is_ready_to_play_locked(self) -> bool:
        if self._total_bytes <= 0:
            return self._complete
        startup_threshold = self._startup_threshold_locked()
        if not self._is_range_cached_locked(0, startup_threshold):
            return False
        for metadata_start, metadata_end in self._metadata_required_ranges:
            if not self._is_range_cached_locked(metadata_start, metadata_end):
                return False
        return True

    @staticmethod
    def _dedupe_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        deduped: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for start, end in ranges:
            normalized_start = max(0, int(start))
            normalized_end = int(end)
            if normalized_end < normalized_start:
                continue
            key = (normalized_start, normalized_end)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped

    @staticmethod
    def _resolve_total_bytes(response) -> int:
        content_range = str(response.headers.get("content-range") or "")
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                return int(tail)
        content_length = str(response.headers.get("content-length") or "").strip()
        return int(content_length) if content_length.isdigit() else 0


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.proxy.handle_http_request(self, include_body=True)

    def do_HEAD(self):
        self.server.proxy.handle_http_request(self, include_body=False)

    def log_message(self, _format: str, *_args):
        return


class _ProxyServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], proxy: "VideoProxyBase"):
        self.proxy = proxy
        super().__init__(server_address, _ProxyHandler)


class VideoProxyBase(QtCore.QObject):
    """本地视频中继基类：一个 token 一条流，range/cache/进度全在这里。

    子类只需要：

    - ``server_version``：HTTP ``Server`` 头里的名字；
    - ``_stream(url, *, method, headers, timeout)``：返回已校验状态的流式响应
      上下文（可抛自定义异常，见 ``_fatal_exception_types``）；
    - 如需要，包装 ``register_source`` 为站点语义的方法。
    """

    #: 第一个参数是 key（站点给的主键；pawchive 直接给源 URL），第二个是错误文案。
    request_failed = QtCore.Signal(object, str)
    #: (key, 账号验证异常对象)——仅站点存在验证挑战时使用。
    challenge_detected = QtCore.Signal(object, object)
    cache_progress = QtCore.Signal(object)
    cache_ready = QtCore.Signal(object)

    #: 这些异常当作"验证挑战"上报，而不是普通失败。
    challenge_exception_types: tuple[type, ...] = ()
    server_version = "VideoProxy/1.0"
    cache_prefix = "video"

    def __init__(self, parent: t.Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._closed = False
        self._routes: dict[str, VideoRoute] = {}
        self._key_tokens: dict[t.Any, str] = {}
        self._ready_keys: set = set()
        self._server = _ProxyServer(("127.0.0.1", 0), self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"{type(self).__name__}Server",
            daemon=True,
        )
        self._progress_thread = threading.Thread(
            target=self._progress_loop,
            name=f"{type(self).__name__}Progress",
            daemon=True,
        )
        self._thread.start()
        self._progress_thread.start()

    # ---- 子类钩子 ----
    @contextmanager
    def _stream(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: t.Optional[dict] = None,
        timeout: float = STREAM_TIMEOUT,
    ):
        raise NotImplementedError("video proxy subclasses must implement _stream()")
        yield  # pragma: no cover - 保持 contextmanager 语义

    # ---- 生命周期 ----
    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            routes = list(self._routes.values())
            self._routes.clear()
            self._key_tokens.clear()
            self._ready_keys.clear()
        for route in routes:
            route.cache.close()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)
        self._progress_thread.join(timeout=2.0)

    # ---- 登记 / 释放 ----
    def register_source(self, source_url: str, key: t.Any = None) -> str:
        """登记一条源（同一 key + 同一 URL 复用会话），返回本地播放 URL。"""
        url = str(source_url or "")
        if not url:
            raise ValueError("video source url is required")
        route_key = url if key is None else key
        with self._lock:
            self._ensure_open()
            token = self._key_tokens.get(route_key)
            route = self._routes.get(token) if token is not None else None
            if route is not None and route.source_url == url:
                return self._build_local_url(token)
            if route is not None:
                route.cache.close()
                self._ready_keys.discard(route.post_id)
            if token is not None:
                self._routes.pop(token, None)
            token = secrets.token_urlsafe(24)
            cache = VideoCacheSession(
                post_id=route_key,
                source_url=url,
                stream_factory=self._stream,
                cache_prefix=self.cache_prefix,
            )
            self._key_tokens[route_key] = token
            self._routes[token] = VideoRoute(post_id=route_key, source_url=url, cache=cache)
            return self._build_local_url(token)

    def release_source(self, key: t.Any = None):
        """释放一条源（``key is None`` = 释放全部）。"""
        with self._lock:
            if key is None:
                tokens = list(self._routes.keys())
            else:
                token = self._key_tokens.get(key)
                tokens = [token] if token else []
            routes = [self._routes.pop(token, None) for token in tokens]
            for token, route in zip(tokens, routes):
                if route is None:
                    continue
                self._key_tokens.pop(route.post_id, None)
                self._ready_keys.discard(route.post_id)
        for route in routes:
            if route is not None:
                route.cache.close()

    def progress_for(self, key: t.Any) -> t.Optional[VideoCacheProgress]:
        with self._lock:
            token = self._key_tokens.get(key)
            route = self._routes.get(token) if token is not None else None
        return None if route is None else route.cache.progress()

    # ---- HTTP 入口（server 线程）----
    def handle_http_request(self, handler: _ProxyHandler, *, include_body: bool):
        route = self._route_for_path(handler.path)
        if route is None:
            self._send_error_response(handler, HTTPStatus.NOT_FOUND, "unknown video token")
            return
        try:
            if include_body:
                if not route.cache.wait_until_ready():
                    raise TimeoutError(
                        f"video cache was not ready within 30s: key={route.post_id}"
                    )
            elif not route.cache.wait_until_described():
                raise TimeoutError(
                    f"video metadata was not available within 30s: key={route.post_id}"
                )
            total_bytes, content_type = route.cache.describe()
            range_header = str(handler.headers.get("Range") or "").strip()
            try:
                start, end, status = self._resolve_response_range(range_header, total_bytes)
            except RangeNotSatisfiable as exc:
                self._send_range_not_satisfiable_response(handler, total_bytes, str(exc))
                return
            self._send_cache_response_headers(
                handler, status, start, end, total_bytes, content_type
            )
            if include_body:
                self._forward_cache_range(handler, route.cache, start, end)
        except ClientDisconnected:
            return
        except self.challenge_exception_types as exc:  # type: ignore[misc]
            self.challenge_detected.emit(route.post_id, exc)
            try:
                self._send_error_response(handler, HTTPStatus.BAD_GATEWAY, str(exc))
            except ClientDisconnected:
                return
        except OSError as exc:
            if str(exc).strip():
                self.request_failed.emit(route.post_id, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成本地 502，不能吊死 Qt
            message = redacted_error_message(exc, route.source_url)
            self.request_failed.emit(route.post_id, message)
            try:
                self._send_error_response(handler, HTTPStatus.BAD_GATEWAY, message)
            except ClientDisconnected:
                return
            _LOG.error(
                "%s request failed key=%s source_url=%s error=%s",
                type(self).__name__,
                route.post_id,
                safe_proxy_url(route.source_url),
                type(exc).__name__,
            )

    # ---- 进度线程 ----
    def _progress_loop(self):
        while True:
            with self._lock:
                if self._closed:
                    return
                routes = list(self._routes.values())
            for route in routes:
                progress = route.cache.next_progress_event()
                if progress is None:
                    continue
                self.cache_progress.emit(progress)
                if progress.ready_to_play and progress.post_id not in self._ready_keys:
                    self._ready_keys.add(progress.post_id)
                    self.cache_ready.emit(progress.post_id)
            QtCore.QThread.msleep(PROGRESS_TICK_MS)

    # ---- 工具 ----
    def _ensure_open(self):
        if self._closed:
            raise RuntimeError(f"{type(self).__name__} is already closed")

    def _build_local_url(self, token: str) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{PROXY_PATH_PREFIX}{token}"

    def _route_for_path(self, raw_path: str) -> t.Optional[VideoRoute]:
        path = urlsplit(str(raw_path or "")).path
        if not path.startswith(PROXY_PATH_PREFIX):
            return None
        token = path.removeprefix(PROXY_PATH_PREFIX).strip("/")
        if not token:
            return None
        with self._lock:
            return self._routes.get(token)

    @staticmethod
    def _resolve_response_range(range_header: str, total_bytes: int) -> tuple[int, int, HTTPStatus]:
        if total_bytes <= 0:
            return 0, 0, HTTPStatus.OK
        if not range_header:
            return 0, total_bytes - 1, HTTPStatus.OK
        if not range_header.lower().startswith("bytes="):
            raise RangeNotSatisfiable(f"unsupported range header: {range_header}")
        raw_range = range_header.split("=", 1)[1].split(",", 1)[0].strip()
        start_raw, _, end_raw = raw_range.partition("-")
        try:
            if start_raw:
                start = int(start_raw)
                end = int(end_raw) if end_raw else total_bytes - 1
            else:
                suffix_size = int(end_raw)
                start = max(0, total_bytes - suffix_size)
                end = total_bytes - 1
        except ValueError as exc:
            raise RangeNotSatisfiable(f"invalid range header: {range_header}") from exc
        if start < 0 or start >= total_bytes or end < start:
            raise RangeNotSatisfiable(f"range not satisfiable: {range_header}")
        return start, min(end, total_bytes - 1), HTTPStatus.PARTIAL_CONTENT

    # ---- 响应写出 ----
    def _send_range_not_satisfiable_response(
        self, handler: _ProxyHandler, total_bytes: int, message: str
    ):
        payload = str(message or HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE.phrase).encode(
            "utf-8", "replace"
        )
        handler.send_response(int(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE))
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Range", f"bytes */{max(0, total_bytes)}")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD":
            self._write_response_payload(handler, payload)

    def _send_error_response(self, handler: _ProxyHandler, status: HTTPStatus, message: str):
        payload = str(message or status.phrase).encode("utf-8", "replace")
        handler.send_response(int(status))
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command == "HEAD":
            return
        self._write_response_payload(handler, payload)

    def _write_response_payload(self, handler: _ProxyHandler, payload: bytes):
        try:
            handler.wfile.write(payload)
            handler.wfile.flush()
        except OSError as exc:
            if is_client_disconnect_error(exc):
                raise ClientDisconnected() from exc
            raise

    @staticmethod
    def _send_cache_response_headers(
        handler: _ProxyHandler,
        status: HTTPStatus,
        start: int,
        end: int,
        total_bytes: int,
        content_type: str,
    ):
        content_length = max(0, end - start + 1) if total_bytes > 0 else 0
        handler.send_response(int(status))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Type", content_type or DEFAULT_CONTENT_TYPE)
        handler.send_header("Content-Length", str(content_length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{total_bytes}")
        handler.send_header("Connection", "close")
        handler.end_headers()

    def _forward_cache_range(
        self, handler: _ProxyHandler, cache: VideoCacheSession, start: int, end: int
    ):
        offset = start
        while offset <= end:
            chunk_end = min(end, offset + STREAM_CHUNK_SIZE - 1)
            if not cache.wait_for_range(offset, chunk_end):
                raise TimeoutError(
                    f"video cache range wait timed out: start={offset} end={chunk_end}"
                )
            chunk = cache.read_range(offset, chunk_end)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except OSError as exc:
                if is_client_disconnect_error(exc):
                    raise ClientDisconnected() from exc
                raise
            offset += len(chunk)
        try:
            handler.wfile.flush()
        except OSError as exc:
            if is_client_disconnect_error(exc):
                raise ClientDisconnected() from exc
            raise


__all__ = [
    "ClientDisconnected",
    "DEFAULT_CONTENT_TYPE",
    "RangeNotSatisfiable",
    "VideoCacheProgress",
    "VideoCacheSession",
    "VideoProxyBase",
    "VideoRoute",
    "redacted_error_message",
    "safe_proxy_url",
]