"""Acggr qconfig layer.

Only the script-reachable config surfaces live here: the global ``AcggrConfig``
(DoH / proxy / script window geometry) plus ``cbg`` / ``pawchive`` /
``danbooru``. Mainline items (WebDAV, subscription, hidden site choices,
publish URLs, addons) are out of scope for this repo.
"""
import pathlib
import shutil

from loguru import logger
from qfluentwidgets import QConfig, ConfigItem, RangeConfigItem, RangeValidator, Theme, qconfig

from core.config import ScriptConf, conf_dir, qconfig_dir
from core.config.history import MRUHistory
from core.network.doh_policy import normalize_doh_url
from core.script.entry_migrate import move_entry_identity
from deploy import Env


def _qconfig_path(name: str):
    target = qconfig_dir.joinpath(name)
    legacy = conf_dir.joinpath(name)
    if legacy.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
    return target


def _strip_shared_theme_section(path):
    """Drop a stale ``QFluentWidgets`` block from a *secondary* qconfig file.

    Only ``qc.json`` may own that section. Secondary files written by older
    builds carry it, and because they are loaded after ``qc.json`` they would
    otherwise override the user's current theme on every launch. Rewriting the
    file here (once) makes the stored state match what we now write.
    """
    import json

    try:
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict) or "QFluentWidgets" not in payload:
        return
    payload.pop("QFluentWidgets", None)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")


def _default_danbooru_view_ratio() -> int:
    danbooru_config = getattr(ScriptConf(), "danbooru", {}) or {}
    return int(danbooru_config.get("view_ratio") or 65)


class AcggrConfig(QConfig):
    proxyHistory = ConfigItem("Proxy", "History", ["127.0.0.1:7890"], restart=False)
    #: aria2 download proxy -- separate list from the access proxy above: the two
    #: rows take different values (one is httpx, the other the download engine).
    downloadProxyHistory = ConfigItem("Proxy", "DownloadHistory", [], restart=False)
    aiUrlHistory = ConfigItem("AI", "UrlHistory", [], restart=False)
    comfyHostHistory = ConfigItem("Comfy", "HostHistory", [], restart=False)
    dohUrl = ConfigItem("DoH", "Url", "", restart=False)
    dohHistory = ConfigItem("DoH", "History", [], restart=False)
    searchHistory = ConfigItem("Search", "History", [], restart=False)
    scriptWinRect = ConfigItem("ScriptWindow", "Rect", [], restart=False)
    #: jsoneriPalaces 存储根目录；空 = 没选过，用 ``DEFAULT_JSONERI_STORAGE_DIR``。
    jsoneriStorageDir = ConfigItem("Jsoneri", "StorageDir", "", restart=False)
    #: Update detection state. Kept here (not in a side JSON) so one ``save()``
    #: covers the whole surface: the last check timestamp throttles the automatic
    #: probe, the cached release lets a throttled launch render the card offline,
    #: and ``SkippedVersion`` is the user's "do not tell me about this one again".
    updateLastCheckAt = ConfigItem("Update", "LastCheckAt", 0, restart=False)
    updateSkippedVersion = ConfigItem("Update", "SkippedVersion", "", restart=False)
    #: Last version announced by the one-shot startup toast. "One disturbance per
    #: version" needs its own field: the badge stays until acted on, while the
    #: toast must fire exactly once even across restarts.
    updateNotifiedVersion = ConfigItem("Update", "NotifiedVersion", "", restart=False)
    updateCachedVersion = ConfigItem("Update", "CachedVersion", "", restart=False)
    updateCachedNotes = ConfigItem("Update", "CachedNotes", "", restart=False)
    updateCachedPublishedAt = ConfigItem("Update", "CachedPublishedAt", "", restart=False)
    updateCachedReleaseUrl = ConfigItem("Update", "CachedReleaseUrl", "", restart=False)
    histories: "UrlHistories"
    doh: "AcggrConfig.DoH"
    search: "AcggrConfig.Search"

    class DoH:
        def __init__(self, cfg: "AcggrConfig"):
            self._cfg = cfg

        @staticmethod
        def _normalize(value: object) -> str:
            raw_value = str(value or "").strip()
            return normalize_doh_url(raw_value) if raw_value else ""

        def get_url(self) -> str:
            return self._normalize(self._cfg.dohUrl.value)

        def get_history(self) -> list[str]:
            # One implementation, shared with every other remembered field.
            return self._cfg.histories.doh.get()

        def set_url(self, value: object) -> str:
            normalized = self._normalize(value)
            self._cfg.dohUrl.value = normalized
            if normalized:
                # ``add`` persists the whole qconfig, including ``dohUrl``.
                self._cfg.histories.doh.add(normalized)
            else:
                # Cleared URL: the history list does not change, the stored URL must.
                self._cfg.save()
            return normalized

    class Search:
        """Script-window search keyword history (site-agnostic MRU)."""

        MAX_HISTORY = 35

        def __init__(self, cfg: "AcggrConfig"):
            self._cfg = cfg

        @staticmethod
        def canonicalize(term: object) -> str:
            return " ".join(str(term or "").split())

        def get_history(self) -> list[str]:
            history = []
            for item in list(self._cfg.searchHistory.value or []):
                normalized = self.canonicalize(item)
                if normalized and normalized not in history:
                    history.append(normalized)
            return history

        def add_history(self, term: object) -> list[str]:
            canonical = self.canonicalize(term)
            if not canonical or canonical.lower().startswith("dc:"):
                return self.get_history()
            history = [item for item in self.get_history() if item != canonical]
            history.insert(0, canonical)
            self._cfg.searchHistory.value = history[: self.MAX_HISTORY]
            self._cfg.save()
            return self.get_history()


