from __future__ import annotations

import weakref
from pathlib import Path

from PySide6.QtCore import QObject, Qt, qtTrId
from PySide6.QtWebEngineCore import QWebEngineDownloadRequest
from PySide6.QtWidgets import QFileDialog
from qfluentwidgets import InfoBar, InfoBarPosition


_DISPATCHER_ATTR = "_acggr_download_dispatcher"


class _ProfileDownloadDispatcher(QObject):
    """Route ``profile.downloadRequested`` to the carrier that owns the page.

    Since every persistent carrier (AriaNg monitor window, each station tab in
    the WebInterface tab strip) shares ONE profile, a plain per-carrier ``connect``
    would pop one save dialog per carrier for a single download. The dispatcher
    is connected once per profile and hands the request to the controller whose
    view owns ``download.page()``; the most recently registered carrier is the
    fallback when the page cannot be resolved.
    """

    def __init__(self, profile):
        super().__init__(profile)
        self._profile = profile
        self._controllers: list[weakref.ref] = []
        profile.downloadRequested.connect(self._on_download_requested)

    def register(self, controller: "BrowserDownloadController") -> None:
        self._prune()
        self._controllers.append(weakref.ref(controller))

    def unregister(self, controller: "BrowserDownloadController") -> None:
        self._controllers = [
            ref for ref in self._controllers
            if ref() is not None and ref() is not controller
        ]

    def _prune(self) -> None:
        self._controllers = [ref for ref in self._controllers if ref() is not None]

    def _on_download_requested(self, download_item: QWebEngineDownloadRequest) -> None:
        self._prune()
        controllers = [ref() for ref in self._controllers]
        controllers = [controller for controller in controllers if controller is not None]
        if not controllers:
            # Nobody can show a save dialog; an unaccepted request is dropped by
            # Qt6 anyway, cancelling makes that explicit instead of silent.
            download_item.cancel()
            return
        try:
            page = download_item.page()
        except RuntimeError:
            page = None
        for controller in reversed(controllers):
            if controller.owns_page(page):
                controller.handle_download_request(download_item)
                return
        controllers[-1].handle_download_request(download_item)


def _profile_dispatcher(profile) -> _ProfileDownloadDispatcher:
    dispatcher = getattr(profile, _DISPATCHER_ATTR, None)
    if dispatcher is not None:
        try:
            if dispatcher._profile is profile:
                return dispatcher
        except RuntimeError:
            dispatcher = None
    dispatcher = _ProfileDownloadDispatcher(profile)
    setattr(profile, _DISPATCHER_ATTR, dispatcher)
    return dispatcher


class BrowserDownloadController(QObject):
    """Qt WebEngine download owner: save dialog + accept().

    Default path MUST NOT use conf.sv_path (comic crawl root). Station/browser
    file saves belong in the OS/profile download location only.
    """

    def __init__(self, window, profile):
        super().__init__(window)
        self._window = window
        self._profile = profile
        self._active_downloads: set[QWebEngineDownloadRequest] = set()
        self._connected = True
        self._dispatcher = _profile_dispatcher(profile)
        self._dispatcher.register(self)

    def shutdown(self) -> None:
        if self._connected:
            self._dispatcher.unregister(self)
            self._connected = False
        for download_item in list(self._active_downloads):
            self._forget_download(download_item)

    def owns_page(self, page) -> bool:
        """True when ``page`` belongs to this carrier's WebEngine view."""
        if page is None:
            return False
        view = getattr(self._window, "view", None)
        if view is None:
            return False
        try:
            return view.page() is page
        except RuntimeError:
            return False

    def handle_download_request(self, download_item: QWebEngineDownloadRequest) -> None:
        suggested_name = str(download_item.suggestedFileName() or "").strip() or "download"
        default_path = self._default_directory().joinpath(suggested_name)
        selected_path, _selected_filter = QFileDialog.getSaveFileName(
            self._window,
            qtTrId("browser.download.select_path"),
            str(default_path),
        )
        if not selected_path:
            download_item.cancel()
            return

        save_path = Path(selected_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        download_item.setDownloadDirectory(str(save_path.parent))
        download_item.setDownloadFileName(save_path.name)
        self._track_download(download_item)
        download_item.accept()
        self._show_info(
            InfoBar.success,
            qtTrId("browser.download.started").format(name=save_path.name),
            duration=2500,
        )

    def _track_download(self, download_item: QWebEngineDownloadRequest) -> None:
        self._active_downloads.add(download_item)
        # stateChanged 会带上 DownloadState 参数；lambda 第一个形参若写成 item=
        # 会被该枚举覆盖，变成 'DownloadState' object has no attribute 'state'。
        download_item.stateChanged.connect(
            lambda _state, item=download_item: self._on_download_state_changed(item),
        )
        download_item.isFinishedChanged.connect(
            lambda item=download_item: self._on_download_finished_changed(item),
        )

    def _on_download_state_changed(self, download_item: QWebEngineDownloadRequest) -> None:
        if download_item is None:
            return
        state = download_item.state()
        if state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            self._show_info(
                InfoBar.success,
                qtTrId("browser.download.completed").format(
                    path=self._resolved_path(download_item)),
                duration=4500,
            )
            self._forget_download(download_item)
            return
        if state == QWebEngineDownloadRequest.DownloadState.DownloadCancelled:
            self._forget_download(download_item)
            return
        if state == QWebEngineDownloadRequest.DownloadState.DownloadInterrupted:
            reason = str(download_item.interruptReasonString() or "").strip() or qtTrId(
                "browser.download.unknown_reason")
            self._show_info(
                InfoBar.error,
                qtTrId("browser.download.interrupted").format(reason=reason),
                duration=6000,
            )
            self._forget_download(download_item)

    def _on_download_finished_changed(self, download_item: QWebEngineDownloadRequest) -> None:
        if download_item is None or not isinstance(download_item, QWebEngineDownloadRequest):
            return
        if not download_item.isFinished():
            return
        if download_item not in self._active_downloads:
            return
        # Some Qt builds finish without re-emitting the terminal state we care about.
        self._on_download_state_changed(download_item)

    def _forget_download(self, download_item: QWebEngineDownloadRequest) -> None:
        self._active_downloads.discard(download_item)

    def _default_directory(self) -> Path:
        # Never default under conf.sv_path — that tree is the comic crawl/library root.
        profile_download_path = str(self._profile.downloadPath() or "").strip()
        if profile_download_path:
            return Path(profile_download_path)
        return Path.home().joinpath("Downloads")

    @staticmethod
    def _resolved_path(download_item: QWebEngineDownloadRequest) -> str:
        directory = str(download_item.downloadDirectory() or "").strip()
        file_name = str(download_item.downloadFileName() or "").strip()
        if directory and file_name:
            return str(Path(directory).joinpath(file_name))
        return file_name or directory or "unknown"

    def _show_info(self, factory, content: str, *, duration: int) -> None:
        parent = getattr(self._window, "view", None) or self._window
        factory(
            title="",
            content=content,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=duration,
            parent=parent,
        )
