# -*- coding: utf-8 -*-
"""Accent tab strip shared by WebInterface and SettingInterface.

One implementation, two call sites -- the settings tabs must look exactly like
the web tabs, so this is extracted rather than copied. Only ``TabBar`` (not
``Pivot``) gives fixed-width tabs with an independent item size, which is why
both strips are TabBars.

``AccentTabItem`` corrects two qfw behaviours (documented on the class), and
``AccentTabBar`` owns its own theming so every inserted tab is styled the
moment it exists, from any call site.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from qfluentwidgets import TabBar, TabItem

from core import ori_path
from ui.core.theme import CustTheme, theme_mgr
from ui.core.theme.qss_template import read_templated_qss_tokens, render_templated_qss_section

#: QSS template holding the strip background and its per-theme tokens.
TAB_STRIP_QSS_PATH = ori_path.joinpath("ui/core/theme/web_tabs.qss")


def current_tab_theme_name() -> str:
    return "dark" if theme_mgr.get_theme() == CustTheme.DARK else "light"


class AccentTabItem(TabItem):
    """TabItem that keeps qfw's painting, fixes its text opacity, adds an underline.

    1. qfw's paintEvent sets painter opacity for the unselected icon and never resets it, so the
       label paints dimmer than requested; _drawText restores full opacity first.
    2. TabBar dropped the accent underline the old Pivot drew: the underline is the single focus
       affordance (no selected pill, no per-tab colour swap). Hover feedback stays stock qfw.
    """

    underline_color: QColor = QColor("#4db8ff")

    def _drawText(self, painter: QPainter) -> None:
        painter.save()
        painter.setOpacity(1.0)
        super()._drawText(painter)
        painter.restore()

    def _drawSelectedBackground(self, painter: QPainter) -> None:
        # Deliberately empty: the underline is the only focus affordance, so the
        # selected tab keeps the strip's own background instead of qfw's
        # near-invisible pill (1.20:1 dark, 1.04:1 light) plus its hairline
        # borders.
        return

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.isSelected:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.underline_color)
        inset = 14
        width = max(1, self.width() - inset * 2)
        painter.drawRoundedRect(QRectF(inset, self.height() - 4, width, 3), 1.5, 1.5)


class AccentTabBar(TabBar):
    """TabBar that owns its own tab theming.

    qfw's ``insertTab`` hardcodes ``TabItem`` and knows nothing about the focus
    underline or a theme-driven label colour, so every insert would need the
    caller to remember a follow-up styling pass. Owning both here means a tab
    added from anywhere (business controller, tests, future call sites) is
    styled correctly the moment it exists.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text_color = QColor("#e0e0e0")
        self.accent_color = QColor("#4db8ff")

    def apply_palette(self, *, text: QColor, accent: QColor) -> None:
        self.text_color = QColor(text)
        self.accent_color = QColor(accent)
        self.restyle_all()

    def restyle_all(self) -> None:
        for index in range(self.count()):
            self.restyle_item(index)

    def restyle_item(self, index: int) -> None:
        item = self.tabItem(index)
        if item is None:
            return
        item.setTextColor(self.text_color)
        item.setBorderRadius(10)
        if isinstance(item, AccentTabItem):
            item.underline_color = self.accent_color
            item.update()

    def insertTab(self, index, routeKey, text, icon=None, onClick=None):
        if routeKey in self.itemMap:
            raise ValueError(f"The route key `{routeKey}` is duplicated.")
        if index == -1:
            index = len(self.items)
        if index <= self.currentIndex() and self.currentIndex() >= 0:
            self._currentIndex += 1

        item = AccentTabItem(text, self.view, icon)
        item.setRouteKey(routeKey)
        width = self.tabMaximumWidth() if self.isScrollable() else self.tabMinimumWidth()
        item.setMinimumWidth(width)
        item.setMaximumWidth(self.tabMaximumWidth())
        item.setShadowEnabled(self.isTabShadowEnabled())
        item.setCloseButtonDisplayMode(self.closeButtonDisplayMode)
        item.pressed.connect(self._onItemPressed)
        item.closed.connect(lambda: self.tabCloseRequested.emit(self.items.index(item)))
        if onClick:
            item.pressed.connect(onClick)

        self.itemLayout.insertWidget(index, item, 1)
        self.items.insert(index, item)
        self.itemMap[routeKey] = item
        self.restyle_item(index)
        if len(self.items) == 1:
            self.setCurrentIndex(0)
        return item


def apply_tab_strip_style(strip_frame) -> None:
    """Paint a strip frame + tab palette for the current theme.

    Both interfaces call this: the strip background (and its per-theme tokens)
    lives in one QSS template so the two tab strips cannot drift apart. The
    selector targets either object name, see ``web_tabs.qss``.
    """
    tokens = read_templated_qss_tokens(TAB_STRIP_QSS_PATH, current_tab_theme_name())
    strip_frame.setStyleSheet(render_templated_qss_section(
        TAB_STRIP_QSS_PATH, current_tab_theme_name(), "strip",
    ))
    bar = getattr(strip_frame, "tab_bar", None)
    if bar is not None:
        bar.apply_palette(
            text=QColor(tokens["TAB_TEXT"]),
            accent=QColor(tokens["TAB_ACCENT"]),
        )
    strip_frame.update()
