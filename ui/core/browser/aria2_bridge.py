# -*- coding: utf-8 -*-
"""Host side of the aria2 page bridge, owned by one top-level BrowserWindow.

``ui`` may not import ``app``, so the page-side bridge lives here and the app
layer imports it. ``core.config`` re-exports ``deploy.curr_os`` to keep the
ui-to-core edge intact.

- ``build_aria2_bridge_source`` — pure, no window argument.
- QWebChannel shape: ``Aria2Bridge`` is a plain ``QObject`` parented to the
  window, the channel created against the page.
- ``openItemDir`` — the page may only name a 16-hex gid; the path comes from
  the host's own ``aria2.tellStatus`` (or the host's ``download_history.dir``
  for a derived gid), so there is nothing to traverse.
- DocumentCreation + MainWorld injection (host-injected scripts bypass a
  page's ``script-src`` CSP).
"""
from __future__ import annotations

import json
import pathlib as p
import re

from PySide6.QtCore import QObject, Qt, Signal, Slot

ARIA2_BRIDGE_SCRIPT_NAME = "acggrAria2Bridge"
# QWebChannel object name the page reaches as channel.objects.acggrAria2.
ARIA2_BRIDGE_OBJECT_NAME = "acggrAria2"
# aria2 gids are 16 lowercase hex chars. Validated on both sides: the injected
# JS rejects early (so a bad gid never costs a round trip), and the host checks
# again because the page side is not a trust boundary.
_GID_PATTERN = re.compile(r"[0-9a-f]{16}")
# Local RPC only: a hung aria2 must never freeze the calling worker forever.
BRIDGE_RPC_TIMEOUT_S = 10.0

# Tagging surface: the page may only name a plain file name inside the host's
# own download dir, and may only set these keys. Everything else is dropped on
# the host side, so a page cannot make the host write arbitrary metadata fields
# (or reach a path it was never given).
#
# Two vocabularies, one bound. The station's detail payload says ``name`` /
# ``artists`` / ``track_number`` / ``lyric`` + ``tlyric``; the tagger normalises
# to ``title`` / ``artist`` / ``track`` / ``lyrics``. The bridge accepts both
# spellings and passes them through untouched — the normalisation stays in one
# place (the tagger), so the two sides cannot drift into disagreeing about what
# a key means. What matters here is that the set is closed: anything not listed
# is dropped before it ever reaches the tagger.
_TAG_META_KEYS = (
    "title",
    "artist",
    "album",
    "date",
    "track",
    "lyrics",
    "name",
    "artists",
    "artist_string",
    "year",
    "track_number",
    "trackNumber",
    "lyric",
    "tlyric",
    "pic_url",
)
# Extensions the host is willing to tag. FLAC goes through VORBIS_COMMENT +
# PICTURE, mp3 through ID3v2.4; anything else is refused rather than guessed at.
_TAG_SUFFIXES = (".flac", ".mp3")
# Same bound as the station's own ``fetch_cover_bytes``.
_TAG_MAX_COVER_BYTES = 8 * 1024 * 1024
# A cover below this size is an error page or a stub, not artwork.
_TAG_MIN_COVER_BYTES = 64
# Where the host is willing to fetch cover art from. ``pic_url`` is page input,
# so this is an allow-list, not a filter: the station's CDN hosts and nothing
# else. Same suffixes as ``muspalace/download/proxy.py``.
_TAG_COVER_HOSTS = ("music.126.net", "music.163.com")
_TAG_COVER_TIMEOUT_S = 8.0
# A whitelisted host that 302s somewhere else must be re-checked per hop; more
# than this many hops is treated as hostile rather than chased.
_TAG_MAX_COVER_REDIRECTS = 3
_TAG_COVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
    "Accept-Encoding": "identity",
}


def _tag_cover_host_allowed(host: str) -> bool:
    normalized = (host or "").strip().lower().rstrip(".")
    if not normalized:
        return False
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}") for suffix in _TAG_COVER_HOSTS
    )


def _tag_cover_host_is_public(host: str) -> bool:
    """True only when every resolved address is public.

    A whitelisted name can still resolve into the LAN (rebinding, hostile
    resolver, a hijacked ``hosts`` file), so one private/loopback/link-local
    answer vetoes the fetch. Resolution failure vetoes too — fail closed.
    """
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


def fetch_tag_cover(url: str) -> tuple[bytes | None, str]:
    """Fetch cover art for a page-supplied ``pic_url``; ``(None, "")`` on refusal.

    The host fetches (the page's public origin is CORS-refused by the CDN).
    SSRF hardening: host allow-list, public-address check, redirects
    re-checked per hop, size/content-type bounded. A cover failure is never
    a tagging failure.
    """
    from urllib.parse import urlsplit

    cleaned = (url or "").strip()
    if not cleaned:
        return None, ""
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return None, ""
    if parts.scheme not in {"http", "https"}:
        return None, ""
    host = parts.hostname or ""
    if not _tag_cover_host_allowed(host) or not _tag_cover_host_is_public(host):
        return None, ""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a declared dependency
        return None, ""
    current = cleaned
    for _hop in range(_TAG_MAX_COVER_REDIRECTS + 1):
        try:
            response = httpx.get(
                current,
                headers=_TAG_COVER_HEADERS,
                timeout=_TAG_COVER_TIMEOUT_S,
                follow_redirects=False,
            )
        except Exception:  # noqa: BLE001 - network noise must not fail tagging
            return None, ""
        if not response.is_redirect:
            break
        location = response.headers.get("location", "")
        try:
            nxt = urlsplit(str(httpx.URL(current).join(location)))
        except Exception:  # noqa: BLE001 - malformed Location header
            return None, ""
        if nxt.scheme not in {"http", "https"}:
            return None, ""
        next_host = nxt.hostname or ""
        if not _tag_cover_host_allowed(next_host) or not _tag_cover_host_is_public(next_host):
            return None, ""
        current = str(httpx.URL(current).join(location))
    else:
        return None, ""
    if response.status_code != 200:
        return None, ""
    data = response.content or b""
    if not (_TAG_MIN_COVER_BYTES <= len(data) <= _TAG_MAX_COVER_BYTES):
        return None, ""
    mime = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if not mime.startswith("image/"):
        return None, ""
    return data, mime


