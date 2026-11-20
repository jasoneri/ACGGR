from __future__ import annotations

import base64
import pathlib as p

import httpx
from PySide6.QtCore import Qt, QTimer, qtTrId, QRectF, QUrl
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QStackedWidget, QVBoxLayout
from ui.qfluent.components.accent_tabs import (
    AccentTabBar, AccentTabItem, current_tab_theme_name,
)
from qfluentwidgets import (
    BodyLabel, IndeterminateProgressBar, InfoBar, InfoBarPosition,
    TabBar, TabCloseButtonDisplayMode, TabItem,
)

from core import ori_path
from core.config import conf_dir
from core.script.aria2.lock import read_instance
from core.website.preprocess_script import ReleaseAssetCache, _ensure_data_client
from ui.async_task import AsyncTaskManager, TaskConfig
from ui.core.theme import CustTheme, theme_mgr
from ui.core.theme.qss_template import read_templated_qss_tokens, render_templated_qss_section

ARIANG_HTML_NAME = "AriaNg.html"
ARIANG_CACHE_TTL_HOURS = 168
ARIANG_RPC_COMMAND_PATH = "#!/settings/rpc/set"
ARIANG_TAB_KEY = "ariang"
_DOWNLOAD_TASK_ID = "ariang_page_download"
_ENGINE_TASK_ID = "ariang_engine_ensure"
SERVICE_TAB_PREFIX = "svc:"
WEB_TAB_WIDTH = 148
WEB_TABS_QSS_PATH = ori_path.joinpath("ui/core/theme/web_tabs.qss")


def _current_theme_name() -> str:
    return current_tab_theme_name()


class _AccentTabItem(AccentTabItem):
    """Backwards-compatible alias; the implementation lives in
    ``ui.qfluent.components.accent_tabs`` so SettingInterface can share it."""


class _AccentTabBar(AccentTabBar):
    """Backwards-compatible alias (see ``_AccentTabItem``)."""


def service_tab_key(service_name: str) -> str:
    return f"{SERVICE_TAB_PREFIX}{str(service_name or '').strip()}"


def premount_web_shell(window) -> None:
    """A: build the light Web shell on the first idle tick after ``show()``.

    ``prewarm_webengine()`` runs a nested QEventLoop before ``window.show()``;
    it consumes a plain 0ms QTimer early (offscreen probe output:
    ``AFTER_PREWARM visible False fired [('web', False)]``). Re-queue until the
    window is actually visible, then materialize once. First-visit work lives in
    ``WebInterface.showEvent`` and must never run on this pre-mount path.
    """
    if not window.isVisible():
        QTimer.singleShot(0, window, lambda: premount_web_shell(window))
        return
    window._materialize_interface("web")


class AriaNgReleaseAsset(ReleaseAssetCache):
    """AriaNg AllInOne page from the upstream preset release, cached at conf_dir."""

    def __init__(self, data_client: httpx.AsyncClient, progress_callback=None):
        from core.core import ManagedAsset

        super().__init__(
            name="ariang", db_path=conf_dir.joinpath(ARIANG_HTML_NAME),
            download_urls=ManagedAsset("index.html").urls, data_client=data_client,
            progress_callback=progress_callback, timeout=60, cache_ttl_hours=ARIANG_CACHE_TTL_HOURS,
        )


def _download_ariang_task():
    """AsyncTask thread body: fetch the AriaNg page from the preset release."""
    import asyncio

    async def _run():
        client = _ensure_data_client()
        try:
            return await AriaNgReleaseAsset(client).ensure()
        finally:
            await client.aclose()

    return asyncio.run(_run())


def build_ariang_url(record) -> QUrl:
    """file:// URL of the cached page; with a record, carrying the RPC set command."""
    html_path = conf_dir.joinpath(ARIANG_HTML_NAME)
    base = QUrl.fromLocalFile(str(html_path))
    if record is None:
        return base
    secret = base64.urlsafe_b64encode(str(record.secret).encode("utf-8")).decode("ascii").rstrip("=")
    base.setFragment(f"{ARIANG_RPC_COMMAND_PATH.lstrip('#')}/http/127.0.0.1/{int(record.port)}/jsonrpc/{secret}")
    return base