class UrlHistories:
    """One :class:`MRUHistory` per remembered URL field.

    Fields merely hold an instance and call ``get`` / ``add`` / ``set``; the
    qconfig read-write path exists exactly once, in ``MRUHistory``.
    """

    def __init__(self, cfg: "AcggrConfig"):
        self.proxy = MRUHistory(cfg.proxyHistory, owner=cfg)
        self.download_proxy = MRUHistory(cfg.downloadProxyHistory, owner=cfg)
        self.doh = MRUHistory(cfg.dohHistory, owner=cfg, normalize=AcggrConfig.DoH._normalize)
        self.ai_url = MRUHistory(cfg.aiUrlHistory, owner=cfg)
        self.comfy_host = MRUHistory(cfg.comfyHostHistory, owner=cfg)


#: 全新安装的主题默认值：跟随系统深浅色。
DEFAULT_THEME_MODE = Theme.AUTO


def _apply_default_theme_mode(path) -> None:
    """Give a never-configured install the "follow the system" default.

    ``QConfig.themeMode`` is a class-level item defaulting to ``Theme.LIGHT``, so
    a fresh install on a dark system opens light -- and because every later
    ``qconfig.save()`` (proxy history, DoH history, window rect) serialises the
    whole ``QFluentWidgets`` section, that *default* is written out as if the
    user had picked it, after which "follow the system" is unreachable without
    knowing to look for it. Checking the file first keeps a real choice intact
    while a never-configured install stays on Auto.
    """
    import json

    try:
        payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        return
    stored = (payload.get("QFluentWidgets") or {}).get("ThemeMode")
    if stored:
        return
    # ``save=False``: the file keeps no key until the user actually chooses, so
    # this stays "default" rather than becoming a second hard-coded value.
    qconfig.set(qconfig.themeMode, DEFAULT_THEME_MODE, save=False)


_QC_JSON = _qconfig_path("qc.json")
acggr_cfg = AcggrConfig()
qconfig.load(_QC_JSON, acggr_cfg)
_apply_default_theme_mode(_QC_JSON)
# Before DoH: its get_history / set_url delegate to ``histories.doh``.
acggr_cfg.histories = UrlHistories(acggr_cfg)
acggr_cfg.doh = AcggrConfig.DoH(acggr_cfg)
acggr_cfg.search = AcggrConfig.Search(acggr_cfg)


