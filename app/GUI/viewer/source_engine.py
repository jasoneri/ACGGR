"""站点无关的 viewer 原图/缩略图选型引擎（纯决策 + 进程内测速记录）。

本模块只做"该用哪种源"的决策与测速结果记录：

- **站点无关**：不 import 任何站点网络层、不解析任何站点的 URL；测速用的
  ``fetcher`` 由调用方注入，避免环依赖。
- **纯决策**：:func:`choose_source` / :func:`estimate_bytes` 是无副作用的函数，
  不持有状态、不发信号。
- **无 QWidget**：本模块不依赖 PySide6 / qfluentwidgets，可在无 Qt 的纯 Python
  进程里直接 import 与单测。

测速结果只活在进程内存（:data:`_speed_cache`），每进程重测；不落 ConfigItem。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: auto 档的预算：预估耗时（毫秒）不超过它则取原图，否则先出缩略图。
SOURCE_BUDGET_MS: int = 300

#: 绝对大小快路径：不超过此字节数的原图不看测速直接取原图。300KB 级的原图在任何可用网络下
#: 都远快于 300ms。集中常量，允许调。
SMALL_ORIGINAL_BYTES: int = 1_500_000

#: 每进程每站点的测速结果缓存（键 = site）。值 <=0（尤其 -1.0）表示未知/失败。
_speed_cache: dict[str, float] = {}
#: 保护 ``_speed_cache`` 读写的锁；测速本身在锁外进行，避免阻塞其它站点。
_speed_lock = threading.Lock()


@dataclass(frozen=True)
class SourceProbe:
    """一次选型的输入探针（不可变，便于跨调用方安全传递）。

    bytes_per_ms 为下载速率（<=0 表示未知）；estimated_bytes 是预估字节数；
    is_animated 仅用于决策日志可观测性。
    """

    bytes_per_ms: float
    estimated_bytes: int
    is_animated: bool = False


def choose_source(
    mode: str,
    probe: SourceProbe,
    *,
    budget_ms: int = SOURCE_BUDGET_MS,
    small_bytes: int = SMALL_ORIGINAL_BYTES,
) -> str:
    """按档位与探针决定用 "original" 还是 "thumbnail"（纯函数）。

    - mode == "on" → 恒取原图；mode == "off" → 恒取缩略图；
    - 其余（含 "auto" 与未知值）：1) 绝对大小快路径：0 < estimated_bytes <= small_bytes → 原图，
      不看速率；2) 体积未知（<=0）→ 缩略图；3) 速率未知（<=0）→ 缩略图；
      4) 否则按 estimated_bytes / bytes_per_ms <= budget_ms 判定。
    bytes_per_ms 的推荐来源是记录真实图片下载吞吐（record_throughput）。
    """
    est_ms = (
        probe.estimated_bytes / probe.bytes_per_ms
        if probe.bytes_per_ms > 0 else float("inf")
    )
    if mode == "on":
        result = "original"
    elif mode == "off":
        result = "thumbnail"
    elif probe.estimated_bytes <= 0:
        result = "thumbnail"
    elif probe.estimated_bytes <= small_bytes:
        result = "original"
    elif probe.bytes_per_ms <= 0:
        result = "thumbnail"
    else:
        result = "original" if est_ms <= budget_ms else "thumbnail"

    logger.debug(
        "viewer source decision mode=%s est_ms=%.1f bytes=%d small=%d animated=%s -> %s",
        mode, est_ms, probe.estimated_bytes, small_bytes, probe.is_animated, result,
    )
    return result


def ensure_speed(site: str, url: str = "", fetcher=None) -> float:
    """确保 ``site`` 的下载速率已知，返回 bytes/ms（失败或未知为 -1.0）。

    每进程每站点最多测一次：命中缓存（含 -1.0 负缓存）直接返回；否则先写入 -1.0 占位防重入，
    再在锁外调用 fetcher(url) 计时，异常缓存 -1.0。线程安全由 _speed_lock 保证。
    url / fetcher 只在首次测速时需要；只传 site 即纯读缓存（不写占位）。
    同步函数：首次测速在调用线程直接执行 fetcher(url)，GUI 线程须放后台线程。
    """
    with _speed_lock:
        if site in _speed_cache:
            return _speed_cache[site]
        if fetcher is None:
            return -1.0
        _speed_cache[site] = -1.0

    try:
        started = time.perf_counter()
        data = fetcher(url)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        rate = len(data) / max(elapsed_ms, 0.001)
    except Exception:
        rate = -1.0

    with _speed_lock:
        _speed_cache[site] = rate
    return rate


def speed_of(site: str) -> float:
    """只读 ``site`` 的已缓存速率（bytes/ms），供 GUI 线程安全、非阻塞读取。

    未测过、测速进行中或失败（缓存 <=0）都返回 -1.0；本函数**不发请求、不写缓存**，
    因此不会干扰后台正在进行的测速。
    """
    with _speed_lock:
        rate = _speed_cache.get(site, -1.0)
    return rate if rate > 0 else -1.0


def estimate_bytes(content_length: int = 0, size=None) -> int:
    """预估图片字节数：优先 content-length，否则按 size 的 w*h*4 粗估，读不到为 0。

    ``size`` 只需提供 ``width()`` / ``height()``（QSize 即满足）；这里不 import
    Qt，靠鸭子类型读取，保持本模块的站点/框架无关性。
    """

    def _dim(value):
        return value() if callable(value) else value

    if content_length and content_length > 0:
        return int(content_length)
    if size is not None:
        width = _dim(getattr(size, "width", None))
        height = _dim(getattr(size, "height", None))
        if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
            return int(width * height * 4)
    return 0


__all__ = [
    "SMALL_ORIGINAL_BYTES",
    "SOURCE_BUDGET_MS",
    "SourceProbe",
    "choose_source",
    "ensure_speed",
    "estimate_bytes",
    "speed_of",
]
