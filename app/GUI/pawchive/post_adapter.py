"""pawchive 卡片适配器（stage 3 / P1）。

把服务层的 ``(post, attachment)`` 适配成卡片控件需要的 duck-typed post 对象：
一格一图，所以**一个 attachment 对应一个卡片项**，键为 ``(post_id, attachment_path)``。

依赖纪律：本模块只用标准库（``typing`` + ``dataclasses``），不 import ``app.GUI.*``，
也不 import ``core.website.pawchive.service``（服务层并行落码，这里按属性名鸭子类型
对接），以便将来独立于界面层演进。**卡片控件不在本模块**（见 ``card.py``），
别把「纯 stdlib」误读为「卡也在这里」。

扩展名集合是本模块自有的（不 import danbooru 的 constants）：
pawchive 是独立站点，判定规则必须能单独演进。
"""

import typing as t
from dataclasses import dataclass, field

IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "gif"})
VIDEO_EXTENSIONS = frozenset({"mp4", "webm"})
ARCHIVE_EXTENSIONS = frozenset({"zip"})

# IA v4 §3.4：CardKey 两段皆 str，不反解 sha（R7-b）。
CardKey = t.Tuple[str, str]


def normalize_file_ext(value: t.Optional[str]) -> str:
    """与 danbooru 侧 ``DanbooruPost.normalize_file_ext`` 同口径：去点、去空白、小写。"""
    return str(value or "").strip().lower().lstrip(".")


def file_ext_from_name(name: t.Optional[str]) -> str:
    """从附件 ``name`` 取扩展名；``name`` 可能带目录段，只看最后一段。"""
    leaf = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in leaf:
        return ""
    return normalize_file_ext(leaf.rsplit(".", 1)[-1])


def attachment_path(attachment: t.Any) -> str:
    return str(getattr(attachment, "path", "") or "")


def post_id_of(post: t.Any) -> str:
    return str(getattr(post, "post_id", "") or "")


def card_key(post: t.Any, attachment: t.Any) -> CardKey:
    """卡片键 = (post_id, attachment.path)，两段直接取，不做任何摘要/反解。"""
    return (post_id_of(post), attachment_path(attachment))


@dataclass(frozen=True, slots=True)
class PawchiveCardItem:
    """一个格子的全部数据。字段名刻意对齐 ``DanbooruCardWidget`` 读取的成员。

    提供 DanbooruCardWidget 读取的 14 个成员：post_id / preview_width / preview_height /
    image_width / image_height / is_downloadable / is_supported / preview_asset_is_video /
    is_archive / has_renderable_preview_asset / uses_viewer_video / viewer_video_media_ext /
    file_ext / preview_file_url。
    """

    post_id: str
    attachment_path: str
    name: str
    file_ext: str
    preview_url: t.Optional[str]
    downloadable: bool
    post: t.Any = field(repr=False, default=None)
    attachment: t.Any = field(repr=False, default=None)

    # ---- card.py 读取的成员 ----
    @property
    def preview_file_url(self) -> t.Optional[str]:
        return self.preview_url

    # API 完全不给尺寸（实测 post 无 width/height 键）。
    # 恒 0 让 card 走 metrics 兜底；IA v4 M1 要求预览尺寸只由 metrics 决定。
    preview_width = 0
    preview_height = 0
    image_width = 0
    image_height = 0

    @property
    def is_archive(self) -> bool:
        return self.file_ext in ARCHIVE_EXTENSIONS

    @property
    def preview_asset_is_video(self) -> bool:
        return self.file_ext in VIDEO_EXTENSIONS

    @property
    def has_renderable_preview_asset(self) -> bool:
        return bool(self.preview_url) and (
            self.file_ext in IMAGE_EXTENSIONS or self.file_ext in VIDEO_EXTENSIONS
        )

    @property
    def is_supported(self) -> bool:
        return self.file_ext in IMAGE_EXTENSIONS or self.file_ext in VIDEO_EXTENSIONS

    @property
    def uses_viewer_video(self) -> bool:
        return self.file_ext in VIDEO_EXTENSIONS

    @property
    def viewer_video_media_ext(self) -> str:
        return self.file_ext if self.file_ext in VIDEO_EXTENSIONS else ""

    @property
    def is_downloadable(self) -> bool:
        return bool(self.downloadable)

    @property
    def key(self) -> CardKey:
        return (self.post_id, self.attachment_path)

    @property
    def published(self) -> str:
        """帖子发布日期（"YYYY-MM-DD…"）。时间范围选择靠它，不看文件名/顺序。"""
        return str(getattr(self.post, "published", "") or "")


@dataclass(frozen=True, slots=True)
class PawchivePostGroup:
    """帖级分组信息（组头用）。"""

    post_id: str
    title: str
    published: str
    count: int
    items: t.Tuple[PawchiveCardItem, ...]
    collapsed_limit: int

    @property
    def hidden_count(self) -> int:
        return max(0, self.count - self.collapsed_limit)

    @property
    def is_empty(self) -> bool:
        return self.count == 0


def merged_attachments(post: t.Any, *, service_attachments: t.Optional[t.Sequence[t.Any]] = None) -> list:
    """单帖图片集合 = ``[file] + attachments``（file 不在 attachments 里）。

    ``service_attachments`` 允许直接传服务层 ``service.attachments(post)`` 的结果，
    否则本地按同规则合并，便于无服务层时自测。
    """
    if service_attachments is not None:
        return list(service_attachments)
    out: list = []
    file = getattr(post, "file", None)
    if file:
        out.append(file)
    for item in getattr(post, "attachments", None) or ():
        if item and item not in out:
            out.append(item)
    return out


def build_card_item(
    post: t.Any,
    attachment: t.Any,
    *,
    preview_url: t.Optional[str],
    downloadable: bool,
) -> PawchiveCardItem:
    """构造单个格子。``preview_url`` 与 ``downloadable`` 由调用方从服务层取。

    - ``preview_url`` 走服务层 ``thumbnail_url(attachment)``（只拼 img 缩略图域）。
    - ``downloadable`` 走服务层 ``resolve_download_url(attachment) is not None``（D-14）。
    """
    name = str(getattr(attachment, "name", "") or "")
    return PawchiveCardItem(
        post_id=post_id_of(post),
        attachment_path=attachment_path(attachment),
        name=name,
        # 现盘写成 ``file_ext_from_name(...) or normalize_file_ext(...)``：两段同源，
        # 后半段恒不生效（死表达式，01 §2.1），只留前者。
        file_ext=file_ext_from_name(name),
        preview_url=preview_url,
        downloadable=downloadable,
        post=post,
        attachment=attachment,
    )


def build_post_group(
    post: t.Any,
    *,
    thumbnail_url: t.Callable[[t.Any], t.Optional[str]],
    resolve_downloadable: t.Callable[[t.Any], bool],
    collapsed_limit: int = 12,
    service_attachments: t.Optional[t.Sequence[t.Any]] = None,
) -> PawchivePostGroup:
    """构造一个帖分组。零附件的帖返回 ``count == 0`` 的组（由网格决定不建组框）。"""
    attachments = merged_attachments(post, service_attachments=service_attachments)
    items = tuple(
        build_card_item(
            post,
            attachment,
            preview_url=thumbnail_url(attachment),
            downloadable=resolve_downloadable(attachment),
        )
        for attachment in attachments
        if attachment is not None
    )
    return PawchivePostGroup(
        post_id=post_id_of(post),
        title=str(getattr(post, "title", "") or ""),
        published=str(getattr(post, "published", "") or ""),
        count=len(items),
        items=items,
        collapsed_limit=max(1, int(collapsed_limit)),
    )