#: jsoneriPalaces 存储根目录的默认值。
#:
#: 和 pawchive/danbooru 的 ``sv_path`` 一样，在同一个根目录
#: （``Env.default_acggr_path``）下 ``joinpath`` 出站点目录，所以「没选过」也有一个
#: 真实路径，而不是让每个调用方各自回忆默认值。再往下的一级是 jsoneriPalaces 自己
#: 按服务建的子目录（歌曲下载默认就是 ``<这个目录>/μsPalace``），这里只负责根目录。
DEFAULT_JSONERI_STORAGE_DIR = Env.default_acggr_path.joinpath("jsoneriPalaces")


def jsoneri_storage_root() -> pathlib.Path:
    """jsoneriPalaces 存储根目录；没选过 → :data:`DEFAULT_JSONERI_STORAGE_DIR`。

    只做「读值 + 判可用」判定，不建目录 —— 建目录是写路径（存目录按钮、下载落地）
    的责任，读侧凭空 mkdir 会让一个纯查询产生磁盘副作用。
    """
    raw = str(acggr_cfg.jsoneriStorageDir.value or "").strip()
    if not raw:
        return DEFAULT_JSONERI_STORAGE_DIR
    try:
        return pathlib.Path(raw).expanduser()
    except (OSError, ValueError):
        logger.warning(f"[jsoneri] storage dir value is not a usable path: {raw!r}")
        return DEFAULT_JSONERI_STORAGE_DIR


def jsoneri_service_storage_dir(service_name: str) -> pathlib.Path | None:
    """某个 jsoneri 服务这次的存储目录 = 根目录下的同名子目录。

    根目录永远有值（没选过是默认根目录），所以这里没有「未设置」分支：要么给出
    ``<root>/<service_name>``，要么只剩「服务名不可用」这一种 ``None``。目录不存在
    时**创建**，因为这是「用户已经决定了放哪里」的落地动作；创建失败也当没这个目录
    （调用方回落），不把异常抛给 UI 线程。

    ``service_name`` 来自远端探测 API，是信任边界外的输入：先剔除路径分隔符和
    ``..``，别让它把写入点挪出根目录。名字被剔空 → 当没这个服务处理。
    """
    root = jsoneri_storage_root()
    leaf = str(service_name or "").strip().replace("/", "_").replace("\\", "_")
    leaf = leaf.replace("..", "_").strip(". ")
    if not leaf:
        logger.warning(f"[jsoneri] service name {service_name!r} has no usable leaf name")
        return None
    target = root.joinpath(leaf)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(f"[jsoneri] cannot create storage dir {target}: {exc}")
        return None
    return target



class _NoSharedThemeSection(QConfig):
    """``QConfig`` whose ``toDict`` drops the class-level QFluentWidgets items.

    themeMode / themeColor / fontFamilies live on the QConfig base, so every subclass serialises
    them and qconfig.load overwrites the same shared items. Persisting them in the secondary
    files makes the theme sticky and order-dependent (those load after qc.json). Only qc.json
    owns that section; the rest must never write it.
    """

    def toDict(self, serialize=True):
        data = super().toDict(serialize=serialize)
        data.pop("QFluentWidgets", None)
        return data


class CbgConfig(_NoSharedThemeSection):
    scanRoot = ConfigItem("Scan", "Root", "", restart=False)
    includePrevious = ConfigItem("Random", "IncludePrevious", True, restart=False)
    randomCount = ConfigItem("Random", "Count", 10, restart=False)
    generatedPaths = ConfigItem("History", "GeneratedPaths", [], restart=False)


cbg_cfg = CbgConfig()
_strip_shared_theme_section(_qconfig_path("qc_cbg.json"))
qconfig.load(_qconfig_path("qc_cbg.json"), cbg_cfg)


