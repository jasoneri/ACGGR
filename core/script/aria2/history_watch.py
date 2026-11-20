"""Owner-side poller: mirror aria2 stopped downloads into ``download_history``.

aria2 keeps ``force-save=false`` (no .aria2 stubs, no growing session), so "what did I
download" has to be captured while the daemon is alive. The owner process runs this daemon
thread: every 5 s it reads the instance record, asks for the stopped list and inserts rows.

Rules: get_history_limit() == 0 means "do not keep history" (thread not started; a running
thread no-ops each tick rather than being stopped). Every tick re-reads the limit. Nothing here
touches Qt; sqlite writes happen on this thread. status() exposes alive / last_tick / written /
last_error so a dead poller is visible. Dedupe is the table's job (identity_key + INSERT OR
IGNORE).
"""
from __future__ import annotations

import os
import threading
import time
import typing as t

from loguru import logger

from core.script.aria2.lock import (
    DEFAULT_RPC_HOST,
    Aria2InstanceError,
    rpc_call,
    read_instance,
)
from core.script.aria2.settings import get_history_limit
from core.sql.download_history import build_identity_key, record_download

POLL_INTERVAL_S = 5.0
# tellStopped(offset, num, keys): the window is re-read every tick, dedupe handles repeats.
STOPPED_OFFSET = 0
STOPPED_NUM = 1000
STOPPED_KEYS = ["gid", "status", "dir", "files", "totalLength", "errorCode", "uris"]
# tellStopped also reports removed-by-user and failed downloads; keep all three.
STOPPED_STATUSES = frozenset({"complete", "error", "removed"})
RPC_TIMEOUT_S = 2.0
STOP_JOIN_TIMEOUT_S = 5.0

_state_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_last_tick: float = 0.0
_written: int = 0
_last_error: str = ""


def _set_last_error(message: str) -> None:
    global _last_error
    _last_error = message


def _first_uri(entry: dict) -> str:
    """First URI of the download (entry-level, then files[0]-level)."""
    uris = entry.get("uris")
    if isinstance(uris, list):
        for item in uris:
            if isinstance(item, dict) and item.get("uri"):
                return str(item["uri"])
    files = entry.get("files")
    if isinstance(files, list):
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            file_uris = file_entry.get("uris")
            if isinstance(file_uris, list):
                for item in file_uris:
                    if isinstance(item, dict) and item.get("uri"):
                        return str(item["uri"])
    return ""


def _saved_name(entry: dict, save_dir: str) -> str:
    """``out`` as aria2 sees it: files[0].path relative to the download dir."""
    files = entry.get("files")
    if not isinstance(files, list) or not files:
        return ""
    first = files[0] if isinstance(files[0], dict) else {}
    path_text = str(first.get("path") or "")
    if not path_text:
        return ""
    if save_dir:
        try:
            # aria2 stores files[].path as dir + name; keep the relative shape so
            # a moved download dir does not change the identity key.
            relative = os.path.relpath(path_text, save_dir)
        except ValueError:
            relative = os.path.basename(path_text)
        if relative and not relative.startswith(".."):
            return relative.replace("\\", "/")
    return os.path.basename(path_text)


def _record_entry(entry: dict) -> bool:
    """Write one tellStopped entry; True when a new row appeared."""
    status = str(entry.get("status") or "")
    if status not in STOPPED_STATUSES:
        return False
    gid = str(entry.get("gid") or "")
    save_dir = str(entry.get("dir") or "")
    out = _saved_name(entry, save_dir)
    uri = _first_uri(entry)
    identity_key = build_identity_key(save_dir, out, uri)
    return record_download(
        identity_key=identity_key,
        gid=gid,
        uri=uri,
        dir=save_dir,
        out=out,
        total_bytes=entry.get("totalLength"),
        status=status,
        error_code=entry.get("errorCode"),
        source="",
    )


