"""Danbooru 侧视频中继：只声明"怎么发上游请求"，其余全部继承共享实现。

共享实现见 ``app/GUI/viewer/video_proxy.py``（缓存 / 分段预取 / Range / 进度）。
本文件**不得**再写缓存或 HTTP 响应逻辑。
"""

from __future__ import annotations

import typing as t

from PySide6 import QtCore

from app.GUI.viewer import (
    STREAM_TIMEOUT,
    VideoCacheProgress,
    VideoProxyBase,
    safe_proxy_url,
)

from core.script.image.danbooru.client import DanbooruClient
from core.script.image.danbooru.http import DanbooruChallengeRequired
from core.script.image.danbooru.models import DanbooruPost


class VideoProxy(VideoProxyBase):
    """Danbooru 视频中继：key = ``post_id``，上游走 ``DanbooruClient.stream_remote``。"""

    #: 需要人工/自动过验证的异常：控制器据此弹 challenge UI。
    challenge_exception_types = (DanbooruChallengeRequired,)
    cache_prefix = "danbooru"

    def __init__(self, request_client: DanbooruClient, parent: t.Optional[QtCore.QObject] = None):
        self._request_client = request_client
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
        return self._request_client.stream_remote(
            url, method=method, headers=headers, timeout=timeout
        )

    # ---- 站点语义包装（保持既有调用点不变）----
    def register_post(self, post: DanbooruPost) -> str:
        source_url = str(post.viewer_video_source_url or "")
        if not source_url:
            raise ValueError(f"Danbooru video source url is required: post_id={post.post_id}")
        return self.register_source(source_url, key=int(post.post_id))

    def progress_for_post(self, post_id: int) -> t.Optional[VideoCacheProgress]:
        return self.progress_for(int(post_id))

    @staticmethod
    def _source_url_for_post(post: DanbooruPost) -> str:
        return str(post.viewer_video_source_url or "")


__all__ = ["VideoProxy", "VideoCacheProgress", "safe_proxy_url"]