class PawchiveConfig(_NoSharedThemeSection):
    """Pawchive配置管理，包含过滤和收藏功能"""
    filterText = ConfigItem("Filter", "FilterText", "", restart=False)
    favoriteAuthors = ConfigItem("Favorites", "Authors", [], restart=False)
    # L2 网格缩略图三档缩放的当前档位（0=128 / 1=208 / 2=288，见 app/GUI/pawchive/tabs.py）。
    grid_zoom_index = ConfigItem("Grid", "ZoomIndex", 1, restart=False)
    fav: "PawchiveConfig.Favorites"

    class Favorites:
        def __init__(self, cfg: "PawchiveConfig"):
            self._cfg = cfg

        def in_(self, author_id: str) -> bool:
            return author_id in self.get_favorites()

        def faved_(self, author_id: str) -> bool:
            return self.in_(author_id)

        def toggle_favorite(self, author_id: str) -> bool:
            favorites = self.get_favorites()
            if author_id in favorites:
                favorites.remove(author_id)
                is_favorited = False
            else:
                favorites.append(author_id)
                is_favorited = True
            self._cfg.favoriteAuthors.value = favorites
            self._cfg.save()
            return is_favorited

        def get_favorites(self) -> list[str]:
            return list(self._cfg.favoriteAuthors.value or [])


pawchive_cfg = PawchiveConfig()
move_entry_identity()
_strip_shared_theme_section(_qconfig_path("qc_pawchive.json"))
qconfig.load(_qconfig_path("qc_pawchive.json"), pawchive_cfg)
pawchive_cfg.fav = PawchiveConfig.Favorites(pawchive_cfg)


