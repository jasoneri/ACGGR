"""danbooru 预览加载器（网格缩略图 + 详情大图共用，P0 图片管线）。

职责：只做线程池调度 / 缓存 / 失败分类 / 解码；网络层全部归
``DanbooruClient.fetch_remote_bytes``（唯一下载入口），不自带请求头/UA/限速常量。
线程纪律（硬性）：``request()`` / ``cancel_keys()`` / ``cancel_tab()`` / ``shutdown()`` 是
主线程唯一入口，状态只在主线程读写；worker 只 emit 内部信号、绝不构造 ``QPixmap``。
缓存：双层 LRU（bytes 键 ``url`` 512、QImage 键 ``(url, width)`` 192），换档重解码不重下载。
失败：challenge 透传；timeout/network 按 httpx 类型重试一次；解码失败永久失败。
"""

from __future__ import annotations

import time
import typing as t
from collections import OrderedDict

import httpx
from PySide6 import QtCore, QtGui
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, qtTrId
from PySide6.QtGui import QImage, QImageReader

from app.GUI.viewer.image_pipeline import get_disk_cache, record_throughput
from core.script.image.danbooru.http import DanbooruChallengeRequired
from ui.async_task import TaskError

__all__ = ["DanbooruPreviewLoader"]

#: bytes 层上限（项），键 = url。换 width 档只触发 image 层重解码，不动这里。
BYTES_CACHE_MAX = 512
#: image 层上限（项），键 = (url, width)。
IMAGE_CACHE_MAX = 192
#: bytes 层按字节总量二次封顶：detail 大图（width=0）单张可达几十 MB，只按条数
#: 上限会把 512 × 30MB 撑到 GB 级。缩略图（≈100-300KB）远够不着这个预算，
#: 命中的实际是 detail 原图。超出时丢最旧的 bytes（image 层已解码的仍可用）。
BYTES_CACHE_TOTAL_MAX = 160 * 1024 * 1024
DEFAULT_MAX_CONCURRENCY = 4
#: 每次合并派发的最大数量与合并窗口（照 pawchive：一帧内到达的请求一次发）。
DISPATCH_BATCH_SIZE = 6
DISPATCH_DELAY_MS = 16
FETCH_TIMEOUT_SECONDS = 20.0

#: 网络层瞬时错误：值得原样再试一次（第二次仍失败才转永久）。按异常类型精确
#: 匹配 httpx 类层次，不做 getattr 鸭子猜。
RETRYABLE_EXCEPTIONS: "tuple[t.Type[BaseException], ...]" = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
)

ImageCacheKey = t.Tuple[str, int]


class _ImageDecodeError(Exception):
    """QImageReader 读出 null：数据不是有效图片或已损坏。永久失败，不可重试。"""


def decode_failed_error(detail: str = "") -> TaskError:
    """解码失败的对外口径：``str(error)`` 一句人话，细节只进 ``debug_text``。"""
    return TaskError(
        qtTrId("danbooru.preview.decode_failed"),
        hint=qtTrId("danbooru.preview.decode_failed_hint"),
        detail=str(detail or ""),
        exception_type="DecodeError",
    )


def safe_emit(signal, *args) -> None:
    """宿主可能在任务在飞时被销毁（关 tab/关窗）：emit 打在已删 C++ 对象上会抛
    ``RuntimeError: Signal source has been deleted``，吞掉即可（结果已无意义）。"""
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


