"""pawchive 主题层（P1）。

机制同 app/GUI/danbooru/style.py（qss_template 渲染 + theme_mgr 主题名），但不共用文件：
ui/core/theme/pawchive.qss 独立，避免 section 同名冲突（05 §2.7 两条铁律）。

依赖纪律：只依赖 PySide6 + ui.core.theme，不 import app.GUI.danbooru.*。

与 danbooru 的差异：PawchiveCardMetrics 没有 preview_base_height / preview_max_height
（一格一图 + 正方形占位，预览边长只由 width 推出来，preview_edge = width - 20）。
三档 148 / 228 / 308 → 边长 128 / 208 / 288。
"""

import re
from dataclasses import dataclass

from PySide6 import QtGui

from core import ori_path
from ui.core.theme import CustTheme, theme_mgr
from ui.core.theme.qss_template import (
    read_templated_qss_tokens,
    render_templated_qss_section,
)

CARD_WIDTH_BASE = 228
CARD_PREVIEW_WIDTH_PADDING = 20
PAWCHIVE_GRID_WIDTHS = (148, 228, 308)
DEFAULT_PAWCHIVE_ZOOM_INDEX = 1  # 228 档

_PAWCHIVE_QSS_PATH = ori_path.joinpath("ui/core/theme/pawchive.qss")