class DanbooruConfig(_NoSharedThemeSection):
    DEFAULT_FAVORITE_GROUP = "normal"
    LEGACY_DEFAULT_FAVORITE_GROUP = "Favorites"
    RESERVED_SEARCH_KEYS = frozenset({"History", LEGACY_DEFAULT_FAVORITE_GROUP, DEFAULT_FAVORITE_GROUP})

    searchHistory = ConfigItem("Search", "History", [], restart=False)
    searchExtra = ConfigItem("Search", "SearchExtra", [], restart=False)
    searchFavorites = ConfigItem("Search", "Favorites", {}, restart=False)
    searchFavoritesTranslateMap = ConfigItem("Search", "FavoritesTranslateMap", {}, restart=False)
    view_ratio = RangeConfigItem("Viewer", "ViewRatio", _default_danbooru_view_ratio(), RangeValidator(30, 85), restart=False)
    # viewer 显示清晰度档位：auto=按实测网速自适应选原图/缩略图，on=恒用原图，off=恒用缩略图。
    viewerSourceMode = ConfigItem("Viewer", "SourceMode", "auto", restart=False)
    #: 网格列表隐藏「不可预览」条目（无受支持原图 / 无可播视频 / 无静态预览件）。
    #: 只影响显示与 viewer 上/下一张序列；服务端结果、分页与裁剪算术均不受影响。
    hideUnpreviewable = ConfigItem("Grid", "HideUnpreviewable", True, restart=False)
    # viewer 的 pin（置顶）按钮在新会话里的初始勾选态。只当初始值用：会话内用户
    # 点 pin 仍改当前窗口，不回写这里；改动在下一次创建 viewer 时生效。
    isDanbooruViewerDafaultPin = ConfigItem("Viewer", "IsDanbooruViewerDafaultPin", True, restart=False)
    player = ConfigItem("Viewer", "Player", {}, restart=False)
    zoom_index = ConfigItem("Viewer", "ZoomIndex", 2, restart=False)
    # Tag 导出面板屏幕几何 [x, y, width, height]；空列表表示尚未记忆，走默认尺寸。
    tagExportPanelRect = ConfigItem("TagExportPanel", "Rect", [], restart=False)
    # Tag 导出面板 Comfy 生成设置：UNET 预设键、图内补全开关、重绘强度，随面板开合记忆。
    tagExportComfyUnet = ConfigItem("TagExportPanel", "ComfyUnet", "turbo", restart=False)
    tagExportWd14Enabled = ConfigItem("TagExportPanel", "Wd14Enabled", False, restart=False)
    tagExportDenoise = RangeConfigItem("TagExportPanel", "Denoise", 100, RangeValidator(10, 100), restart=False)
    # Fav-chip group → PromptDoc section (body/character/artist/series). Preference bag only;
    # never Search/Favorites content — see danbooru-global-push-pattern panel-pref boundary.
    tagExportFavGroupSections = ConfigItem("TagExportPanel", "FavGroupSections", {}, restart=False)
    #: 「pin tab」的持久化顺序表：canonical query 列表，顺序即 pinTabsGroup 展示顺序。
    #: 只存 query，不存 tab_id —— tab_id 里的 counter 跨会话不稳定。
    tabPinTabs = ConfigItem("Tab", "PinTabs", [], restart=False)

    @staticmethod
    def canonicalize_term(term: str) -> str:
        return " ".join((term or "").split())

    def _normalize_search_favorites_payload(self, payload: object) -> dict[str, list[str]]:
        if not isinstance(payload, dict):
            return {}

        normalized_payload = {}
        for raw_name, raw_tags in payload.items():
            group_name = self.canonicalize_term(str(raw_name))
            if not group_name or group_name == "History":
                continue
            if group_name == self.LEGACY_DEFAULT_FAVORITE_GROUP:
                group_name = self.DEFAULT_FAVORITE_GROUP
            if not isinstance(raw_tags, list):
                continue

            tags = []
            seen_tags = set()
            for raw_tag in raw_tags:
                tag = self.canonicalize_term(str(raw_tag))
                if not tag or tag in seen_tags:
                    continue
                seen_tags.add(tag)
                tags.append(tag)
            normalized_payload[group_name] = tags
        return normalized_payload

    def _normalize_search_favorites_value(self) -> dict[str, list[str]]:
        normalized_payload = self._normalize_search_favorites_payload(self.searchFavorites.value)
        if normalized_payload != self.searchFavorites.value:
            self.searchFavorites.value = normalized_payload
        return normalized_payload

    def _normalize_translate_map_payload(self, payload: object) -> dict[str, str]:
        if not isinstance(payload, dict):
            return {}
        normalized_payload: dict[str, str] = {}
        for raw_origin, raw_translated in payload.items():
            origin = self.canonicalize_term(str(raw_origin))
            translated = self.canonicalize_term(str(raw_translated))
            if not origin or not translated:
                continue
            normalized_payload[origin] = translated
        return normalized_payload

    def get_translate_map(self) -> dict[str, str]:
        """归一化后的翻译表（**带缓存**）。

        缓存原因：canonicalize_term() 是正则级清洗、一条跑两次（key+value），词库上百条时首次
        构建会重复归一化同一份没变的配置。
        失效判定用「源字典对象身份」：ConfigItem.value 返回同一个 dict 对象，未被整体替换/
        重新加载缓存即有效；save_translate_map() / merge_translate_map() 写回新对象，身份
        变化即自然失效。
        """
        payload = self.searchFavoritesTranslateMap.value
        cached_source, cached = getattr(self, "_translate_map_cache", (None, None))
        if cached is not None and cached_source is payload:
            return dict(cached)

        normalized_payload = self._normalize_translate_map_payload(payload)
        if normalized_payload != payload:
            self.searchFavoritesTranslateMap.value = normalized_payload
        # 记录归一化后真正挂在配置上的对象（可能与入参不是同一个）。
        self._translate_map_cache = (self.searchFavoritesTranslateMap.value, normalized_payload)
        return dict(normalized_payload)

    def save_translate_map(self, payload: object) -> dict[str, str]:
        normalized_payload = self._normalize_translate_map_payload(payload)
        if normalized_payload != self.searchFavoritesTranslateMap.value:
            self.searchFavoritesTranslateMap.value = normalized_payload
            self.save()
        return dict(normalized_payload)

    def merge_translate_map(self, translations: object) -> dict[str, str]:
        """Background-safe merge: keep existing map entries, overwrite only provided keys."""
        merged = self.get_translate_map()
        if isinstance(translations, dict):
            for raw_origin, raw_translated in translations.items():
                origin = self.canonicalize_term(str(raw_origin))
                translated = self.canonicalize_term(str(raw_translated))
                if not origin or not translated:
                    continue
                merged[origin] = translated
        return self.save_translate_map(merged)

    def drop_translate_keys(self, tags: object) -> dict[str, str]:
        current_map = self.get_translate_map()
        if not current_map:
            return current_map
        drop_keys = {
            canonical
            for raw_tag in (tags or [])
            if (canonical := self.canonicalize_term(str(raw_tag)))
        }
        if not drop_keys:
            return current_map
        next_map = {
            origin: translated
            for origin, translated in current_map.items()
            if origin not in drop_keys
        }
        return self.save_translate_map(next_map)

    def display_tag(self, origin: str) -> str:
        canonical = self.canonicalize_term(origin)
        if not canonical:
            return ""
        return self.get_translate_map().get(canonical) or canonical

    def toDict(self, serialize=True):
        self._normalize_search_favorites_value()
        self.get_translate_map()
        return super().toDict(serialize=serialize)

    def get_view_ratio_percent(self) -> int:
        return int(self.view_ratio.value)

    def get_view_ratio(self) -> float:
        return self.get_view_ratio_percent() / 100

    def get_player(self) -> dict:
        player = self.player.value if isinstance(self.player.value, dict) else {}
        muted = player.get("muted", True)
        volume = player.get("volume", 30)
        normalized = {
            "muted": muted if isinstance(muted, bool) else True,
            "volume": max(0, min(100, volume if isinstance(volume, int) else 30)),
        }
        if normalized != self.player.value:
            self.player.value = dict(normalized)
        return dict(normalized)

    def save_player(self, *, muted=None, volume=None) -> dict:
        player = self.get_player()
        if muted is not None:
            player["muted"] = bool(muted)
        if volume is not None:
            player["volume"] = max(0, min(100, int(volume)))
        if player != self.player.value:
            self.player.value = dict(player)
            self.save()
        return dict(player)

    def get_tag_export_panel_rect(self) -> list[int]:
        raw_rect = self.tagExportPanelRect.value
        if not isinstance(raw_rect, list) or len(raw_rect) < 4:
            return []
        try:
            return [int(value) for value in raw_rect[:4]]
        except (TypeError, ValueError):
            return []

    def save_tag_export_panel_rect(self, x: int, y: int, width: int, height: int) -> list[int]:
        rect = [int(x), int(y), int(width), int(height)]
        if rect != self.tagExportPanelRect.value:
            self.tagExportPanelRect.value = list(rect)
            self.save()
        return list(rect)

    def get_tag_export_comfy_unet(self) -> str:
        """返回存储的 UNET 预设键；预设键合法性由调用方按注册表校验。"""
        return str(self.tagExportComfyUnet.value or "")

    def save_tag_export_comfy_unet(self, preset_name: str) -> str:
        preset_name = str(preset_name or "")
        if preset_name != self.tagExportComfyUnet.value:
            self.tagExportComfyUnet.value = preset_name
            self.save()
        return preset_name

    def get_tag_export_wd14_enabled(self) -> bool:
        return bool(self.tagExportWd14Enabled.value)

    def save_tag_export_wd14_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        if enabled != self.tagExportWd14Enabled.value:
            self.tagExportWd14Enabled.value = enabled
            self.save()
        return enabled

    def get_tag_export_denoise(self) -> int:
        return int(self.tagExportDenoise.value)

    def save_tag_export_denoise(self, value: int) -> int:
        value = max(10, min(100, int(value)))
        if value != self.tagExportDenoise.value:
            self.tagExportDenoise.value = value
            self.save()
        return value

    _TAG_EXPORT_FAV_SECTIONS = frozenset({"body", "character", "artist", "series"})

    def get_tag_export_fav_group_sections(self) -> dict[str, str]:
        """Panel preference bag: favorite group name → insertion section.

        Dumb read only — no favorites invalidate / no tab push.
        """
        raw_mapping = self.tagExportFavGroupSections.value
        if not isinstance(raw_mapping, dict):
            return {}
        normalized: dict[str, str] = {}
        for raw_group_name, raw_section in raw_mapping.items():
            group_name = str(raw_group_name or "").strip()
            section = str(raw_section or "").strip()
            if not group_name or section not in self._TAG_EXPORT_FAV_SECTIONS:
                continue
            normalized[group_name] = section
        return normalized

    def save_tag_export_fav_group_sections(self, mapping: dict[str, str] | None) -> dict[str, str]:
        """Persist fav-group section map under TagExportPanel; never touches Search/Favorites."""
        normalized: dict[str, str] = {}
        if isinstance(mapping, dict):
            for raw_group_name, raw_section in mapping.items():
                group_name = str(raw_group_name or "").strip()
                section = str(raw_section or "").strip()
                if not group_name or section not in self._TAG_EXPORT_FAV_SECTIONS:
                    continue
                normalized[group_name] = section
        if normalized != self.tagExportFavGroupSections.value:
            self.tagExportFavGroupSections.value = dict(normalized)
            self.save()
        return dict(normalized)

    def get_history(self):
        return list(self.searchHistory.value)

    def add_history(self, term: str):
        canonical = self.canonicalize_term(term)
        if not canonical:
            return []
        history = [item for item in self.searchHistory.value if item != canonical]
        history.insert(0, canonical)
        self.searchHistory.value = history[:50]
        self.save()
        return self.get_history()

    def get_search_extra(self):
        return list(self.searchExtra.value)

    def add_search_extra(self, term: str):
        canonical = self.canonicalize_term(term)
        if not canonical:
            return
        extras = [item for item in self.searchExtra.value if item != canonical]
        extras.insert(0, canonical)
        self.searchExtra.value = extras[:30]
        self.save()

    def get_pin_tabs(self) -> list[str]:
        """pinTabsGroup 的 canonical query 列表（顺序即展示顺序，已去空去重）。"""
        ordered: list[str] = []
        for raw_query in list(self.tabPinTabs.value or []):
            canonical = self.canonicalize_term(raw_query)
            if canonical and canonical not in ordered:
                ordered.append(canonical)
        return ordered

    def save_pin_tabs(self, queries: object) -> list[str]:
        """整表写回 pin tab 顺序；空列表 = 清空 pin 组。有变化才持久化。"""
        ordered: list[str] = []
        for raw_query in list(queries or []):
            canonical = self.canonicalize_term(raw_query)
            if canonical and canonical not in ordered:
                ordered.append(canonical)
        if ordered != self.get_pin_tabs():
            self.tabPinTabs.value = ordered
            self.save()
        return ordered

    def remove_pin_tab(self, query: object) -> list[str]:
        """从 pin 表剔除单个 canonical query 并持久化（pin tab 关闭时走这里）。"""
        canonical = self.canonicalize_term(str(query or ""))
        current = self.get_pin_tabs()
        remaining = [item for item in current if item != canonical]
        if remaining != current:
            self.tabPinTabs.value = remaining
            self.save()
        return remaining

    class Favorites:
        def __init__(self, cfg: "DanbooruConfig"):
            self._cfg = cfg

        @property
        def payload(self) -> dict[str, list[str]]:
            normalized_payload = self._cfg._normalize_search_favorites_value()
            return {group_name: list(tags) for group_name, tags in normalized_payload.items()}

        def save_payload(self, payload: object) -> dict[str, list[str]]:
            normalized_payload = self._cfg._normalize_search_favorites_payload(payload)
            if normalized_payload != self._cfg.searchFavorites.value:
                self._cfg.searchFavorites.value = normalized_payload
            else:
                self._cfg.searchFavorites.value = {group_name: list(tags) for group_name, tags in normalized_payload.items()}
            self._cfg.save()
            return {group_name: list(tags) for group_name, tags in normalized_payload.items()}


danbooru_cfg = DanbooruConfig()
_strip_shared_theme_section(_qconfig_path("qc_danbooru.json"))
qconfig.load(_qconfig_path("qc_danbooru.json"), danbooru_cfg)
danbooru_cfg.fav = DanbooruConfig.Favorites(danbooru_cfg)
danbooru_cfg._normalize_search_favorites_value()


def _restore_primary_qconfig() -> None:
    """Point ``qconfig`` back at ``qc.json`` after the secondary loads.

    ``qconfig.load(file, cfg)`` rebinds the module-level singleton to the cfg it
    was last given, so after the four loads above it owns the *danbooru* config.
    Every ``qconfig.set(..., save=True)`` -- theme mode, theme colour, font
    families -- then writes into ``qc_danbooru.json``, which
    ``_NoSharedThemeSection.toDict`` strips of exactly that section: the write
    is silently discarded and the theme never persists.

    Re-binding here makes ``qc.json`` the single writer again, which is what the
    ``_NoSharedThemeSection`` docstring above already assumes.
    """
    qconfig._cfg = acggr_cfg


_restore_primary_qconfig()
