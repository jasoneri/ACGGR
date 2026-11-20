# -*- coding: utf-8 -*-
"""Two SettingCard shapes qfluentwidgets does not ship.

qfw's SwitchSettingCard / ComboBoxSettingCard / RangeSettingCard bind to a ConfigItem and persist
through qconfig; the Settings page cannot use those because its values live in conf.yml (a
dataclass + yaml) and the controls are not always a switch/combo (language picker, theme switch).

These classes keep qfw's visual contract (icon + title + hint + trailing control, same
SETTING_CARD stylesheet, same 70/50px height) while leaving persistence to the caller.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from qfluentwidgets import LineEdit, SettingCard


class WidgetSettingCard(SettingCard):
    """SettingCard hosting an arbitrary trailing control.

    Used for controls that need their own persistence path: the language
    drop-down, the theme switch, or a plain button.
    """

    def __init__(self, icon, title, content=None, widget=None, parent=None):
        super().__init__(icon, title, content, parent)
        self.widget = widget
        if widget is not None:
            widget.setParent(self)
            self.hBoxLayout.addWidget(widget, 0, Qt.AlignRight)
        self.hBoxLayout.addSpacing(16)


class LineEditSettingCard(SettingCard):
    """SettingCard with a trailing ``LineEdit``.

    ``placeholder`` carries the example value (e.g. ``127.0.0.1:7890``) rather
    than the hint line: the hint belongs in ``content`` so it wraps under the
    title like every other card, and the field stays free for the real value.
    """

    def __init__(self, icon, title, content=None, placeholder="", parent=None):
        super().__init__(icon, title, content, parent)
        self.lineEdit = LineEdit(self)
        self.lineEdit.setPlaceholderText(placeholder)
        self.lineEdit.setClearButtonEnabled(True)
        self.lineEdit.setMinimumWidth(280)
        self.hBoxLayout.addWidget(self.lineEdit, 0, Qt.AlignRight)
        self.hBoxLayout.addSpacing(16)