def _tick() -> None:
    """One poll: no-op unless history is enabled, an instance answers, RPC works."""
    global _last_tick, _written
    started_at = time.time()
    try:
        limit = int(get_history_limit())
    except Exception as exc:  # noqa: BLE001 - settings converges bad *values* itself,
        # so this only fires on an unreadable/being-rewritten conf; skip the tick.
        _set_last_error(f"history_limit: {exc}")
        logger.warning(f"[AcggrAria2] history poller could not read history_limit: {exc}")
        _last_tick = started_at
        return
    if limit <= 0:
        # Disabled: stay alive and idle so a later enable needs no restart race.
        _set_last_error("")
        _last_tick = started_at
        return
    try:
        record = read_instance()
    except Aria2InstanceError as exc:
        # Unreadable/foreign record: nothing to poll this tick, not an error.
        logger.debug(f"[AcggrAria2] history poller skipped tick, unusable instance record: {exc}")
        _last_tick = started_at
        return
    if record is None:
        # No instance record: owner gone / engine not up. Silent, not an error.
        _last_tick = started_at
        return
    body = rpc_call(
        DEFAULT_RPC_HOST,
        record.port,
        record.secret,
        "aria2.tellStopped",
        params=[STOPPED_OFFSET, STOPPED_NUM, STOPPED_KEYS],
        timeout_s=RPC_TIMEOUT_S,
    )
    if body is None:
        # Endpoint silent or rejected us: same as "not ours right now".
        _last_tick = started_at
        return
    entries = body.get("result")
    if not isinstance(entries, list):
        _set_last_error("tellStopped returned no list")
        _last_tick = started_at
        return
    written_this_tick = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if _record_entry(entry):
            written_this_tick += 1
    _written += written_this_tick
    _set_last_error("")
    _last_tick = started_at
    if written_this_tick:
        logger.debug(
            f"[AcggrAria2] history poller wrote {written_this_tick} rows "
            f"(limit={limit}, total={_written})"
        )


def _run(stop_event: threading.Event) -> None:
    logger.info(f"[AcggrAria2] history poller started interval={POLL_INTERVAL_S}s")
    while not stop_event.is_set():
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001 - the thread must never die
            _set_last_error(f"{type(exc).__name__}: {exc}")
            logger.warning(f"[AcggrAria2] history poller tick failed: {exc}")
        # Wait() instead of sleep() so stop() returns immediately.
        stop_event.wait(POLL_INTERVAL_S)
    logger.info("[AcggrAria2] history poller stopped")


def start() -> None:
    """Start the daemon poller; no-op when history is disabled or already running."""
    global _thread, _stop_event, _last_tick, _written, _last_error
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            return
        try:
            limit = int(get_history_limit())
        except Exception as exc:  # noqa: BLE001 - see _tick: conf read, not a value problem
            logger.warning(f"[AcggrAria2] history poller not started, history_limit unreadable: {exc}")
            return
        if limit <= 0:
            logger.info("[AcggrAria2] history poller not started: cgs_aria2.history_limit=0")
            return
        _last_tick = 0.0
        _written = 0
        _last_error = ""
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_run,
            args=(stop_event,),
            name="acggr-aria2-history",
            daemon=True,
        )
        _stop_event = stop_event
        _thread = thread
        thread.start()


def stop(*, timeout_s: float = STOP_JOIN_TIMEOUT_S) -> None:
    """Signal the poller and join it (bounded). Safe to call when never started."""
    global _thread, _stop_event
    with _state_lock:
        thread = _thread
        stop_event = _stop_event
        _thread = None
        _stop_event = None
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout_s)


def status() -> dict[str, t.Any]:
    """Liveness probe so a silently dead poller is discoverable.

    - ``alive``: poller thread object exists and is running.
    - ``last_tick``: wall-clock time of the last tick attempt (0.0 = never).
    - ``written``: cumulative rows actually inserted since this poller started.
    - ``last_error``: last non-fatal failure (empty when healthy).
    """
    with _state_lock:
        thread = _thread
        return {
            "alive": bool(thread is not None and thread.is_alive()),
            "last_tick": float(_last_tick),
            "written": int(_written),
            "last_error": str(_last_error),
        }


__all__ = ["POLL_INTERVAL_S", "start", "status", "stop"]
