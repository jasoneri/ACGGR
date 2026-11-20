"""pawchive L3 详情浮窗控制器（P3）。

职责：
1. 序列：翻页序列 = 已加载结果列表（PawchivePostGroup）按帖展开的附件序列；折叠只影响
   视图格数；翻页零额外请求（V-7）。next_id / prev_id 不作主路径。
2. 取图：一律经 core.website.pawchive.api.fetch_bytes，且只在 worker 线程调用；worker 只搬
   bytes，QPixmap 只在主线程构造；不自带任何 UA / header / 网络库。
3. 下载本图：plan_items + submit 单张走 run_async 与任务线程；落盘名 / 目录 / 后缀去重全归 04。
4. 已下载态：以网格账本（grid.is_downloaded）为先，服务层 is_downloaded 兜底。

不 import app.GUI.danbooru.*；不读 danbooru_cfg；不改 res/。
"""

import asyncio
import logging
import time
import typing as t
from collections import OrderedDict

from PySide6 import QtCore
from PySide6.QtCore import qtTrId
from PySide6.QtGui import QPixmap

from ui.async_task import AsyncTaskManager

from .post_adapter import CardKey
from .preview_loader import is_animated_image, probe_image_size
from .viewer import (
    PawchiveImageViewer,
    PawchiveViewerItem,
    PawchiveViewerSource,
)
from app.GUI.viewer.image_pipeline import decode_scaled, throughput_of

# 原图/缩略图自适应网关（agentA 的 source_engine.py / settings.py）。模块级安全 import，
# 任一依赖缺席都降级为既有「resolve 原样取图」路径，绝不让浮窗炸在 import 期。
try:  # noqa: SIM105 - 依赖缺席是合法降级路径
    from app.GUI.viewer.source_engine import (
        SourceProbe, choose_source, estimate_bytes, speed_of,
    )
    from app.GUI.viewer.settings import viewer_source_mode
except Exception:  # noqa: BLE001
    SourceProbe = None  # type: ignore[assignment]
    choose_source = None  # type: ignore[assignment]
    estimate_bytes = None  # type: ignore[assignment]
    speed_of = None  # type: ignore[assignment]
    viewer_source_mode = None  # type: ignore[assignment]


def _cached_speed(site: str) -> float:
    """只读进程内测速（GUI 线程安全，非阻塞，不触发任何网络请求）。

    ``ensure_speed`` 同步阻塞，取图前绝不能在主线程调它；这里只读 B1 后台测速写好
    的结果。引擎的 ``speed_of`` 是专门的只读接口：未命中/进行中/失败一律返回 -1.0，
    不写缓存、不写占位，不干扰后台测速。
    """
    if speed_of is None:
        return -1.0
    try:
        return float(speed_of(site))
    except Exception:  # noqa: BLE001
        return -1.0

#: 本模块经 ``_tr_or`` 使用的全部 i18n 键（``res/i18n/build.py`` 的 AST 间接通道，
#: 见 ``viewer._VIEWER_I18N_KEYS`` 的说明；新增/改名键时必须同步）。
_CONTROLLER_I18N_KEYS = (
    "pawchive.card.download_only",
    "pawchive.card.preview_error",
    "pawchive.download.failed",
    "pawchive.download.no_url",
    "pawchive.error.blocked",
    "pawchive.viewer.loading",
)

#: 取图超时（秒）：原图可达数 MB，给足但不无限等。
_FETCH_TIMEOUT_S = 30.0

#: 允许预览的 ``kind``：``image`` 才取图；``archive`` / ``unknown`` 只给提示。
_IMAGE_KIND = "image"
_ARCHIVE_KIND = "archive"

#: 原始字节小缓存上限（动图整段 bytes 比 pixmap 大，只留相邻几张）。
_RAW_CACHE_LIMIT = 4

#: 动图静态化诊断日志（见 :meth:`_on_fetch_done`）。默认 DEBUG，不影响正常运行。
_LOG = logging.getLogger(__name__)

#: 看起来是动图、却可能拿到静态首帧的扩展名（用于诊断，也参与 auto 选源）。
_ANIMATED_LIKE_EXTS = frozenset({"gif", "webp", "apng"})


def _looks_animated_attachment(attachment: t.Any) -> bool:
    """按附件扩展名判断「应当是动图」（仅诊断用；真实判定仍走 ``is_animated_image``）。"""
    from .post_adapter import file_ext_from_name

    name = str(getattr(attachment, "name", "") or "")
    path = str(getattr(attachment, "path", "") or "")
    return (file_ext_from_name(name) or file_ext_from_name(path)) in _ANIMATED_LIKE_EXTS


