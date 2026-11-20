"""站点无关的浮窗几何换算（**唯一**实现）。

danbooru 与 pawchive 的 x/y/width/height 必须由同一组纯函数算出，站点文件不允许再出现
520/340/196/65 这类 magic number；只放「屏幕 → 外壳 → 画布 → 图像面」的算术。
尺寸链：_screen_geometry → chrome_extent → media_bounds → fit_calculator → column_width；
area_ratio 只作显式入参（本模块不 import 配置/设置模块），取值在 ViewerChromeMixin._view_ratio()。

硬约束（回归红线）：须让 danbooru 浮窗数值逐字段不变。**frame 外框 = 内容区 + 1px/边**
（每轴 +2px：window_size = chrome_extent + 2），仅在 frame 保留 1px/边内缩时成立，改
border/padding 后须复核；图像面本身无额外内缩，不要再从 fit 目标里减边框宽度。
"""

from __future__ import annotations

import math
import typing as t
from dataclasses import dataclass

from PySide6.QtCore import QMargins, QRect, QSize

__all__ = [
    "ChromeExtent",
    "LEFT_COLUMN_WIDTH",
    "UNKNOWN_SOURCE_CANVAS",
    "ViewerFitResult",
    "axis_scale",
    "chrome_extent",
    "clamp_into_screen",
    "column_width",
    "default_canvas_size",
    "fit_calculator",
    "fit_display_size",
    "fit_scale",
    "max_display_bounds",
    "media_bounds",
    "normalize_size",
    "ratio_from_percent",
    "zoom_canvas_size",
]

#: 左列宽度（danbooru 标签组 / pawchive 信息栏同宽）。站点不要再各自写 196。
LEFT_COLUMN_WIDTH = 196
#: 源图尺寸未知时的中性画布（1.53:1，沿用现盘观感）；实际使用前夹进 media bounds。
UNKNOWN_SOURCE_CANVAS = QSize(520, 340)


@dataclass(frozen=True, slots=True)
class ChromeExtent:
    """外壳在横向/纵向上吃掉的像素（边框、留白、左列、顶栏）。"""

    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ViewerFitResult:
    """一次 fit 的全部中间量（字段名与 ``DanbooruViewerFitResult`` 逐一对应）。

    保留中间量是为了让"danbooru 零回归"能逐字段比对，也让调用方能在不重算的前提下
    读 ``target_area`` / ``axis_scale`` 做诊断。
    """

    available_bounds: QSize
    max_display_bounds: QSize
    display_size: QSize
    target_area: int
    axis_scale: float


def normalize_size(size: t.Optional[QSize]) -> QSize:
    """非法/空尺寸归一成 1×1，避免下游除零与 0 宽窗口。"""
    if size is None:
        return QSize(1, 1)
    return QSize(max(1, size.width()), max(1, size.height()))


def ratio_from_percent(percent: float) -> float:
    """``65`` → ``0.65``；配置项存百分比，算法层统一用 0..1 的面积比。"""
    return float(percent) / 100.0


def axis_scale(ratio: float) -> float:
    """面积比 → 单轴缩放系数（两边等比缩放，边长按平方根走）。"""
    return math.sqrt(max(0.0, float(ratio)))


def _shrink_bounds(available: QSize, ratio: float) -> QSize:
    """已归一化的可用区域 → 面积比收缩后的最大图像面。"""
    scale = axis_scale(ratio)
    return QSize(
        max(1, min(available.width(), int(available.width() * scale))),
        max(1, min(available.height(), int(available.height() * scale))),
    )


def max_display_bounds(bounds: QSize, ratio: float) -> QSize:
    """媒体区域按面积比收缩后的最大图像面（danbooru ``max_display_bounds``）。"""
    return _shrink_bounds(normalize_size(bounds), ratio)


