"""站点无关的 viewer 图片管线：分层尺寸、缩放解码、真实吞吐、磁盘缓存。

本模块是纯基础设施，不 import 任何站点模块，只依赖 Qt 与 core.config（后者仅取默认缓存
目录时惰性 import）：

- tier_edge 把 thumb/preview/original 档位换算成解码目标边；
- decode_scaled 在 worker 线程按目标尺寸解码（QImageReader.setScaledSize 必须在 read() 前），
  产出 reentrant 的 QImage；
- record_throughput / throughput_of 用真实图片下载的 bytes/elapsed 做 EWMA；
- ThumbnailDiskCache 是字节级磁盘 LRU（canonical key 去掉会过期的签名参数，原子写）。

线程纪律：decode_scaled / 磁盘 IO 只允许在 QThreadPool 的 worker 里执行；本模块不构造 QPixmap。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import typing as t
from pathlib import Path
from urllib.parse import urlsplit

from PySide6 import QtCore
from PySide6.QtGui import QImage, QImageReader

logger = logging.getLogger(__name__)

ImageTier = t.Literal["thumb", "preview", "original"]

#: 磁盘缓存总预算（字节）。集中常量，便于实测后调。
SOURCE_CACHE_BUDGET_BYTES: int = 512 * 1024 * 1024

#: 预览档解码边的最小值：太小的画布不值得为它缩解码，避免糊。
_MIN_PREVIEW_EDGE = 256

#: 吞吐 EWMA 平滑系数：0.3 表示新样本占三成，兼顾"跟得上变网速"与"不抖"。
_THROUGHPUT_ALPHA = 0.3

_throughput_lock = threading.Lock()
_throughput: dict[str, float] = {}

_disk_cache_lock = threading.Lock()
_disk_cache: "t.Optional[ThumbnailDiskCache]" = None


def _dim(value: t.Any, name: str) -> int:
    attr = getattr(value, name, 0)
    try:
        return int(attr() if callable(attr) else attr)
    except (TypeError, ValueError):
        return 0


def tier_edge(
    tier: str,
    viewport: QtCore.QSize,
    *,
    dpr: float = 1.0,
    thumb_edge: int = 208,
    max_edge: int = 4096,
) -> int:
    """把档位换算成"最长边"解码目标（px）。

    - ``thumb``：网格卡尺寸（默认 208），不随视口变；
    - ``preview``：视口最长边 × DPR，clamp 到 ``[256, max_edge]``；
    - ``original``：``max_edge``（调用方可传更小的 2048 控制内存）。
    """
    if tier == "thumb":
        return max(1, int(thumb_edge))
    if tier == "original":
        return max(_MIN_PREVIEW_EDGE, int(max_edge))
    longest = max(_dim(viewport, "width"), _dim(viewport, "height"))
    edge = int(round(max(1, longest) * max(0.1, float(dpr))))
    return max(_MIN_PREVIEW_EDGE, min(edge, max(1, int(max_edge))))


def decode_scaled(raw: bytes, edge: int) -> t.Optional[QImage]:
    """原始字节 → 最长边不超过 ``edge`` 的 ``QImage``（线程安全，worker 专用）。

    setScaledSize 必须在 read() 前设置：JPEG 才会走真·缩放解码（免全尺寸解码）。不传格式串
    （站点混发 jpeg/png/webp）。坏数据返回 None，不抛异常。
    """
    data = bytes(raw or b"")
    if not data:
        return None
    target = max(0, int(edge))
    reader = QImageReader()
    buffer = QtCore.QBuffer()
    buffer.setData(data)
    if not buffer.open(QtCore.QIODevice.ReadOnly):
        return None
    try:
        reader.setDevice(buffer)
        try:
            reader.setAutoTransform(True)
        except Exception:  # noqa: BLE001 - 个别插件不支持，退回无旋转
            pass
        if target > 0:
            source_size = reader.size()
            if source_size.isValid() and source_size.width() > target:
                scaled_size = source_size.scaled(
                    QtCore.QSize(target, target), QtCore.Qt.KeepAspectRatio
                )
                if scaled_size.isValid() and scaled_size.width() > 0 and scaled_size.height() > 0:
                    reader.setScaledSize(scaled_size)
        image = reader.read()
    except Exception:  # noqa: BLE001 - 插件异常按"解不开"处理
        image = QImage()
    finally:
        try:
            reader.setDevice(None)
        except Exception:  # noqa: BLE001
            pass
        buffer.close()
    if image.isNull():
        return None
    if target > 0 and (image.width() > target or image.height() > target):
        image = image.scaled(
            target, target, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
        )
    return image


def record_throughput(site: str, nbytes: int, elapsed_ms: float) -> None:
    """记录一次**真实图片下载**的吞吐（bytes/ms），按站点做 EWMA。

    这是 auto 选源的主输入。JSON/API 往返的延迟不参与——那衡量的是 RTT，
    不是带宽（旧实现由此把 12KB JSON 的 250ms 延迟算成 48 bytes/ms）。
    """
    site = str(site or "")
    if not site:
        return
    try:
        size = int(nbytes or 0)
        elapsed = float(elapsed_ms or 0.0)
    except (TypeError, ValueError):
        return
    if size <= 0 or elapsed <= 0:
        return
    rate = size / max(elapsed, 0.001)
    with _throughput_lock:
        previous = _throughput.get(site)
        if previous is None or previous <= 0:
            _throughput[site] = rate
        else:
            _throughput[site] = previous + (rate - previous) * _THROUGHPUT_ALPHA


def throughput_of(site: str) -> float:
    """只读站点的吞吐估计（bytes/ms）；未知/失效返回 ``-1.0``。"""
    with _throughput_lock:
        rate = _throughput.get(str(site or ""), -1.0)
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return -1.0
    return rate if rate > 0 else -1.0


def canonical_url(url: str, *, attachment: t.Any = None) -> str:
    """把 URL 归一成可长期做缓存键的稳定串。

    优先用不随签名变化的业务标识（danbooru ``md5`` / pawchive ``path``）；
    否则丢弃整个 query——pawchive 的 ``temp_url`` 带 ``?e=&s=`` 会过期，
    用整条 URL 做键会让缓存永远不命中。
    """
    try:
        if attachment is not None:
            md5 = str(getattr(attachment, "md5", "") or "").strip()
            if md5:
                return f"danbooru:{md5}"
            path = str(getattr(attachment, "path", "") or "").strip()
            if path:
                return f"pawchive:{path}"
        text = str(url or "").strip()
        if not text:
            return ""
        parts = urlsplit(text)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
        return text.split("?", 1)[0].split("#", 1)[0]
    except Exception:  # noqa: BLE001 - 归一失败退回原串，不影响功能
        return str(url or "")


def _default_cache_root() -> Path:
    try:
        from core.config import conf_dir

        return Path(conf_dir) / "cache" / "img"
    except Exception:  # noqa: BLE001 - 极早期 import/配置不可用时退回仓库 __temp
        return Path.cwd() / "__temp" / "viewer_img_cache"


class ThumbnailDiskCache:
    """缩略图/预览字节的磁盘 LRU（跨进程复用，键已 canonical 化）。"""

    INDEX_NAME = "index.json"

    def __init__(self, root: t.Optional[t.Union[str, Path]] = None, *, budget_bytes: int = SOURCE_CACHE_BUDGET_BYTES):
        self._root = Path(root) if root is not None else _default_cache_root()
        self._budget_bytes = max(0, int(budget_bytes))
        self._lock = threading.RLock()
        self._index: t.Optional[dict[str, list]] = None
        self._total_bytes = 0

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def key(url: str, edge: int, *, attachment: t.Any = None) -> str:
        material = f"{canonical_url(url, attachment=attachment)}|{int(max(0, int(edge)))}"
        return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()

    def _entry_path(self, key: str) -> Path:
        return self._root / key[:2] / f"{key}.bin"

    def _load_index_locked(self) -> dict[str, list]:
        if self._index is not None:
            return self._index
        index: dict[str, list] = {}
        try:
            path = self._root / self.INDEX_NAME
            if path.is_file():
                payload = json.loads(path.read_text("utf-8"))
                if isinstance(payload, dict):
                    for raw_key, raw_value in payload.items():
                        if isinstance(raw_value, (list, tuple)) and len(raw_value) == 2:
                            index[str(raw_key)] = [int(raw_value[0]), float(raw_value[1])]
        except Exception:  # noqa: BLE001 - 索引坏了就当空缓存，不阻断取图
            index = {}
        self._index = index
        self._total_bytes = sum(max(0, int(value[0])) for value in index.values())
        return index

    def _save_index_locked(self) -> None:
        index = self._index or {}
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            path = self._root / self.INDEX_NAME
            tmp = self._root / f".{self.INDEX_NAME}.{os.getpid()}.{threading.get_ident()}.tmp"
            tmp.write_text(json.dumps(index, separators=(",", ":")), "utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001 - 索引写失败只影响淘汰精度
            logger.debug("thumbnail cache index save failed", exc_info=True)

    def _evict_locked(self) -> None:
        if self._budget_bytes <= 0 or self._total_bytes <= self._budget_bytes:
            return
        index = self._index or {}
        for key, entry in sorted(index.items(), key=lambda item: float(item[1][1])):
            if self._total_bytes <= self._budget_bytes:
                break
            size = max(0, int(entry[0]))
            try:
                self._entry_path(key).unlink(missing_ok=True)
            except OSError:
                pass
            index.pop(key, None)
            self._total_bytes = max(0, self._total_bytes - size)

    def get(self, key: str) -> t.Optional[bytes]:
        cache_key = str(key or "")
        if not cache_key:
            return None
        with self._lock:
            index = self._load_index_locked()
            entry = index.get(cache_key)
            if entry is None:
                return None
            path = self._entry_path(cache_key)
            try:
                raw = path.read_bytes()
            except OSError:
                index.pop(cache_key, None)
                self._total_bytes = max(0, self._total_bytes - max(0, int(entry[0])))
                self._save_index_locked()
                return None
            now = time.time()
            index[cache_key] = [len(raw), now]
            try:
                os.utime(path, (now, now))
            except OSError:
                pass
            return raw

    def put(self, key: str, raw: bytes) -> None:
        cache_key = str(key or "")
        data = bytes(raw or b"")
        if not cache_key or not data:
            return
        size = len(data)
        with self._lock:
            index = self._load_index_locked()
            path = self._entry_path(cache_key)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                os.replace(tmp, path)
            except OSError:
                logger.debug("thumbnail cache put failed", exc_info=True)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            previous_size = max(0, int(index.get(cache_key, [0, 0.0])[0]))
            index[cache_key] = [size, time.time()]
            self._total_bytes = max(0, self._total_bytes - previous_size) + size
            self._evict_locked()
            self._save_index_locked()

    def delete(self, key: str) -> None:
        """删除单个键（坏缓存/手动失效）。任何异常吞掉，不影响取图主路径。"""
        cache_key = str(key or "")
        if not cache_key:
            return
        with self._lock:
            index = self._load_index_locked()
            entry = index.pop(cache_key, None)
            if entry is not None:
                self._total_bytes = max(0, self._total_bytes - max(0, int(entry[0])))
            try:
                self._entry_path(cache_key).unlink(missing_ok=True)
            except OSError:
                pass
            self._save_index_locked()

    def clear(self) -> None:
        with self._lock:
            self._index = {}
            self._total_bytes = 0
            try:
                for shard in self._root.glob("*"):
                    if shard.is_dir():
                        for entry in shard.glob("*.bin"):
                            entry.unlink(missing_ok=True)
                (self._root / self.INDEX_NAME).unlink(missing_ok=True)
            except OSError:
                logger.debug("thumbnail cache clear failed", exc_info=True)


def get_disk_cache() -> ThumbnailDiskCache:
    """进程级磁盘缓存单例（root 默认取用户配置目录下的 ``cache/img``）。"""
    global _disk_cache
    with _disk_cache_lock:
        if _disk_cache is None:
            _disk_cache = ThumbnailDiskCache()
        return _disk_cache


__all__ = [
    "ImageTier",
    "SOURCE_CACHE_BUDGET_BYTES",
    "ThumbnailDiskCache",
    "canonical_url",
    "decode_scaled",
    "get_disk_cache",
    "record_throughput",
    "throughput_of",
    "tier_edge",
]