def _decide_source_url(
    attachment: t.Any,
    source: tuple[str, str],
    observed_bytes: int = 0,
) -> tuple[str, str]:
    """在 ``resolve`` 之后按 viewer 档位选原图/缩略图 URL（B2）。

    - video（mp4）一律短路，沿用调用方现状（本函数不该被 video 调用，保险起见也直返）。
    - 档位 off→缩略图、on→原图，跳过测速；auto 交引擎决策。
    - 目标 URL 取不到就保留 resolve 原值（不崩、不空手）。
    - 不改 ``resolve`` 自身的"原图域优先"逻辑（上一轮 GIF 修复）——本函数只在其后叠加。
    """
    if choose_source is None or SourceProbe is None:
        return source
    if PawchiveViewerSource.is_video_attachment(attachment):
        return source

    resolved_kind = source[0]
    mode = str(viewer_source_mode()) if viewer_source_mode is not None else "auto"
    if mode == "on":
        want = "original"
    elif mode == "off":
        want = "thumbnail"
    else:
        # auto：动图直接走原图。缩略图域对 gif 只返静态 WebP 首帧，走缩略图 = 必然不动；
        # 且附件取字节前 est 恒 0，速度网关会把动图误判成缩略图。静态图才走速度网关
        # （慢网缩略图先出画面、快网原图）。
        if _looks_animated_attachment(attachment):
            want = "original"
        else:
            # 主输入是**真实图片吞吐**（EWMA），JSON 探针只作回退：旧口径把
            # posts.json 的 12KB/250ms 当带宽（48 bytes/ms），把 300KB 全判成缩略图。
            bpm = throughput_of("pawchive") if throughput_of is not None else -1.0
            if bpm <= 0:
                bpm = _cached_speed("pawchive")
            # 体积输入优先级：附件自带 file_size > 实抓字节回填 > 读不到(0)。
            est = 0
            try:
                est = int(getattr(attachment, "file_size", 0) or 0)
            except (TypeError, ValueError):
                est = 0
            if est <= 0:
                est = int(observed_bytes or 0)
            probe = SourceProbe(
                bytes_per_ms=bpm,
                estimated_bytes=est,
                is_animated=False,
            )
            try:
                want = str(choose_source(mode, probe))
            except Exception:  # noqa: BLE001
                want = resolved_kind

    if want == resolved_kind:
        return source
    if want == "thumbnail":
        thumb = PawchiveViewerSource.thumbnail_fallback(attachment)
        if thumb:
            return ("thumbnail", thumb)
        return source
    original = PawchiveViewerSource.media_url(attachment)
    if original:
        return ("original", original)
    return source


def run_async(coro):
    """同步跑协程（worker 线程内）。**不跨 loop 复用**：每次调用现建现关。"""
    return asyncio.run(coro)


def _tr_or(key: str, fallback: str) -> str:
    text = qtTrId(key)
    return fallback if text == key else text


class _FetchOutcome:
    """worker → 主线程的唯一载荷：原始字节、已解码 QImage 或失败分类。"""

    __slots__ = ("from_thumbnail", "image", "kind", "raw", "status")

    def __init__(
        self,
        raw: bytes = b"",
        kind: str = "",
        status: t.Any = None,
        from_thumbnail: bool = False,
        image: t.Any = None,
    ):
        self.raw = raw
        self.kind = kind
        self.status = status
        #: 诊断用：True 表示这份 bytes 是原图 403 后回退缩略图域拿到的（通常只有静态首帧）。
        self.from_thumbnail = bool(from_thumbnail)
        #: worker 内按视口尺寸解出的 QImage（静态图）；动图为 None，raw 留给 QMovie。
        self.image = image


def _pawchive_referer() -> str:
    try:
        from core.website.pawchive import domain
    except Exception:  # noqa: BLE001 - 取不到域也不该挡住取图
        return "https://pawchive.pw/"
    return f"https://{domain or 'pawchive.pw'}/"


def _fetch_raw(url: str) -> bytes:
    """worker 内同步取字节。**唯一网络入口**（03 的 ``fetch_bytes``）。"""
    from core.website.pawchive.api import fetch_bytes

    return fetch_bytes(url, referer=_pawchive_referer(), timeout=_FETCH_TIMEOUT_S)


