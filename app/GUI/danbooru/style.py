import html
import re
from dataclasses import dataclass

from PySide6 import QtGui
from PySide6.QtCore import qtTrId

from core import ori_path
from ui.core.font import font_color
from ui.core.theme import CustTheme, theme_mgr
from ui.core.theme.qss_template import read_templated_qss_tokens, render_templated_qss_section

DEFAULT_TAB_STATUS_CLASS = "theme-tip"


def default_tab_status_text() -> str:
    return qtTrId("danbooru.search.empty_query_home")

CARD_WIDTH_BASE = 228
CARD_PREVIEW_BASE_HEIGHT = 168
CARD_PREVIEW_MIN_HEIGHT = 156
CARD_PREVIEW_MAX_HEIGHT = 182
CARD_PREVIEW_WIDTH_PADDING = 20
CARD_ZOOM_WIDTHS = (148, 188, 228, 268, 308, 348, 388)
DEFAULT_CARD_ZOOM_INDEX = 2

_RGB_COLOR_RE = re.compile(
    r"(?P<fn>rgb|rgba)\(\s*(?P<r>\d{1,3})\s*,\s*(?P<g>\d{1,3})\s*,\s*(?P<b>\d{1,3})"
    r"(?:\s*,\s*(?P<a>[\d.]+))?\s*\)$",
    re.IGNORECASE,
)
_DANBOORU_QSS_PATH = ori_path.joinpath("ui/core/theme/danbooru.qss")

# resolve() 一次并复用：qss_template 缓存以 str(path.resolve()) 为键，Windows 每次
# resolve 触发 nt._getfinalpathname（昂贵）；qss 是随包资源，会话期内不变。
_RESOLVED_QSS_PATH = _DANBOORU_QSS_PATH.resolve()

#: 会话级 QSS 产物缓存（键 = 主题名；主题切换后首次调用按名字自动重建）。
#: 返回的 dict / str 是共享实例，调用方只读，不得原地修改。
_qss_tokens_cache: dict[str, dict[str, str]] = {}
_palette_cache: dict[str, "DanbooruUiPalette"] = {}
_card_theme_cache: dict[tuple[str, bool], "DanbooruCardTheme"] = {}
_card_stylesheet_cache: dict[tuple[str, bool], str] = {}
_section_stylesheet_cache: dict[tuple[str, str], str] = {}


@dataclass(frozen=True, slots=True)
class DanbooruCardMetrics:
    width: int
    preview_base_height: int
    preview_min_height: int
    preview_max_height: int

    @property
    def preview_content_width(self) -> int:
        return max(1, self.width - CARD_PREVIEW_WIDTH_PADDING)


def _build_card_metrics(width: int) -> DanbooruCardMetrics:
    scale = width / CARD_WIDTH_BASE
    return DanbooruCardMetrics(
        width=width,
        preview_base_height=max(1, int(round(CARD_PREVIEW_BASE_HEIGHT * scale))),
        preview_min_height=max(1, int(round(CARD_PREVIEW_MIN_HEIGHT * scale))),
        preview_max_height=max(1, int(round(CARD_PREVIEW_MAX_HEIGHT * scale))),
    )


CARD_ZOOM_METRICS = tuple(_build_card_metrics(width) for width in CARD_ZOOM_WIDTHS)
DEFAULT_CARD_METRICS = CARD_ZOOM_METRICS[DEFAULT_CARD_ZOOM_INDEX]


def _current_theme_name() -> str:
    return "dark" if theme_mgr.get_theme() == CustTheme.DARK else "light"


def get_danbooru_qss_tokens() -> dict[str, str]:
    theme_name = _current_theme_name()
    cached = _qss_tokens_cache.get(theme_name)
    if cached is None:
        cached = read_templated_qss_tokens(_RESOLVED_QSS_PATH, theme_name)
        _qss_tokens_cache[theme_name] = cached
    return cached


def _render_qss_section(name: str, **overrides: str) -> str:
    cache_key = (_current_theme_name(), name)
    if not overrides:
        cached = _section_stylesheet_cache.get(cache_key)
        if cached is None:
            cached = render_templated_qss_section(_RESOLVED_QSS_PATH, cache_key[0], name)
            _section_stylesheet_cache[cache_key] = cached
        return cached
    return render_templated_qss_section(_RESOLVED_QSS_PATH, cache_key[0], name, **overrides)


