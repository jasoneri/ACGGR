from __future__ import annotations

from PySide6.QtCore import qtTrId
from PySide6.QtWidgets import QFileDialog
from qfluentwidgets import (
    FluentIcon as FIF,
    PushButton,
)

from core.config.qc import acggr_cfg, jsoneri_storage_root
from ui.qfluent.components.elided_card import ElidedGroupHeaderCardWidget


class JsoneriGroupCard(ElidedGroupHeaderCardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setting_interface = parent
        self.setObjectName("JsoneriGroupCard")
        self.setTitle(qtTrId("settings.jsoneri.title"))

        self.storageButton = PushButton(qtTrId("common.save_path.select"))
        self.storageButton.setFixedWidth(120)
        self.storageButton.clicked.connect(self._choose_storage_dir)
        self.storageCard = self.addGroup(
            FIF.DOWNLOAD, qtTrId("common.save_path.title"),
            self._storage_dir_text(), self.storageButton,
        )

    def _storage_dir_text(self) -> str:
        """The root is never "unset": like the pawchive/danbooru rows, an
        unchosen value resolves to the site directory under the shared root
        (``DEFAULT_JSONERI_STORAGE_DIR``), which is where downloads land, so
        showing that path beats showing a state the row cannot act on."""
        return str(jsoneri_storage_root())

    def _choose_storage_dir(self) -> None:
        """Pick the storage root: write qconfig, then ask the jsoneri interface
        to re-attach its bridge so open pages read the new value (the contract
        the old header button had)."""
        # Start the dialog at the effective root (the resolved default before a
        # first choice) instead of at a blank start directory.
        current = str(jsoneri_storage_root())
        folder = QFileDialog.getExistingDirectory(self, qtTrId("settings.jsoneri.storage_select"), current)
        if not folder:
            return
        acggr_cfg.jsoneriStorageDir.value = folder
        acggr_cfg.save()
        self.storageCard.setContent(self._storage_dir_text())
        self._reload_station_window()

    def _reload_station_window(self) -> int:
        """Returns how many station windows were reloaded (0 or 1)."""
        parent = self.setting_interface.parent_window if self.setting_interface else None
        interface = getattr(parent, "jsoneriPalacesProbeInterface", None) if parent else None
        if interface is None:
            return 0
        return interface.reload_station_window()
