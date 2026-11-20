"""pawchive 进程内服务层（P2）。

- 模块级零构造：不建 httpx client / aria2 rpc / sqlite 连接，不改 sys.path、不动 loguru
  handler、不 new/set event loop；相关依赖一律函数体内惰性 import。
- 自有资源单轮现建现关：list_posts / get_post 一建一关，plan_items 整轮一建一关，submit 的
  rpc 同理；跨 asyncio.run 复用一律禁止。
- URL 三态按字段优先级（temp_url → {server}/data{path}?f={name} → None），禁 file.{domain}，
  禁按 preview_only 二分支。
- 已下载态：identity_key 为文件级 sha1 40hex，落 PAWCHIVE_SQL_TABLE（与 danbooru 同库不同表）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable, Iterable, Literal
from urllib.parse import urlsplit

import httpx

from core.variables import APP_NAME, __version__
from core.website.pawchive import (
    DEFAULT_PAWCHIVE_SAVE_PATH,
    creator_posts,
    img_domain,
    post as post_endpoint,
)

if TYPE_CHECKING:  # 类型注解专用：运行期不 import，保住模块级 C1 禁 import 清单
    from core.script.aria2.rpc import Aria2RpcClient
    from core.sql import SqlRecorder


#: 已下载记录表名。与 danbooru 同库不同表；外部（06 判据 / 测试）一律引用本常量，
#: 不得再出现表名字面量。
PAWCHIVE_SQL_TABLE = "pawchive_md5_table"

#: 站点分页步长（``o`` 必须是 50 的倍数，见 ``pawchive_api_facts.md`` §3）。
PAGE_SIZE = 50

#: 文件域下载并发硬上限（403 正文：no more than 5 active downloads）。
MAX_DOWNLOAD_CONCURRENCY = 5

#: 轮询连续失败容忍次数（沿用旧链阈值）。
POLL_FAILURE_LIMIT = 5

#: ``tell_status`` 显式 5 键：``files`` 是 gid → 落盘名的唯一来源，
#: ``totalLength`` 不加入（``files[0].length`` 已带）。
ARIA2_STATUS_KEYS = ("status", "errorCode", "errorMessage", "files", "completedLength")

#: 禁当原图取值的域前缀：``img.`` 只有缩略图，``file.`` 是 302 重定向器。
FORBIDDEN_HOST_PREFIXES = ("img.", "file.")

ProgressState = Literal["queued", "active", "waiting", "complete", "error", "removed"]

_STATE_MAP: dict[str, ProgressState] = {
    "waiting": "waiting",
    "active": "active",
    "paused": "waiting",
    "complete": "complete",
    "error": "error",
    "removed": "removed",
}

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


class PawchiveError(Exception):
    """服务层异常基类（UI 可只捕获这一个类型）。"""


class PawchiveApiError(PawchiveError):
    """API 非 2xx。``path`` 已剥 query，签名不会进日志。"""

    def __init__(self, status: int, path: str, body: str = "") -> None:
        self.status = int(status)
        self.path = str(path)
        self.body = str(body)
        detail = f"pawchive API {self.status} {self.path}"
        if self.body:
            detail = f"{detail}: {self.body}"
        super().__init__(detail)


class PawchiveConfigError(PawchiveError):
    """配置不可用（典型：没配代理就直连 —— ConnectError）。"""


@dataclass(frozen=True, slots=True)
class PawchiveAttachment:
    """单个附件。``kind`` 由「字段在不在」判定，不看 ``preview_only`` / 扩展名。"""

    name: str
    path: str
    preview_only: bool
    temp_url: str | None
    temp_expires: str | None
    server: str | None
    kind: Literal["image", "archive", "unknown"]


@dataclass(frozen=True, slots=True)
class PawchivePost:
    """一帖。``creator_name`` 由外部注入（payload 只有 creator id，无作者名）。"""

    post_id: str
    creator_id: str
    creator_name: str
    service: str
    title: str
    published: str
    tags: str
    file: PawchiveAttachment | None
    attachments: list[PawchiveAttachment]
    next_id: str | None
    prev_id: str | None
    raw: dict


@dataclass(frozen=True, slots=True)
class PawchivePage:
    """一页帖列表。``len(posts) < 50`` ⇒ ``has_more = False``。"""

    posts: list[PawchivePost]
    offset: int
    has_more: bool
    page_size: int = PAGE_SIZE


@dataclass(frozen=True, slots=True)
class PawchiveDownloadItem:
    """一个可提交项：帖 + 附件 + 文件级 identity + 落点目录 + 落盘名。"""

    post: PawchivePost
    attachment: PawchiveAttachment
    identity_key: str
    target_dir: Path
    out_name: str


@dataclass(frozen=True, slots=True)
class PawchiveSubmittedFile:
    """提交成功记录：``disk_path`` 是 identity ↔ 落盘名解耦的唯一兑现途径。"""

    gid: str
    identity_key: str
    out_name: str
    disk_path: str


@dataclass(frozen=True, slots=True)
class PawchiveProgress:
    """单个文件的进度事件。``total_bytes is None`` = 未知，禁 0 兜底。"""

    identity_key: str
    state: ProgressState
    completed_bytes: int = 0
    total_bytes: int | None = None
    detail: str = ""


@dataclass(slots=True)
class PawchiveDownloadPlan:
    """一次 ``submit`` 的四类结果（字段名与 danbooru ``DownloadPlan`` 同形）。"""

    to_submit: list[PawchiveSubmittedFile]
    deduped_skipped: list[PawchiveDownloadItem]
    failed_pre_submit: list[PawchiveDownloadItem]
    submission_errors: list[str]


@dataclass(frozen=True, slots=True)
class PawchiveServiceConfig:
    """服务层配置。每个字段都有现成读点（``from_conf``），不新增 conf 项。"""

    save_path: Path
    cookie: str = ""
    download_concurrency: int = 3
    poll_interval_s: float = 1.2
    #: 列表/详情读取的单次超时。
    #:
    #: 原值 30s 会让"代理没通"变成"页面卡半分钟才告诉你"（用户现场原话：
    #: 卡个几十秒都没 list）。站点正常时首页远小于 15s，超时后由网格层给出
    #: 「请求超时 + 重试」，比干等更有用。整文件下载走 aria2，不受此限。
    request_timeout_s: float = 15.0
    min_submit_interval_s: float = 1.0
    user_agent: str = f"{APP_NAME}/{__version__}"
    doh_url: str = ""
    proxies: tuple[str, ...] = ()
    #: 裁定 5 逃生舱：仅离线单测用。产品路径永远经 ``proxies``。
    allow_direct: bool = False
    filter_text: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.save_path, Path):
            object.__setattr__(self, "save_path", Path(self.save_path))
        concurrency = int(self.download_concurrency)
        if concurrency < 1:
            concurrency = 1
        elif concurrency > MAX_DOWNLOAD_CONCURRENCY:
            concurrency = MAX_DOWNLOAD_CONCURRENCY
        if concurrency != self.download_concurrency:
            object.__setattr__(self, "download_concurrency", concurrency)
        if not isinstance(self.proxies, tuple):
            object.__setattr__(self, "proxies", tuple(str(item) for item in self.proxies))

    @classmethod
    def from_conf(cls) -> "PawchiveServiceConfig":
        """从现盘配置读值（C6：不新增 conf 项，每字段都有读点）。"""
        from core.config.qc import acggr_cfg, pawchive_cfg
        from core.script import conf as script_conf

        section = getattr(script_conf, "pawchive", None) or {}
        save_path = section.get("sv_path") or DEFAULT_PAWCHIVE_SAVE_PATH
        cookie = str(section.get("cookie") or "")
        proxies = tuple(
            str(item) for item in (getattr(script_conf, "proxies", None) or []) if str(item).strip()
        )
        try:
            doh_url = acggr_cfg.doh.get_url()
        except Exception:
            doh_url = ""
        try:
            filter_text = str(pawchive_cfg.filterText.value or "")
        except Exception:
            filter_text = ""
        return cls(
            save_path=Path(save_path),
            cookie=cookie,
            doh_url=doh_url,
            proxies=proxies,
            filter_text=filter_text,
        )


def safe_url(url: str) -> str:
    """剥掉 query / fragment（``temp_url`` 的签名不得进日志或异常文本）。"""
    parts = urlsplit(str(url or ""))
    if not parts.scheme and not parts.netloc:
        return str(url or "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def resolve_attachment_url(attachment: PawchiveAttachment) -> str | None:
    """URL 三态（字段优先级），``kind`` 与提交链共用同一实现。

    ① ``temp_url``（现签原图）→ ② ``{server}/data{path}?f={name}``（镜像直连）
    → ③ ``None``（分支 B：该附件不可下载）。
    """
    if attachment.temp_url:
        return attachment.temp_url
    if attachment.server:
        host = (urlsplit(attachment.server).hostname or "").lower()
        if not host.startswith(FORBIDDEN_HOST_PREFIXES):
            return f"{attachment.server.rstrip('/')}/data{attachment.path}?f={attachment.name}"
    return None


def attachment_kind(attachment: PawchiveAttachment) -> Literal["image", "archive", "unknown"]:
    """与 :func:`resolve_attachment_url` 同源，保证「有 URL ⇔ kind != unknown」。"""
    if attachment.temp_url:
        return "image"
    if attachment.server and resolve_attachment_url(attachment) is not None:
        return "archive"
    return "unknown"


def _as_int(value: object) -> int | None:
    """aria2 RPC 全返 ``str``；缺席 / 不可解析回 ``None``（禁 0 兜底）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _display_name(attachment: PawchiveAttachment) -> str:
    """落盘名：``name`` 优先；``path`` 的 basename 只在 ``name`` 缺失时兜底。"""
    if attachment.name:
        return attachment.name
    return PurePosixPath(attachment.path or "").name


