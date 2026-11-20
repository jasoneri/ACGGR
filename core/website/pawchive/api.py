# -*- coding: utf-8 -*-
"""Pawchive HTTP API 客户端（零模块级副作用）。

端点常量 / URL 规则的唯一来源：本包 ``__init__`` 从这里 re-export，禁止复制第二份。
模块级只 import stdlib 与 ``httpx``；一切 ``core.*`` 依赖延迟到函数 / 构造体内。
"""
from __future__ import annotations

import typing as t
from functools import lru_cache

import httpx

__all__ = [
    "domain", "headers", "base", "creator_posts", "discord_info", "post",
    "favorites", "img_domain", "file_domain", "creators_txt",
    "PawchiveApiError", "PawchiveHttpError", "FetchError", "MAX_FETCH_BYTES", "Api",
    "data_url", "resolve_url", "thumbnail_url",
    "user_agent", "download_headers", "thumbnail_headers",
    "fetch_bytes", "close_sync_client",
]

# --- 站点常量：逐字节等于拆分前的值（旧 CLI 的 aria2 header 行为因此不变）---
domain = "pawchive.pw"
headers: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
    "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    'Accept': 'text/css',
}
base = f"https://{domain}/api/v1"
creator_posts = base + "/{service}/user/{creator_id}/posts"
discord_info = base + "/discord/{kind}/{_id}"
post = base + "/{service}/user/{creator_id}/post/{post_id}"
favorites = base + "/account/favorites"
img_domain = f"img.{domain}"    # thumbnail server: /thumbnail/data/... (direct OK)
file_domain = f"file.{domain}"  # file server
creators_txt = base + "/creators"


class PawchiveApiError(Exception):
    """pawchive API 层基类（非 HTTP 状态错误）。"""


class PawchiveHttpError(PawchiveApiError):
    """响应非 2xx。三字段即定位三件套，禁止只塞一个字符串。

    ``url`` **只放 scheme+host+path，剥掉 query**：``temp_url`` 的 ``?e=&s=`` 是
    short-lived 签名，进日志/异常回溯等于把可用凭证写进磁盘。
    """

    status_code: int
    url: str
    body_snippet: str

    def __init__(self, status_code: int, url: str, body_snippet: str = "") -> None:
        super().__init__(status_code, url, body_snippet)
        self.status_code = status_code
        self.url = url
        self.body_snippet = body_snippet

    def __str__(self) -> str:
        return f"HTTP {self.status_code} {self.url} {self.body_snippet}".strip()


def _safe_url(url: str) -> str:
    """剥 query/fragment，只留 ``scheme://host/path``。异常与日志的唯一出口。"""
    raw = str(url or "")
    if not raw:
        return ""
    try:
        parsed = httpx.URL(raw)
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.host}{port}{parsed.path}"
    except Exception:
        return raw.split("?", 1)[0].split("#", 1)[0]


#: `fetch_bytes` 的单次读上限（1 个原图/缩略图远小于此值；防被超大响应打爆内存）。
MAX_FETCH_BYTES = 64 * 1024 * 1024

#: 异常/日志里正文片段的截断长度。
_SNIPPET_BYTES = 200


class FetchError(Exception):
    """**`fetch_bytes` 的全部失败统一抛本异常**（与 `PawchiveHttpError` 互不继承）。

    05/07 **只鸭子类型读 `kind` / `status`**，不 import 本模块（UI 不依赖网络层）。
    """

    kind: t.Literal["http", "blocked", "timeout", "too_large", "network"]
    status: int | None
    url: str  # 已剥 query/fragment（_safe_url）
    detail: str

    def __init__(self, kind: str, *, status: int | None = None,
                 url: str = "", detail: str = "") -> None:
        super().__init__(kind, status, url, detail)
        self.kind = kind
        self.status = status
        self.url = url
        self.detail = detail

    def __str__(self) -> str:
        head = f"{self.kind}" + (f" {self.status}" if self.status is not None else "")
        tail = f"{self.url} {self.detail}".strip()
        return f"{head} {tail}".strip()


def _classify_status(status_code: int) -> str:
    """403 → ``blocked``（域拦截/UA 被拒），其余非 2xx → ``http``。"""
    return "blocked" if status_code == 403 else "http"


def _snippet(body: bytes) -> str:
    """二进制安全的正文片段（不解码整段、不带控制字符回显）。"""
    return bytes(body[:_SNIPPET_BYTES]).decode("utf-8", "replace")