_RGB_COLOR_RE = re.compile(
    r"(?P<fn>rgb|rgba)\(\s*(?P<r>\d{1,3})\s*,\s*(?P<g>\d{1,3})\s*,\s*(?P<b>\d{1,3})"
    r"(?:\s*,\s*(?P<a>[\d.]+))?\s*\)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PawchiveCardMetrics:
    """卡片度量：正方形预览，边长只由 ``width`` 决定。"""

    width: int

    @property
    def preview_edge(self) -> int:
        return max(1, self.width - CARD_PREVIEW_WIDTH_PADDING)


PAWCHIVE_CARD_METRICS = tuple(PawchiveCardMetrics(w) for w in PAWCHIVE_GRID_WIDTHS)
DEFAULT_PAWCHIVE_CARD_METRICS = PAWCHIVE_CARD_METRICS[DEFAULT_PAWCHIVE_ZOOM_INDEX]


def _current_theme_name() -> str:
    return "dark" if theme_mgr.get_theme() == CustTheme.DARK else "light"


def get_pawchive_qss_tokens() -> dict[str, str]:
    return read_templated_qss_tokens(_PAWCHIVE_QSS_PATH, _current_theme_name())


def _render_qss_section(name: str, **overrides: str) -> str:
    return render_templated_qss_section(
        _PAWCHIVE_QSS_PATH, _current_theme_name(), name, **overrides
    )


def qcolor_from_css(color: str) -> QtGui.QColor:
    """``rgba(r, g, b, a)`` / ``#rrggbb`` → QColor（与 danbooru 同口径，本模块自持）。"""
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


@dataclass(frozen=True, slots=True)
class PawchiveUiPalette:
    """网格/工具栏/组头用的颜色。三份：Card / Grid / Selection。"""

    canvas: str
    shell: str
    shell_border: str
    text: str
    muted_text: str
    card: str
    card_hover: str
    downloaded_card: str
    downloaded_hover: str
    preview: str
    preview_hover: str
    group_background: str
    group_border: str
    group_highlight: str
    toolbar_background: str
    toolbar_border: str
    selection_border: str
    selection_background: str
    page_nav_fab_bg: str
    page_nav_fab_fg: str
    page_nav_fab_border: str
    page_nav_fab_hover_border: str
    page_nav_fab_shadow: str

    @classmethod
    def current(cls) -> "PawchiveUiPalette":
        tokens = get_pawchive_qss_tokens()
        return cls(
            canvas=tokens["CANVAS"],
            shell=tokens["SHELL"],
            shell_border=tokens["SHELL_BORDER"],
            text=tokens["TEXT"],
            muted_text=tokens["MUTED_TEXT"],
            card=tokens["CARD_BACKGROUND_IDLE"],
            card_hover=tokens["CARD_HOVER_BORDER_IDLE"],
            downloaded_card=tokens["CARD_BACKGROUND_DOWNLOADED"],
            downloaded_hover=tokens["CARD_HOVER_BORDER_DOWNLOADED"],
            preview=tokens["CARD_PREVIEW_BACKGROUND"],
            preview_hover=tokens["CARD_PREVIEW_BACKGROUND_HOVER"],
            group_background=tokens["GROUP_BACKGROUND"],
            group_border=tokens["GROUP_BORDER"],
            group_highlight=tokens["GROUP_HIGHLIGHT"],
            toolbar_background=tokens["TOOLBAR_BACKGROUND"],
            toolbar_border=tokens["TOOLBAR_BORDER"],
            selection_border=tokens["SELECTION_BORDER"],
            selection_background=tokens["SELECTION_BACKGROUND"],
            page_nav_fab_bg=tokens["PAGE_NAV_FAB_BG"],
            page_nav_fab_fg=tokens["PAGE_NAV_FAB_FG"],
            page_nav_fab_border=tokens["PAGE_NAV_FAB_BORDER"],
            page_nav_fab_hover_border=tokens["PAGE_NAV_FAB_HOVER_BORDER"],
            page_nav_fab_shadow=tokens["PAGE_NAV_FAB_SHADOW"],
        )


@dataclass(frozen=True, slots=True)
class PawchiveCardTheme:
    """卡片像素级效果用的颜色/系数（灰度、选中渐变、视频徽标、进度环）。"""

    background: str
    border: str
    hover_border: str
    selection_border: str
    preview_background: str
    preview_background_hover: str
    preview_selected_overlay_start: str
    preview_selected_overlay_end: str
    preview_unsupported_text: str
    preview_downloaded_grayscale: float
    preview_downloaded_opacity: float
    glow_color: str
    video_badge_bg: str
    video_badge_text: str
    progress_plate: str
    progress_track: str
    progress_arc: str
    progress_failed: str


def get_pawchive_card_theme(already_downloaded: bool) -> PawchiveCardTheme:
    tokens = get_pawchive_qss_tokens()
    return PawchiveCardTheme(
        background=(
            tokens["CARD_BACKGROUND_DOWNLOADED"]
            if already_downloaded
            else tokens["CARD_BACKGROUND_IDLE"]
        ),
        border=(
            tokens["CARD_BORDER_DOWNLOADED"]
            if already_downloaded
            else tokens["CARD_BORDER_IDLE"]
        ),
        hover_border=(
            tokens["CARD_HOVER_BORDER_DOWNLOADED"]
            if already_downloaded
            else tokens["CARD_HOVER_BORDER_IDLE"]
        ),
        selection_border=tokens["CARD_SELECTION_BORDER"],
        preview_background=tokens["CARD_PREVIEW_BACKGROUND"],
        preview_background_hover=tokens["CARD_PREVIEW_BACKGROUND_HOVER"],
        preview_selected_overlay_start=tokens["CARD_PREVIEW_SELECTED_OVERLAY_START"],
        preview_selected_overlay_end=tokens["CARD_PREVIEW_SELECTED_OVERLAY_END"],
        preview_unsupported_text=tokens["CARD_PREVIEW_UNSUPPORTED_TEXT"],
        preview_downloaded_grayscale=max(
            0.0, min(1.0, float(tokens["CARD_PREVIEW_DOWNLOADED_GRAYSCALE"]))
        ),
        preview_downloaded_opacity=max(
            0.0, min(1.0, float(tokens["CARD_PREVIEW_DOWNLOADED_OPACITY"]))
        ),
        glow_color=tokens["CARD_GLOW_COLOR"],
        video_badge_bg=tokens["CARD_VIDEO_BADGE_BG"],
        video_badge_text=tokens["CARD_VIDEO_BADGE_TEXT"],
        progress_plate=tokens["CARD_PROGRESS_PLATE"],
        progress_track=tokens["CARD_PROGRESS_TRACK"],
        progress_arc=tokens["CARD_PROGRESS_ARC"],
        progress_failed=tokens["CARD_PROGRESS_FAILED"],
    )


def build_card_stylesheet(
    palette: PawchiveUiPalette, already_downloaded: bool
) -> str:
    _ = palette
    card_theme = get_pawchive_card_theme(already_downloaded)
    return _render_qss_section(
        "card",
        CARD_BACKGROUND=card_theme.background,
        CARD_BORDER=card_theme.border,
        CARD_HOVER_BORDER=card_theme.hover_border,
    )


def build_grid_stylesheet(palette: PawchiveUiPalette) -> str:
    """组头 + 工具栏 + 空/错误/加载三态条（两个 section 拼一段）。"""
    _ = palette
    return "\n".join((_render_qss_section("post_group"), _render_qss_section("grid_toolbar")))


def build_selection_stylesheet(palette: PawchiveUiPalette) -> str:
    """拖选框样式：border/background 由 palette 直接给，不经过 section 模板。"""
    return (
        f"border: 2px dashed {palette.selection_border};"
        f" background: {palette.selection_background};"
    )


def build_selection_colors(palette: PawchiveUiPalette) -> tuple[str, str]:
    """``(border, background)``，供 ``selection.apply_theme`` 用。"""
    return (palette.selection_border, palette.selection_background)


def build_page_nav_stylesheet(palette: PawchiveUiPalette | None = None) -> str:
    _ = palette
    return _render_qss_section("page_nav")


def build_bottom_bar_stylesheet(palette: PawchiveUiPalette | None = None) -> str:
    """页面贴底操作条（作者表格 / 过滤规则 / 打开目录 / 页码）。"""
    _ = palette
    return _render_qss_section("bottom_bar")


def build_viewer_stylesheet() -> str:
    """浮窗样式：**共享 chrome**（danbooru / pawchive 同一份）。

    pawchive 不再自带 viewer 颜色：各站点只保留自己的内容段（tag chips / meta 列），
    frame / 图片面 / 提示 / 视频面 / 顶栏全部来自 ui/core/theme/viewer.qss。

    不设站点兜底样式（R4：站点不写壳层样式）。chrome 渲染失败时由共享层负责降级。
    """
    from app.GUI.viewer import build_viewer_chrome_stylesheet

    return build_viewer_chrome_stylesheet()


__all__ = [
    "CARD_PREVIEW_WIDTH_PADDING",
    "CARD_WIDTH_BASE",
    "DEFAULT_PAWCHIVE_CARD_METRICS",
    "DEFAULT_PAWCHIVE_ZOOM_INDEX",
    "PAWCHIVE_CARD_METRICS",
    "PAWCHIVE_GRID_WIDTHS",
    "PawchiveCardMetrics",
    "PawchiveCardTheme",
    "PawchiveUiPalette",
    "build_bottom_bar_stylesheet",
    "build_card_stylesheet",
    "build_grid_stylesheet",
    "build_page_nav_stylesheet",
    "build_selection_colors",
    "build_viewer_stylesheet",
    "build_selection_stylesheet",
    "get_pawchive_card_theme",
    "get_pawchive_qss_tokens",
    "qcolor_from_css",
]
