"""pawchive 缩略图加载器（P1）。

只做线程池调度 / 可见优先 / 双层 LRU / 失败分类 / 解码。网络层全部归
core.website.pawchive.api.fetch_bytes；本文件不得出现 UA 串/http 库名/版本串（PB-6）。
线程纪律（硬性）：fetcher 只在 QThreadPool 的 QRunnable 里调用；request() 是纯入队、主线程
唯一入口。worker 只解码到 QImage，绝不构造 QPixmap；QImage→QPixmap 只在主线程槽里做。
_requested/_inflight/_failed_permanent 只在主线程读写；worker 只 emit 内部信号。
失败分类走鸭子类型（getattr(exc,"kind")/status）。缓存：双层 LRU（bytes 键 path 512、QImage
键 (path,edge) 192）；换档只重裁不重取；跨 tab 不复用。
"""

import typing as t
import time
from collections import OrderedDict

from PySide6 import QtCore
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap

from app.GUI.viewer.image_pipeline import (
    decode_scaled,
    get_disk_cache,
    record_throughput,
)

BYTES_CACHE_MAX = 512
PIXMAP_CACHE_MAX = 192
DEFAULT_MAX_CONCURRENCY = 3
DISPATCH_BATCH_SIZE = 6
DISPATCH_DELAY_MS = 16

#: ``blocked`` 后的全局暂停窗口（403/429 不反复砸服务端）。
BLOCKED_PAUSE_MS = 5000

CardKey = t.Tuple[str, str]

#: 永久失败：不再入队，占位文案保持（05 §2.5 表）。
PERMANENT_KINDS = frozenset({"too_large", "decode"})
#: 可重试一次：回到队尾，二次失败转永久。
RETRYABLE_KINDS = frozenset({"timeout", "network"})


def decode_image(raw: bytes, edge: int) -> t.Optional[QImage]:
    """原始字节 → 已缩到 ``edge`` 的 ``QImage``。**线程安全**，worker 与主线程都可调。

    解码走 image_pipeline.decode_scaled：QImageReader.setScaledSize 在 read() 前设置，
    JPEG 免全尺寸解码。不传格式串（pawchive 缩略图是 image/jpeg）。动图只解第一帧；
    帧序列由主线程的 QMovie 负责。
    """
    image = decode_scaled(raw, edge)
    if image is None:
        return None
    if edge > 0 and (image.width() > edge or image.height() > edge):
        image = image.scaled(
            edge, edge, QtCore.Qt.KeepAspectRatioByExpanding, QtCore.Qt.SmoothTransformation
        )
    return image


def _make_image_reader(raw: bytes) -> t.Tuple[QImageReader, QtCore.QBuffer]:
    """``raw`` → 已打开的 ``QImageReader`` / ``QBuffer``。调用方负责关闭 buffer。

    **按格式能力判定，不按后缀名**：``supportsAnimation()`` 由解码插件回答，
    将来 pawchive 出现 WebP/APNG 动图同样能走动画链路。
    """
    buffer = QtCore.QBuffer()
    buffer.setData(QtCore.QByteArray(bytes(raw or b"")))
    if not buffer.open(QtCore.QIODevice.ReadOnly):
        return QImageReader(), buffer
    return QImageReader(buffer), buffer


def is_animated_image(raw: bytes) -> bool:
    """``raw`` 是否是多帧动画图（线程安全：只用 ``QImageReader`` + ``QBuffer``）。"""
    data = bytes(raw or b"")
    if not data:
        return False
    reader, buffer = _make_image_reader(data)
    try:
        if not reader.supportsAnimation():
            return False
        frame_count = int(reader.imageCount())
        # 有些插件对单帧文件也回 supportsAnimation=True；帧数明确为 1 时按静态处理。
        return frame_count != 1
    finally:
        reader.setDevice(None)
        buffer.close()


def animated_frame_size(raw: bytes) -> QtCore.QSize:
    """动画图的第一帧尺寸（只读头部，不解整帧）；读不出回 ``QSize()``。"""
    data = bytes(raw or b"")
    if not data:
        return QtCore.QSize()
    reader, buffer = _make_image_reader(data)
    try:
        return QtCore.QSize(reader.size())
    finally:
        reader.setDevice(None)
        buffer.close()