def user_agent() -> str:
    """``acggr/<version>``：延迟 import core.variables，模块级不碰 core。"""
    from core.variables import APP_NAME, __version__

    return f"{APP_NAME}/{__version__}"


def download_headers() -> dict[str, str]:
    """原图 / 镜像域请求头：UA 必须 ``acggr/<ver>``（Mozilla 前缀或 spider 字样会被拒绝）。"""
    return {
        "User-Agent": user_agent(),
        "Accept": "*/*",
        "Accept-Language": headers["Accept-Language"],
    }


def thumbnail_headers() -> dict[str, str]:
    """img 域请求头：UA 不敏感，给 Accept 即可。"""
    return {
        "User-Agent": user_agent(),
        "Accept": "image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5",
    }


def _headers_for(url: str) -> dict[str, str]:
    """按域分层 UA：``img.`` 前缀走缩略图头，其余走下载头。"""
    try:
        host = httpx.URL(str(url or "")).host or ""
    except Exception:
        host = ""
    return thumbnail_headers() if host.startswith("img.") else download_headers()


@lru_cache(maxsize=1)
def _sync_client() -> httpx.Client:
    """模块级**惰性**单例：首次调用才建，导入期不建。

    transport 延迟 import core.network.doh / core.config.qc，代理取 ``core.conf.proxies``。
    """
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


def close_sync_client() -> None:
    """关掉惰性单例并清 cache；幂等。给测试与「想立刻释放连接」的调用方用。"""
    cached = _sync_client.cache_info()
    if cached.currsize:
        try:
            _sync_client().close()
        except Exception:
            pass
    _sync_client.cache_clear()


def fetch_bytes(url: str, *, referer: str = "", timeout: float = 30,
                client: httpx.Client | None = None,
                max_bytes: int = MAX_FETCH_BYTES) -> bytes:
    """**阻塞** GET，返回原始字节（不解码）。05 缩略图 / 07 详情原图共用。

    按域分层 UA：``img.`` 前缀 → :func:`thumbnail_headers`；
    其余（原图 / 镜像 / t3）→ :func:`download_headers`（``acggr/<version>``）。
    ``referer`` 非空则塞 ``Referer``。``follow_redirects=True``（旧 302 域兜底）。
    失败**一律**抛 :class:`FetchError`：非 2xx → ``http``（403 → ``blocked``）／
    ``httpx.TimeoutException`` → ``timeout``／超出 ``max_bytes`` → ``too_large``／
    其余 ``httpx.TransportError`` → ``network``。
    **不写盘、不建目录、不缓存 URL。**
    """
    safe = _safe_url(url)
    req_headers = _headers_for(url)
    if referer:
        req_headers["Referer"] = referer
    cli = _sync_client() if client is None else client
    try:
        with cli.stream("GET", url, headers=req_headers, timeout=timeout,
                        follow_redirects=True) as resp:
            status_code = resp.status_code
            if not 200 <= status_code < 300:
                chunk = bytearray()
                for part in resp.iter_bytes():
                    chunk += part
                    if len(chunk) >= _SNIPPET_BYTES:
                        break
                raise FetchError(
                    _classify_status(status_code), status=status_code,
                    url=safe, detail=_snippet(bytes(chunk)),
                )
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise FetchError(
                    "too_large", url=safe,
                    detail=f"content-length {declared} > {max_bytes}",
                )
            buf = bytearray()
            for part in resp.iter_bytes():
                buf += part
                if len(buf) > max_bytes:
                    raise FetchError(
                        "too_large", url=safe,
                        detail=f"read > {max_bytes}",
                    )
            return bytes(buf)
    except httpx.TimeoutException as exc:
        raise FetchError("timeout", url=safe, detail=type(exc).__name__) from exc
    except httpx.TransportError as exc:
        raise FetchError("network", url=safe, detail=type(exc).__name__) from exc


def resolve_url(attachment: t.Mapping[str, t.Any]) -> str | None:
    """全尺寸 URL 三态（契约 #1）：``temp_url`` → ``{server}/data{path}?f={name}`` → ``None``。

    禁止按 ``preview_only`` 二分支；禁止拼 ``file.{domain}``（302 域）。
    """
    temp_url = attachment.get("temp_url")
    if temp_url:
        return str(temp_url)
    server = attachment.get("server")
    path = attachment.get("path")
    if not server or not path:
        return None
    name = attachment.get("name") or str(path).rsplit("/", 1)[-1]
    return f"{str(server).rstrip('/')}/data{path}?f={name}"