def _is_cache_fresh(path: p.Path) -> bool:
    if not path.exists():
        return False
    from datetime import datetime, timedelta

    file_time = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - file_time <= timedelta(hours=ARIANG_CACHE_TTL_HOURS)


class _EmbeddedHost:
    tab_key: str = ""
    tab_service_name: str = ""

    def bind_embedded_tab(self, ariang_interface, *, tab_key: str, service_name: str) -> None:
        self._ariang = ariang_interface
        self.tab_key = tab_key
        self.tab_service_name = service_name

    def close(self) -> None:  # noqa: A003 - tab close instead of window close
        self._ariang.close_embedded_tab(self.tab_key)

    def setGeometry(self, *args, **kwargs) -> None:
        # Layout-managed inside the interface: the controller's popup-geometry
        # sync must not fight the stacked layout.
        return

    def shutdown_bridge(self) -> None:
        self.shutdown()


def _embedded_host_class():
    from app.GUI.web.service_tab import ServiceTabHost

    class _EmbeddedServiceTabHost(_EmbeddedHost, ServiceTabHost):
        pass

    return _EmbeddedServiceTabHost


class WebInterface(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.parent_window = parent
        self.setObjectName("WebInterface")
        self._task_mgr = AsyncTaskManager(self)
        self._profile = None
        self._ariang_host = None
        self._ariang_html_loaded = False
        self._service_tabs: dict[str, QFrame] = {}
        self._setup_ui()
        self._render_ariang_state()
        from ui.core.lang import lang_mgr
        from ui.core.theme import theme_mgr

        theme_mgr.subscribe(self._on_theme_changed)
        lang_mgr.subscribe(self._on_language_changed)
        self.destroyed.connect(lambda *_: theme_mgr.unsubscribe(self._on_theme_changed))
        self.destroyed.connect(lambda *_: lang_mgr.unsubscribe(self._on_language_changed))

    def _on_theme_changed(self, _theme) -> None:
        self._apply_tab_strip_theme()
        self._apply_to_pages("apply_page_theme")

    def _on_language_changed(self, _lang) -> None:
        self._apply_to_pages("apply_page_language")

    def _apply_to_pages(self, method: str) -> None:
        """Fan one page-level re-apply out to every embedded window.

        Theme and language share this: both bake a decision into the page
        (colours vs wording) and both must reach the AriaNg host *and* the
        service tabs, so neither gets its own traversal.
        """
        hosts = [self._ariang_host, *self._service_tabs.values()]
        for host in hosts:
            if host is not None:
                getattr(host.aria2_bridge, method)()

    def _apply_tab_strip_theme(self) -> None:
        """Repaint the tab strip for the current theme.

        Focus is carried by the accent underline alone: ``_AccentTabItem``
        suppresses qfw's selected pill, so the current tab keeps the strip
        background and only gains the underline. Labels share one theme-driven
        colour -- 9.29:1 on the dark strip (``#353535``), 11.59:1 on the light
        one (``#f5f5f5``); the underline itself measures 5.62:1 / 4.13:1
        against the strip, so the focus marker clears AA in both modes.
        """
        tokens = read_templated_qss_tokens(WEB_TABS_QSS_PATH, _current_theme_name())
        self.tab_strip.setStyleSheet(render_templated_qss_section(
            WEB_TABS_QSS_PATH, _current_theme_name(), "strip",
        ))
        self.tab_bar.apply_palette(
            text=QColor(tokens["TAB_TEXT"]),
            accent=QColor(tokens["TAB_ACCENT"]),
        )
        self.tab_strip.update()

    # --- UI ---------------------------------------------------------------

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        # Browser-style tab strip (same pattern as the Danbooru interface):
        # strictly fixed-width tabs, close buttons on hover, no add button --
        # tabs exist only for the persistent AriaNg monitor and stations opened
        # through the business controller. The old qfw Pivot stretched every
        # item to share the strip width (stretch=1 per item), which re-flowed
        # the overlay close button into wrong positions whenever a station tab
        # was added. TabBar keeps item width independent of strip width.
        self.tab_strip = QFrame(self)
        self.tab_strip.setObjectName("WebTabStrip")
        strip_layout = QHBoxLayout(self.tab_strip)
        strip_layout.setContentsMargins(6, 2, 6, 2)
        strip_layout.setSpacing(0)
        self.tab_bar = _AccentTabBar(self.tab_strip)
        self.tab_bar.setScrollable(True)
        self.tab_bar.setTabMaximumWidth(WEB_TAB_WIDTH)
        self.tab_bar.setTabShadowEnabled(False)
        self.tab_bar.setAddButtonVisible(False)
        self.tab_bar.setCloseButtonDisplayMode(TabCloseButtonDisplayMode.ON_HOVER)
        self.tab_bar.enableTransparentBackground()
        self.tab_bar.itemLayout.setContentsMargins(0, 5, 0, 5)
        self.tab_bar.itemLayout.setSpacing(6)
        self.tab_bar.currentChanged.connect(self._on_tab_bar_index_changed)
        self.tab_bar.tabCloseRequested.connect(self._on_tab_close_requested)
        strip_layout.addWidget(self.tab_bar, 1)
        layout.addWidget(self.tab_strip)

        ariang_item = self.tab_bar.insertTab(-1, ARIANG_TAB_KEY, qtTrId("ariang.title"))
        ariang_item.setToolTip(qtTrId("ariang.title"))
        # The AriaNg monitor is the persistent home tab: no close affordance.
        ariang_item.setCloseButtonDisplayMode(TabCloseButtonDisplayMode.NEVER)
        self._apply_tab_strip_theme()

        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)

        state_page = QFrame(self.stack)
        state_layout = QVBoxLayout(state_page)
        state_layout.setContentsMargins(24, 20, 24, 20)
        state_layout.addStretch(1)
        self.progressBar = IndeterminateProgressBar(state_page, start=False)
        self.progressBar.setFixedHeight(6)
        self.progressBar.hide()
        state_layout.addWidget(self.progressBar)
        self._status_label = BodyLabel("", state_page)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)
        state_layout.addWidget(self._status_label)
        state_layout.addStretch(1)
        self._state_page = state_page
        self.stack.addWidget(state_page)
        self.stack.setCurrentWidget(state_page)

    # --- persistent AriaNg tab ---------------------------------------------

    def _on_tab_bar_index_changed(self, index: int) -> None:
        tab = self.tab_bar.tabItem(index)
        if tab is None:
            return
        if tab.routeKey() == ARIANG_TAB_KEY:
            self.stack.setCurrentWidget(
                self._ariang_host if self._ariang_host is not None else self._state_page,
            )
        else:
            host = self._service_tabs.get(tab.routeKey().removeprefix(SERVICE_TAB_PREFIX))
            if host is not None:
                self.stack.setCurrentWidget(host)

    def _on_tab_close_requested(self, index: int) -> None:
        tab = self.tab_bar.tabItem(index)
        if tab is None:
            return
        self.close_embedded_tab(tab.routeKey())

    def _show_ariang_tab(self, *_args) -> None:
        self.tab_bar.setCurrentTab(ARIANG_TAB_KEY)
        self.stack.setCurrentWidget(
            self._ariang_host if self._ariang_host is not None else self._state_page,
        )

    def _render_ariang_state(self) -> None:
        if self._ariang_host is not None:
            self._render_state_message(show_progress=False, message=self._connection_text())
            return
        if self._task_mgr.is_task_running(_DOWNLOAD_TASK_ID):
            self._render_state_message(show_progress=True, message=qtTrId("aria2.ariang.downloading"))
            return
        if _is_cache_fresh(conf_dir.joinpath(ARIANG_HTML_NAME)):
            self._render_state_message(show_progress=False, message=self._connection_text())
            return
        self._render_state_message(
            show_progress=False,
            message=qtTrId("aria2.ariang.not_downloaded"),
        )

    def _render_state_message(self, *, show_progress: bool, message: str) -> None:
        self._status_label.setText(message)
        if show_progress:
            self.progressBar.show()
            if not self.progressBar.isStarted():
                self.progressBar.start()
        else:
            self.progressBar.stop()
            self.progressBar.hide()

    def _connection_text(self) -> str:
        try:
            record = read_instance()
        except Exception:
            return qtTrId("aria2.ariang.ready.instance_unreadable")
        if record is None:
            return qtTrId("aria2.ariang.ready.not_running")
        return qtTrId("aria2.ariang.ready.rpc").format(port=record.port)

    # --- lifecycle --------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        self._maybe_start_first_download()
        self._ensure_engine_async()

    # --- aria2 engine boot --------------------------------------------------

    def _ensure_engine_async(self):
        """Boot the aria2 engine off the UI thread on first visit (idempotent).

        Nothing in the product startup chain runs ensure_engine, so the AriaNg
        tab read a None instance record and stayed "not connected" forever.
        Warm path is a 0.003s RPC ping; cold path spawns aria2c (~1-2s).
        ``proxy`` is resolved here on the UI thread and handed over explicitly
        (same rule as _schedule_download_engine_restart): the worker must not
        read the conf itself because script_conf.update() rewrites it from the
        UI thread and a concurrent read can observe the truncated window.
        """
        if self._task_mgr.is_task_running(_ENGINE_TASK_ID):
            return
        from core.script.aria2 import ensure_engine
        from core.script.aria2.settings import get_proxy

        config = TaskConfig(
            task_func=ensure_engine,
            kwargs={"proxy": get_proxy()},
            success_callback=self._on_engine_ready,
            error_callback=self._on_engine_error,
            tooltip_title="",
            show_success_info=False,
            show_error_info=False,
            show_tooltip=False,
            tooltip_parent=None,
        )
        self._task_mgr.execute_task(_ENGINE_TASK_ID, config)

    def _on_engine_ready(self, _endpoint) -> None:
        # Replay only tabs whose bridge never got a usable record (built while
        # the engine was down). showEvent re-runs ensure_engine on every visit
        # to this interface, so this callback fires on every re-entry; a tab
        # whose bridge is already usable holds live page state (a station SPA's
        # search results), and the old unconditional load_home() here is what
        # wiped it on every interface switch. usable flips True inside that one
        # replay's load_home→refresh, so each tab is replayed at most once.
        for host in list(self._service_tabs.values()):
            try:
                if host.aria2_bridge.usable:
                    continue
                host.load_home()
            except Exception:
                continue
        # Race fix, scoped to this callback: _on_download_ok (or the fresh cache path in
        # _maybe_start_first_download) owns building the AriaNg embed. Here we only replay
        # an ALREADY built host once, so a page loaded before the engine still gets the RPC
        # set command from the now-readable record.
        ariang = self._ariang_host
        if ariang is not None and not getattr(self, "_ariang_engine_replayed", False):
            try:
                ariang.home_url = build_ariang_url(read_instance())
                ariang.load_home()
            except Exception:
                pass  # keep the flag unset so a later ready can retry
            else:
                self._ariang_engine_replayed = True
        self._render_state_message(show_progress=False, message=self._connection_text())

    def _on_engine_error(self, _error: str) -> None:
        self._render_state_message(show_progress=False, message=self._connection_text())

    def _maybe_start_first_download(self):
        if self._ariang_host is not None:
            return
        if self._task_mgr.is_task_running(_DOWNLOAD_TASK_ID):
            self._render_ariang_state()
            return
        if _is_cache_fresh(conf_dir.joinpath(ARIANG_HTML_NAME)):
            self._build_ariang_embed()
            return
        config = TaskConfig(
            task_func=_download_ariang_task,
            success_callback=self._on_download_ok,
            error_callback=self._on_download_error,
            tooltip_title="",
            show_success_info=False,
            show_error_info=False,
            show_tooltip=False,
            tooltip_parent=None,
        )
        self._task_mgr.execute_task(_DOWNLOAD_TASK_ID, config)
        self._render_ariang_state()

    # --- results ----------------------------------------------------------

    def _on_download_ok(self, result):
        path = p.Path(str(result.db_path))
        if not path.exists():
            self._render_state_message(
                show_progress=False,
                message=qtTrId("aria2.ariang.file_missing").format(path=path),
            )
            return
        self._build_ariang_embed()

    def _on_download_error(self, error: str):
        from ui.async_task import summarize_error_message

        self._render_ariang_state()
        InfoBar.error(
            title="", content=qtTrId("aria2.ariang.download_failed").format(error=summarize_error_message(error)),
            orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.BOTTOM,
            duration=-1, parent=self,
        )

    def _build_ariang_embed(self) -> None:
        """Swap the persistent tab from the state page to the embedded AriaNg page."""
        if self._ariang_host is not None:
            return  # late success after a retry: keep whatever tab the user is on
        try:
            record = read_instance()
        except Exception:
            record = None
        self._ariang_host = self._create_host(
            tab_key=ARIANG_TAB_KEY, service_name="", url=build_ariang_url(record),
        )
        self._ariang_html_loaded = True
        self.stack.addWidget(self._ariang_host)
        self._show_ariang_tab()

    # --- shared web infra ---------------------------------------------------

    def _ensure_profile(self):
        """One shared persistent profile for every embedded tab (localStorage survives)."""
        if self._profile is None:
            from ui.core.browser.profile import create_browser_window_profile

            self._profile = create_browser_window_profile(self, persistent=True)
        return self._profile

    def _create_host(self, *, tab_key: str, service_name: str, url: QUrl | None = None) -> QFrame:
        from core.script.aria2.tagging import tag_file

        host = _embedded_host_class()(self)
        host.bind_embedded_tab(self, tab_key=tab_key, service_name=service_name)
        host.aria2_bridge.set_tagger(tag_file)
        host.aria2_bridge.attach(service_name=service_name)
        if url is not None:
            host.home_url = url
            host.load_home()
        # Both service tabs and the persistent AriaNg tab are created through
        # here, so queue the heavy carrier build once for either path. The
        # returned object is still a light QFrame: ensure_content runs on the
        # next event-loop tick and replays whichever home_url is current then.
        host.schedule_content()
        return host

    # --- service tabs (µsPalace etc.) ---------------------------------------

    def _switch_parent_here(self) -> None:
        """Bring this interface to the front before touching the inner tab strip.

        Ordering matters: switching the main window while the inner stack swaps
        to a live QWebEngineView made the 76px PopUpAniStackedWidget slide run
        against the page's first paint, which reads as the whole window closing
        and reopening. The window is switched first, and its slide is skipped
        for this one switch (other page switches keep their animation).
        """
        window = self.parent_window
        if window is None:
            return
        stacked = getattr(window, "stackedWidget", None)
        disable_pop = stacked is not None and getattr(stacked, "isAnimationEnabled", False)
        if disable_pop:
            stacked.setAnimationEnabled(False)
        try:
            window.switchTo(self)
        finally:
            if disable_pop:
                stacked.setAnimationEnabled(True)

    def open_service_tab(self, service_name: str, url: str) -> QFrame:
        """Station-controller window_factory: one persistent tab per service.

        Repeat opens re-point at the existing tab (the controller reloads the
        URL); the whole interface switches to the front so the tab is visible.
        """
        self._switch_parent_here()
        host = self._service_tabs.get(service_name)
        if host is None:
            tab_key = service_tab_key(service_name)
            host = self._create_host(tab_key=tab_key, service_name=service_name)
            self._service_tabs[service_name] = host
            self.stack.addWidget(host)
            item = self.tab_bar.insertTab(-1, tab_key, service_name)
            item.setToolTip(service_name)
            host.destroyed.connect(
                lambda *_args, name=service_name: self._service_tabs.pop(name, None),
            )
        # setCurrentTab only moves the selection (qfw TabBar does not emit
        # currentChanged for programmatic switches); the stack follows here.
        self.tab_bar.setCurrentTab(host.tab_key)
        self.stack.setCurrentWidget(host)
        return host

    def close_embedded_tab(self, tab_key: str) -> None:
        if tab_key == ARIANG_TAB_KEY:
            return  # the AriaNg monitor tab is persistent
        service_name = tab_key.removeprefix(SERVICE_TAB_PREFIX)
        host = self._service_tabs.pop(service_name, None)
        if host is None:
            return
        current_tab = self.tab_bar.currentTab()
        if current_tab is not None and current_tab.routeKey() == tab_key:
            # Move the selection off the dying tab first so removeTabByKey
            # neither auto-selects a neighbour nor emits a switch for it.
            self.tab_bar.setCurrentTab(ARIANG_TAB_KEY)
            self.stack.setCurrentWidget(self._ariang_host or self._state_page)
        self.tab_bar.removeTabByKey(tab_key)
        self.stack.removeWidget(host)
        try:
            host.shutdown_bridge()
        except RuntimeError:
            pass
        host.deleteLater()

    def remove_service_tab(self, service_name: str) -> None:
        """Compatibility shim over close_embedded_tab (tab_key lookup by name)."""
        self.close_embedded_tab(service_tab_key(service_name))