def probe_image_size(raw: bytes) -> QtCore.QSize:
    """**只读文件头**取图像尺寸（不整图解码），读不出回空 ``QSize()``。

    用途：详情浮窗在解码落图前先按真实尺寸定画布，消掉"占位尺寸 → fit"的跳变。
    静态图与动图同一入口（QImageReader.size() 对 GIF/WebP/APNG 返回容器首选帧尺寸）；
    释放顺序先 setDevice(None) 再 buffer.close()；任何异常/非法尺寸回空 QSize，不向上抛。
    """
    data = bytes(raw or b"")
    if not data:
        return QtCore.QSize()
    try:
        reader, buffer = _make_image_reader(data)
    except Exception:  # noqa: BLE001 - 构造 reader 失败按"探不出"处理
        return QtCore.QSize()
    try:
        size = reader.size()
    except Exception:  # noqa: BLE001 - 插件异常同样按"探不出"处理
        return QtCore.QSize()
    finally:
        try:
            reader.setDevice(None)
        except Exception:  # noqa: BLE001
            pass
        buffer.close()
    if size.width() <= 0 or size.height() <= 0:
        return QtCore.QSize()
    return QtCore.QSize(size)


class _FetchTask(QRunnable):
    """一次缩略图取字节 + 解码。只在 loader 的线程池里跑，**不碰任何共享状态**。"""

    def __init__(self, loader: "PawchivePreviewLoader", key: CardKey, url: str, edge: int):
        super().__init__()
        self._loader = loader
        self._key = key
        self._url = url
        self._edge = edge
        self.setAutoDelete(True)

    def run(self):
        loader = self._loader
        if loader is None:
            return
        cache = get_disk_cache()
        cache_key = cache.key(self._url, self._edge)
        data = cache.get(cache_key) or b""
        if not data:
            started = time.perf_counter()
            try:
                raw = loader._call_fetcher(self._url)
            except Exception as exc:  # noqa: BLE001 - 分类在主线程做，这里只搬 kind/status
                self._emit(
                    loader._worker_failed,
                    self._key,
                    str(getattr(exc, "kind", "") or "network"),
                    getattr(exc, "status", None),
                )
                return
            data = bytes(raw or b"")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if data:
                record_throughput("pawchive", len(data), elapsed_ms)
                cache.put(cache_key, data)
        image = decode_image(data, self._edge) if data else None
        if image is None:
            self._emit(loader._worker_failed, self._key, "decode", None)
            return
        # 动画判定在 worker 里做（只读头部）：主线程拿到 raw 再建 QMovie。
        animated = is_animated_image(data)
        self._emit(loader._worker_decoded, self._key, image, data, self._edge, animated)

    @staticmethod
    def _emit(signal, *args) -> None:
        """宿主可能在任务在飞时被销毁（关 tab/关窗）：emit 打在已删 C++ 对象上会抛
        ``RuntimeError: Signal source has been deleted``，此处吞掉即可（结果已无意义）。"""
        try:
            signal.emit(*args)
        except RuntimeError:
            pass


