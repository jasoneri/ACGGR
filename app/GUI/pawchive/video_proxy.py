"""pawchive 侧视频中继：只声明"怎么发上游请求"，其余全部继承共享实现。

共享实现见 ``app/GUI/viewer/video_proxy.py``（缓存 / 分段预取 / Range / 进度）。
本文件原先有第二份 700 行拷贝，长期漂移后 playback 退化为"每个 Range 现场打上游"，
用户侧表现为"播放卡到半死"。**不要在这里再写缓存/HTTP 逻辑。**
"""

from __future__ import annotations

import typing as t

import httpx
from PySide6 import QtCore

from app.GUI.viewer import (
    RANGE_PROBE_HEADER,
    STREAM_TIMEOUT,
    VideoCacheProgress,
    VideoProxyBase,
    redacted_error_message,
    safe_proxy_url,
)

__all__ = [
    "PawchiveVideoProxy",
    "VideoCacheProgress",
    "build_pawchive_http_client",
    "redacted_error_message",
    "safe_proxy_url",
]


def _upstream_headers() -> dict[str, str]:
    """pawchive 的分域 UA + Referer；取不到常量时也要能发出请求。"""
    headers = {"Accept": "*/*"}
    try:
        from core.website.pawchive.api import download_headers

        headers.update(download_headers())
    except Exception:  # noqa: BLE001 - 常量缺失不该挡住播放
        pass
    try:
        from core.website.pawchive import domain

        headers["Referer"] = f"https://{domain or 'pawchive.pw'}/"
    except Exception:  # noqa: BLE001
        headers.setdefault("Referer", "https://pawchive.pw/")
    return headers


def build_pawchive_http_client() -> httpx.Client:
    """同步 httpx client：与站点其它请求共用代理 / DoH / 证书策略。"""
    from core import conf as core_conf
    from core import get_httpx_verify
    from core.config.qc import acggr_cfg
    from core.network.doh import build_http_transport

    transport, trust_env = build_http_transport(
        "proxy",
        getattr(core_conf, "proxies", None) or [],
        doh_url=acggr_cfg.doh.get_url(),
        is_async=False,
        retries=2,
        verify=get_httpx_verify(),
        http2=True,
    )
    return httpx.Client(transport=transport, trust_env=trust_env)


class PawchiveVideoProxy(VideoProxyBase):
    """pawchive 视频中继：key = 源 URL（一次只播一项）。"""

    cache_prefix = "pawchive"

    def __init__(
        self,
        parent: t.Optional[QtCore.QObject] = None,
        *,
        client_factory: t.Optional[t.Callable[[], httpx.Client]] = None,
    ):
        self._client_factory = client_factory or build_pawchive_http_client
        self._client: t.Optional[httpx.Client] = None
        super().__init__(parent)

    # ---- 唯一站点差异：怎么发一个上游请求 ----
    def _stream(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: t.Optional[dict] = None,
        timeout: float = STREAM_TIMEOUT,
    ):
        merged = _upstream_headers()
        if headers:
            merged.update(headers)
        client = self._http_client()
        return _raising_stream(client, url, method=method, headers=merged, timeout=timeout)

    def _http_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = self._client_factory()
        return self._client

    def close(self):
        super().close()
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关连接失败不影响退出
                pass


class _raising_stream:
    """把 ``httpx.Client.stream`` 包成"非 2xx 抛异常"的上下文。

    共享基类假定上游流已 ``raise_for_status``（danbooru 的 ``stream_remote`` 就是
    这样），这里补齐同一契约，避免 404/403 被当成正常流读取。
    """

    def __init__(self, client, url, *, method, headers, timeout):
        self._context = client.stream(
            method, url, headers=headers, timeout=timeout, follow_redirects=True
        )
        self._response = None

    def __enter__(self):
        self._response = self._context.__enter__()
        try:
            self._response.raise_for_status()
        except Exception:
            self._context.__exit__(None, None, None)
            raise
        return self._response

    def __exit__(self, exc_type, exc, tb):
        return self._context.__exit__(exc_type, exc, tb)


# ``RANGE_PROBE_HEADER`` 由基类使用；此处 import 保证 re-export 可用（旧调用点）。
_ = RANGE_PROBE_HEADER
