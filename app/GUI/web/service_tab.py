from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWidgets import QFrame, QVBoxLayout

from ui.core.browser.aria2_bridge import Aria2PageBridge
from ui.core.browser.devtools import DevToolsToggle


class _InertChromeStub:

    def show(self) -> None:
        pass

    def hide(self) -> None:
        pass

    def setToolTip(self, text: str) -> None:
        pass


class ServiceTabHost(QFrame):

    pageLoadFinishedDetailed = Signal(bool, float)
    pageInteractive = Signal(str, float)

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("ServiceTabHost")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # Light shell only. The QWebEngine carrier is built by ensure_content() on the next
        # event-loop tick, after window_factory returned and the controller stored home_url.
        # Building the view in __init__ blocked the main window on the first expandBtn click
        # and read as a full-window white flash.
        self.view = None
        self.profile = None
        self.download_controller = None
        self.page_runtime = None
        self.interceptor = None
        self.window_mode = None
        self._content_ready = False
        self._content_scheduled = False
        self._shutdown_done = False

        # Attributes the synchronous controller contract touches before content
        # exists: _load_window_url sets home_url then calls load_home;
        # _configure_station_browser_chrome hides copyBtn/ensureBtn;
        # WebInterface._create_host sets the tagger and attaches the bridge;
        # a theme flip re-applies it. Aria2PageBridge.refresh() is a no-op while
        # view is None, so attach()/apply_page_theme() stay safe on the shell.
        self.home_url = QUrl("about:blank")
        self.copyBtn = _InertChromeStub()
        self.ensureBtn = _InertChromeStub()
        self.aria2_bridge = Aria2PageBridge(self)

    def schedule_content(self) -> None:
        """Queue the heavy build once; context-bound so a closed tab drops it."""
        if self._content_ready or self._content_scheduled or self._shutdown_done:
            return
        self._content_scheduled = True
        QTimer.singleShot(0, self, self.ensure_content)

    def ensure_content(self) -> None:
        """Build the QWebEngine carrier once, then replay the latest home_url."""
        self._content_scheduled = False
        if self._content_ready or self._shutdown_done:
            return
        self._content_ready = True

        from PySide6.QtWebEngineCore import QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from ui.browser_window import CustomWebEnginePage
        from ui.core.browser.download import BrowserDownloadController
        from ui.core.browser.page_runtime import BrowserPageRuntime
        from ui.core.browser.profile import create_browser_window_profile
        from ui.core.browser.runtime import BrowserRequestInterceptor
        from ui.core.browser.window_mode import BrowserWindowModeController

        self.profile = create_browser_window_profile(self, persistent=True)
        self.download_controller = BrowserDownloadController(self, self.profile)
        # Plain QWebEngineView, deliberately not FramelessWebEngineView: that helper calls
        # self.window().updateFrameless() from its constructor, so building a tab inside the
        # interface re-computes the MAIN window's frameless geometry and the window ends up
        # hidden. A tab is a layout-managed child; it has no frameless chrome of its own.
        self.view = QWebEngineView(self)
        self._layout.addWidget(self.view, 1)
        self.view.setPage(CustomWebEnginePage(self.profile, self.view))
        # F12 devtools for embedded tabs too: the carrier is a layout child of
        # the main window, so the shortcut must be scoped to it
        # (WidgetWithChildrenShortcut) and not steal F12 from other interfaces.
        self.dev_tools = DevToolsToggle(self)
        self.page_runtime = BrowserPageRuntime(self)
        self.interceptor = BrowserRequestInterceptor(self)
        self.window_mode = BrowserWindowModeController(self, self.interceptor)
        self.window_mode.enter_plain_page_mode()

        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.AutoLoadImages, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)

        # The URL may have been set by the controller after window_factory
        # returned (or by reload_service_window after a storage-dir change).
        # Loading here is the replay: the bridge now has a real page to inject.
        self.load_home()

    def load_home(self) -> None:
        if self.view is None:
            # window_factory / reload_service_window can call this while the
            # shell is still light. home_url is already stored; ensure_content
            # replays it as soon as the view exists.
            self.schedule_content()
            return
        self.page_runtime.prepare_navigation()
        self.aria2_bridge.refresh()
        self.view.load(self.home_url)

    def reload_current_view(self) -> None:
        if self.view is None:
            self.schedule_content()
            return
        self.view.reload()

    def shutdown(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        for attr in ("aria2_bridge", "page_runtime", "window_mode", "download_controller", "dev_tools"):
            target = getattr(self, attr, None)
            release = getattr(target, "shutdown", None)
            if not callable(release):
                continue
            try:
                release()
            except RuntimeError:
                pass