@dataclass(frozen=True, slots=True)
class DanbooruUiPalette:
    canvas: str
    shell: str
    shell_border: str
    card: str
    card_hover: str
    downloaded_card: str
    downloaded_hover: str
    preview: str
    preview_hover: str
    section: str
    section_border: str
    text: str
    muted_text: str
    status_text: str
    viewer_frame: str
    viewer_border: str
    viewer_image: str
    viewer_tag: str
    viewer_tag_hover: str
    selection_border: str
    title_accent: str
    pivot_selected: str
    pivot_button_hover: str
    pivot_button_pressed: str
    page_border_color: str

    @classmethod
    def current(cls) -> "DanbooruUiPalette":
        theme_name = _current_theme_name()
        cached = _palette_cache.get(theme_name)
        if cached is not None:
            return cached
        tokens = get_danbooru_qss_tokens()
        cached = cls(
            canvas=tokens["CANVAS"],
            shell=tokens["SHELL"],
            shell_border=tokens["SHELL_BORDER"],
            card=tokens["CARD_BACKGROUND_IDLE"],
            card_hover=tokens["CARD_HOVER_BORDER_IDLE"],
            downloaded_card=tokens["CARD_BACKGROUND_DOWNLOADED"],
            downloaded_hover=tokens["CARD_HOVER_BORDER_DOWNLOADED"],
            preview=tokens["CARD_PREVIEW_BACKGROUND"],
            preview_hover=tokens["CARD_PREVIEW_BACKGROUND_HOVER"],
            section=tokens["SECTION_BACKGROUND"],
            section_border=tokens["SECTION_BORDER"],
            text=tokens["TEXT"],
            muted_text=tokens["MUTED_TEXT"],
            status_text=tokens["STATUS_TEXT"],
            viewer_frame=tokens["VIEWER_FRAME"],
            viewer_border=tokens["VIEWER_BORDER"],
            viewer_image=tokens["VIEWER_IMAGE"],
            viewer_tag=tokens["VIEWER_TAG"],
            viewer_tag_hover=tokens["VIEWER_TAG_HOVER"],
            selection_border=tokens["SELECTION_BORDER"],
            title_accent=tokens["TITLE_ACCENT"],
            pivot_selected=tokens["PIVOT_SELECTED"],
            pivot_button_hover=tokens["PIVOT_BUTTON_HOVER"],
            pivot_button_pressed=tokens["PIVOT_BUTTON_PRESSED"],
            page_border_color=tokens["CARD_PAGE_BORDER_COLOR"],
        )
        _palette_cache[theme_name] = cached
        return cached


@dataclass(frozen=True, slots=True)
class DanbooruCardTheme:
    background: str
    border: str
    hover_border: str
    selection_border: str
    selection_fill_start: str
    selection_fill_mid: str
    selection_fill_end: str
    preview_background: str
    preview_background_hover: str
    preview_selected_border: str
    preview_selected_overlay_start: str
    preview_selected_overlay_end: str
    preview_unsupported_text: str
    preview_downloaded_grayscale: float
    preview_downloaded_opacity: float
    glow_color: str
    video_badge_bg: str
    video_badge_text: str


def get_danbooru_card_theme(already_downloaded: bool) -> DanbooruCardTheme:
    theme_name = _current_theme_name()
    cache_key = (theme_name, already_downloaded)
    cached = _card_theme_cache.get(cache_key)
    if cached is not None:
        return cached
    tokens = get_danbooru_qss_tokens()
    card_theme = DanbooruCardTheme(
        background=tokens["CARD_BACKGROUND_DOWNLOADED"] if already_downloaded else tokens["CARD_BACKGROUND_IDLE"],
        border=tokens["CARD_BORDER_DOWNLOADED"] if already_downloaded else tokens["CARD_BORDER_IDLE"],
        hover_border=tokens["CARD_HOVER_BORDER_DOWNLOADED"] if already_downloaded else tokens["CARD_HOVER_BORDER_IDLE"],
        selection_border=tokens["CARD_SELECTION_BORDER"],
        selection_fill_start=tokens["CARD_SELECTION_FILL_START"],
        selection_fill_mid=tokens["CARD_SELECTION_FILL_MID"],
        selection_fill_end=tokens["CARD_SELECTION_FILL_END"],
        preview_background=tokens["CARD_PREVIEW_BACKGROUND"],
        preview_background_hover=tokens["CARD_PREVIEW_BACKGROUND_HOVER"],
        preview_selected_border=tokens["CARD_PREVIEW_SELECTED_BORDER"],
        preview_selected_overlay_start=tokens["CARD_PREVIEW_SELECTED_OVERLAY_START"],
        preview_selected_overlay_end=tokens["CARD_PREVIEW_SELECTED_OVERLAY_END"],
        preview_unsupported_text=tokens["CARD_PREVIEW_UNSUPPORTED_TEXT"],
        preview_downloaded_grayscale=max(0.0, min(1.0, float(tokens["CARD_PREVIEW_DOWNLOADED_GRAYSCALE"]))),
        preview_downloaded_opacity=max(0.0, min(1.0, float(tokens["CARD_PREVIEW_DOWNLOADED_OPACITY"]))),
        glow_color=tokens["CARD_GLOW_COLOR"],
        video_badge_bg=tokens["CARD_VIDEO_BADGE_BG"],
        video_badge_text=tokens["CARD_VIDEO_BADGE_TEXT"],
    )
    _card_theme_cache[cache_key] = card_theme
    return card_theme


