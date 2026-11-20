from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path


from core.website.pawchive import *


_SORT_COLUMNS = frozenset({"name", "service", "updated", "favorited"})
_FTS_SPECIAL_RE = re.compile(r'["\'\*\^\~\(\)\:\{\}\[\]\\]')


@dataclass(frozen=True, slots=True)
class PawchiveAuthor:
    id: str
    name: str
    service: str
    updated: int
    favorited: int

    @property
    def avatar(self) -> str:
        return f"https://{domain}/icons/{self.service}/{self.id}"

    def to_payload(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AuthorQuery:
    text: str = ""
    favorite_ids: tuple[str, ...] | None = None
    sort_column: str = "favorited"
    sort_desc: bool = True


def _normalize_author(item: dict) -> PawchiveAuthor:
    if not isinstance(item, dict):
        raise TypeError(f"invalid pawchive creator row: {item!r}")
    return PawchiveAuthor(
        id=str(item["id"]),
        name=str(item["name"]),
        service=str(item["service"]),
        updated=int(item["updated"]),
        favorited=int(item["favorited"]),
    )


def _author_from_row(row: tuple) -> PawchiveAuthor:
    row_id, name, service, updated, favorited = row
    return PawchiveAuthor(
        id=str(row_id),
        name=str(name),
        service=str(service),
        updated=int(updated),
        favorited=int(favorited),
    )


#: ``pawchive_db_valid`` 的 reason 域。``""`` = 就绪；其余 6 值各自对应一条页面文案
#: （``pawchive.author_db.missing`` / ``.empty`` / ``.invalid``）。
DB_REASON_OK = ""
DB_REASON_MISSING = "missing"
DB_REASON_EMPTY = "empty"
DB_REASON_NOT_SQLITE = "not_sqlite"
DB_REASON_NO_AUTHORS_TABLE = "no_authors_table"
DB_REASON_NO_ROWS = "no_rows"
DB_REASON_FTS_MISMATCH = "fts_mismatch"


def _readonly_connect(db_path: Path) -> sqlite3.Connection:
    """只读打开：``mode=ro`` 保证判据自身永不创建库文件（裸 ``connect`` 会建）。"""
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def _fts_doc_count(conn: sqlite3.Connection) -> int:
    """``authors`` 表应有几条 FTS 文档 —— **不是** ``COUNT(*) FROM authors_fts``。

    authors_fts 是 content='authors' 的 external-content 表，对它的 COUNT(*) 会扫内容表、恒等于
    COUNT(*) FROM authors，判不出"索引少一行/没重建"；真正记录已索引文档数的是 shadow 表
    authors_fts_docsize。返回 -1 表示没有 authors_fts 表（视为一致，建索引流程另行处理）。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authors_fts'"
    ).fetchone()
    if row is None:
        return -1
    docsize = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authors_fts_docsize'"
    ).fetchone()
    if docsize is None:
        # 非 external-content 形态（contentless / 普通 fts5）：COUNT(*) 就是文档数。
        return int(conn.execute("SELECT COUNT(*) FROM authors_fts").fetchone()[0])
    return int(conn.execute("SELECT COUNT(*) FROM authors_fts_docsize").fetchone()[0])


def pawchive_db_valid(db_path: Path) -> tuple[bool, str]:
    """作者库就绪判据：``(ok, reason)``，不抛异常。

    就绪 = 文件在 且 非空字节 且 可只读打开 且 有 authors 表 且 行数 > 0 且 FTS 行数一致。
    顺序不能重排：1) is_file() / st_size == 0 必须排在任何 sqlite3.connect 前（ensure_fts 一次
    调用就把 0 字节文件写成合法空库）；2) 只读打开 + 首查吞 DatabaseError 归 not_sqlite；
    3) 行数是唯一的"非空"证据，不许用 fts_ready() 代替（它对 0 行库返回 True）。
    """
    path = Path(db_path)
    if not path.is_file():
        return False, DB_REASON_MISSING
    try:
        if path.stat().st_size == 0:
            return False, DB_REASON_EMPTY
    except OSError:
        return False, DB_REASON_MISSING

    try:
        with closing(_readonly_connect(path)) as conn:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authors'"
            ).fetchone()
            if has_table is None:
                return False, DB_REASON_NO_AUTHORS_TABLE
            authors_count = int(
                conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
            )
            if authors_count <= 0:
                return False, DB_REASON_NO_ROWS
            fts_count = _fts_doc_count(conn)
            # -1 = 索引还没建：不算"库坏了"（建索引是页面自己的流程）。
            if fts_count >= 0 and fts_count != authors_count:
                return False, DB_REASON_FTS_MISMATCH
    except sqlite3.DatabaseError:
        # 非 sqlite 内容（HTTP 错误页）或半截文件：一律"损坏"，不向上抛。
        return False, DB_REASON_NOT_SQLITE
    except OSError:
        return False, DB_REASON_NOT_SQLITE
    return True, DB_REASON_OK


def _sanitize_fts_query(raw_text: str) -> str:
    cleaned = _FTS_SPECIAL_RE.sub(" ", raw_text.strip())
    tokens = [token for token in cleaned.split() if token]
    if not tokens:
        return ""
    return " ".join(f'"{token}"' for token in tokens)


class PawchiveAuthorsDb:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._fts_ready_cached: bool | None = None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS authors (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    service TEXT NOT NULL,
                    updated INTEGER NOT NULL,
                    favorited INTEGER NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_authors_service ON authors(service)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_authors_favorited ON authors(favorited DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_authors_updated ON authors(updated DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_authors_name ON authors(name)"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS pawchive_db_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _fts_table_exists(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authors_fts'"
        ).fetchone()
        return row is not None

    def fts_ready(self) -> bool:
        if self._fts_ready_cached is not None:
            return self._fts_ready_cached
        if not self.db_path.exists():
            self._fts_ready_cached = False
            return False
        with closing(self._connect()) as conn:
            if not self._fts_table_exists(conn):
                self._fts_ready_cached = False
                return False
            # Support both old 'kemono_db_meta' and new 'pawchive_db_meta' table names
            for meta_name in ("pawchive_db_meta", "kemono_db_meta"):
                meta_exists = conn.execute(
                    f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{meta_name}'"
                ).fetchone()
                if meta_exists is not None:
                    break
            if meta_exists is None:
                self._fts_ready_cached = False
                return False
            meta = conn.execute(
                f"SELECT value FROM {meta_name} WHERE key='fts_built'"
            ).fetchone()
            if meta is None or meta[0] != "1":
                self._fts_ready_cached = False
                return False
            authors_count = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
            fts_count = conn.execute("SELECT COUNT(*) FROM authors_fts").fetchone()[0]
            # 0 行库不能算"就绪"：`0 == 0` 会让空壳库通过索引门禁。判据本体是
            # `pawchive_db_valid`，这里只把同一个"非空"前提补齐，避免两套语义打架。
            ready = int(authors_count) > 0 and int(authors_count) == int(fts_count)
            self._fts_ready_cached = ready
            return ready

    def ensure_fts(self, *, force: bool = False) -> None:
        self.ensure_schema()
        if not force and self.fts_ready():
            return
        with closing(self._connect()) as conn:
            cursor = conn.cursor()
            cursor.execute("DROP TABLE IF EXISTS authors_fts")
            cursor.execute(
                """
                CREATE VIRTUAL TABLE authors_fts USING fts5(
                    name,
                    content='authors',
                    content_rowid='rowid'
                )
                """
            )
            cursor.execute(
                "INSERT INTO authors_fts(rowid, name) SELECT rowid, name FROM authors"
            )
            cursor.execute(
                """
                INSERT INTO pawchive_db_meta(key, value) VALUES('fts_built', '1')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """
            )
            conn.commit()
        # 无条件 True 会让"0 行库 + 刚建好的空 FTS"永远就绪；按同一判据重算。
        # 先清缓存，否则 `fts_ready()` 直接返回进来之前那次 False。
        self._fts_ready_cached = None
        self._fts_ready_cached = self.fts_ready()

    def replace_from_creators(self, creators: list[dict]) -> int:
        self.ensure_schema()
        self._fts_ready_cached = None
        authors = [_normalize_author(item) for item in creators]
        with closing(self._connect()) as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT INTO authors (id, name, service, updated, favorited)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    service=excluded.service,
                    updated=excluded.updated,
                    favorited=excluded.favorited
                """,
                [
                    (author.id, author.name, author.service, author.updated, author.favorited)
                    for author in authors
                ],
            )
            conn.commit()
        self.ensure_fts(force=True)
        return len(authors)

    def load_all(self) -> dict[str, PawchiveAuthor]:
        with closing(self._connect()) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, service, updated, favorited FROM authors")
            rows = cursor.fetchall()
        return {str(row[0]): _author_from_row(row) for row in rows}

    def _resolve_sort(self, query: AuthorQuery) -> tuple[str, str]:
        sort_column = query.sort_column if query.sort_column in _SORT_COLUMNS else "favorited"
        sort_direction = "DESC" if query.sort_desc else "ASC"
        return sort_column, sort_direction

    def _build_where(self, query: AuthorQuery) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []

        if query.favorite_ids is not None:
            if not query.favorite_ids:
                return "1=0", []
            placeholders = ",".join("?" for _ in query.favorite_ids)
            clauses.append(f"authors.id IN ({placeholders})")
            params.extend(query.favorite_ids)

        text = (query.text or "").strip()
        if text and query.favorite_ids is None:
            like_pattern = f"%{text}%"
            fts_match = _sanitize_fts_query(text)
            text_clauses: list[str] = []
            text_params: list = []
            if fts_match and self.fts_ready():
                text_clauses.append(
                    "authors.rowid IN (SELECT rowid FROM authors_fts WHERE authors_fts MATCH ?)"
                )
                text_params.append(fts_match)
            text_clauses.extend(
                [
                    "authors.service LIKE ?",
                    "CAST(authors.favorited AS TEXT) LIKE ?",
                    "strftime('%Y-%m-%d', authors.updated, 'unixepoch') LIKE ?",
                    "authors.name LIKE ?",
                ]
            )
            text_params.extend([like_pattern, like_pattern, like_pattern, like_pattern])
            clauses.append("(" + " OR ".join(text_clauses) + ")")
            params.extend(text_params)

        if not clauses:
            return "1=1", []
        return " AND ".join(clauses), params

    def count(self, query: AuthorQuery) -> int:
        where_sql, params = self._build_where(query)
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM authors WHERE {where_sql}",
                params,
            ).fetchone()
        return int(row[0])

    def fetch(self, query: AuthorQuery, *, offset: int, limit: int) -> list[PawchiveAuthor]:
        if limit <= 0:
            return []
        safe_offset = max(0, int(offset))
        safe_limit = max(0, int(limit))
        where_sql, params = self._build_where(query)
        sort_column, sort_direction = self._resolve_sort(query)
        sql = (
            f"SELECT authors.id, authors.name, authors.service, authors.updated, authors.favorited "
            f"FROM authors WHERE {where_sql} "
            f"ORDER BY authors.{sort_column} {sort_direction}, authors.id ASC "
            f"LIMIT ? OFFSET ?"
        )
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, [*params, safe_limit, safe_offset]).fetchall()
        return [_author_from_row(row) for row in rows]


def build_pawchive_db_from_creators_bytes(db_path: Path, payload: bytes) -> int:
    data = json.loads(payload.decode("utf-8"))
    return PawchiveAuthorsDb(db_path).replace_from_creators(data)


def load_pawchive_authors(db_path: Path) -> dict[str, PawchiveAuthor]:
    return PawchiveAuthorsDb(db_path).load_all()