class PawchivePreviewLoader(QObject):
    """缩略图调度器：可见优先、并发 3、双层 LRU、失败分类。"""

    ready = Signal(object, object)  # (CardKey, QPixmap 已裁到 edge)
    #: 动图原始字节（与 ``ready`` 同键）：主线程据此建 ``QMovie``。``ready`` 仍会发
    #: 首帧，卡在动画未就绪/被暂停时仍有静态画面可画。**worker 绝不建 QMovie。**
    animated = Signal(object, bytes)  # (CardKey, raw)
    failed = Signal(object, str)  # (CardKey, kind)
    blocked = Signal()  # 403/429：由上层决定 infobar / 暂停派发

    #: 内部：worker → 主线程（自动排队）。**不要在 worker 里直接改成员集合**。
    _worker_decoded = Signal(object, object, object, int, bool)  # (key, QImage, raw, edge, 动图)
    _worker_failed = Signal(object, str, object)  # (key, kind, status)

    def __init__(self, fetcher: t.Callable[..., bytes], *, parent=None,
                 max_concurrency: int = DEFAULT_MAX_CONCURRENCY):
        super().__init__(parent)
        self._fetcher = fetcher
        self._edge = 208
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_concurrency)))
        self._bytes_cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._pixmap_cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._requested: set[CardKey] = set()
        self._inflight: set[CardKey] = set()
        self._failed_permanent: set[CardKey] = set()
        self._retry_used: set[CardKey] = set()
        self._queue: list[tuple[CardKey, str]] = []
        self._urls: dict[CardKey, str] = {}
        self._dispatch_timer = QtCore.QTimer(self)
        self._dispatch_timer.setSingleShot(True)
        self._dispatch_timer.setInterval(DISPATCH_DELAY_MS)
        self._dispatch_timer.timeout.connect(self._dispatch_batch)
        self._blocked_until = 0
        self._worker_decoded.connect(self._on_worker_decoded)
        self._worker_failed.connect(self._on_worker_failed)

    # ---- 对外 ----
    def request(self, keys, *, visible: t.Container[CardKey] = frozenset()) -> None:
        """主线程唯一入队口：幂等（``requested``/``inflight`` 去重），可见优先。"""
        ordered = [key for key in keys if self._should_enqueue(key)]
        if not ordered:
            return
        visible_set = set(visible or ())
        head = [key for key in ordered if key in visible_set]
        tail = [key for key in ordered if key not in visible_set]
        for key in head + tail:
            self._requested.add(key)
            self._queue.append((key, self._urls[key]))
        self._schedule_dispatch()

    def set_url(self, key: CardKey, url: t.Optional[str]) -> None:
        """登记键 → 缩略图 URL。URL 为空即不可取（无缩略图域）。"""
        if url:
            self._urls[key] = str(url)
        else:
            self._urls.pop(key, None)

    def set_edge(self, edge: int) -> None:
        """三档切换：只重裁，不重取。命中 bytes 缓存的键立刻 ready。"""
        edge = max(1, int(edge))
        if edge == self._edge:
            return
        self._edge = edge
        reissued: set[CardKey] = set()
        for path, raw in list(self._bytes_cache.items()):
            card_key = self._key_for_path(path)
            if card_key is None:
                continue
            image = decode_image(raw, edge)
            if image is None:
                continue
            pixmap = QPixmap.fromImage(image)
            self._store_pixmap(path, edge, pixmap)
            self.ready.emit(card_key, pixmap)
            reissued.add(card_key)
        missing = [k for k in self._requested if k not in reissued]
        if missing:
            for key in missing:
                self._retry_used.discard(key)
            self.request(missing)

    def has_pixmap(self, key: CardKey, edge: t.Optional[int] = None) -> bool:
        return self._pixmap_cache.get((self._path_of(key), int(edge or self._edge))) is not None

    def cached_pixmap(self, key: CardKey, edge: t.Optional[int] = None) -> t.Optional[QPixmap]:
        return self._pixmap_cache.get((self._path_of(key), int(edge or self._edge)))

    def forget(self, key: CardKey) -> None:
        """键被移除（收起/换页）：从 requested 里摘掉，避免永久去重挡住再次展开。"""
        self._requested.discard(key)

    def set_active(self, active: bool) -> None:
        """多 tab 节流闸：只有当前可见 tab 允许派发（2026-11-17 主线程卡死修正）。

        每个 tab 有自己的 loader；挂起 tab 的队列保留，重新激活时由网格侧
        ``_request_visible_previews`` 补发。正在飞的请求不取消（线程池会自己收尾），
        只是回包照样 emit——卡只收自己 tab 的信号，不会错画。
        """
        was_active = getattr(self, "_active", True)
        self._active = bool(active)
        if self._active and not was_active:
            self._schedule_dispatch()

    def _dispatch_allowed(self) -> bool:
        return getattr(self, "_active", True)

    def reload(self, key: CardKey) -> None:
        """右键「重新加载缩略图」：丢掉该路径的双层缓存 + 失败标记，再按当前档入队。

        **必须能救回永久失败**（``decode`` / ``too_large`` / ``blocked`` 都会进
        ``_failed_permanent``，之后 ``_should_enqueue`` 一律拒收）——这正是一个
        只能靠手动重试才能修复的场景。在飞中的同键请求不打断，只清状态等它回包。
        """
        if key is None:
            return
        path = self._path_of(key)
        self._bytes_cache.pop(path, None)
        for cache_key in [k for k in self._pixmap_cache if k[0] == path]:
            self._pixmap_cache.pop(cache_key, None)
        self._requested.discard(key)
        self._failed_permanent.discard(key)
        self._retry_used.discard(key)
        # 队列里可能还压着旧条目：先摘掉，避免同一键两次入队。
        self._queue = [(queued_key, url) for queued_key, url in self._queue if queued_key != key]
        if key in self._inflight:
            return
        self.request([key])

    def reset(self) -> None:
        """换作者 tab：清缓存 + 清队列（pixmap 与主题无关，但跨作者不复用）。"""
        self._queue.clear()
        self._requested.clear()
        self._inflight.clear()
        self._failed_permanent.clear()
        self._retry_used.clear()
        self._urls.clear()
        self._bytes_cache.clear()
        self._pixmap_cache.clear()
        self._dispatch_timer.stop()

    def shutdown(self) -> None:
        # 先断信号再清池：在飞的 _FetchTask 持有 self 引用，若 QThreadPool 已开始
        # 驱逐（clear/waitForDone），任务收尾时 emit 会打在已删 C++ 对象上抛
        # "Signal source has been deleted"。断开后 emit 变 no-op，线程自然退出。
        self._worker_decoded.disconnect()
        self._worker_failed.disconnect()
        self._dispatch_timer.stop()
        self._queue.clear()
        self._requested.clear()
        self._inflight.clear()
        self._pool.clear()
        self._pool.waitForDone(2000)

    # ---- 内部（全部在主线程） ----
    def _should_enqueue(self, key: CardKey) -> bool:
        if key in self._requested or key in self._inflight:
            return False
        if key in self._failed_permanent:
            return False
        if key not in self._urls:
            return False
        if self.cached_pixmap(key) is not None:
            return False
        return True

    @staticmethod
    def _path_of(key: CardKey) -> str:
        return str(key[1]) if isinstance(key, (tuple, list)) and len(key) > 1 else str(key)

    def _key_for_path(self, path: str) -> t.Optional[CardKey]:
        for key in self._requested:
            if self._path_of(key) == path:
                return key
        for key in self._urls:
            if self._path_of(key) == path:
                return key
        return None

    def _schedule_dispatch(self):
        if self._dispatch_timer.isActive():
            return
        self._dispatch_timer.start()

    def _dispatch_batch(self):
        now = QtCore.QDateTime.currentMSecsSinceEpoch()
        if now < self._blocked_until:
            self._dispatch_timer.start(max(50, int(self._blocked_until - now)))
            return
        if not self._dispatch_allowed():
            # 挂起的 tab：整批停在队列里，等 set_active(True) 再续。
            return
        sent = 0
        while self._queue and sent < DISPATCH_BATCH_SIZE:
            key, url = self._queue.pop(0)
            if key in self._inflight or key in self._failed_permanent:
                continue
            self._inflight.add(key)
            self._pool.start(_FetchTask(self, key, url, self._edge))
            sent += 1
        if self._queue:
            self._schedule_dispatch()

    def _call_fetcher(self, url: str) -> bytes:
        return self._fetcher(url)

    def _on_worker_decoded(
        self, key: CardKey, image: QImage, raw: bytes, edge: int, animated: bool = False
    ):
        self._inflight.discard(key)
        pixmap = QPixmap.fromImage(image)
        self._store_bytes(self._path_of(key), raw)
        self._store_pixmap(self._path_of(key), edge, pixmap)
        self._requested.discard(key)
        # 先发静态首帧（卡立刻有图），再发原始字节让主线程建 QMovie。
        self.ready.emit(key, pixmap)
        if animated and raw:
            self.animated.emit(key, bytes(raw))
        self._schedule_dispatch()

    def _on_worker_failed(self, key: CardKey, kind: str, status: t.Any):
        self._inflight.discard(key)
        if kind == "http" and status is not None:
            try:
                if int(status) == 404:
                    self._fail(key, "http")
                    return
            except (TypeError, ValueError):
                pass
        if kind in ("blocked", "http"):
            # 403/429：全局 infobar 由上层弹，暂停派发 5s；卡不改"已下载/可下载"判定。
            self._blocked_until = (
                QtCore.QDateTime.currentMSecsSinceEpoch() + BLOCKED_PAUSE_MS
            )
            self.blocked.emit()
            self._fail(key, "blocked")
            return
        if kind in RETRYABLE_KINDS and key not in self._retry_used:
            self._retry_used.add(key)
            url = self._urls.get(key)
            if url:
                self._queue.append((key, url))
                self._schedule_dispatch()
            return
        self._fail(key, kind)

    def _fail(self, key: CardKey, kind: str):
        self._failed_permanent.add(key)
        self._requested.discard(key)
        self.failed.emit(key, kind)
        self._schedule_dispatch()

    def _store_bytes(self, path: str, data: bytes):
        self._bytes_cache[path] = data
        self._bytes_cache.move_to_end(path)
        while len(self._bytes_cache) > BYTES_CACHE_MAX:
            self._bytes_cache.popitem(last=False)

    def _store_pixmap(self, path: str, edge: int, pixmap: QPixmap):
        cache_key = (path, int(edge))
        self._pixmap_cache[cache_key] = pixmap
        self._pixmap_cache.move_to_end(cache_key)
        while len(self._pixmap_cache) > PIXMAP_CACHE_MAX:
            self._pixmap_cache.popitem(last=False)


__all__ = [
    "BLOCKED_PAUSE_MS",
    "BYTES_CACHE_MAX",
    "DEFAULT_MAX_CONCURRENCY",
    "PIXMAP_CACHE_MAX",
    "PawchivePreviewLoader",
    "animated_frame_size",
    "decode_image",
    "is_animated_image",
    "probe_image_size",
]