def _published_date(published: str) -> str:
    """``published`` 先归一成 ``%Y-%m-%d`` 再进目录名（raw 含 ``:``）。"""
    text = str(published or "")
    if not text:
        return "unknown"
    head = text.split(".")[0]
    try:
        return datetime.strptime(head, _TIMESTAMP_FORMAT).strftime("%Y-%m-%d")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return head[:10] or "unknown"


def _sanitize_title(title: str) -> str:
    """旧链两步：``sanitize_for_path`` → ``.strip().rstrip(" .")``。"""
    from core.core import sanitize_for_path

    return sanitize_for_path(str(title or "")).strip().rstrip(" .")


class _SubmitThrottle:
    """``add_uri`` 前 ≤1 req/s。锁按 loop 惰性重建，跨 ``asyncio.run`` 不炸。"""

    __slots__ = ("_interval", "_next_at", "_lock", "_loop")

    def __init__(self, interval: float) -> None:
        self._interval = max(0.0, float(interval))
        self._next_at = 0.0
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _guard(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        async with self._guard():
            while True:
                remaining = self._next_at - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            self._next_at = time.monotonic() + self._interval

    def reset(self) -> None:
        self._next_at = 0.0
        self._lock = None
        self._loop = None


class PawchiveService:
    """pawchive 进程内服务层。

    依赖全部可注入；缺省分支在该轮调用内现建现关，实例上不驻留
    ``httpx.AsyncClient`` / ``Aria2RpcClient`` / ``asyncio.Semaphore``。
    """

    def __init__(
        self,
        *,
        config: PawchiveServiceConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
        rpc_client: "Aria2RpcClient | None" = None,
        sql_recorder: "SqlRecorder | None" = None,
    ) -> None:
        self._config = config or PawchiveServiceConfig.from_conf()
        self._injected_client = http_client
        self._injected_rpc = rpc_client
        self._injected_recorder = sql_recorder
        self._recorder_obj = None
        self._throttle = _SubmitThrottle(self._config.min_submit_interval_s)
        self._submitting = False

    # -- 配置 ---------------------------------------------------------------

    @property
    def config(self) -> PawchiveServiceConfig:
        return self._config

    # -- 传输层 -------------------------------------------------------------

    def _build_client(self) -> httpx.AsyncClient:
        """每轮现建；无代理直接显式失败（裁定 5），绝不静默直连。"""
        if not self._config.proxies and not self._config.allow_direct:
            raise PawchiveConfigError(
                "pawchive service requires proxies; configure conf.proxies "
                "(direct connection measured: ConnectError 10054)"
            )
        from core import get_httpx_verify
        from core.network.doh import build_http_transport

        transport, trust_env = build_http_transport(
            "proxy",
            list(self._config.proxies),
            doh_url=self._config.doh_url,
            is_async=True,
            retries=2,
            verify=get_httpx_verify(),
            http2=True,
        )
        return httpx.AsyncClient(
            transport=transport,
            trust_env=trust_env,
            timeout=httpx.Timeout(self._config.request_timeout_s, connect=10.0),
        )

    async def _request_json(self, client: httpx.AsyncClient, url: str, *, params=None):
        """只带诚实 UA；非 2xx 转 :class:`PawchiveApiError`（path 不含 query）。"""
        response = await client.get(
            url,
            params=params,
            headers={"User-Agent": self._config.user_agent},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                body = (exc.response.text or "")[:40]
            except Exception:
                body = ""
            raise PawchiveApiError(exc.response.status_code, safe_url(url), body) from exc
        return response.json()

    # -- 读链 ---------------------------------------------------------------

    async def list_posts(
        self,
        *,
        creator_id: str,
        service: str,
        creator_name: str = "",
        offset: int = 0,
    ) -> PawchivePage:
        """单作者帖列表。``offset`` 必须是 50 的倍数（否则本地报错、不发请求）。"""
        if offset % PAGE_SIZE != 0:
            raise ValueError(f"offset must be a multiple of {PAGE_SIZE}: {offset}")
        url = creator_posts.format(service=service, creator_id=creator_id)
        params = {"o": offset} if offset else None
        client = self._injected_client
        own = client is None
        if own:
            client = self._build_client()
        try:
            payload = await self._request_json(client, url, params=params)
        finally:
            if own:
                await client.aclose()
        rows = payload if isinstance(payload, list) else []
        posts = [
            self._to_post(row, creator_id=creator_id, service=service, creator_name=creator_name)
            for row in rows
            if isinstance(row, dict)
        ]
        return PawchivePage(posts=posts, offset=offset, has_more=len(rows) >= PAGE_SIZE)

    async def get_post(
        self,
        *,
        creator_id: str,
        service: str,
        creator_name: str = "",
        post_id: str,
    ) -> PawchivePost:
        """单帖 detail。唯一用途：提交前现取 ``temp_url``。"""
        client = self._injected_client
        own = client is None
        if own:
            client = self._build_client()
        try:
            return await self._get_post_with(
                client,
                creator_id=creator_id,
                service=service,
                creator_name=creator_name,
                post_id=post_id,
            )
        finally:
            if own:
                await client.aclose()

    async def _get_post_with(
        self,
        client: httpx.AsyncClient,
        *,
        creator_id: str,
        service: str,
        creator_name: str,
        post_id: str,
    ) -> PawchivePost:
        url = post_endpoint.format(service=service, creator_id=creator_id, post_id=post_id)
        payload = await self._request_json(client, url)
        if not isinstance(payload, dict):
            raise PawchiveApiError(200, safe_url(url), "payload is not an object")
        return self._to_post(
            payload, creator_id=creator_id, service=service, creator_name=creator_name
        )

    @staticmethod
    def _to_attachment(payload: object) -> PawchiveAttachment:
        """API 附件 payload → :class:`PawchiveAttachment`（``kind`` 与 URL 同源）。"""
        data = payload if isinstance(payload, dict) else {}
        attachment = PawchiveAttachment(
            name=str(data.get("name") or ""),
            path=str(data.get("path") or ""),
            preview_only=bool(data.get("preview_only")),
            temp_url=str(data.get("temp_url") or "") or None,
            temp_expires=str(data.get("temp_expires") or "") or None,
            server=str(data.get("server") or "") or None,
            kind="unknown",
        )
        return replace(attachment, kind=attachment_kind(attachment))

    def _to_post(
        self,
        payload: dict,
        *,
        creator_id: str,
        service: str,
        creator_name: str = "",
    ) -> PawchivePost:
        """API 帖 payload → :class:`PawchivePost`（``creator_name`` 外部注入）。"""
        raw_file = payload.get("file")
        file_attachment = (
            self._to_attachment(raw_file) if isinstance(raw_file, dict) and raw_file else None
        )
        raw_attachments = payload.get("attachments") or []
        attachments = [
            self._to_attachment(item)
            for item in raw_attachments
            if isinstance(item, dict)
        ]
        next_id = payload.get("next")
        prev_id = payload.get("prev")
        return PawchivePost(
            post_id=str(payload.get("id") or ""),
            creator_id=str(payload.get("user") or creator_id or ""),
            creator_name=str(creator_name or ""),
            service=str(payload.get("service") or service or ""),
            title=str(payload.get("title") or ""),
            published=str(payload.get("published") or ""),
            tags=str(payload.get("tags") or ""),
            file=file_attachment,
            attachments=attachments,
            next_id=str(next_id) if next_id else None,
            prev_id=str(prev_id) if prev_id else None,
            raw=payload,
        )

    def attachments(self, post: PawchivePost) -> list[PawchiveAttachment]:
        """``file`` 非空且不在 ``attachments`` 内则前置；空 ``file`` 不参与（分支 A）。"""
        items = list(post.attachments)
        file_attachment = post.file
        if file_attachment is not None and file_attachment not in items:
            items.insert(0, file_attachment)
        return items

    async def plan_items(
        self, post: PawchivePost, selected_paths: Iterable[str]
    ) -> list[PawchiveDownloadItem]:
        """按帖现取一次 detail，组装可提交项。

        只返回可提交项（``resolve_download_url`` 非 ``None``）；不可解析项由
        ``submit()`` 再分类进 ``failed_pre_submit``。``out_name`` 按该帖
        ``[file] + attachments`` 全集预计算，与勾选无关。
        """
        # 去重但保留调用方顺序：结果顺序稳定，UI 不必再排一次。
        wanted = list(dict.fromkeys(str(path) for path in selected_paths))
        if not wanted:
            return []
        client = self._injected_client
        own = client is None
        if own:
            client = self._build_client()
        try:
            fresh = await self._get_post_with(
                client,
                creator_id=post.creator_id,
                service=post.service,
                creator_name=post.creator_name,
                post_id=post.post_id,
            )
        finally:
            if own:
                await client.aclose()

        fresh_attachments = {item.path: item for item in self.attachments(fresh)}
        name_counts = self._name_counts(fresh)
        target_dir = self._target_dir(fresh)
        items: list[PawchiveDownloadItem] = []
        for path in wanted:
            attachment = fresh_attachments.get(path)
            if attachment is None:
                continue
            if resolve_attachment_url(attachment) is None:
                continue
            items.append(
                PawchiveDownloadItem(
                    post=fresh,
                    attachment=attachment,
                    identity_key=self.identity_key(fresh, attachment),
                    target_dir=target_dir,
                    out_name=self._out_name(attachment, fresh, name_counts=name_counts),
                )
            )
        return items

    # -- URL ----------------------------------------------------------------

    def resolve_download_url(self, attachment: PawchiveAttachment) -> str | None:
        """同步纯函数：不发请求、无 ``fresh`` 参数、返回值不是缩略图域。"""
        return resolve_attachment_url(attachment)

    def thumbnail_url(self, attachment: PawchiveAttachment) -> str | None:
        """网格缩略图（``img.`` 域）。永不被提交链使用。"""
        if not attachment.path:
            return None
        url = f"https://{img_domain}/thumbnail/data{attachment.path}"
        if attachment.name:
            return f"{url}?f={attachment.name}"
        return url

    # -- 纯函数：identity / target_dir / out_name ---------------------------

    def identity_key(self, post: PawchivePost, attachment: PawchiveAttachment) -> str:
        """文件级 identity：``sha1(f"{service}/{creator_id}/{post_id}/{path}")`` 40hex。"""
        path = attachment.path
        if not path:
            raise ValueError("attachment.path is empty; identity_key needs a path component")
        raw = f"{post.service}/{post.creator_id}/{post.post_id}/{path}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _target_dir(self, post: PawchivePost) -> Path:
        published = _published_date(post.published)
        title = _sanitize_title(post.title)
        user_service = f"{post.creator_name or post.creator_id}_{post.service}"
        return self._config.save_path.joinpath(user_service, f"[{published}]{title}")

    def _name_counts(self, post: PawchivePost) -> dict[str, int]:
        counts: dict[str, int] = {}
        for attachment in self.attachments(post):
            name = _display_name(attachment)
            counts[name] = counts.get(name, 0) + 1
        return counts

    def _out_name(
        self,
        attachment: PawchiveAttachment,
        post: PawchivePost,
        *,
        name_counts: dict[str, int] | None = None,
    ) -> str:
        """``out=name``；同 ``target_dir`` 内重名才加 ``_<sha1(path)[:8]>``（插在扩展名前）。"""
        name = _display_name(attachment)
        counts = name_counts if name_counts is not None else self._name_counts(post)
        if counts.get(name, 0) <= 1:
            return name
        suffix = hashlib.sha1((attachment.path or "").encode("utf-8")).hexdigest()[:8]
        stem, dot, ext = name.rpartition(".")
        if dot and stem:
            return f"{stem}_{suffix}.{ext}"
        return f"{name}_{suffix}"

    # -- 已下载账本 ---------------------------------------------------------

    def _recorder(self) -> "SqlRecorder":
        if self._injected_recorder is not None:
            return self._injected_recorder
        if self._recorder_obj is None:
            from core.sql import SqlRecorder

            self._recorder_obj = SqlRecorder(table=PAWCHIVE_SQL_TABLE)
        return self._recorder_obj

    def is_downloaded(self, identity_key: str) -> bool:
        """单点查询。循环里逐卡调它是禁用的（改用 ``is_downloaded_many``）。"""
        if not identity_key:
            return False
        return bool(self._recorder().check_dupe(identity_key))

    def is_downloaded_many(self, keys: Iterable[str]) -> set[str]:
        """批量查询（448 卡回填只许 1 次入口调用；分批由 ``batch_check_dupe`` 负责）。

        去重与空串跳过在 ``batch_check_dupe`` 内部完成，本层不重复处理、不自行拼 SQL。
        """
        wanted = list(keys)
        if not wanted:
            return set()
        return set(self._recorder().batch_check_dupe(wanted))

    def mark_downloaded(self, identity_key: str) -> None:
        """只在 ``status == "complete"`` 后写（``INSERT OR IGNORE``，二次调用不抛）。"""
        if not identity_key:
            return
        self._recorder().add(identity_key)

    def filter_downloaded(
        self, items: Iterable[PawchiveDownloadItem]
    ) -> tuple[list[PawchiveDownloadItem], list[PawchiveDownloadItem]]:
        """一次批量分类：返回 ``(待提交, 已下载)``，只触发 1 次 ``batch_check_dupe``。"""
        item_list = list(items)
        if not item_list:
            return [], []
        duplicated = self._recorder().batch_check_dupe(
            [item.identity_key for item in item_list]
        )
        pending = [item for item in item_list if item.identity_key not in duplicated]
        downloaded = [item for item in item_list if item.identity_key in duplicated]
        return pending, downloaded

    # -- 提交链 -------------------------------------------------------------

    def _aria2_options(self) -> dict:
        """A1/A2：显式诚实 UA（绝不留空让 RPC 补 ``ComicGUISpider/1.0``）。"""
        from core.network.doh_policy import dns_stub_server
        from core.script.aria2 import build_dns_options

        return {
            **build_dns_options(dns_server=dns_stub_server(self._config.doh_url)),
            "header": [f"User-Agent: {self._config.user_agent}", "Accept: */*"],
        }

    async def submit(
        self,
        items: Iterable[PawchiveDownloadItem],
        *,
        on_progress: Callable[[PawchiveProgress], None] | None = None,
        on_complete: Callable[[str, bool], None] | None = None,
    ) -> PawchiveDownloadPlan:
        """去重 → ``add_uri`` → 轮询 → 回调 → 记账。每 item 的 ``on_complete`` 恰好一次。"""
        if self._submitting:
            raise RuntimeError("submit already running")

        plan = PawchiveDownloadPlan([], [], [], [])
        candidates = list(items)
        if not candidates:
            return plan

        submittable: list[PawchiveDownloadItem] = []
        keys: list[str] = []
        for item in candidates:
            if resolve_attachment_url(item.attachment) is None:
                plan.failed_pre_submit.append(item)
                plan.submission_errors.append(
                    f"{item.identity_key[:8]} {item.out_name}: NO_URL"
                )
                continue
            submittable.append(item)
            keys.append(item.identity_key)
        if not submittable:
            return plan

        duplicated = self._recorder().batch_check_dupe(keys)
        pending: list[PawchiveDownloadItem] = []
        for item in submittable:
            if item.identity_key in duplicated:
                plan.deduped_skipped.append(item)
            else:
                pending.append(item)
        if not pending:
            return plan

        self._submitting = True
        rpc = self._injected_rpc
        own_rpc = rpc is None
        if own_rpc:
            from core.script.aria2 import create_managed_rpc_client

            rpc = create_managed_rpc_client()
        try:
            options = self._aria2_options()
            sem = asyncio.Semaphore(self._config.download_concurrency)
            results = await asyncio.gather(
                *[
                    self._run_item(rpc, item, sem, options, on_progress, on_complete)
                    for item in pending
                ],
                return_exceptions=True,
            )
            for item, result in zip(pending, results):
                if isinstance(result, BaseException):
                    plan.failed_pre_submit.append(item)
                    plan.submission_errors.append(
                        f"{item.identity_key[:8]} {item.out_name}: "
                        f"UNEXPECTED: {type(result).__name__}: {result}"
                    )
                    self._emit_complete(on_complete, item.identity_key, False)
                    continue
                outcome = result[1]
                if isinstance(outcome, PawchiveSubmittedFile):
                    plan.to_submit.append(outcome)
                else:
                    plan.failed_pre_submit.append(item)
                    plan.submission_errors.append(
                        f"{item.identity_key[:8]} {item.out_name}: {outcome}"
                    )
        finally:
            if own_rpc:
                await rpc.aclose()
            self._submitting = False
        return plan

    async def _run_item(
        self,
        rpc: "Aria2RpcClient",
        item: PawchiveDownloadItem,
        sem: asyncio.Semaphore,
        options: dict,
        on_progress: Callable[[PawchiveProgress], None] | None,
        on_complete: Callable[[str, bool], None] | None,
    ) -> tuple[PawchiveDownloadItem, "PawchiveSubmittedFile | str"]:
        """信号量包整个 item 生命周期（add_uri + 轮询），并发即真实在跑数。"""
        async with sem:
            try:
                return await self._download_one(
                    rpc, item, options, on_progress, on_complete
                )
            except Exception as exc:
                self._emit_complete(on_complete, item.identity_key, False)
                return item, f"UNEXPECTED: {type(exc).__name__}: {exc}"

    async def _download_one(
        self,
        rpc: "Aria2RpcClient",
        item: PawchiveDownloadItem,
        options: dict,
        on_progress: Callable[[PawchiveProgress], None] | None,
        on_complete: Callable[[str, bool], None] | None,
    ) -> tuple[PawchiveDownloadItem, "PawchiveSubmittedFile | str"]:
        url = resolve_attachment_url(item.attachment)
        if url is None:
            self._emit_complete(on_complete, item.identity_key, False)
            return item, "NO_URL"

        try:
            item.target_dir.mkdir(parents=True, exist_ok=True)
            await self._throttle.wait()
            gid = await rpc.add_uri(
                url,
                target_dir=item.target_dir,
                out=item.out_name,
                task_id=f"pawchive-{item.post.post_id}-{item.identity_key[:8]}",
                options=options,
            )
        except Exception as exc:
            self._emit_complete(on_complete, item.identity_key, False)
            return item, f"ADD_URI: {type(exc).__name__}: {exc}"

        failures = 0
        while True:
            try:
                status_payload = await rpc.tell_status(gid, keys=list(ARIA2_STATUS_KEYS))
            except Exception as exc:
                failures += 1
                if failures >= POLL_FAILURE_LIMIT:
                    self._emit_complete(on_complete, item.identity_key, False)
                    return item, f"POLL_FAILED×{failures}: {type(exc).__name__}"
                await asyncio.sleep(self._config.poll_interval_s)
                continue
            failures = 0

            status = str(status_payload.get("status") or "")
            files = status_payload.get("files") or []
            first_file = files[0] if files and isinstance(files[0], dict) else None
            completed_bytes = _as_int(status_payload.get("completedLength")) or 0
            total_bytes = _as_int(first_file.get("length")) if first_file else None

            self._emit_progress(
                on_progress,
                PawchiveProgress(
                    identity_key=item.identity_key,
                    state=_STATE_MAP.get(status, "waiting"),
                    completed_bytes=completed_bytes,
                    total_bytes=total_bytes,
                    detail=status,
                ),
            )

            if status == "complete":
                try:
                    self.mark_downloaded(item.identity_key)
                except Exception:
                    pass
                self._emit_complete(on_complete, item.identity_key, True)
                disk_path = str(first_file.get("path") or "") if first_file else ""
                return item, PawchiveSubmittedFile(
                    gid=gid,
                    identity_key=item.identity_key,
                    out_name=item.out_name,
                    disk_path=disk_path,
                )
            if status in ("error", "removed"):
                self._emit_complete(on_complete, item.identity_key, False)
                return item, (
                    f"STATUS: {status} code={status_payload.get('errorCode')} "
                    f"msg={status_payload.get('errorMessage')}"
                )
            await asyncio.sleep(self._config.poll_interval_s)

    @staticmethod
    def _emit_progress(
        callback: Callable[[PawchiveProgress], None] | None,
        event: PawchiveProgress,
    ) -> None:
        if callback is None:
            return
        try:
            callback(event)
        except Exception:
            pass

    @staticmethod
    def _emit_complete(
        callback: Callable[[str, bool], None] | None,
        identity_key: str,
        success: bool,
    ) -> None:
        if callback is None:
            return
        try:
            callback(identity_key, success)
        except Exception:
            pass

    # -- 收尾 ---------------------------------------------------------------

    async def aclose(self) -> None:
        """只复位自有状态：不关注入对象，也不关 ``SqlRecorder``（共享池 no-op）。"""
        self._submitting = False
        self._throttle.reset()
        self._recorder_obj = None