def thumbnail_url(attachment: t.Mapping[str, t.Any]) -> str | None:
    """``https://{img_domain}/thumbnail/data{path}``；无 ``path`` 返回 ``None``。"""
    path = attachment.get("path")
    if not path:
        return None
    return f"https://{img_domain}/thumbnail/data{path}"


def data_url(path: str, name: str, *, preview_only: bool = False) -> str:
    """**仅供旧 CLI（redis 入队）**：字符串与拆分前 ``Api.data_url`` 逐字节一致。

    新代码（网格 / 下载 / 详情）一律用 :func:`resolve_url` / :func:`thumbnail_url`。
    """
    if preview_only:
        return f"https://{img_domain}/thumbnail/data{path}?f={name}"
    return f"https://{file_domain}/data{path}?f={name}"


class Api:
    # 类属性与模块常量同对象（保持 ``Api.base`` 等旧读法）
    base = base
    creator_posts = creator_posts
    discord_info = discord_info
    post = post
    favorites = favorites
    img_domain = img_domain
    file_domain = file_domain
    creators_txt = creators_txt

    def __init__(self, conf: object | None = None, *,
                 cookie: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 trust_env: bool | None = None) -> None:
        self._cookie_arg = cookie
        self.conf = {} if conf is None else getattr(conf, "pawchive", conf)
        if transport is None:
            from core import get_httpx_verify
            from core.config.qc import acggr_cfg
            from core.network.doh import build_http_transport

            proxies = getattr(conf, "proxies", None)
            if not proxies and isinstance(self.conf, dict):
                proxies = self.conf.get("proxies")
            transport, trust_env = build_http_transport(
                "proxy",
                proxies or [],
                doh_url=acggr_cfg.doh.get_url(),
                is_async=True,
                retries=2,
                verify=get_httpx_verify(),
                http2=True,
            )
        elif trust_env is None:
            trust_env = False
        self.sess = httpx.AsyncClient(transport=transport, trust_env=trust_env)

    @property
    def cookie(self) -> str | None:
        if self._cookie_arg:
            return self._cookie_arg
        try:
            return self.conf.get("cookie")
        except AttributeError:
            return None

    def data_url(self, path: str, name: str, *, preview_only: bool = False) -> str:
        return data_url(path, name, preview_only=preview_only)

    async def req(self, url: str, method: str = "GET", **kw: t.Any) -> httpx.Response:
        """post almost not use, detail for /api/schema?logged_in=yes&role=consumer"""
        if not kw.get("timeout"):
            kw.update(timeout=30)
        resp = await self.sess.request(method, url, **kw)
        return resp

    async def get_favorites(self) -> t.Any:
        resp = await self.req(self.favorites, headers=headers,
                              cookies={'session': self.cookie})
        return resp.json()

    async def get_creator_posts(self, creator_id, service, **kw: t.Any) -> t.Any:
        resp = await self.req(self.creator_posts.format(creator_id=creator_id, service=service),
                              headers=headers, **kw)
        return resp.json()

    async def get_discord_info(self, _id, kind: str = "server", **kw: t.Any) -> t.Any:
        resp = await self.req(self.discord_info.format(kind=kind, _id=_id),
                              headers=headers, **kw)
        return resp.json()

    async def get_post(self, creator_id, service, post_id, **kw: t.Any) -> t.Any:
        resp = await self.req(self.post.format(creator_id=creator_id, service=service, post_id=post_id),
                              headers=headers, **kw)
        return resp.json()

    async def get_json(self, url: str, *, method: str = "GET", timeout: float = 30,
                       headers: t.Mapping[str, str] | None = None,
                       cookies: t.Mapping[str, str] | None = None,
                       params: t.Mapping[str, t.Any] | None = None,
                       follow_redirects: bool = False) -> t.Any:
        """**新路径专用**：非 2xx → :class:`PawchiveHttpError`（URL 已剥 query）。"""
        resp = await self.req(url, method, timeout=timeout, headers=headers,
                              cookies=cookies, params=params,
                              follow_redirects=follow_redirects)
        if not 200 <= resp.status_code < 300:
            raise PawchiveHttpError(resp.status_code, _safe_url(url),
                                    _snippet(resp.content))
        try:
            return resp.json()
        except ValueError as exc:
            raise PawchiveApiError(f"invalid JSON from {_safe_url(url)}") from exc

    async def aclose(self) -> None:
        """幂等：已关 / 未建 ``sess`` 均不抛；``self.sess`` 仍公开（旧 ``_cleanup`` 依赖）。"""
        sess = getattr(self, "sess", None)
        if sess is None:
            return
        try:
            await sess.aclose()
        except Exception:
            pass
