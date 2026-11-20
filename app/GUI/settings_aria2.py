"""Aria2 download-engine settings card for Script SettingInterface.

CGS005: Settings content only — do not touch ScriptWindow navigation lifecycle.
"""
from __future__ import annotations

from PySide6.QtCore import qtTrId
from qfluentwidgets import (
    FluentIcon as FIF,
    LineEdit,
    SpinBox,
)

from ui.qfluent.components.elided_card import ElidedGroupHeaderCardWidget


class Aria2GroupCard(ElidedGroupHeaderCardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setting_interface = parent
        self.setObjectName("Aria2GroupCard")
        self.setTitle(qtTrId("aria2.config.title"))
        self.setBorderRadius(8)

        self.proxy_edit = LineEdit(self)
        self.proxy_edit.setObjectName("DownloadProxyEdit")
        # Same "例如: URL" shape as the access-proxy row above, and the example
        # carries its scheme: a bare host:port read as a different kind of value.
        self.proxy_edit.setPlaceholderText(
            f"{qtTrId('common.example_prefix')} {qtTrId('aria2.config.proxy_placeholder')}")
        self.proxy_edit.setClearButtonEnabled(True)
        self.proxy_edit.setMinimumWidth(320)
        # No hard-coded candidates here: the history completer is installed by
        # SettingInterface from the shared MRU store, like every other URL row.

        # Row count, not an on/off switch: acggr owns the download history
        # (``download_history`` table), so the user decides how many of the
        # most recent finished downloads stay listed; 0 disables history.
        # Imported here rather than at module scope: ``core.script.aria2``
        # initialises the engine package (paths, binary layout) on import, which
        # a settings card should not trigger just by being imported.
        from core.script.aria2.settings import DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT

        self.history_limit_spin = SpinBox(self)
        self.history_limit_spin.setObjectName("KeepHistorySpinBox")
        self.history_limit_spin.setRange(0, MAX_HISTORY_LIMIT)
        self.history_limit_spin.setSingleStep(100)
        self.history_limit_spin.setValue(DEFAULT_HISTORY_LIMIT)

        self.addGroup(FIF.VPN, qtTrId("aria2.config.download_proxy"), qtTrId("aria2.config.download_proxy_hint"), self.proxy_edit)
        self.addGroup(
            FIF.HISTORY, qtTrId("aria2.config.keep_history"),
            qtTrId("aria2.config.keep_history_hint"), self.history_limit_spin,
        )

    def get_proxy_text(self) -> str:
        return self.proxy_edit.text().strip()

    def set_proxy_text(self, text: object) -> None:
        self.proxy_edit.setText(str(text or ""))

    def get_history_limit(self) -> int:
        return int(self.history_limit_spin.value())

    def set_history_limit(self, limit: int) -> None:
        self.history_limit_spin.setValue(int(limit))