def _fit_size_within_bounds(
    image_size: t.Optional[QSize],
    bounds: QSize,
) -> QSize:
    """``DanbooruViewerFitCalculator._fit_size_within_bounds`` 的逐行同义实现。

    等比内接、只缩不放、``round``（**不要改成 ceil/floor**：danbooru 基准就是这个数）。
    """
    normalized = normalize_size(bounds)
    if image_size is None or image_size.width() <= 0 or image_size.height() <= 0:
        return normalized
    max_width = max(1, min(normalized.width(), image_size.width()))
    max_height = max(1, min(normalized.height(), image_size.height()))
    if image_size.width() <= max_width and image_size.height() <= max_height:
        return QSize(image_size)
    if max_width * image_size.height() <= max_height * image_size.width():
        fitted_width = max_width
        fitted_height = max(1, int(round(fitted_width * image_size.height() / image_size.width())))
        return QSize(fitted_width, fitted_height)
    fitted_height = max_height
    fitted_width = max(1, int(round(fitted_height * image_size.width() / image_size.height())))
    return QSize(fitted_width, fitted_height)


def fit_calculator(
    available_bounds: QSize,
    image_size: t.Optional[QSize],
    area_ratio: float,
) -> ViewerFitResult:
    """站点无关的 fit 计算（danbooru ``DanbooruViewerFitCalculator.calculate`` 同义）。

    ``area_ratio`` 是 0..1 的面积比（**显式入参**：本模块不读任何配置）；调用方传
    ``app.GUI.viewer.settings.viewer_view_ratio()`` 或测试里的定值。
    """
    normalized_available = normalize_size(available_bounds)
    scale = axis_scale(area_ratio)
    max_bounds = _shrink_bounds(normalized_available, area_ratio)
    display_size = _fit_size_within_bounds(image_size, max_bounds)
    return ViewerFitResult(
        available_bounds=normalized_available,
        max_display_bounds=max_bounds,
        display_size=display_size,
        target_area=max(1, int(round(
            normalized_available.width() * normalized_available.height() * area_ratio
        ))),
        axis_scale=scale,
    )


def fit_display_size(
    image_size: t.Optional[QSize],
    bounds: QSize,
    ratio: float,
) -> QSize:
    """等比「只缩不放」到 ``max_display_bounds`` 内的图像面尺寸。

    就是 ``fit_calculator(...).display_size``；源图未知时退化成最大可用图像面，
    绝不返回 0。
    """
    return fit_calculator(bounds, image_size, ratio).display_size


def fit_scale(image_size: t.Optional[QSize], bounds: QSize, ratio: float) -> float:
    """与 ``fit_display_size`` 同源的等比缩放**因子**（自绘画布的 zoom 基准）。

    只缩不放：结果恒 ``<= 1.0``；源图未知时返回 1.0（画布按最大图像面兜底）。

    **fit 态的画布尺寸不要用本因子反投影**：整数化那一步（``round`` 的半整数 + 浮点
    误差）在临界点会差 1px。fit 态直接取 ``fit_display_size`` 的整数结果，因子只用于
    已放大（zoom > fit）时的裁剪/夹紧换算。
    """
    target = max_display_bounds(bounds, ratio)
    if image_size is None or image_size.width() <= 0 or image_size.height() <= 0:
        return 1.0
    if image_size.width() <= target.width() and image_size.height() <= target.height():
        return 1.0
    return min(
        target.width() / max(1, image_size.width()),
        target.height() / max(1, image_size.height()),
    )


def zoom_canvas_size(image_size: QSize, factor: float, bounds: QSize) -> QSize:
    """缩放后的画布尺寸：向上取整后夹进 ``bounds``（自绘画布不必预先缩图）。

    与 ``fit_display_size`` 的差别只在取整方向：画布是像素容器，内容宁可多 1px
    由裁剪负责，也不能少 1px 露出底色。
    """
    available = normalize_size(bounds)
    need = QSize(
        math.ceil(max(0, image_size.width()) * float(factor)),
        math.ceil(max(0, image_size.height()) * float(factor)),
    )
    return QSize(
        min(available.width(), max(1, need.width())),
        min(available.height(), max(1, need.height())),
    )