def qcolor_from_css(color: str) -> QtGui.QColor:
    qcolor = QtGui.QColor(color)
    if qcolor.isValid():
        return qcolor

    match = _RGB_COLOR_RE.fullmatch(color.strip())
    if match is None:
        raise ValueError(f"Unsupported color literal: {color}")

    red = min(255, int(match.group("r")))
    green = min(255, int(match.group("g")))
    blue = min(255, int(match.group("b")))
    alpha_group = match.group("a")
    if alpha_group is None:
        return QtGui.QColor(red, green, blue)

    alpha_value = float(alpha_group)
    if alpha_value <= 1:
        return QtGui.QColor.fromRgbF(red / 255, green / 255, blue / 255, alpha_value)
    return QtGui.QColor(red, green, blue, min(255, int(round(alpha_value))))


def format_tip_rich_text(text: str, cls: str = DEFAULT_TAB_STATUS_CLASS) -> str:
    color = theme_mgr.font_color
    css = (
        f"font.theme-tip {{ color: {color.tip}; }}"
        f"font.theme-success {{ color: {color.success}; }}"
        f"font.theme-err {{ color: {color.err}; }}"
    )
    safe_text = html.escape(text or "").replace("\n", "<br/>")
    fallback_color = {
        "theme-success": color.success,
        "theme-err": color.err,
    }.get(cls, color.tip)
    return f"<style>{css}</style>{font_color(safe_text, cls=cls or DEFAULT_TAB_STATUS_CLASS, color=fallback_color)}"


def build_card_stylesheet(palette: DanbooruUiPalette, already_downloaded: bool) -> str:
    _ = palette
    cache_key = (_current_theme_name(), already_downloaded)
    cached = _card_stylesheet_cache.get(cache_key)
    if cached is not None:
        return cached
    card_theme = get_danbooru_card_theme(already_downloaded)
    cached = _render_qss_section(
        "card",
        CARD_BACKGROUND=card_theme.background,
        CARD_BORDER=card_theme.border,
        CARD_HOVER_BORDER=card_theme.hover_border,
    )
    _card_stylesheet_cache[cache_key] = cached
    return cached


def build_viewer_stylesheet(palette: DanbooruUiPalette) -> str:
    _ = palette
    return _render_qss_section("viewer")


def build_tab_stylesheet(palette: DanbooruUiPalette) -> str:
    _ = palette
    return _render_qss_section("tab")


def build_interface_stylesheet(palette: DanbooruUiPalette) -> str:
    _ = palette
    return _render_qss_section("interface")


def build_page_nav_stylesheet(palette: DanbooruUiPalette | None = None) -> str:
    _ = palette
    return _render_qss_section("page_nav")


def build_favorites_tree_item_stylesheet() -> str:
    return _render_qss_section("favorites_tree_item_inline")


def build_title_label_stylesheet(palette: DanbooruUiPalette) -> str:
    _ = palette
    return _render_qss_section("title_label_inline")


def build_tip_line_stylesheet(palette: DanbooruUiPalette) -> str:
    _ = palette
    return _render_qss_section("tip_line_inline")


def build_comfy_jobs_stylesheet(palette: DanbooruUiPalette) -> str:
    _ = palette
    return _render_qss_section("comfy_jobs")