def decode_scaled_image(raw: bytes, width: int) -> t.Optional[QImage]:
    """原始字节 → ``QImage``（``width>0`` 时解码期缩放到该宽，保比例、只缩不放）。

    **线程安全**（QImage reentrant）：worker 与主线程的 bytes 缓存命中路径共用。
    不传格式串：danbooru 缩略图 jpeg/png/webp 混发，写死格式会漏解码。
    ``setScaledSize`` 对 JPEG 是真·缩放解码（免全尺寸解码）；对不支持缩放解码
    的格式 Qt 会在读出后自行补缩放，EXIF 旋转/取整口径差则由读出后的
    ``scaledToWidth`` 兜底一次。原图不宽于目标档时不放大（放大只会糊 + 费内存）。
    """
    data = bytes(raw or b"")
    if not data:
        return None
    target_width = max(0, int(width))
    reader = QImageReader()
    source_buffer = QtCore.QBuffer()
    source_buffer.setData(data)
    source_buffer.open(QtCore.QIODevice.ReadOnly)
    reader.setDevice(source_buffer)
    if target_width > 0:
        source_size = reader.size()
        if source_size.isValid() and source_size.width() > target_width:
            scaled_height = max(1, round(source_size.height() * target_width / source_size.width()))
            reader.setScaledSize(QtCore.QSize(target_width, scaled_height))
    image = reader.read()
    if image.isNull():
        return None
    if target_width > 0 and image.width() > target_width:
        image = image.scaledToWidth(target_width, QtCore.Qt.SmoothTransformation)
    return image


def _key_belongs_to_tab(key, tab_id: str) -> bool:
    """cancel_tab 的键匹配口径：``(tab_id, ...)`` 元组，或 ``f"{tab_id}:..."`` 字符串。"""
    first = key[0] if isinstance(key, (tuple, list)) and key else None
    if first == tab_id:
        return True
    return isinstance(key, str) and key.startswith(f"{tab_id}:")


class _PreviewFetchTask(QRunnable):
    """一次「取字节 + 解码」。只在 loader 的线程池里跑，**不碰任何共享状态与控件**。"""

    def __init__(self, loader: "DanbooruPreviewLoader", key, kind: str, url: str, width: int):
        super().__init__()
        self._loader = loader
        self._key = key
        self._kind = kind
        self._url = url
        self._width = int(width)
        self.setAutoDelete(True)

    def run(self):
        loader = self._loader
        if loader is None:
            return
        cache = get_disk_cache()
        cache_key = cache.key(self._url, self._width)
        raw = cache.get(cache_key)
        if not raw:
            started = time.perf_counter()
            try:
                raw = loader._call_fetch(self._url)
            except DanbooruChallengeRequired as exc:
                safe_emit(loader._worker_challenge, self._key, exc)
                return
            except Exception as exc:  # noqa: BLE001 - 分类在主线程做，这里只搬异常对象
                safe_emit(loader._worker_failed, self._key, self._kind, exc)
                return
            raw = bytes(raw or b"")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if raw:
                record_throughput("danbooru", len(raw), elapsed_ms)
                cache.put(cache_key, raw)
        image = decode_scaled_image(raw, self._width)
        if image is None:
            safe_emit(
                loader._worker_failed,
                self._key,
                self._kind,
                _ImageDecodeError(f"url={self._url} width={self._width}"),
            )
            return
        safe_emit(loader._worker_done, self._key, self._url, self._width, image, bytes(raw))