def default_canvas_size(bounds: QSize) -> QSize:
    """源图尺寸未知时的画布占位尺寸：中性比例，且绝不大于 media bounds。

    站点不要再写死 ``(520, 340)``：小屏/窄窗下这个常量会把窗口顶出可用区域。
    """
    available = normalize_size(bounds)
    return QSize(
        min(UNKNOWN_SOURCE_CANVAS.width(), available.width()),
        min(UNKNOWN_SOURCE_CANVAS.height(), available.height()),
    )


def column_width(
    content_width: int,
    top_bar_min_width: t.Optional[int] = None,
    available_width: t.Optional[int] = None,
) -> int:
    """承载列宽度：不窄于顶栏最小宽，且（给出可用宽时）不宽出可用区域。

    available_width 非空那一支即 danbooru 的 min(_max_right_panel_width(), max(display_w,
    top_bar_min_w))；缺了它会（窄屏下）让列宽顶掉窗口宽度。
    """
    width = max(int(content_width), int(top_bar_min_width or 0), 1)
    if available_width is not None:
        width = min(width, max(1, int(available_width)))
    return width


def media_bounds(screen_rect: QRect, chrome_width: int, chrome_height: int) -> QSize:
    """媒体可用区域 = 屏幕可用区域 − 外壳；任一侧为负时夹到 1。"""
    return QSize(
        max(1, int(screen_rect.width()) - int(chrome_width)),
        max(1, int(screen_rect.height()) - int(chrome_height)),
    )


def clamp_into_screen(size: QSize, screen: QRect) -> tuple[int, int]:
    """把 ``size`` 的窗口放进 ``screen``：**居中优先 + 四边钳制**，返回左上角全局坐标。

    规则：
    - 该轴能放下（size <= screen）：取向左/上取整的居中值，再夹进
      [screen.left, screen.left + screen_w - w]（纵向同理）→ 整窗必在屏内，居中偏差 ≤ 1px。
    - 该轴放不下：钉 top/left，不产生负偏移。
    """
    width = max(1, int(size.width()))
    height = max(1, int(size.height()))
    left, top = int(screen.x()), int(screen.y())
    screen_width = max(1, int(screen.width()))
    screen_height = max(1, int(screen.height()))

    x = left + (screen_width - width) // 2
    y = top + (screen_height - height) // 2
    max_x = left + screen_width - width
    max_y = top + screen_height - height

    x = left if max_x < left else min(max(x, left), max_x)
    y = top if max_y < top else min(max(y, top), max_y)
    return x, y


def chrome_extent(
    *,
    outer_margins: QMargins,
    main_margins: QMargins,
    column_margins: t.Optional[QMargins] = None,
    left_panel_width: int = 0,
    main_spacing: int = 0,
    column_spacing: int = 0,
    top_bar_height: int = 0,
) -> ChromeExtent:
    """外壳尺寸：窗口外圈留白 + frame 内留白 + 左列 + 间距 + 顶栏。

    - 横向：``outer`` 左右 + ``main`` 左右 + ``column`` 左右 + 左列宽 + ``main_spacing``
      （左右两列之间的 gap）。
    - 纵向：``outer`` 上下 + ``main`` 上下 + ``column`` 上下 + 顶栏高 + ``column_spacing``
      （顶栏与图像面之间的 gap）。

    ``column_margins`` 取「承载媒体那一列」的布局边距：danbooru 是
    ``right_panel_layout``（0），pawchive 是 ``view_column``（0）。
    """
    column = column_margins if column_margins is not None else QMargins(0, 0, 0, 0)
    width = (
        outer_margins.left() + outer_margins.right()
        + main_margins.left() + main_margins.right()
        + column.left() + column.right()
        + int(left_panel_width)
        + int(main_spacing)
    )
    height = (
        outer_margins.top() + outer_margins.bottom()
        + main_margins.top() + main_margins.bottom()
        + column.top() + column.bottom()
        + int(top_bar_height)
        + int(column_spacing)
    )
    return ChromeExtent(width=width, height=height)