class PawchiveViewerController(QtCore.QObject):
    """浮窗控制器：序列 / 取图 / 下载 / 已下载态。"""

    def __init__(
        self,
        viewer: PawchiveImageViewer,
        *,
        service_factory: t.Optional[t.Callable[[], t.Any]] = None,
        task_mgr: t.Optional[AsyncTaskManager] = None,
        parent=None,
    ):
        super().__init__(parent or viewer)
        self.viewer = viewer
        self._service_factory = service_factory or self._default_service_factory
        self._task_mgr = task_mgr or AsyncTaskManager(viewer)
        self._tab: t.Optional[t.Any] = None
        self._item: t.Optional[PawchiveViewerItem] = None
        self._prefetching: set[str] = set()
        #: 原始字节小缓存：动图必须留 bytes 才能建 QMovie（只留首帧 pixmap 会"点开不动"）。
        self._raw_cache: "OrderedDict[str, bytes]" = OrderedDict()
        #: 两阶段加载：已升级到原图（动画）的 key，缩略图后到不得覆盖它。
        self._upgraded_keys: set[str] = set()
        #: 已用网格缩略图垫过首帧的键（阶段1 已完成，不必再取缩略图域）。
        self._seeded_keys: set[str] = set()
        #: 实抓到的原图字节数（附件无 file_size 时给 auto 当体积输入）。
        self._observed_bytes: dict[str, int] = {}
        #: 换项代号：翻页/换档后旧回包按代号丢弃，避免旧图落到新项。
        self._generation: int = 0
        #: 上一次换项时刻：用于快速连翻时的 300ms 防抖（正常节奏不额外等待）。
        self._last_show_at: float = 0.0
        #: 本次换项是否发生在上一项 300ms 内（快速连翻才延迟发起，避免每格闪一下）。
        self._rapid_nav: bool = False
        viewer.previous_requested.connect(lambda: self.open_adjacent(-1))
        viewer.next_requested.connect(lambda: self.open_adjacent(1))
        viewer.download_requested.connect(self._on_download_requested)
        viewer.closed.connect(self._on_closed)
        # 档位热切换：设置里改 自动/开/关 后，活着的浮窗按新档重出当前项。
        # 订阅放共享 bus（两站纪律禁止彼此 import），构造一次即可。
        self._subscribe_source_mode()

    def _subscribe_source_mode(self) -> None:
        try:
            from app.GUI.viewer.settings import subscribe_source_mode_changed
        except Exception:  # noqa: BLE001 - 共享层缺席是合法降级
            return
        subscribe_source_mode_changed(self._on_source_mode_changed)

    def _on_source_mode_changed(self, _value: str = "") -> None:
        """设置侧改档后回调：丢弃当前项旧源字节，按新档重出图（无则静默）。

        只处理**图像**项——视频源不随档位变；且只在新值确实与旧值不同时动作，
        避免开档瞬间的冗余重取。
        """
        item = self._item
        if item is None or self._tab is None:
            return
        if PawchiveViewerSource.is_video_attachment(item.attachment):
            return
        key = item.identity_key
        # 旧源（可能是上一档取的原图/缩略图）不能再复用：清掉两级缓存再重取。
        # 只走 `_render_item`（不重新 show_item）：保持当前缩放/窗口位置，只把源换掉。
        self.viewer.drop_pixmap(key)
        self._raw_cache.pop(key, None)
        self._upgraded_keys.discard(key)
        self._render_item(item)

    def _report_viewer_error(self, current_key: str, text: str) -> None:
        """错误反馈统一走 viewer 的 InfoBar（R6），parent 归 viewer 实例。

        main 会在两个 viewer.py 上加 ``show_error_infobar(text)``（parent=self 的
        InfoBar + AcggrIcon）。缺该方法时回退 set_placeholder，保证 import/运行不炸。
        仅用于**错误**文案；Loading / No more posts 等临时状态仍用 label。
        """
        viewer = self.viewer
        fn = getattr(viewer, "show_error_infobar", None)
        if callable(fn):
            fn(str(text or ""))
            return
        viewer.set_placeholder(current_key, str(text or ""))

    # ---- 服务层（鸭子类型，缺配置时静默降级） ----
    @staticmethod
    def _default_service_factory():
        from core.website.pawchive.service import PawchiveService

        return PawchiveService()

    def _service(self):
        try:
            return self._service_factory()
        except Exception:  # noqa: BLE001 - 缺配置/无代理：预览仍可用，下载会失败
            return None

    # ---- 序列（零额外请求） ----
    def _sequence(self, tab) -> list[PawchiveViewerItem]:
        """展开序列：帖顺序 × ``attachments()`` 顺序（``file`` 前置）。

        结果列表就是 tab 已加载的 ``PawchivePostGroup`` 序列（每次 ``append_page``
        追加）；折叠只是不渲染，不影响本序列。
        """
        result_list = list(getattr(tab, "result_list", None) or ())
        service = self._service()
        seq: list[PawchiveViewerItem] = []
        for group in result_list:
            # 右侧资产列表里的不可预览条目永不进 viewer；只有瀑布流里的可渲染卡
            # （图片 / 有缩略图的视频）参与上一张/下一张序列。
            items = [
                item
                for item in (getattr(group, "items", None) or ())
                if item.has_renderable_preview_asset
            ]
            total = len(items)
            for index, card_item in enumerate(items):
                post = getattr(card_item, "post", None)
                attachment = getattr(card_item, "attachment", None)
                if post is None or attachment is None:
                    continue
                seq.append(
                    PawchiveViewerItem(
                        post=post,
                        attachment=attachment,
                        identity_key=self._identity_key(service, post, attachment),
                        index=index,
                        total=max(1, total),
                        seq=len(seq),
                        seq_total=0,
                    )
                )
        total = len(seq)
        if total:
            seq = [
                PawchiveViewerItem(
                    post=item.post,
                    attachment=item.attachment,
                    identity_key=item.identity_key,
                    index=item.index,
                    total=item.total,
                    seq=item.seq,
                    seq_total=total,
                )
                for item in seq
            ]
        return seq

    @staticmethod
    def _identity_key(service, post, attachment) -> str:
        """identity 由 04 纯函数算（UI 不自己拼 sha1）。服务层不可用时退回本地键。"""
        if service is not None:
            try:
                return str(service.identity_key(post, attachment))
            except Exception:  # noqa: BLE001 - path 为空等脏数据
                pass
        post_id = str(getattr(post, "post_id", "") or "")
        path = str(getattr(attachment, "path", "") or "")
        return f"{post_id}:{path}"

    @staticmethod
    def _card_key(item: PawchiveViewerItem) -> CardKey:
        return (
            str(getattr(item.post, "post_id", "") or ""),
            str(getattr(item.attachment, "path", "") or ""),
        )

    def _find_seq(self, sequence: t.Sequence[PawchiveViewerItem], key: CardKey) -> int:
        for item in sequence:
            if self._card_key(item) == key:
                return item.seq
        return -1

    # ---- 打开 / 翻页 ----
    def open_key(self, tab, key: CardKey) -> bool:
        """卡片单击入口：``CardKey`` → 序列项 → 显示。返回是否打开。"""
        if tab is not self._tab:
            self._raw_cache.clear()
        self._tab = tab
        sequence = self._sequence(tab)
        index = self._find_seq(sequence, (str(key[0]), str(key[1])))
        if index < 0:
            return False
        self._show(sequence[index])
        return True

    def open_adjacent(self, step: int) -> bool:
        """``seq ± step``：越界直接 return（不发请求、不拉下一页）。"""
        if self._item is None or self._tab is None:
            return False
        sequence = self._sequence(self._tab)
        current = self._find_seq(sequence, self._card_key(self._item))
        target = current + int(step)
        if current < 0 or target < 0 or target >= len(sequence):
            return False
        self._show(sequence[target])
        return True

    def sync_navigation(self) -> None:
        """``seq > 0`` ⇒ 可上一张；``seq < seq_total-1`` ⇒ 可下一张。"""
        item = self._item
        if item is None:
            self.viewer.set_navigation_enabled(False, False)
            return
        self.viewer.set_navigation_enabled(item.seq > 0, item.seq < item.seq_total - 1)

    def _show(self, item: PawchiveViewerItem) -> None:
        now = time.monotonic()
        self._rapid_nav = self._last_show_at > 0 and (now - self._last_show_at) < 0.3
        self._last_show_at = now
        self._generation += 1
        self._item = item
        author_name = str(getattr(self._tab, "author_name", "") or "")
        # 字节已在本地缓存（预取/上一次看过的动图）时，先探尺寸再开窗：
        # ``show_item(source_size=...)`` 会让**首帧**就是最终 fit 尺寸，不出现占位
        # 窗口。raw 不在缓存里（冷开、要走网络）就传 None，退回占位 + 解码后 fit。
        self.viewer.show_item(
            item,
            downloaded=self._is_downloaded(item),
            author_name=author_name,
            source_size=self._probe_cached_size(item.identity_key),
        )
        # 0 网络首帧：把网格里已有的缩略图先贴上（不写缓存/不改尺寸链），
        # 后台的 preview/original 到达后再无缝替换。
        self._seed_from_card(item)
        self.sync_navigation()
        self._render_item(item)

    def _seed_from_card(self, item: PawchiveViewerItem) -> None:
        """用网格卡已有的缩略图垫首帧：纯内存操作、不发请求。"""
        if PawchiveViewerSource.is_video_attachment(item.attachment):
            return
        key = item.identity_key
        if self.viewer.cached_pixmap(key) is not None:
            return
        grid = getattr(self._tab, "grid", None)
        lookup = getattr(grid, "card_for_key", None)
        if not callable(lookup):
            return
        try:
            card = lookup(self._card_key(item))
        except Exception:  # noqa: BLE001 - 卡片可能已被回收
            card = None
        getter = getattr(card, "preview_pixmap", None)
        if not callable(getter):
            return
        try:
            pixmap = getter()
        except Exception:  # noqa: BLE001
            return
        if pixmap is None or pixmap.isNull():
            return
        if self.viewer.seed_pixmap(key, pixmap):
            self._seeded_keys.add(key)

    def _decode_edge(self) -> int:
        """按当前视口算 preview 档解码边（最长边×DPR，clamp [512,2048]）。"""
        try:
            width = int(self.viewer.width())
            height = int(self.viewer.height())
            dpr = float(self.viewer.devicePixelRatioF())
        except Exception:  # noqa: BLE001
            return 1024
        longest = max(width, height) * max(1.0, dpr)
        return max(512, min(int(longest), 2048))

    def _probe_cached_size(self, identity_key: str) -> t.Optional[QtCore.QSize]:
        """本地 raw 缓存里的真实尺寸；没有缓存/探不出 → ``None``（=未知）。

        只读内存缓存，**不发请求**：探针的存在不能增加网络开销（07 零额外请求）。
        ``None`` 让调用点读起来就是"没有 hint"，避免把无效 ``QSize(-1,-1)`` 当尺寸用。
        """
        raw = self._raw_cache.get(identity_key)
        if not raw:
            return None
        size = probe_image_size(raw)
        if size.width() <= 0 or size.height() <= 0:
            return None
        return size

    # ---- 已下载态 ----
    def _is_downloaded(self, item: PawchiveViewerItem) -> bool:
        grid = getattr(self._tab, "grid", None)
        checker = getattr(grid, "is_downloaded", None)
        if callable(checker):
            try:
                if checker(self._card_key(item)):
                    return True
            except Exception:  # noqa: BLE001 - 账本不可读时退回服务层
                pass
        service = self._service()
        if service is None:
            return False
        try:
            return bool(service.is_downloaded(item.identity_key))
        except Exception:  # noqa: BLE001 - sql 不可用时按未下载处理
            return False

    # ---- 取图 ----
    def _render_item(self, item: PawchiveViewerItem) -> None:
        """按 ``kind`` 分派：image 取图，archive/unknown 给提示（不取图）。

        **视频先短路**：mp4/webm 由 ``show_item`` → ``_load_video_item`` 交给
        QtMultimedia 播放；绝不能落到下面的图片 ``resolve()`` 分支——那里对视频
        恒返回 ``None``，会覆盖播放面并弹「仅可下载 / 无 URL」，这就是
        "mp4 点开只看到报错"的根因。
        """
        key = item.identity_key
        if PawchiveViewerSource.is_video_attachment(item.attachment):
            self._prefetch_neighbours(item)
            return
        cached_raw = self._raw_cache.get(key)
        if cached_raw:
            self._raw_cache.move_to_end(key)
            if is_animated_image(cached_raw) and self.viewer.set_animated_image(key, cached_raw):
                self._prefetch_neighbours(item)
                return
        cached = self.viewer.cached_pixmap(key)
        if cached is not None and not cached.isNull():
            self.viewer.set_image(key, cached)
            self._prefetch_neighbours(item)
            return

        kind = str(getattr(item.attachment, "kind", "") or "")
        # 取图与"能否下载"分开判断：只要有 ``path`` 就能拼出缩略图，先把图显示出来；
        # 只有连缩略图都拼不出（无 path）才给占位文案。否则会出现"网格里有缩略图、
        # 点开却说没图"的反差（现场事故，见 ``PawchiveViewerSource.resolve``）。
        source = PawchiveViewerSource.resolve(item.attachment)
        if source is None:
            if kind == _ARCHIVE_KIND:
                # "仅可下载"是不可看状态：走 viewer InfoBar，不在图像/视频区留文字 label。
                self._report_viewer_error(
                    key, _tr_or("pawchive.card.download_only", "Download only")
                )
            else:
                self._report_viewer_error(
                    key, _tr_or("pawchive.download.no_url", "No download url")
                )
            return
        # B2：resolve 保持"原图域优先"不动，档位决策在其后叠加（video 已在 resolve
        # 短路为 None，这里不会再进来；_decide_source_url 内再兜一层短路）。
        source = _decide_source_url(
            item.attachment, source, self._observed_bytes.get(key, 0)
        )
        self.viewer.set_loading(True)
        seeded = key in self._seeded_keys
        # 两阶段对**所有**图片：先出缩略图（网格已垫过则阶段1已完成），原图到了再升级。
        # 旧实现只对动图开，静态大图只能干等全尺寸下载 —— 5s 体感的主因之一。
        if source[0] == "original":
            # 升级窗开闸：缩略图/网格首帧上屏后进度条**继续转**（1→2 过渡），
            # 直到原图落地/失败由 _on_fetch_done/_on_fetch_failed 收口。
            self.viewer.set_upgrading(True)
            if seeded:
                self._seeded_keys.discard(key)
            else:
                self._fetch_thumbnail_fast(item)
        elif seeded:
            # off 档：网格缩略图就是终态，不再取图。
            self._seeded_keys.discard(key)
            self.viewer.set_loading(False)
            self.viewer.set_upgrading(False)
            self._prefetch_neighbours(item)
            return
        self._fetch_item(item, source)
        self._prefetch_neighbours(item)

    def _schedule_fetch(self, item: PawchiveViewerItem, generation: int, starter: t.Callable[[], None]) -> None:
        """快速连翻时延迟 300ms 再发请求；正常节奏（上一项 >300ms）立即发。

        seed 走本地内存、永不走这里；延迟只为避免"每格都闪一下"的请求风暴。
        """
        if not self._rapid_nav:
            starter()
            return

        def run_if_current():
            if self._generation != generation or self._item is not item:
                return
            starter()

        QtCore.QTimer.singleShot(300, run_if_current)

    def _fetch_thumbnail_fast(self, item: PawchiveViewerItem) -> None:
        """阶段1：缩略图域秒取静态首帧，让图像区立刻有画面（原图后台继续）。"""
        key = item.identity_key
        thumb = PawchiveViewerSource.thumbnail_fallback(item.attachment)
        if not thumb:
            return
        self._upgraded_keys.discard(key)  # 新一轮换项，清掉旧升级标记
        generation = self._generation
        edge = min(1024, self._decode_edge())

        def task() -> _FetchOutcome:
            started = time.perf_counter()
            raw = _fetch_raw(thumb)
            data = bytes(raw or b"")
            if data:
                from app.GUI.viewer.image_pipeline import record_throughput

                record_throughput("pawchive", len(data), (time.perf_counter() - started) * 1000.0)
            return _FetchOutcome(raw=data, image=decode_scaled(data, edge) if data else None)

        self._schedule_fetch(
            item,
            generation,
            lambda: self._task_mgr.execute_simple_task(
                task,
                success_callback=lambda outcome, _item=item: self._on_thumbnail_done(_item, outcome),
                error_callback=lambda _msg, _i=item: None,  # 缩略图失败静默，等原图
                show_success_info=False,
                show_error_info=False,
                show_tooltip=False,
                task_id=f"pawchive-viewer-thumb-{key[:12]}",
            ),
        )

    def _on_thumbnail_done(self, item: PawchiveViewerItem, outcome: _FetchOutcome) -> None:
        """阶段1落地：仅当仍是当前项、原图尚未升级、且画布无图时显示静态首帧。"""
        key = item.identity_key
        if self._item is None or key != self._item.identity_key:
            return
        if key in self._upgraded_keys:
            return  # 原图/动画已到，缩略图后到不得覆盖
        image = getattr(outcome, "image", None)
        raw = bytes(getattr(outcome, "raw", b"") or b"")
        if image is not None and not image.isNull():
            self.viewer.set_size_hint(key, QtCore.QSize(image.width(), image.height()))
            if self.viewer._current_display_image() is None:
                self.viewer.set_image(key, QPixmap.fromImage(image))
            return
        if not raw:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            return
        if self.viewer._current_display_image() is None:
            self.viewer.set_size_hint(key, probe_image_size(raw))
            self.viewer.set_image(key, pixmap)  # set_image 会停进度条 → 用户立刻看到图

    def _fetch_item(self, item: PawchiveViewerItem, source: tuple[str, str]) -> None:
        key = item.identity_key
        url = source[1]
        thumbnail = PawchiveViewerSource.thumbnail_fallback(item.attachment)
        generation = self._generation
        edge = self._decode_edge()
        animated = _looks_animated_attachment(item.attachment)

        def task(_url=url, _thumb=thumbnail) -> _FetchOutcome:
            started = time.perf_counter()
            try:
                raw = _fetch_raw(_url)
            except Exception as exc:  # noqa: BLE001 - 分类在主线程做
                kind = str(getattr(exc, "kind", "") or "network")
                status = getattr(exc, "status", None)
                if _thumb and (kind == "blocked" or str(status) == "403"):
                    try:
                        raw = _fetch_raw(_thumb)
                    except Exception as retry_exc:  # noqa: BLE001
                        return _FetchOutcome(
                            kind=str(getattr(retry_exc, "kind", "") or "network"),
                            status=getattr(retry_exc, "status", None),
                        )
                    return _FetchOutcome(raw=bytes(raw or b""), from_thumbnail=True)
                return _FetchOutcome(kind=kind, status=status)
            data = bytes(raw or b"")
            if data:
                from app.GUI.viewer.image_pipeline import record_throughput

                record_throughput("pawchive", len(data), (time.perf_counter() - started) * 1000.0)
                self._observed_bytes[key] = len(data)
            # 动图不解码整段（QMovie 要 raw）；静态图按视口尺寸解出 QImage，主线程 fromImage。
            image = None if animated else decode_scaled(data, edge)
            return _FetchOutcome(raw=data, image=image)

        self._schedule_fetch(
            item,
            generation,
            lambda: self._task_mgr.execute_simple_task(
                task,
                success_callback=lambda outcome, _item=item: self._on_fetch_done(_item, outcome),
                error_callback=lambda message, _item=item: self._on_fetch_failed(_item, message),
                show_success_info=False,
                show_error_info=False,
                show_tooltip=False,
                task_id=f"pawchive-viewer-{key[:12]}",
            ),
        )

    def _on_fetch_done(self, item: PawchiveViewerItem, outcome: _FetchOutcome) -> None:
        if item.identity_key != (self._item.identity_key if self._item else ""):
            return  # 回包键 ≠ 当前键：丢弃
        raw = bytes(getattr(outcome, "raw", b"") or b"")
        if not raw:
            self._on_fetch_failed(item, str(getattr(outcome, "kind", "") or "network"))
            return
        # 解码前先定画布：同一份 bytes 只读文件头（不二次解码、不额外请求），让
        # "LOADING 占位尺寸 → fit 尺寸"的跳变发生在落图前。探不出尺寸 → 返回 False，
        # 原有"占位 → 解码后 fit"路径照旧（不阻塞预览）。
        self.viewer.set_size_hint(item.identity_key, probe_image_size(raw))
        # 动图：交给 QMovie（首帧也会立刻落画布）；静态图走原有 pixmap 路径。
        animated = is_animated_image(raw)
        if not animated and _looks_animated_attachment(item.attachment):
            # 诊断：附件扩展名是动图、字节却被判静态——典型成因是原图 403 后回退到
            # img./thumbnail（只返静态首帧）或服务端 temp_url 直接给了静态预览。仅记录，
            # 不改播放判定，避免把静态字节硬塞给 QMovie 引起误播/崩溃。
            _LOG.debug(
                "pawchive viewer 动图静态化: key=%s from_thumbnail=%s magic=%s bytes=%d",
                item.identity_key,
                bool(getattr(outcome, "from_thumbnail", False)),
                raw[:12].hex(),
                len(raw),
            )
        # 原图（阶段2）已到：标记升级，缩略图后到不得再覆盖；同时关升级窗——
        # 终态落地，进度条该停（set_animated_image/set_image 内部也会各自停）。
        self._upgraded_keys.add(item.identity_key)
        self.viewer.set_upgrading(False)
        if animated and self.viewer.set_animated_image(item.identity_key, raw):
            self._cache_raw(item.identity_key, raw)
            self._prefetch_neighbours(item)
            return
        # 静态图优先用 worker 里按视口解好的 QImage（免主线程整图解码）；
        # 动图或解码失败才回落到 raw 全尺寸 loadFromData。
        image = getattr(outcome, "image", None)
        if image is not None and not image.isNull():
            self.viewer.set_image(item.identity_key, QPixmap.fromImage(image))
            self._prefetch_neighbours(item)
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            self._report_viewer_error(
                item.identity_key, _tr_or("pawchive.card.preview_error", "Preview failed")
            )
            return
        self.viewer.set_image(item.identity_key, pixmap)
        self._prefetch_neighbours(item)

    def _cache_raw(self, key: str, raw: bytes) -> None:
        data = bytes(raw or b"")
        if not key or not data:
            return
        self._raw_cache[key] = data
        self._raw_cache.move_to_end(key)
        while len(self._raw_cache) > _RAW_CACHE_LIMIT:
            self._raw_cache.popitem(last=False)

    def _on_fetch_failed(self, item: PawchiveViewerItem, message: str) -> None:
        if self._item is None or item.identity_key != self._item.identity_key:
            return
        # 终态（失败）：关闭升级窗，别让两阶段进度条在报错后还空转。
        self.viewer.set_upgrading(False)
        if "403" in str(message) or "blocked" in str(message):
            self.viewer.show_blocked_hint()
            return
        self._report_viewer_error(
            item.identity_key, _tr_or("pawchive.card.preview_error", "Preview failed")
        )

    def _prefetch_neighbours(self, item: PawchiveViewerItem) -> None:
        """预取前后各 2 张（失败静默）：缓存命中的翻页瞬开。

        体积上限：``file_size > 8MB`` 的邻图跳过（防内存/带宽被邻图吃掉）。
        """
        if self._tab is None:
            return
        sequence = self._sequence(self._tab)
        for offset in (-2, -1, 1, 2):
            target = item.seq + offset
            if target < 0 or target >= len(sequence):
                continue
            neighbour = sequence[target]
            if str(getattr(neighbour.attachment, "kind", "") or "") != _IMAGE_KIND:
                continue
            try:
                if int(getattr(neighbour.attachment, "file_size", 0) or 0) > 8 * 1024 * 1024:
                    continue
            except (TypeError, ValueError):
                pass
            key = neighbour.identity_key
            if key in self._prefetching or self.viewer.cached_pixmap(key) is not None:
                continue
            source = PawchiveViewerSource.resolve(neighbour.attachment)
            if source is None:
                continue
            source = _decide_source_url(neighbour.attachment, source, self._observed_bytes.get(key, 0))
            self._prefetching.add(key)
            self._fetch_prefetch(key, source[1])

    def _fetch_prefetch(self, key: str, url: str) -> None:
        edge = self._decode_edge()

        def task(_url=url) -> _FetchOutcome:
            started = time.perf_counter()
            try:
                raw = _fetch_raw(_url)
            except Exception:  # noqa: BLE001 - 预取失败静默
                return _FetchOutcome()
            data = bytes(raw or b"")
            if data:
                from app.GUI.viewer.image_pipeline import record_throughput

                record_throughput("pawchive", len(data), (time.perf_counter() - started) * 1000.0)
            return _FetchOutcome(raw=data, image=decode_scaled(data, edge) if data else None)

        self._task_mgr.execute_simple_task(
            task,
            success_callback=lambda outcome, _key=key: self._on_prefetch_done(_key, outcome),
            error_callback=lambda _message, _key=key: self._prefetching.discard(_key),
            show_success_info=False,
            show_error_info=False,
            show_tooltip=False,
            task_id=f"pawchive-viewer-prefetch-{key[:12]}",
        )

    def _on_prefetch_done(self, key: str, outcome: _FetchOutcome) -> None:
        self._prefetching.discard(key)
        data = bytes(getattr(outcome, "raw", b"") or b"")
        if not data:
            return
        self._observed_bytes.setdefault(key, len(data))
        # 动图的整段字节也要留下：只缓存首帧 pixmap 会让"预取过的 GIF"点开是静态图。
        if is_animated_image(data):
            self._cache_raw(key, data)
        # 静态图用 worker 里已按视口解好的 QImage（免主线程整图解码）。
        image = getattr(outcome, "image", None)
        if image is not None and not image.isNull():
            self.viewer.cache_pixmap(key, QPixmap.fromImage(image))

    # ---- 下载本图（plan_items + submit，单张） ----
    def _on_download_requested(self, item: PawchiveViewerItem) -> None:
        if self._item is None or item.identity_key != self._item.identity_key:
            return
        if not PawchiveViewerSource.is_downloadable(item.attachment):
            return
        self.viewer.set_download_pending(True)
        path = str(getattr(item.attachment, "path", "") or "")
        post = item.post
        identity_key = item.identity_key

        def task() -> tuple[int, int, str]:
            service = self._service()
            if service is None:
                return 0, 1, "service unavailable"
            try:
                items = run_async(service.plan_items(post, [path]))
                if not items:
                    return 0, 1, "no item"
                plan = run_async(service.submit(items))
                errors = "; ".join(str(e) for e in plan.submission_errors)
                return len(plan.to_submit), len(plan.failed_pre_submit), errors
            except Exception as exc:  # noqa: BLE001 - 失败文案回主线程再拼
                return 0, 1, f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    run_async(service.aclose())
                except Exception:  # noqa: BLE001
                    pass

        self._task_mgr.execute_simple_task(
            task,
            success_callback=lambda result, _key=identity_key: self._on_download_done(
                _key, result
            ),
            error_callback=lambda message, _key=identity_key: self._on_download_failed(
                _key, message
            ),
            show_success_info=False,
            show_error_info=False,
            show_tooltip=False,
            task_id=f"pawchive-viewer-download-{identity_key[:12]}",
        )

    def _on_download_done(self, identity_key: str, result) -> None:
        submitted, failed, errors = (0, 1, "")
        if isinstance(result, tuple) and len(result) >= 3:
            submitted, failed, errors = int(result[0]), int(result[1]), str(result[2])
        if submitted > 0:
            self._apply_downloaded(identity_key)
            return
        self._on_download_failed(identity_key, errors or "submit failed")

    def _on_download_failed(self, identity_key: str, message: str) -> None:
        if self._item is None or identity_key != self._item.identity_key:
            return
        self.viewer.set_download_pending(False)
        text = str(message or "")
        if "403" in text or "blocked" in text.lower():
            self.viewer.show_blocked_hint()
            return
        self.viewer.show_status_hint(
            _tr_or("pawchive.download.failed", "Download failed: {error}").format(
                error=text.splitlines()[0] if text else ""
            )
        )

    def _apply_downloaded(self, identity_key: str) -> None:
        """下载完成回灌：浮窗按钮置灰 + 网格账本置灰（键走 CardKey，不碰选中集）。"""
        if self._item is not None and identity_key == self._item.identity_key:
            self.viewer.set_download_state(True)
            grid = getattr(self._tab, "grid", None)
            marker = getattr(grid, "mark_downloaded", None)
            if callable(marker):
                try:
                    marker([self._card_key(self._item)])
                except Exception:  # noqa: BLE001 - 账本写失败不该挡住浮窗态
                    pass

    # ---- 关闭：焦点回卡 ----
    def _on_closed(self) -> None:
        item = self._item
        tab = self._tab
        if item is None or tab is None:
            return
        grid = getattr(tab, "grid", None)
        lookup = getattr(grid, "card_for_key", None)
        if not callable(lookup):
            return
        try:
            card = lookup(self._card_key(item))
        except Exception:  # noqa: BLE001
            return
        if card is None:
            return
        target = getattr(card, "preview_button", None) or card
        focus = getattr(target, "setFocus", None)
        if callable(focus):
            try:
                focus()
            except Exception:  # noqa: BLE001 - 卡已销毁
                pass


__all__ = [
    "PawchiveViewerController",
    "run_async",
]