class DanbooruPreviewLoader(QObject):
    """danbooru 网格/详情预览调度器：线程池并发、双层 LRU、失败分类。

    loader 实例归 ``DanbooruInterface``（不全局单例，生命周期与界面一致）；
    跨 tab 去重由「同 interface 一个 loader」保证——url 是内容寻址键，不同 tab
    相同 md5 自动合并。
    """

    KIND_THUMB = "thumb"
    KIND_DETAIL = "detail"

    ready = Signal(object, QtGui.QImage)  # (key, image)
    failed = Signal(object, object)  # (key, TaskError)
    challenge = Signal(object, object)  # (key, DanbooruChallengeRequired)

    #: 内部：worker → 主线程（自动排队）。**不要在 worker 里改成员集合**。
    #: ``raw`` 顺势带回给 bytes 层缓存——worker 不碰共享状态（pawchive 同款），
    #: 比单发 image 多带一份 bytes 才能支撑「换档重解码不重下载」。
    _worker_done = Signal(object, str, int, object, object)  # (key, url, width, QImage, raw)
    _worker_failed = Signal(object, str, object)  # (key, kind, exception)；kind 只是请求种类（thumb/detail），分类靠异常类型
    _worker_challenge = Signal(object, object)  # (key, DanbooruChallengeRequired)

    def __init__(self, request_client, *, parent=None, max_concurrency: int = DEFAULT_MAX_CONCURRENCY):
        super().__init__(parent)
        self._request_client = request_client
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_concurrency)))
        self._bytes_cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._image_cache: "OrderedDict[ImageCacheKey, QImage]" = OrderedDict()
        self._requested: set = set()  # 已入队（含在飞）的 key：request() 去重的依据
        self._inflight: set = set()
        self._failed_permanent: set = set()
        self._retry_used: set = set()
        self._params: dict = {}  # key -> (kind, url, width)；入队期间持有，收口即弹出
        self._queue: list = []  # 待派发 key，FIFO
        self._dispatch_timer = QtCore.QTimer(self)
        self._dispatch_timer.setSingleShot(True)
        self._dispatch_timer.setInterval(DISPATCH_DELAY_MS)
        self._dispatch_timer.timeout.connect(self._dispatch_batch)
        self._shutting_down = False
        self._worker_done.connect(self._on_worker_done)
        self._worker_failed.connect(self._on_worker_failed)
        self._worker_challenge.connect(self._on_worker_challenge)

    # ---- 对外（主线程唯一入口） ----
    def request(self, kind: str, url: str, *, width: int, key) -> bool:
        """主线程唯一入队口。

        命中 image LRU → 同轮 emit ready 并返回 True；bytes LRU 命中 → 主线程
        解码该 bytes → 存 image 层 → 同轮 ready；两者都未命中才入队派发。
        重复 key（已入队或在飞）与已永久失败的 key 返回 False，不重复下载。
        ``width>0`` 解码期缩放到该宽（保比例）；``width=0`` 原尺寸。
        """
        if self._shutting_down:
            return False
        url = str(url or "")
        if not url or key in self._requested or key in self._inflight:
            return False
        if key in self._failed_permanent:
            # 已永久失败的 key 永远不会回包：返回 False 让调用方立即收场
            # （例如 detail 控制器弹出 inflight 条目），不留悬挂状态。
            self._fail_permanent(key, decode_failed_error("previously failed"))
            return False
        width = max(0, int(width))
        image_key: ImageCacheKey = (url, width)
        cached_image = self._image_cache.get(image_key)
        if cached_image is not None:
            self._image_cache.move_to_end(image_key)
            safe_emit(self.ready, key, cached_image)
            return True
        raw = self._bytes_cache.get(url)
        if raw is not None:
            image = decode_scaled_image(raw, width)
            if image is not None:
                self._store_image(image_key, image)
                safe_emit(self.ready, key, image)
                return True
            # bytes 层命中但解不开（坏数据）：丢掉坏条目，本 key 按解码失败收口。
            # 其他 key 同 url 会走缓存未命中重新下载，不被这里连坐。
            self._bytes_cache.pop(url, None)
            self._fail_permanent(key, decode_failed_error("bytes cache hit but undecodable"))
            return False
        self._requested.add(key)
        self._params[key] = (str(kind or self.KIND_THUMB), url, width)
        self._queue.append(key)
        self._schedule_dispatch()
        return True

    def cancel_keys(self, keys) -> None:
        """从 requested/queue 摘除；在飞请求不中断，回包由接收方校验存活后丢弃。"""
        wanted = set(keys or ())
        if not wanted:
            return
        for key in wanted:
            self._requested.discard(key)
            self._params.pop(key, None)
            self._retry_used.discard(key)
        if self._queue:
            self._queue = [key for key in self._queue if key not in wanted]

    def cancel_tab(self, tab_id: str) -> None:
        """摘除该 tab 的所有 key：``(tab_id, ...)`` 元组或 ``f"{tab_id}:..."`` 字符串。"""
        tab = str(tab_id or "")
        if not tab:
            return
        doomed = [key for key in self._requested if _key_belongs_to_tab(key, tab)]
        self.cancel_keys(doomed)

    def shutdown(self) -> None:
        """先断信号再清池：在飞任务收尾时 emit 会打在已删 C++ 对象上抛
        "Signal source has been deleted"。断开后 emit 变 no-op，线程自然退出。"""
        self._shutting_down = True
        self._worker_done.disconnect()
        self._worker_failed.disconnect()
        self._worker_challenge.disconnect()
        self._dispatch_timer.stop()
        self._queue.clear()
        self._requested.clear()
        self._inflight.clear()
        self._params.clear()
        self._pool.clear()
        self._pool.waitForDone(2000)

    # ---- 内部（全部在主线程） ----
    def _call_fetch(self, url: str) -> bytes:
        return self._request_client.fetch_remote_bytes(url, timeout=FETCH_TIMEOUT_SECONDS)

    def _schedule_dispatch(self):
        if self._shutting_down or self._dispatch_timer.isActive():
            return
        self._dispatch_timer.start()

    def _dispatch_batch(self):
        sent = 0
        while self._queue and sent < DISPATCH_BATCH_SIZE:
            key = self._queue.pop(0)
            if key in self._inflight or key in self._failed_permanent or key not in self._requested:
                continue
            kind, url, width = self._params[key]
            self._inflight.add(key)
            self._pool.start(_PreviewFetchTask(self, key, kind, url, width))
            sent += 1
        if self._queue:
            self._schedule_dispatch()

    def _on_worker_done(self, key, url: str, width: int, image, raw: bytes):
        self._inflight.discard(key)
        self._requested.discard(key)
        self._params.pop(key, None)
        self._store_bytes(url, raw)
        self._store_image((url, width), image)
        safe_emit(self.ready, key, image)
        self._schedule_dispatch()

    def _on_worker_challenge(self, key, exc):
        self._inflight.discard(key)
        self._requested.discard(key)
        self._params.pop(key, None)
        # 不重试、不标永久失败：challenge 由上层既有控制器处理，验证通过后重新
        # request() 即可（此刻 key 已不在 _requested，重入队不被去重挡住）。
        safe_emit(self.challenge, key, exc)

    def _on_worker_failed(self, key, kind, exc):
        self._inflight.discard(key)
        if key not in self._requested:
            # 已被 cancel 摘除的在飞回包：不再重试/标记，接收方按存活校验丢弃。
            return
        if isinstance(exc, _ImageDecodeError):
            self._fail_permanent(key, decode_failed_error(str(exc)))
            return
        if isinstance(exc, RETRYABLE_EXCEPTIONS) and key not in self._retry_used:
            self._retry_used.add(key)
            self._queue.append(key)
            self._schedule_dispatch()
            return
        self._fail_permanent(key, TaskError.coerce(exc))

    def _fail_permanent(self, key, error):
        self._params.pop(key, None)
        self._retry_used.discard(key)
        self._requested.discard(key)
        self._failed_permanent.add(key)
        safe_emit(self.failed, key, error)
        self._schedule_dispatch()

    def _store_bytes(self, url: str, data: bytes):
        self._bytes_cache[url] = data
        self._bytes_cache.move_to_end(url)
        while len(self._bytes_cache) > BYTES_CACHE_MAX:
            self._bytes_cache.popitem(last=False)
        total_bytes = sum(len(data) for data in self._bytes_cache.values())
        while total_bytes > BYTES_CACHE_TOTAL_MAX and self._bytes_cache:
            _dropped_url, dropped_data = self._bytes_cache.popitem(last=False)
            total_bytes -= len(dropped_data)

    def _store_image(self, cache_key: ImageCacheKey, image):
        self._image_cache[cache_key] = image
        self._image_cache.move_to_end(cache_key)
        while len(self._image_cache) > IMAGE_CACHE_MAX:
            self._image_cache.popitem(last=False)
