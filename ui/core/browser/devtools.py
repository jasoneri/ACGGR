# -*- coding: utf-8 -*-
"""F12 DevTools toggle shared by every webengine carrier.

One inspector per carrier, lazily created on the first F12 and reused: F12 again hides it, one
more shows it. The inspector is a parentless QWebEngineView (its own top-level window) so it
works the same for the popup BrowserWindow and embedded ServiceTabHost tabs (embedding the
console in the page would fight the frameless chrome and the tab strip stack).

The shortcut uses WidgetWithChildrenShortcut so it only fires while the web view (or the carrier
window) has focus. setInspectedPage is re-issued whenever the inspected page differs from the
mirrored one, so one console follows the live page.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, qtTrId
from PySide6.QtGui import QKeySequence, QShortcut


class DevToolsToggle(QObject):
    """Bind F12 on one carrier; own its single DevTools window."""

    def __init__(self, carrier):
        super().__init__(carrier)
        self._carrier = carrier
        self._dev_view = None
        self._shortcut = QShortcut(QKeySequence(Qt.Key.Key_F12), carrier)
        self._shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut.activated.connect(self.toggle)

    # --- actions -----------------------------------------------------------

    def toggle(self) -> None:
        if self._dev_view is not None and self._dev_view.isVisible():
            self._dev_view.hide()
            return
        self.show()

    def show(self) -> None:
        view = getattr(self._carrier, "view", None)
        if view is None or view.page() is None:
            return  # light shell (ServiceTabHost before ensure_content)
        if self._dev_view is None:
            from PySide6.QtWebEngineWidgets import QWebEngineView

            self._dev_view = QWebEngineView()
            self._dev_view.setWindowTitle(qtTrId("browser.devtools.title"))
            self._dev_view.resize(1100, 720)
        dev_page = self._dev_view.page()
        if dev_page.inspectedPage() is not view.page():
            dev_page.setInspectedPage(view.page())
        self._dev_view.show()
        self._dev_view.raise_()
        self._dev_view.activateWindow()

    # --- lifecycle ---------------------------------------------------------

    def shutdown(self) -> None:
        if self._dev_view is None:
            return
        try:
            self._dev_view.page().setInspectedPage(None)
        except RuntimeError:  # page already destroyed with its carrier
            pass
        self._dev_view.close()
        self._dev_view.deleteLater()
        self._dev_view = None