def _tag_result_ok(result: dict) -> bool:
    """Did the injected tagger report success?

    Two shapes exist (app-side ``{"success": ...}`` and the bridge's
    ``{"ok": ...}``); normalise here instead of making one side adopt the
    other's key. Absent both keys the tagger ran without complaint, which is
    the only case that may be read as success.
    """
    if "ok" in result:
        return bool(result["ok"])
    if "success" in result:
        return bool(result["success"])
    return True


def _tag_result_error(result: dict) -> dict:
    """Failure payload for the page, from either tagger shape."""
    error = result.get("error")
    if isinstance(error, dict) and error:
        return error
    code = result.get("code")
    message = result.get("message")
    if code is None and message is None:
        return {"code": -1, "message": "tag_failed"}
    return {"code": -1 if code is None else code, "message": "" if message is None else str(message)}

PAGE_SCROLLBAR_SCRIPT_NAME = "acggrPageScrollbar"
ARIANG_THEME_SCRIPT_NAME = "acggrAriaNgTheme"
ARIANG_LANGUAGE_SCRIPT_NAME = "acggrAriaNgLanguage"
OPEN_DIR_SCRIPT_NAME = "acggrAriaNgOpenDir"
#: Merges acggr's own ``download_history`` rows into AriaNg's stopped list.
#: Installed *only* on the local AriaNg page: it hooks ``aria2TaskService`` and
#: knows AriaNg's own task shape, so a station SPA (μsPalace) must never get it.
ARIANG_HISTORY_SCRIPT_NAME = "acggrAriaNgHistory"
#: File name of the cached AriaNg release asset. Deliberately duplicated from
#: ``app.GUI.web.ARIANG_HTML_NAME``: this module is ``ui`` and may not import
#: ``app`` (``harness/docs/import-layout.md`` §2), and the constant is the page's own
#: file name, not app state.
ARIANG_PAGE_FILE_NAME = "AriaNg.html"

#: acggr language code -> AriaNg's own language key. AriaNg keys its catalogues
#: by ``ariaNgLanguages`` (``en``, ``zh_Hans``, ...) and resolves ``zh_CN`` only
#: through its ``aliases`` list, so the mapping is explicit here instead of
#: relying on that fallback.
#:
#: The key set mirrors ``res.lang_data.SUPPORTED_LANGUAGES``: the caller always
#: passes an already-resolved code (``_current_ui_language``), so a language that
#: is not supported never reaches here -- it is routed to ``en_US`` upstream, and
#: its AriaNg page follows to ``en``. Adding a language to that tuple means
#: adding its AriaNg key here; an unmapped code passes through unchanged, which
#: is what AriaNg does for a browser language it does not ship.
ARIANG_LANGUAGE_KEYS = {
    "en_US": "en",
    "zh_CN": "zh_Hans",
}


def ariang_language_key(lang: str) -> str:
    """AriaNg catalogue key for an acggr language code."""
    code = str(lang or "").strip()
    return ARIANG_LANGUAGE_KEYS.get(code, code)

_QWEBCHANNEL_JS_CACHE: str | None = None


def _qwebchannel_js() -> str:
    """qwebchannel.js from the Qt resource bundle, inlined into the bridge script.

    Inlined rather than installed as a second DocumentCreation script so the
    two cannot run out of order, and so the page never needs to load it.
    """
    global _QWEBCHANNEL_JS_CACHE
    if _QWEBCHANNEL_JS_CACHE is None:
        from PySide6.QtCore import QFile, QIODevice

        # Importing the module is what registers the :/qtwebchannel resource;
        # without it QFile reports the file missing.
        from PySide6 import QtWebChannel  # noqa: F401

        qfile = QFile(":/qtwebchannel/qwebchannel.js")
        if not qfile.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError("Qt resource :/qtwebchannel/qwebchannel.js is missing")
        _QWEBCHANNEL_JS_CACHE = bytes(qfile.readAll().data()).decode("utf-8")
        qfile.close()
    return _QWEBCHANNEL_JS_CACHE


_TEMPLATE_CACHE: dict[str, str] = {}


def _read_js_template(name: str) -> str:
    """Sibling ``js/`` template, cached for the process lifetime.

    The templates hold ``__ACGG_*__`` placeholders; each public source
    builder substitutes its own values, so return values stay byte-identical
    to the old inlined strings.
    """
    template = _TEMPLATE_CACHE.get(name)
    if template is None:
        template = (p.Path(__file__).with_name("js").joinpath(name)).read_text(encoding="utf-8")
        _TEMPLATE_CACHE[name] = template
    return template


def build_aria2_bridge_source(
    *, port: int, ready: bool, download_dir: str, host_owns_dir: bool = False,
) -> str:
    """JS source of the host-side aria2 bridge, injected at DocumentCreation.

    Exposes on ``window``: ``__acggrAria2`` with ``rpc(method, params)``,
    ``ready``, ``whenReady()``, ``dir``, ``addUri(uris, options)``,
    ``openItemDir(gid)``, ``tagFile(filename, meta, dir)``, plus
    ``__acggrHistory.get(limit?)`` over acggr's ``download_history``.

    Transport is QWebChannel, not ``fetch`` (CSP-refused page fetches).
    With ``host_owns_dir`` the host's ``dir`` is authoritative — every
    ``addUri`` (even a direct ``rpc``) has its dir replaced by it.
    ``addUri`` defaults ``continue='false'`` so a name conflict renames
    (never overwrites or silently no-ops); resume is unaffected.
    Errors reject with an ``Error`` carrying ``.code``/``.message``
    (transport failures use ``code = -1``); ``tagFile`` answers on
    ``tagReady``. History is independent of engine readiness — only a
    missing channel disables it.
    """
    template = _read_js_template("aria2_bridge.js")
    return (
        template
        .replace("__ACGG_QWEBCHANNEL__", _qwebchannel_js())
        .replace("__ACGG_PORT__", str(int(port)))
        .replace("__ACGG_READY__", "true" if ready else "false")
        .replace("__ACGG_DIR__", json.dumps(str(download_dir or "")))
        .replace("__ACGG_HOST_OWNS_DIR__", "true" if host_owns_dir else "false")
        .replace("__ACGG_OBJECT_NAME__", ARIA2_BRIDGE_OBJECT_NAME)
    )


