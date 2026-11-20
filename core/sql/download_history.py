"""aria2 download history in conf_dir/record.db.

aria2 can only keep completed downloads across restarts via --force-save, which also leaves a
``<file>.aria2`` stub in the save directory; acggr runs with force-save=false and mirrors the
stopped results here instead.

Write point: core/script/aria2/history_watch.py (owner process, 5 s tellStopped poll, INSERT OR
IGNORE). Read path is a plain SELECT ... ORDER BY finished_at DESC LIMIT n; this round has no
DELETE / pruning / row cap on purpose — do not add a prune path without a decision.
identity_key = md5(dir \0 out \0 md5(uri)) (gid is not unique across session replay).
find_dir_by_gid_prefix answers one row by identity_key[:16] (the gid AriaNg shows).
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import typing as t

from core import conf_dir

_TABLE = "download_history"

_table_ready_path: str | None = None
_table_ready_lock = threading.Lock()


def _record_db_path():
    return conf_dir.joinpath("record.db")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_record_db_path(), timeout=5.0)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _migrate_table(connection: sqlite3.Connection) -> None:
    """CREATE IF NOT EXISTS + the two lookup indexes (no column history yet)."""
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{_TABLE}` (
            identity_key TEXT PRIMARY KEY,
            gid TEXT,
            uri TEXT,
            dir TEXT,
            out TEXT,
            total_bytes INTEGER,
            status TEXT,
            error_code TEXT,
            source TEXT,
            finished_at REAL NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    connection.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_download_history_finished
        ON `{_TABLE}`(finished_at DESC);
        """
    )
    connection.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_download_history_gid
        ON `{_TABLE}`(gid);
        """
    )
    connection.commit()


def ensure_table(connection: sqlite3.Connection) -> None:
    """Create the history table on *any* connection (tests / redirected conf_dir)."""
    _migrate_table(connection)


def _ensure_table(connection: sqlite3.Connection) -> None:
    """CREATE IF NOT EXISTS once per DB path."""
    global _table_ready_path
    path_key = str(_record_db_path().resolve())
    if _table_ready_path == path_key:
        return
    with _table_ready_lock:
        if _table_ready_path == path_key:
            return
        _migrate_table(connection)
        _table_ready_path = path_key


def build_identity_key(dir: object, out: object, uri: object) -> str:
    """Stable dedupe key: md5(dir \\0 out \\0 md5(uri)).

    The URI is digested first so a data:/signed CDN URL never makes the primary
    key row pathologically long.
    """
    dir_text = str(dir or "")
    out_text = str(out or "")
    uri_text = str(uri or "")
    uri_digest = hashlib.md5(uri_text.encode("utf-8", errors="replace")).hexdigest()
    material = f"{dir_text}\x00{out_text}\x00{uri_digest}"
    return hashlib.md5(material.encode("utf-8", errors="replace")).hexdigest()


def record_download(
    *,
    identity_key: str,
    gid: object,
    uri: object,
    dir: object,
    out: object,
    total_bytes: object,
    status: object,
    error_code: object = "",
    source: object = "",
    finished_at: float | None = None,
) -> bool:
    """Insert one stopped-download row; False when the identity_key already existed.

    ``INSERT OR IGNORE`` by design: the poller re-reads the same tellStopped
    window every tick, so a repeat must be a no-op with no status machine and no
    bookkeeping. Safe to call from a worker thread (never from Qt-owning code).
    """
    key = str(identity_key or "").strip()
    if not key:
        raise ValueError("download history row requires non-empty identity_key")
    try:
        total = int(total_bytes or 0)
    except (TypeError, ValueError):
        total = 0
    stopped_at = time.time() if finished_at is None else float(finished_at)
    connection = _connect()
    try:
        _ensure_table(connection)
        cursor = connection.execute(
            f"""
            INSERT OR IGNORE INTO `{_TABLE}`
                (identity_key, gid, uri, dir, out, total_bytes, status, error_code,
                 source, finished_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                key,
                str(gid or ""),
                str(uri or ""),
                str(dir or ""),
                str(out or ""),
                total,
                str(status or ""),
                str(error_code or ""),
                str(source or ""),
                stopped_at,
                time.time(),
            ),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def _row_to_history(row: tuple) -> dict[str, t.Any]:
    return {
        "identity_key": str(row[0]),
        "gid": str(row[1] or ""),
        "uri": str(row[2] or ""),
        "dir": str(row[3] or ""),
        "out": str(row[4] or ""),
        "total_bytes": int(row[5] or 0),
        "status": str(row[6] or ""),
        "error_code": str(row[7] or ""),
        "source": str(row[8] or ""),
        "finished_at": float(row[9] or 0.0),
        "created_at": float(row[10] or 0.0),
    }


def list_history(limit: int) -> list[dict[str, t.Any]]:
    """Newest-first stopped downloads, capped at *limit* rows.

    ``limit <= 0`` returns ``[]`` without touching the DB (history disabled).
    Rows beyond *limit* are intentionally NOT deleted (see module docstring).
    """
    try:
        row_limit = int(limit)
    except (TypeError, ValueError):
        return []
    if row_limit <= 0:
        return []
    connection = _connect()
    try:
        _ensure_table(connection)
        cursor = connection.execute(
            f"""
            SELECT identity_key, gid, uri, dir, out, total_bytes, status, error_code,
                   source, finished_at, created_at
            FROM `{_TABLE}`
            ORDER BY finished_at DESC
            LIMIT ?;
            """,
            (row_limit,),
        )
        return [_row_to_history(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def find_dir_by_gid_prefix(gid_prefix: object) -> str | None:
    """The recorded ``dir`` whose ``identity_key`` starts with *gid_prefix*.

    Read-only, and deliberately separate from :func:`list_history`: this answers
    one *derived* gid, which is how the injected AriaNg page names a history
    row (``identity_key[:16]``, see
    ``ui/core/browser/aria2_bridge.ariang_history_source``). A page-supplied
    16-hex gid that aria2 does not know has no other way back to a path.

    ``None`` for a non-16-hex input, for an unknown prefix, and for a row with
    an empty ``dir``. Newest row wins should one prefix ever collide. The DB is
    opened per call (same style as every other function here) because callers
    run on worker threads.
    """
    prefix = str(gid_prefix or "").strip().lower()
    if len(prefix) != 16 or any(ch not in "0123456789abcdef" for ch in prefix):
        return None
    connection = _connect()
    try:
        _ensure_table(connection)
        cursor = connection.execute(
            f"""
            SELECT dir FROM `{_TABLE}`
            WHERE substr(identity_key, 1, 16) = ?
            ORDER BY finished_at DESC
            LIMIT 1;
            """,
            (prefix,),
        )
        row = cursor.fetchone()
    finally:
        connection.close()
    if not row:
        return None
    return str(row[0] or "").strip() or None


__all__ = [
    "build_identity_key",
    "ensure_table",
    "find_dir_by_gid_prefix",
    "list_history",
    "record_download",
]