class Aria2Bridge(QObject):
    """Host side of the bridge: one per page, owns the RPC secret.

    ``submit`` is fire-and-forget so a slow aria2 never blocks the GUI
    thread; answers return as ``responseReady(request_id, json)``.
    """

    responseReady = Signal(str, str)
    # Separate signal per payload shape so none can be confused with an RPC
    # answer.
    openDirReady = Signal(str, str)
    # A validated folder from a worker thread; the open runs on the GUI thread
    # because ShellExecuteW needs a COM-initialized thread.
    openFolderRequested = Signal(str, str)
    tagReady = Signal(str, str)
    # ``{"ok": true, "rows": [...]}`` rather than an aria2 ``result``.
    historyReady = Signal(str, str)

    def __init__(self, *, port: int = 0, secret: str = "", parent=None):
        super().__init__(parent)
        self._port = int(port or 0)
        self._secret = str(secret or "")
        self._download_dir = ""
        self._tagger = None
        self._pool = None
        self._closed = False
        # Queued on purpose: the emitting side is a pool worker thread.
        self.openFolderRequested.connect(
            self._open_folder_on_gui, Qt.ConnectionType.QueuedConnection
        )

    def configure(self, *, port: int, secret: str, download_dir: str = "") -> None:
        self._port = int(port or 0)
        self._secret = str(secret or "")
        if download_dir:
            self._download_dir = str(download_dir)

    def set_tagger(self, tagger) -> None:
        """Install the host-side tagger: ``tagger(path, meta, cover, mime) -> dict``.

        Injected rather than imported (this module is ``ui`` and may not
        import ``app``). Until installed, ``tagFile`` answers
        ``tagging_unavailable``.
        """
        self._tagger = tagger

    @property
    def usable(self) -> bool:
        return bool(self._port and self._secret)

    def _executor(self):
        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="acggr-aria2-bridge")
        return self._pool

    def _call(self, method: str, params: list) -> dict:
        import urllib.error
        import urllib.request

        call = {
            "jsonrpc": "2.0",
            "id": "acggr-bridge",
            "method": method,
            "params": [f"token:{self._secret}", *params],
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/jsonrpc",
            data=json.dumps(call).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=BRIDGE_RPC_TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # aria2 answers JSON-RPC errors with HTTP 400 and a JSON body that
            # carries the real aria2 code (e.g. 1 = unknown method, 3 = not
            # found). Flattening it to a transport error would hide every
            # actionable aria2 failure from the page.
            raw = exc.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                raise RuntimeError(f"aria2_http_{exc.code}: {raw[:200]}") from exc
            if isinstance(body, dict) and body.get("error"):
                return body
            raise RuntimeError(f"aria2_http_{exc.code}: {raw[:200]}") from exc

    def _emit_result(self, request_id: str, payload: dict) -> None:
        if self._closed:
            return
        self.responseReady.emit(request_id, json.dumps(payload))

    def _run(self, request_id: str, method: str, params: list) -> None:
        try:
            body = self._call(method, params)
        except Exception as exc:  # transport/timeout: code -1, never a silent drop
            self._emit_result(request_id, {"ok": False, "error": {"code": -1, "message": f"{type(exc).__name__}: {exc}"}})
            return
        if body.get("error"):
            self._emit_result(request_id, {"ok": False, "error": body["error"]})
            return
        self._emit_result(request_id, {"ok": True, "result": body.get("result")})

    @Slot(str, str, str)
    def submit(self, request_id: str, method: str, params_json: str) -> None:
        if not self.usable:
            self._emit_result(request_id, {"ok": False, "error": {"code": -1, "message": "aria2_unavailable"}})
            return
        try:
            params = json.loads(params_json or "[]")
        except ValueError:
            self._emit_result(request_id, {"ok": False, "error": {"code": -1, "message": "bad_params_json"}})
            return
        if not isinstance(params, list):
            self._emit_result(request_id, {"ok": False, "error": {"code": -1, "message": "bad_params_shape"}})
            return
        try:
            self._executor().submit(self._run, request_id, str(method), params)
        except RuntimeError:
            self._emit_result(request_id, {"ok": False, "error": {"code": -1, "message": "bridge_shutdown"}})

    def shutdown(self) -> None:
        self._closed = True
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    # --- open the folder holding one task's payload -------------------------

    @Slot(str, str)
    def openItemDir(self, gid: str, request_id: str) -> None:
        """Open the folder of one task's payload; the page may only name a gid.

        Separate slot (not another ``submit`` method): the page's RPC surface
        is unchanged, and this is the only path by which a page can make the
        host touch the filesystem. The path comes from the host's own
        ``aria2.tellStatus`` (or ``download_history.dir`` for a history row's
        derived gid); no path string is ever accepted from the page.
        """
        if not self.usable:
            self._log_open_dir_issue(f"bridge not usable at click time (port={self._port}) gid={gid}")
            self._emit_open_dir_result(request_id, {"ok": False, "error": {"code": -1, "message": "aria2_unavailable"}})
            return
        key = str(gid or "").strip()
        if not _GID_PATTERN.fullmatch(key):
            self._log_open_dir_issue(f"bad gid rejected: {gid!r}")
            self._emit_open_dir_result(request_id, {"ok": False, "error": {"code": -1, "message": "acggr_bad_gid"}})
            return
        try:
            self._executor().submit(self._run_open_dir, request_id, key)
        except RuntimeError:
            self._log_open_dir_issue(f"bridge shutdown mid-request gid={key}")
            self._emit_open_dir_result(request_id, {"ok": False, "error": {"code": -1, "message": "bridge_shutdown"}})

    @staticmethod
    def _log_open_dir_issue(message: str) -> None:
        from loguru import logger

        logger.warning(f"[Aria2Bridge] open_dir: {message}")

    def _emit_open_dir_result(self, request_id: str, payload: dict) -> None:
        if self._closed:
            return
        self.openDirReady.emit(request_id, json.dumps(payload))

    def _run_open_dir(self, request_id: str, gid: str) -> None:
        """Worker thread: tellStatus -> files[0].path -> dirname -> hand to GUI thread.

        The worker only resolves and validates the folder; the open itself runs
        on the GUI thread (``openFolderRequested``). A history row's derived
        gid does not exist in aria2, so both failure paths fall back to the
        history dir before reporting the aria2 error.
        """
        import os

        from loguru import logger

        try:
            body = self._call("aria2.tellStatus", [gid, ["files", "status", "dir"]])
        except Exception as exc:
            logger.error(f"[Aria2Bridge] open_dir tellStatus failed gid={gid}: {type(exc).__name__}: {exc}")
            if self._open_history_dir(request_id, gid):
                return
            self._emit_open_dir_result(request_id, {"ok": False, "error": {"code": -1, "message": f"{type(exc).__name__}: {exc}"}})
            return
        if body.get("error"):
            logger.error(f"[Aria2Bridge] open_dir aria2 error gid={gid}: {body['error']}")
            if self._open_history_dir(request_id, gid):
                return
            self._emit_open_dir_result(request_id, {"ok": False, "error": body["error"]})
            return
        result = body.get("result") or {}
        files = result.get("files") or []
        path = ""
        if files and isinstance(files[0], dict):
            path = str(files[0].get("path") or "").strip()
        folder = os.path.dirname(path) if path else ""
        if not folder:
            # No payload path yet (metadata-only / not started): fall back to the
            # task's own dir, which aria2 reported for this very gid.
            folder = str(result.get("dir") or "").strip()
        if not folder:
            logger.warning(f"[Aria2Bridge] open_dir no payload path gid={gid}")
            self._emit_open_dir_result(request_id, {"ok": False, "error": {"code": -1, "message": "no_payload_path"}})
            return
        if not os.path.isdir(folder):
            logger.warning(f"[Aria2Bridge] open_dir not a directory gid={gid}: {folder}")
            self._emit_open_dir_result(
                request_id,
                {"ok": False, "error": {"code": -1, "message": f"not_a_directory: {folder}"}},
            )
            return
        logger.debug(f"[Aria2Bridge] open_dir handing folder to GUI thread gid={gid} folder={folder}")
        self.openFolderRequested.emit(request_id, folder)

    def _open_history_dir(self, request_id: str, gid: str) -> bool:
        """Fallback for a history row's derived gid: resolve its ``dir`` from record.db.

        Only an existing directory is opened, and the open still hops to the
        GUI thread (never ``startfile`` on the worker thread). Returns
        ``True`` only when the folder was handed over.
        """
        import os

        from loguru import logger

        try:
            from core.sql.download_history import find_dir_by_gid_prefix

            folder = find_dir_by_gid_prefix(gid) or ""
        except Exception as exc:  # noqa: BLE001 - a DB hiccup must not kill the request
            logger.warning(f"[Aria2Bridge] open_dir history lookup failed gid={gid}: {type(exc).__name__}: {exc}")
            return False
        if not folder:
            logger.debug(f"[Aria2Bridge] open_dir no history row for gid={gid}")
            return False
        if not os.path.isdir(folder):
            logger.warning(f"[Aria2Bridge] open_dir history dir missing gid={gid}: {folder}")
            return False
        logger.debug(f"[Aria2Bridge] open_dir history row resolved gid={gid} folder={folder}")
        self.openFolderRequested.emit(request_id, folder)
        return True

    def _open_folder_on_gui(self, request_id: str, folder: str) -> None:
        """GUI thread: open a folder the worker already validated."""
        from loguru import logger

        if self._closed:
            return
        try:
            # ``core.config`` already imports ``deploy.curr_os``; going through it
            # keeps ui -> core -> (deploy) instead of adding ui -> deploy.
            from core.config import curr_os
        except ImportError:  # pragma: no cover - config always provides it
            curr_os = None
        try:
            if curr_os is not None:
                curr_os.open_folder(folder)
            else:
                import os

                os.startfile(folder)  # noqa: S606 - Windows-only host action, same as deploy
        except Exception as exc:
            logger.error(f"[Aria2Bridge] open_dir failed: {type(exc).__name__}: {exc} folder={folder}")
            self._emit_open_dir_result(request_id, {"ok": False, "error": {"code": -1, "message": f"{type(exc).__name__}: {exc}"}})
            return
        logger.debug(f"[Aria2Bridge] open_dir opened folder={folder}")
        self._emit_open_dir_result(request_id, {"ok": True, "result": {"dir": folder}})

    # --- write tags into one downloaded file --------------------------------

    @Slot(str, str, str, str)
    def tagFile(self, filename: str, meta_json: str, dir_hint: str, request_id: str) -> None:
        """Write tags into one file the host already downloaded.

        The page names a *file name*, not a path: the host resolves it
        against its own download dir, refuses non-bare names, refuses
        ``..`` and a symlink target outside the dir. ``meta_json`` is a
        flat object; only ``_TAG_META_KEYS`` survive.
        """
        payload = self._prepare_tag_request(filename, meta_json, dir_hint)
        if isinstance(payload, dict):
            self._emit_tag_result(request_id, {"ok": False, "error": payload})
            return
        try:
            self._executor().submit(self._run_tag, request_id, *payload)
        except RuntimeError:
            self._emit_tag_result(request_id, {"ok": False, "error": {"code": -1, "message": "bridge_shutdown"}})

    def _prepare_tag_request(self, filename: str, meta_json: str, dir_hint: str):
        """Validate everything up front (GUI thread); returns (path, meta) or an error dict."""
        if self._tagger is None:
            return {"code": -1, "message": "tagging_unavailable"}
        name = str(filename or "").strip()
        if not name:
            return {"code": -1, "message": "acggr_bad_filename"}
        # A relative name inside the download root: no drive letter, no leading
        # separator, no ``..`` segment. Sub-directories are allowed because the
        # page names the file relative to the dir aria2 reported landing in,
        # which can sit below the host root. The resolved path is checked
        # against the root below, so this stays a name, not a path grant.
        if name.startswith(("/", "\\")) or p.PurePath(name).drive:
            return {"code": -1, "message": "acggr_bad_filename"}
        parts = [seg for seg in name.replace("\\", "/").split("/") if seg not in ("", ".")]
        if not parts or any(seg == ".." for seg in parts):
            return {"code": -1, "message": "acggr_bad_filename"}
        if p.PurePath(parts[-1]).suffix.lower() not in _TAG_SUFFIXES:
            return {"code": -1, "message": f"acggr_unsupported_type: {p.PurePath(parts[-1]).suffix}"}
        base = self._download_dir
        if not base:
            return {"code": -1, "message": "acggr_no_download_dir"}
        # ``dir_hint`` is where the page says aria2 landed the file. It has to be
        # the host root or a directory inside it — the page reads it from
        # ``tellStatus``'s own ``files[0].path``, so a nested dir is normal, but
        # a hint pointing outside the root is not a place this host will write.
        hint = str(dir_hint or "").strip()
        if hint:
            try:
                root_path = p.Path(base).resolve(strict=False)
                hint_path = p.Path(hint).resolve(strict=False)
                hint_path.relative_to(root_path)
            except (ValueError, OSError):
                return {"code": -1, "message": "acggr_dir_mismatch"}
            base = str(hint_path)
        try:
            meta_raw = json.loads(meta_json or "{}")
        except ValueError:
            return {"code": -1, "message": "acggr_bad_meta_json"}
        if not isinstance(meta_raw, dict):
            return {"code": -1, "message": "acggr_bad_meta_shape"}
        # Scalars are normalised to text; containers are passed through as JSON.
        # ``artists`` arrives as ``[{"name": ...}, ...]`` from the station's
        # detail payload, and ``str()`` on that would hand the tagger
        # ``"[{'name': 'A'}]"`` — a list-shaped value it cannot read.
        meta = {
            key: (meta_raw[key] if isinstance(meta_raw[key], (list, dict)) else str(meta_raw[key]))
            for key in _TAG_META_KEYS
            if meta_raw.get(key) is not None
        }
        cover = meta_raw.get("cover")
        cover_mime = str(meta_raw.get("cover_mime") or "image/jpeg")
        cover_bytes = None
        if cover:
            try:
                cover_bytes = bytes.fromhex(str(cover))
            except ValueError:
                return {"code": -1, "message": "acggr_bad_cover_hex"}
            if len(cover_bytes) > _TAG_MAX_COVER_BYTES:
                return {"code": -1, "message": "acggr_cover_too_large"}
        else:
            # No inline bytes: a mime without bytes would only mislead the
            # tagger into thinking it has artwork.
            cover_mime = ""
        # No inline bytes but a ``pic_url``: the host fetches the artwork itself.
        # The page cannot (CORS on the public origin), so this is the only path
        # that produces a cover. It runs on the worker thread with the tagger,
        # never here, because it is a network call.
        pic_url = ""
        if cover_bytes is None:
            pic_url = str(meta.get("pic_url") or "").strip()
        target = p.Path(base).joinpath(name)
        try:
            resolved = target.resolve(strict=False)
            root = p.Path(base).resolve(strict=False)
            resolved.relative_to(root)
        except (ValueError, OSError):
            return {"code": -1, "message": "acggr_path_escapes_dir"}
        if not target.is_file():
            return {"code": -1, "message": f"acggr_file_not_found: {name}"}
        return (target, meta, cover_bytes, cover_mime, pic_url)

    def _emit_tag_result(self, request_id: str, payload: dict) -> None:
        if self._closed:
            return
        self.tagReady.emit(request_id, json.dumps(payload))

    def _run_tag(self, request_id: str, target, meta: dict, cover, cover_mime: str, pic_url: str = "") -> None:
        """Worker thread: fetch the cover if needed, then hand the file to the tagger."""
        if cover is None and pic_url:
            # A cover failure is a warning, never a tagging failure: the text
            # tags still land, which is what the station's own endpoint does.
            cover, cover_mime = fetch_tag_cover(pic_url)
            if cover is None:
                cover_mime = ""
        try:
            result = self._tagger(target, meta, cover, cover_mime)
        except Exception as exc:
            self._emit_tag_result(request_id, {"ok": False, "error": {"code": -1, "message": f"{type(exc).__name__}: {exc}"}})
            return
        if not isinstance(result, dict):
            # Not a report at all: a tagger that answers something else has not
            # told us the tags landed, and "no report" must never read as
            # success — that is the same silent-bare-file outcome.
            self._emit_tag_result(
                request_id,
                {"ok": False, "error": {"code": -1, "message": f"tag_bad_result: {type(result).__name__}"}},
            )
            return
        if not _tag_result_ok(result):
            self._emit_tag_result(request_id, {"ok": False, "error": _tag_result_error(result)})
            return
        dropped = {"ok", "success", "code", "message", "error"}
        self._emit_tag_result(
            request_id,
            {"ok": True, "result": {"path": str(target), **{k: v for k, v in result.items() if k not in dropped}}},
        )

    # --- download history (acggr's own record.db) ----------------------------

    @Slot(str, str)
    def requestHistory(self, request_id: str, limit_json: str) -> None:
        """Hand the page acggr's own stopped-download history.

        Deliberately *not* gated on :attr:`usable`: the rows live in
        ``conf_dir/record.db``, not in aria2, and "engine down, history still
        there" is the whole point of the table. The read runs on worker
        threads (sqlite may block) and answers on ``historyReady``.
        """
        try:
            self._executor().submit(self._run_history, request_id, str(limit_json or ""))
        except RuntimeError:
            self._emit_history_result(
                request_id, {"ok": False, "error": {"code": -1, "message": "bridge_shutdown"}}
            )

    def _emit_history_result(self, request_id: str, payload: dict) -> None:
        if self._closed:
            return
        self.historyReady.emit(request_id, json.dumps(payload))

    @staticmethod
    def _history_row_payload(row: dict) -> dict:
        """The fields the injected AriaNg script needs, and nothing else.

        ``created_at`` stays host-side: the page builds its synthetic task from
        the finish time alone, and a closed shape means a future column cannot
        silently reach the page.
        """
        return {
            "identity_key": str(row.get("identity_key") or ""),
            "gid": str(row.get("gid") or ""),
            "uri": str(row.get("uri") or ""),
            "dir": str(row.get("dir") or ""),
            "out": str(row.get("out") or ""),
            "total_bytes": int(row.get("total_bytes") or 0),
            "status": str(row.get("status") or ""),
            "error_code": str(row.get("error_code") or ""),
            "source": str(row.get("source") or ""),
            "finished_at": float(row.get("finished_at") or 0.0),
        }

    @staticmethod
    def _history_limit(limit_json: str) -> int:
        """Configured history ceiling, optionally lowered by the page's request.

        ``get_history_limit()`` stays authoritative: ``0`` (history disabled)
        means no rows no matter what the page asks for. A page-supplied limit
        can only narrow the window, never widen it.
        """
        from core.script.aria2.settings import get_history_limit

        limit = int(get_history_limit())
        if limit <= 0:
            return 0
        raw = str(limit_json or "").strip()
        if not raw:
            return limit
        try:
            page_limit = int(json.loads(raw))
        except (TypeError, ValueError):
            try:
                page_limit = int(raw)
            except (TypeError, ValueError):
                page_limit = 0
        return min(limit, page_limit) if page_limit > 0 else limit

    def _run_history(self, request_id: str, limit_json: str) -> None:
        """Worker thread: read ``download_history`` and answer on ``historyReady``."""
        from loguru import logger

        try:
            limit = self._history_limit(limit_json)
            rows: list[dict] = []
            if limit > 0:
                from core.sql.download_history import list_history

                rows = [self._history_row_payload(row) for row in list_history(limit)]
        except Exception as exc:  # noqa: BLE001 - a page request must never raise into Qt
            logger.warning(f"[Aria2Bridge] history read failed: {type(exc).__name__}: {exc}")
            self._emit_history_result(
                request_id,
                {"ok": False, "error": {"code": -1, "message": f"{type(exc).__name__}: {exc}"}},
            )
            return
        self._emit_history_result(request_id, {"ok": True, "rows": rows})


def bridge_download_dir(profile, service_name: str = "") -> str:
    """Where the bridge tells aria2 to write.

    Precedence: jsoneri 存储目录 → ``<root>/<service_name>`` (same root as
    pawchive/danbooru ``sv_path``; the same value is reported to the server,
    so it must be the dir aria2 actually uses), then
    ``profile.downloadPath()``, then ``~/Downloads``. ``profile`` must be the
    window's own: the download controller writes through it.
    """
    if service_name:
        from core.config.qc import jsoneri_service_storage_dir

        mapped = jsoneri_service_storage_dir(service_name)
        if mapped is not None:
            return str(mapped)
    configured = ""
    try:
        configured = str(profile.downloadPath() or "").strip()
    except Exception:
        configured = ""
    return configured or str(p.Path.home().joinpath("Downloads"))


def _is_dark_theme() -> bool:
    from ui.core.theme import CustTheme, theme_mgr

    return theme_mgr.get_theme() == CustTheme.DARK


def _current_ui_language() -> str:
    """The UI language acggr is running in.

    Read from the persisted ``conf.lang`` (a ``QTranslator`` cannot be
    queried); ``""`` resolves to the OS locale, and anything the whitelist
    does not cover resolves to ``en_US``.
    """
    import res

    try:
        from core import conf

        configured = str(getattr(conf, "lang", "") or "")
    except Exception:  # noqa: BLE001 - a page language must never break a window
        configured = ""
    return res.resolve_language(configured or res.lang)


def embedded_page_bg():
    """Opaque page background matching the app window.

    ``QWebEnginePage.backgroundColor`` defaults to white, so a web view's
    first frame flashes white without this. Transparent is avoided: under
    Mica it exposes an uncontrolled layer beneath.
    """
    from PySide6.QtGui import QColor

    return QColor(53, 53, 53) if _is_dark_theme() else QColor(245, 245, 245)


def ariang_theme_source(*, dark: bool) -> str:
    """JS that puts the cached AriaNg page on the app's day/night leg.

    Hard rule: never write a partial ``AriaNg.Options``. AriaNg treats that
    key as its entire settings set and does not merge with defaults, so a
    partial object kills the injector (dead page shell). A present-but-
    partial object is removed (self-heal); an absent key is left alone so
    AriaNg writes its own complete defaults. Only this one key is ever
    touched: the same origin also holds station data that must survive.
    """
    template = _read_js_template("ariang_theme.js")
    return template.replace("__ACGG_DARK__", "true" if dark else "false")


def ariang_language_source(*, lang: str) -> str:
    """JS that puts the cached AriaNg page on acggr's current UI language.

    Same ``AriaNg.Options`` rule as :func:`ariang_theme_source`. The live
    repaint goes through ``$translate.use`` after the injector exists;
    ``setLanguage`` alone only persists and never repaints.
    """
    template = _read_js_template("ariang_language.js")
    return template.replace("__ACGG_LANG__", repr(ariang_language_key(lang)))


def page_scrollbar_source(*, dark: bool) -> str:
    """JS that gives the page a qfluent-style scrollbar (thin pill, no groove).

    Injected through a constructable stylesheet because
    ``document.adoptedStyleSheets`` is not subject to the page's
    ``style-src`` CSP (stricter sites drop an injected <style>).
    """
    rgb = "255,255,255" if dark else "0,0,0"
    css = (
        "::-webkit-scrollbar{width:12px;height:12px;background:transparent}"
        "::-webkit-scrollbar-track{background:transparent}"
        f"::-webkit-scrollbar-thumb{{background:rgba({rgb},0.35);border-radius:6px;"
        "border:3px solid transparent;background-clip:content-box}"
        f"::-webkit-scrollbar-thumb:hover{{background:rgba({rgb},0.55);border:2px solid transparent;"
        "background-clip:content-box}"
        "::-webkit-scrollbar-corner{background:transparent}"
    )
    template = _read_js_template("page_scrollbar.js")
    return template.replace("__ACGG_CSS__", json.dumps(css))


def ariang_open_dir_source() -> str:
    """JS that adds an icon-only "open payload folder" button to each AriaNg row.

    DOM injection instead of a page patch: the cached ``AriaNg.html`` asset
    must stay byte-identical. The scan is reconciling (removes stale and
    duplicate buttons), and the script is deliberately re-entrant - every
    step is idempotent.
    """
    return _read_js_template("ariang_open_dir.js")


def ariang_history_source() -> str:
    """JS that shows acggr's own download history inside AriaNg's stopped list.

    Hooks ``aria2TaskService``/``aria2SettingService`` on the local AriaNg
    page only (never a station SPA). The synthetic task gid is
    ``identity_key[:16]``; de-duplication is by gid and by dir+file name.
    Every failure only ``console.warn``s - a broken merge must never take
    AriaNg's own functionality with it.
    """
    return _read_js_template("ariang_history.js")


class Aria2PageBridge:
    """Page-level owner of one browser window's aria2 bridge.

    One window, one page, one bridge: the script lives in the page's script
    collection and QWebChannel is attached to the page, so a page swap needs a
    re-install. ``refresh`` is idempotent and re-runs it before each
    navigation. Disabled by default — the same window also loads challenge and
    preview pages that must not get an aria2 RPC surface; :meth:`attach` turns
    it on.
    """

    def __init__(self, window):
        self._window = window
        self._bridge: Aria2Bridge | None = None
        self._channel = None
        self._service_name = ""
        self._enabled = False
        self._tagger = None

    def set_tagger(self, tagger) -> None:
        """Install the app-side tagger (see ``Aria2Bridge.set_tagger``)."""
        self._tagger = tagger
        if self._bridge is not None:
            self._bridge.set_tagger(tagger)

    # --- state -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def bridge(self) -> "Aria2Bridge | None":
        return self._bridge

    @property
    def usable(self) -> bool:
        return bool(self._bridge is not None and self._bridge.usable)

    @property
    def download_dir(self) -> str:
        profile = getattr(self._window, "profile", None)
        if profile is None:
            return str(p.Path.home().joinpath("Downloads"))
        return bridge_download_dir(profile, self._service_name)

    def attach(self, *, service_name: str = "") -> None:
        """Enable the bridge for this window and (re-)inject it now.

        ``service_name`` (a μsPalace-style page) makes the host's download dir
        authoritative and routes it to ``<storage root>/<service_name>``.
        Empty for the local AriaNg monitor, which keeps choosing the dir per
        task.
        """
        self._enabled = True
        self._service_name = str(service_name or "")
        self.refresh()

    def detach(self) -> None:
        """Turn the bridge off; the next navigation carries no injection."""
        self._enabled = False
        self._service_name = ""
        page = self._page()
        if page is not None:
            self._remove_script(page, ARIA2_BRIDGE_SCRIPT_NAME)
            self._remove_script(page, OPEN_DIR_SCRIPT_NAME)
            self._remove_script(page, ARIANG_HISTORY_SCRIPT_NAME)

    def shutdown(self) -> None:
        """Drop the RPC secret and stop the worker pool (window is closing)."""
        if self._bridge is not None:
            self._bridge.shutdown()
        self._bridge = None
        self._channel = None
        self._enabled = False

    # --- page helpers ----------------------------------------------------

    def _page(self):
        view = getattr(self._window, "view", None)
        if view is None:
            return None
        try:
            return view.page()
        except RuntimeError:  # page already destroyed
            return None

    @staticmethod
    def _remove_script(page, name: str) -> None:
        scripts = page.scripts()
        for existing in scripts.toList():
            if existing.name() == name:
                scripts.remove(existing)

    @staticmethod
    def _install_script(page, name: str, source: str, *, run_now: bool) -> None:
        """Insert one DocumentCreation/MainWorld script, replacing any prior copy.

        DocumentCreation + MainWorld is what makes host-injected scripts
        bypass a page's ``script-src 'self'`` CSP.
        """
        from PySide6.QtWebEngineCore import QWebEngineScript

        Aria2PageBridge._remove_script(page, name)
        script = QWebEngineScript()
        script.setName(name)
        script.setSourceCode(source)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        page.scripts().insert(script)
        if run_now:
            # Replaces the same code in the live document (theme flips, a
            # re-attach), where DocumentCreation already ran long ago.
            page.runJavaScript(source)

    def page_is_local(self) -> bool:
        """True when the window is pointed at the local cached AriaNg page.

        Only ``file://`` (the cached ``AriaNg.html``) gets the per-row button;
        the URL scheme decides.
        """
        home_url = getattr(self._window, "home_url", None)
        return bool(home_url is not None and home_url.isLocalFile())

    def _serves_local_ariang(self) -> bool:
        """True when this window is (or is about to be) the local AriaNg page.

        A service window is never the monitor, even if ``home_url`` still
        points at a previously loaded local page, so the service name is
        checked first; otherwise the URL decides, plus the blank window before
        the first ``load_home()``.
        """
        if self._service_name:
            return False
        return self.page_is_local() or self._is_blank()

    def _is_ariang_page(self) -> bool:
        """True only when this window is (or is about to be) the local AriaNg page.

        The history merge is AriaNg-specific and must never land in a station
        SPA: no service name and a local ``file://`` URL named
        ``AriaNg.html``. Judged from ``home_url`` because ``refresh`` runs
        before the navigation.
        """
        if self._service_name:
            return False
        home_url = getattr(self._window, "home_url", None)
        if home_url is None:
            return False
        try:
            if not home_url.isLocalFile():
                return False
            return p.PurePath(home_url.toLocalFile()).name == ARIANG_PAGE_FILE_NAME
        except (ValueError, TypeError):
            return False

    def _is_blank(self) -> bool:
        """True while the window has not navigated anywhere yet."""
        home_url = getattr(self._window, "home_url", None)
        if home_url is None:
            return True
        text = home_url.toString()
        return not text or text == "about:blank"

    def host_owns_dir(self) -> bool:
        """Service pages are forced into the host dir; the AriaNg page is not."""
        return bool(self._service_name) and not self.page_is_local()

    # --- injection -------------------------------------------------------

    def refresh(self) -> None:
        """Re-derive the aria2 endpoint and re-inject for the next document.

        Called from ``BrowserWindow.load_home`` right before ``view.load``: the
        engine may have (re)started on another port, and the download dir must
        match the window profile the download controller writes through.
        """
        if not self._enabled:
            return
        page = self._page()
        if page is None:
            return

        from core.script.aria2.lock import read_instance

        try:
            record = read_instance()
        except Exception:
            record = None

        from PySide6.QtWebChannel import QWebChannel

        if self._bridge is None:
            # Parented to the window so the QObject outlives a page swap; the
            # channel is per-page and re-created only when the bridge is.
            self._bridge = Aria2Bridge(parent=self._window)
            self._channel = QWebChannel(page)
            self._channel.registerObject(ARIA2_BRIDGE_OBJECT_NAME, self._bridge)
            page.setWebChannel(self._channel)
        self._bridge.configure(
            port=record.port if record is not None else 0,
            secret=record.secret if record is not None else "",
            download_dir=self.download_dir,
        )
        if self._tagger is not None:
            self._bridge.set_tagger(self._tagger)

        source = build_aria2_bridge_source(
            port=record.port if record is not None else 0,
            ready=self._bridge.usable,
            download_dir=self.download_dir,
            host_owns_dir=self.host_owns_dir(),
        )
        self._install_script(page, ARIA2_BRIDGE_SCRIPT_NAME, source, run_now=False)

        # ``attach()`` runs while ``home_url`` is still ``about:blank``, so
        # judging by ``page_is_local()`` here would remove the script on the
        # very call that enables it. Keep it installed whenever the window is
        # destined for a local page; on a non-AriaNg document it only looks for
        # ``.task-table-body`` rows and does nothing.
        if self._serves_local_ariang():
            self._install_script(page, OPEN_DIR_SCRIPT_NAME, ariang_open_dir_source(), run_now=True)
        else:
            self._remove_script(page, OPEN_DIR_SCRIPT_NAME)

        # History merge: AriaNg only. Installed before the navigation so
        # DocumentCreation carries it; a station SPA gets it removed instead
        # (the window may be re-used for both over its life).
        if self._is_ariang_page():
            self._install_script(
                page, ARIANG_HISTORY_SCRIPT_NAME, ariang_history_source(), run_now=False,
            )
        else:
            self._remove_script(page, ARIANG_HISTORY_SCRIPT_NAME)

        # Page cosmetics belong to the page that lives in this window: installed
        # before the document exists so the very first frame is already on the
        # right theme and language.
        self.apply_page_background()
        self.apply_page_scrollbar()
        self.apply_page_ariang_theme()
        self.apply_page_ariang_language()

    def apply_page_scrollbar(self) -> None:
        """Give this window's page the qfluent-style scrollbar."""
        if not self._enabled:
            return
        page = self._page()
        if page is None:
            return
        source = page_scrollbar_source(dark=_is_dark_theme())
        self._install_script(page, PAGE_SCROLLBAR_SCRIPT_NAME, source, run_now=True)

    def apply_page_background(self) -> None:
        """Repaint the opaque page background after a theme flip."""
        page = self._page()
        if page is None:
            return
        page.setBackgroundColor(embedded_page_bg())

    def apply_page_ariang_theme(self) -> None:
        """Put the cached AriaNg page on the app's current day/night leg.

        Only the local AriaNg page: station SPAs own their palette.
        """
        if not self._enabled or not self.page_is_local():
            return
        page = self._page()
        if page is None:
            return
        source = ariang_theme_source(dark=_is_dark_theme())
        self._install_script(page, ARIANG_THEME_SCRIPT_NAME, source, run_now=True)

    def apply_page_ariang_language(self) -> None:
        """Put the cached AriaNg page on acggr's current UI language.

        Same scope as :meth:`apply_page_ariang_theme`: local AriaNg only.
        """
        if not self._enabled or not self.page_is_local():
            return
        page = self._page()
        if page is None:
            return
        source = ariang_language_source(lang=_current_ui_language())
        self._install_script(page, ARIANG_LANGUAGE_SCRIPT_NAME, source, run_now=True)

    def apply_page_theme(self) -> None:
        """Re-apply everything whose colour is baked into the page.

        Called from the window's ``qconfig.themeChanged`` hook: the page
        background, the scrollbar stylesheet and AriaNg's ``theme-dark`` class
        all carry a literal colour decision.
        """
        if not self._enabled:
            return
        self.apply_page_background()
        self.apply_page_scrollbar()
        self.apply_page_ariang_theme()

    def apply_page_language(self) -> None:
        """Re-apply everything whose wording is baked into the page.

        The language counterpart of :meth:`apply_page_theme`: one call
        re-applies every page whose text comes from acggr, and it can run
        live (AriaNg repaints from its own catalogue).
        """
        if not self._enabled:
            return
        self.apply_page_ariang_language()
