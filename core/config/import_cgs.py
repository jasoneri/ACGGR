#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Importer/synchroniser for user data left behind by an installed CGS.

Acggr owns its own config area (core.config.conf_dir), a sibling of CGS's; on a fresh install it
is empty, so favourites / tag groups / cookies / download overrides would be lost. Three kinds of
data travel: plain files are copied only when the target is missing; qconfig payloads are merged
key-by-key (an existing file gains CGS's missing favourites / tag groups / search history /
DoH-proxy without losing its own values); record.db tables are unioned (danbooru history + Comfy
snapshots; CGS-only tables ignored). Every merge is additive and target-first.

Must run before core.config.qc is imported: qc.py calls qconfig.load at module import time, so a
later merge leaves in-memory configs at defaults and the next save overwrites the merged files.
Importing this module never imports qc / touches qconfig.
"""
from __future__ import annotations

import hashlib
import os
import pathlib as p
import shutil
import sqlite3
import sys
import tempfile
import time

__all__ = [
    "find_cgs_conf_dir",
    "find_cgs_record_db",
    "import_from_cgs",
    "merge_cgs_comfy_snapshots",
    "merge_cgs_danbooru_history",
    "merge_cgs_qconfigs",
    "merge_cgs_record_history",
]

#: CGS config directory name under the platform config location.
_CGS_DIR_NAME = "CGS"

#: Plain file artefacts that move when the target is missing.
#: ``(source relpath, target relpath)``.
#:
#: qconfig files are *not* here: they must be merged key-by-key into an existing
#: target (see :func:`merge_cgs_qconfigs`), never skipped just because the file
#: already exists. ``record.db`` is absent for the same reason — it is merged
#: table-by-table (see :func:`merge_cgs_record_history`).
_MANIFEST: tuple[tuple[str, str], ...] = (
    ("conf.yml", "conf.yml"),
    ("conf_img.yml", "conf_img.yml"),
    ("cookies.pkl", "cookies.pkl"),
)

#: qconfig payloads that are additive-merged into Acggr's own files.
#:
#: ``qc.json`` only contributes sections whose semantics are shared. Its window
#: geometry (``ScriptWindow.Rect``), theme (``QFluentWidgets``), CGS-only
#: windows (``SubscribeWindow`` / ``ToolWorkspaceWindow``) and mainline manga
#: state must not travel. ``DoH`` and ``Proxy.History`` are settings Acggr
#: actually reads and CGS owns real values for, so they are fill/union merged.
_QCONFIG_FILES: tuple[str, ...] = (
    "qconfig/qc.json",
    "qconfig/qc_cbg.json",
    "qconfig/qc_danbooru.json",
    "qconfig/qc_pawchive.json",
)

#: ``None`` = every top-level section except the shared qfluentwidgets one.
_QCONFIG_SECTIONS: dict[str, tuple[str, ...] | None] = {
    "qconfig/qc.json": ("DoH", "Proxy"),
    "qconfig/qc_cbg.json": None,
    "qconfig/qc_danbooru.json": None,
    "qconfig/qc_pawchive.json": None,
}

#: ``conf.yml`` (and its legacy sibling) are *filtered* on the way in, never
#: copied verbatim. CGS owns a dozen top-level keys Acggr has no reader for —
#: ``cv_proj_path`` / ``clip_db`` / ``rv_script`` / ``webdav`` / ``ags_file`` /
#: ``skipped_version`` … — and a verbatim copy is how they ended up living in
#: Acggr's config for good: they were adopted by ``Conf.init_conf`` (which then
#: wrote them back on every save) and shown by nothing. Only keys the Acggr
#: schema declares travel; everything else is reported as dropped.
_CONF_FILTERED = ("conf.yml", "conf_img.yml")


def _acggr_conf_dir() -> p.Path:
    """Read ``conf_dir`` *at call time*, not import time.

    ``core.config.conf_dir`` is a module-level constant, so resolving it
    lazily keeps this module free of import-time side effects and lets callers
    (tests, probes) redirect the destination by patching the attribute.
    """
    import core.config as config_module

    return p.Path(config_module.conf_dir)


def _candidate_cgs_dirs() -> list[p.Path]:
    """Platform config bases that could hold a sibling ``CGS`` directory."""
    bases: list[p.Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        bases.append(p.Path(local_appdata))
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    bases.append(p.Path(xdg_config) if xdg_config else p.Path.home() / ".config")
    if sys.platform == "darwin":
        bases.append(p.Path.home() / "Library" / "Application Support")

    seen: set[str] = set()
    candidates: list[p.Path] = []
    for base in bases:
        # Packaged install first; the ``python/CGS`` sibling is the config dir
        # Qt derives when CGS is launched from source (app name becomes the
        # interpreter name). Both have existed side by side on real machines.
        for candidate in (base / _CGS_DIR_NAME, base / "python" / _CGS_DIR_NAME):
            key = str(candidate).lower()
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
    return candidates


def _has_importable_source(cgs_dir: p.Path) -> bool:
    """True when the directory holds at least one artefact this importer reads."""
    return any(
        (cgs_dir / rel_path).is_file()
        for rel_path in (*[src for src, _ in _MANIFEST], *_QCONFIG_FILES)
    )


def _resolve_record_db(cgs_dir: p.Path | str | None) -> p.Path | None:
    """Accept either CGS's config directory or the ``record.db`` file itself."""
    resolved = p.Path(cgs_dir) if cgs_dir is not None else find_cgs_record_db()
    if resolved is None:
        return None
    return resolved if resolved.is_file() else resolved / "record.db"


def _record_db_target(acggr_dir: p.Path | str | None) -> p.Path:
    return (p.Path(acggr_dir) if acggr_dir is not None else _acggr_conf_dir()) / "record.db"


def find_cgs_conf_dir() -> p.Path | None:
    """Locate an existing CGS config directory, or ``None`` when absent.

    Read-only: never creates anything. A directory only qualifies when it
    actually holds at least one of the artefacts this module imports, so a
    leftover empty ``CGS`` folder is not mistaken for an importable install.
    """
    for candidate in _candidate_cgs_dirs():
        try:
            if candidate.is_dir() and _has_importable_source(candidate):
                return candidate
        except OSError:
            continue
    return None


def find_cgs_record_db() -> p.Path | None:
    """Locate CGS's ``record.db`` download-history database, or ``None``.

    CGS and Acggr both keep the danbooru download history in <config dir>/record.db, table
    danbooru_md5_table; CGS used a standalone DB under its own config dir, so the file has to be
    found and merged explicitly. This probe is separate from find_cgs_conf_dir because a CGS
    install whose config was deleted can still carry history. The LocalAppData/python/CGS
    fallback covers CGS launched from source.
    """
    for candidate in _candidate_cgs_dirs():
        db = candidate / "record.db"
        try:
            if db.is_file():
                return db
        except OSError:
            continue
    return None


def merge_cgs_danbooru_history(
    cgs_dir: p.Path | str | None = None,
    *,
    acggr_dir: p.Path | str | None = None,
) -> dict:
    """Merge CGS's danbooru md5 rows into Acggr's ``record.db`` (idempotent).

    The file itself is never copied: Acggr's DB can already hold its own
    downloads, so the two histories have to be unioned. ``INSERT OR IGNORE``
    keyed by ``identity_md5`` keeps the merge additive and safe to re-run.

    The source is opened read-only; only the Acggr-side DB is written. CGS has a
    second generic ``identity_md5_table`` for its non-danbooru sites, but Acggr
    has no reader for that key space, so only the danbooru table travels.
    """
    from core.script.image.danbooru.constants import DANBOORU_SQL_TABLE

    source = _resolve_record_db(cgs_dir)
    target = _record_db_target(acggr_dir)

    result: dict = {
        "source": str(source) if source is not None else "",
        "target": str(target),
        "table": DANBOORU_SQL_TABLE,
        "source_rows": 0,
        "inserted": 0,
        "skipped": None,
    }
    if source is None or not source.is_file():
        result["skipped"] = "no_record_db"
        return result
    if source.resolve() == target.resolve():
        result["skipped"] = "same_database"
        return result

    # Source is strictly read-only: `mode=ro` cannot checkpoint or create WAL
    # sidecars in the retired CGS directory.
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        has_table = source_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (DANBOORU_SQL_TABLE,),
        ).fetchone()
        if has_table is None:
            result["skipped"] = "no_danbooru_table"
            return result
        rows = [
            str(row[0])
            for row in source_conn.execute(
                f"SELECT identity_md5 FROM `{DANBOORU_SQL_TABLE}` "
                "WHERE identity_md5 IS NOT NULL AND identity_md5 <> ''"
            )
        ]
    finally:
        source_conn.close()

    result["source_rows"] = len(rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    target_conn = sqlite3.connect(str(target), timeout=30.0)
    try:
        target_conn.execute("PRAGMA busy_timeout=5000")
        target_conn.execute(
            f"""CREATE TABLE IF NOT EXISTS `{DANBOORU_SQL_TABLE}` (
                `id` INTEGER PRIMARY KEY AUTOINCREMENT,
                `identity_md5` TEXT NOT NULL UNIQUE
            );"""
        )
        before = target_conn.execute(f"SELECT COUNT(*) FROM `{DANBOORU_SQL_TABLE}`").fetchone()[0]
        if rows:
            target_conn.executemany(
                f"INSERT OR IGNORE INTO `{DANBOORU_SQL_TABLE}` (identity_md5) VALUES (?);",
                ((value,) for value in rows),
            )
        target_conn.commit()
        after = target_conn.execute(f"SELECT COUNT(*) FROM `{DANBOORU_SQL_TABLE}`").fetchone()[0]
    finally:
        target_conn.close()
    result["inserted"] = int(after - before)
    return result


#: Snapshot columns in the order Acggr writes them. CGS's older DBs lack the
#: card-index columns, so the merge fills those with Acggr's defaults.
_COMFY_COLUMNS: tuple[str, ...] = (
    "job_id",
    "editor_prompt",
    "unet",
    "denoise",
    "wd14",
    "created_at",
    "tag_groups_json",
    "status",
    "preview_json",
    "dismissed",
    "updated_at",
)


def merge_cgs_comfy_snapshots(
    cgs_dir: p.Path | str | None = None,
    *,
    acggr_dir: p.Path | str | None = None,
) -> dict:
    """Union CGS's ``comfy_job_snapshots`` into Acggr's table (idempotent).

    Job snapshots are the ComfyJobsDialog card list; losing them drops the CGS-era generations.
    The merge is asymmetric: a job_id Acggr does not have is inserted verbatim; a job_id both
    sides have keeps every non-empty Acggr value and only fills columns Acggr left empty
    (tag_groups_json / preview_json / updated_at). Status, dismissal and the prompt are live
    Acggr state, never overwritten. Source is read-only.
    """
    from core.sql import comfy_job_snapshots

    source = _resolve_record_db(cgs_dir)
    target = _record_db_target(acggr_dir)
    result: dict = {
        "source": str(source) if source is not None else "",
        "target": str(target),
        "table": comfy_job_snapshots._TABLE,
        "source_rows": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": None,
    }
    if source is None or not source.is_file():
        result["skipped"] = "no_record_db"
        return result
    if source.resolve() == target.resolve():
        result["skipped"] = "same_database"
        return result

    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        has_table = source_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (comfy_job_snapshots._TABLE,),
        ).fetchone()
        if has_table is None:
            result["skipped"] = "no_comfy_table"
            return result
        source_columns = {
            str(row[1])
            for row in source_conn.execute(
                f"PRAGMA table_info(`{comfy_job_snapshots._TABLE}`)"
            )
        }
        selected = [column for column in _COMFY_COLUMNS if column in source_columns]
        raw_rows = source_conn.execute(
            f"SELECT {', '.join(f'`{column}`' for column in selected)} "
            f"FROM `{comfy_job_snapshots._TABLE}`"
        ).fetchall()
    finally:
        source_conn.close()

    rows: list[tuple] = []
    for raw in raw_rows:
        record = dict(zip(selected, raw))
        job_id = str(record.get("job_id") or "").strip()
        if not job_id:
            continue
        rows.append(
            (
                job_id,
                str(record.get("editor_prompt") or ""),
                str(record.get("unet") or "turbo"),
                _as_int(record.get("denoise"), 100),
                _as_int(record.get("wd14"), 0),
                _as_float(record.get("created_at"), 0.0),
                record.get("tag_groups_json"),
                str(record.get("status") or "completed"),
                record.get("preview_json"),
                _as_int(record.get("dismissed"), 0),
                _as_float(record.get("updated_at"), None),
            )
        )

    result["source_rows"] = len(rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    target_conn = sqlite3.connect(str(target), timeout=30.0)
    try:
        target_conn.execute("PRAGMA busy_timeout=5000")
        comfy_job_snapshots.ensure_table(target_conn)
        existing = {
            str(row[0]): (row[1], row[2], row[3])
            for row in target_conn.execute(
                f"SELECT job_id, tag_groups_json, preview_json, updated_at "
                f"FROM `{comfy_job_snapshots._TABLE}`"
            )
        }
        to_insert = [row for row in rows if row[0] not in existing]
        to_update = []
        for row in rows:
            current = existing.get(row[0])
            if current is None:
                continue
            current_groups, current_preview, current_updated = current
            fill_groups = row[6] if not current_groups else None
            fill_preview = row[8] if not current_preview else None
            fill_updated = row[10] if current_updated is None else None
            if fill_groups or fill_preview or fill_updated is not None:
                to_update.append((fill_groups, fill_preview, fill_updated, row[0]))
        if to_insert:
            target_conn.executemany(
                f"INSERT OR IGNORE INTO `{comfy_job_snapshots._TABLE}` "
                f"({', '.join(_COMFY_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_COMFY_COLUMNS))});",
                to_insert,
            )
        if to_update:
            target_conn.executemany(
                f"""UPDATE `{comfy_job_snapshots._TABLE}` SET
                    tag_groups_json = COALESCE(NULLIF(tag_groups_json, ''), ?),
                    preview_json = COALESCE(NULLIF(preview_json, ''), ?),
                    updated_at = COALESCE(updated_at, ?)
                    WHERE job_id = ?;""",
                to_update,
            )
        target_conn.commit()
    finally:
        target_conn.close()
    result["inserted"] = len(to_insert)
    result["updated"] = len(to_update)
    return result


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def merge_cgs_record_history(
    cgs_dir: p.Path | str | None = None,
    *,
    acggr_dir: p.Path | str | None = None,
) -> dict:
    """Merge every CGS ``record.db`` dimension Acggr reads (idempotent).

    Two tables travel: danbooru download history (``danbooru_md5_table``) and
    Comfy job snapshots (``comfy_job_snapshots``). Everything else in CGS's DB
    belongs to sites/features Acggr does not ship (manga ``identity_md5_table``,
    ``jm_title_md5``, ``wnacg_title_md5``, ``ehentai_title_md5``), so it is
    deliberately omitted instead of being copied into a table no reader uses.
    """
    danbooru = merge_cgs_danbooru_history(cgs_dir, acggr_dir=acggr_dir)
    snapshots = merge_cgs_comfy_snapshots(cgs_dir, acggr_dir=acggr_dir)
    return {
        "source": danbooru["source"] or snapshots["source"],
        "target": danbooru["target"],
        "danbooru": danbooru,
        "comfy_job_snapshots": snapshots,
        "inserted": int(danbooru.get("inserted") or 0)
        + int(snapshots.get("inserted") or 0),
        "updated": int(snapshots.get("updated") or 0),
    }


def _read_json_mapping(path: p.Path) -> dict | None:
    """Load a JSON object, or ``None`` when the file is absent/unusable."""
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _merge_missing(target: object, source: object) -> tuple[object, int]:
    """Target-first additive merge used for qconfig payloads.

    * dicts recurse, so a section missing from one side is filled key by key;
    * lists union (Acggr's order kept, source-only entries appended) — a
      favourites / MRU-history list is a set, not a whole-value replacement;
    * scalars keep Acggr's value unless it is empty (``None`` / ``""``).

    Returns ``(merged, added_leaf_count)`` where the count only exists so the
    import result can report how much actually came across.
    """
    if isinstance(target, dict) and isinstance(source, dict):
        merged = dict(target)
        added = 0
        for key, source_value in source.items():
            if key in merged:
                merged[key], child_added = _merge_missing(merged[key], source_value)
                added += child_added
            else:
                merged[key] = source_value
                added += _leaf_count(source_value)
        return merged, added
    if isinstance(target, list) and isinstance(source, list):
        merged = list(target)
        added = 0
        for item in source:
            if item not in merged:
                merged.append(item)
                added += 1
        return merged, added
    if target is None or target == "":
        if source is not None and source != "":
            return source, _leaf_count(source)
    return target, 0


def _leaf_count(value: object) -> int:
    if isinstance(value, dict):
        return sum(_leaf_count(item) for item in value.values())
    if isinstance(value, list):
        return len(value)
    return 1


def _filtered_qconfig_payload(payload: dict, rel_path: str) -> dict:
    """Drop sections that must never travel out of a CGS qconfig file."""
    allowed = _QCONFIG_SECTIONS.get(rel_path)
    filtered: dict = {}
    for section, value in payload.items():
        if section == _SHARED_QFLUENT_SECTION:
            continue
        if allowed is not None and section not in allowed:
            continue
        filtered[section] = value
    return filtered


def _atomic_write_json(dst: p.Path, payload: dict) -> None:
    import json

    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".part", dir=str(dst.parent))
    os.close(fd)
    tmp = p.Path(tmp_name)
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8"
        )
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _plan_qconfig_merges(cgs_dir: p.Path, acggr_dir: p.Path) -> list[dict]:
    """Compute merge results without writing anything."""
    changes: list[dict] = []
    for rel_path in _QCONFIG_FILES:
        source_path = cgs_dir / rel_path
        if not source_path.is_file():
            continue
        source_payload = _read_json_mapping(source_path)
        if source_payload is None:
            continue
        target_path = acggr_dir / rel_path
        target_payload: dict = {}
        if target_path.exists():
            loaded = _read_json_mapping(target_path)
            # A malformed Acggr file is left alone: silently replacing it with
            # CGS's payload would destroy whatever the user still has in it.
            if loaded is None:
                continue
            target_payload = loaded
        merged, added = _merge_missing(
            target_payload, _filtered_qconfig_payload(source_payload, rel_path)
        )
        if merged == target_payload:
            continue
        changes.append(
            {
                "name": p.PurePosixPath(rel_path).name,
                "relative_path": rel_path,
                "target": target_path,
                "payload": merged,
                "added": added,
                "created": not target_path.exists(),
            }
        )
    return changes


def merge_cgs_qconfigs(
    cgs_dir: p.Path | str | None = None,
    *,
    acggr_dir: p.Path | str | None = None,
) -> dict:
    """Additive-merge CGS's qconfig files into Acggr's existing ones.

    This is the fix for the old "target exists -> skip" rule: a user who had
    already imported once kept losing every favourite / history entry CGS wrote
    afterwards. The merge never replaces Acggr's file; it only fills sections,
    keys and list entries Acggr does not have. Targets that changed are backed
    up first (same ``import_cgs_backup_*`` layout as the file importer).

    ``qc.json`` is filtered to the sections Acggr reads (DoH / Proxy); theme,
    window geometry and CGS-only sections never travel.
    """
    resolved = p.Path(cgs_dir) if cgs_dir is not None else find_cgs_conf_dir()
    target_dir = p.Path(acggr_dir) if acggr_dir is not None else _acggr_conf_dir()
    result: dict = {
        "source": str(resolved) if resolved is not None else "",
        "target": str(target_dir),
        "files": [],
        "added": 0,
        "backup_dir": None,
        "skipped": None,
    }
    if resolved is None or not resolved.is_dir():
        result["skipped"] = "no_cgs"
        return result

    changes = _plan_qconfig_merges(resolved, target_dir)
    if not changes:
        return result

    # Fresh install: every target is new, so there is nothing to back up and no
    # empty ``import_cgs_backup_*`` directory to leave behind.
    backup_dir = (
        _unique_backup_dir(target_dir)
        if any(not change["created"] for change in changes)
        else None
    )
    for change in changes:
        dst: p.Path = change["target"]
        if backup_dir is not None and dst.exists():
            backup_target = backup_dir / dst.relative_to(target_dir)
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup_target)
        _atomic_write_json(dst, change["payload"])
        result["files"].append(
            {
                "name": change["name"],
                "added": change["added"],
                "created": change["created"],
            }
        )
        result["added"] += change["added"]
    result["backup_dir"] = str(backup_dir) if backup_dir is not None else None
    return result


def _sha256_file(path: p.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: qfluentwidgets stores these on the *class* ``QConfig``, so every
#: ``qconfig.load`` in the process writes into the same items. CGS persists
#: ``ThemeMode: Dark`` in all of its ``qc_*.json``; copying those files verbatim
#: would therefore override Acggr's own ``qc.json`` theme (whichever file is
#: loaded last wins) and silently flip the whole UI to dark.
_SHARED_QFLUENT_SECTION = "QFluentWidgets"


def _atomic_copy(src: p.Path, dst: p.Path, *, drop_json_keys: tuple[str, ...] = (),
                 keep_yaml_keys: frozenset | None = None) -> dict:
    """Copy ``src`` to ``dst`` via a same-directory temp file + ``os.replace``.

    A failure before os.replace leaves the target untouched and removes the temp file, so no
    half-written file is ever observed at dst. drop_json_keys / keep_yaml_keys rewrite a payload
    before it lands. An unparsable payload falls back to a verbatim copy instead of raising (one
    truncated qc_*.json must not abort the batch). Returns ``{"dropped": [...]}``.
    """
    dropped: list = []
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".part", dir=str(dst.parent))
    os.close(fd)
    tmp = p.Path(tmp_name)
    try:
        rewritten = False
        if drop_json_keys:
            import json

            try:
                payload = json.loads(src.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    for key in drop_json_keys:
                        payload.pop(key, None)
                    tmp.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8"
                    )
                    rewritten = True
            except (ValueError, UnicodeDecodeError):
                rewritten = False
        if keep_yaml_keys is not None:
            import yaml

            try:
                payload = yaml.safe_load(src.read_text(encoding="utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                dropped = sorted(str(key) for key in payload if key not in keep_yaml_keys)
                kept = {key: value for key, value in payload.items() if key in keep_yaml_keys}
                tmp.write_text(
                    yaml.dump(kept, allow_unicode=True, sort_keys=False), encoding="utf-8"
                )
                rewritten = True
        if not rewritten:
            shutil.copyfile(src, tmp)
            shutil.copystat(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return {"dropped": dropped}


def _is_generated_default(path: p.Path) -> bool:
    """True when ``path`` is still the untouched default ``conf.yml``.

    ``core.__init__`` runs ``Conf()`` at import time, which writes the default
    payload into ``conf_dir`` when ``conf.yml`` is missing. That generated file
    is a default, not user data, so importing over it is safe — without this
    check a fresh install would report ``conf.yml`` as ``reason="exists"`` and
    never receive CGS's real settings.

    The judgement lives in :func:`core.config.is_default_conf`, so the schema and
    the "is this still untouched" rule cannot drift apart; there is no shipped
    template file to compare bytes against any more.
    """
    from core.config import is_default_conf

    return is_default_conf(path)


def _unique_backup_dir(acggr_dir: p.Path) -> p.Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = acggr_dir / f"import_cgs_backup_{stamp}"
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = acggr_dir / f"import_cgs_backup_{stamp}-{suffix}"
    candidate.mkdir(parents=True)
    return candidate


def _try_merge_record_history(cgs_dir: p.Path, acggr_dir: p.Path) -> dict:
    """Run the additive history merge without letting it abort the file import."""
    try:
        return merge_cgs_record_history(cgs_dir, acggr_dir=acggr_dir)
    except Exception as error:  # noqa: BLE001 - config copy must still succeed
        return {"error": f"{type(error).__name__}: {error}"}


def _try_merge_qconfigs(cgs_dir: p.Path, acggr_dir: p.Path) -> dict:
    """Run the additive qconfig merge without letting it abort the file import."""
    try:
        return merge_cgs_qconfigs(cgs_dir, acggr_dir=acggr_dir)
    except Exception as error:  # noqa: BLE001 - config copy must still succeed
        return {"error": f"{type(error).__name__}: {error}"}


def import_from_cgs(
    cgs_dir: p.Path | str | None = None,
    *,
    overwrite: bool = False,
) -> dict:
    """Merge CGS's user data into Acggr's own config directory.

    cgs_dir defaults to find_cgs_conf_dir; destination is always core.config.conf_dir (read at
    call time). Nothing is ever replaced wholesale: plain files are copied only when the target
    is missing (or is still the generated default), qconfig files are merged key-by-key, and
    record.db is merged table-by-table. overwrite=True may replace plain files, each backed up
    into <acggr_conf_dir>/import_cgs_backup_<ts>/. Idempotent on a repeat call.
    """
    acggr_dir = _acggr_conf_dir()
    resolved = p.Path(cgs_dir) if cgs_dir is not None else find_cgs_conf_dir()

    result: dict = {
        "skipped": None,
        "cgs_dir": str(resolved) if resolved is not None else "",
        "acggr_dir": str(acggr_dir),
        "backup_dir": None,
        "copied": [],
        "kept": [],
        "merged": [],
        "record_history": None,
        "qconfig_merge": None,
    }
    if resolved is None or not resolved.is_dir():
        result["skipped"] = "no_cgs"
        return result

    # Neither download history nor qconfig payloads are file-to-file copies:
    # CGS's values are unioned into Acggr's own files/DB. Do it before the
    # config plan early-returns on "already imported", so a re-run still
    # backfills data that an older build of this importer never touched.
    result["record_history"] = _try_merge_record_history(resolved, acggr_dir)
    result["qconfig_merge"] = _try_merge_qconfigs(resolved, acggr_dir)
    qconfig_files = (result["qconfig_merge"] or {}).get("files") or []
    result["merged"] = list(qconfig_files)

    plan: list[tuple[p.Path, p.Path, str]] = []
    kept: list[dict] = []
    for rel_src, rel_dst in _MANIFEST:
        src = resolved / rel_src
        dst = acggr_dir / rel_dst
        name = p.PurePosixPath(rel_dst).name
        if not src.is_file():
            kept.append({"name": name, "reason": "missing_source"})
            continue
        # A generated default is not user data: overwrite it even when
        # overwrite=False, otherwise the first launch permanently loses the
        # real conf.yml (see _is_generated_default). Only conf.yml has a
        # generated default; cookies.pkl / conf_img.yml must not be parsed as
        # YAML.
        if (
            dst.exists()
            and not overwrite
            and not (name == "conf.yml" and _is_generated_default(dst))
        ):
            kept.append({"name": name, "reason": "exists"})
            continue
        plan.append((src, dst, name))

    if not plan:
        has_existing_target = any(item["reason"] == "exists" for item in kept)
        nothing_merged = not result["merged"]
        result["skipped"] = (
            "already_imported" if has_existing_target and nothing_merged else None
        )
        result["kept"] = kept
        result["backup_dir"] = (result["qconfig_merge"] or {}).get("backup_dir")
        return result

    # Back up only the targets that are actually about to be replaced; nothing
    # to replace (fresh install) => no backup dir, so no empty directory.
    backup_dir: p.Path | None = None
    replaced = [(dst, name) for _src, dst, name in plan if dst.exists()]
    if replaced:
        backup_dir = _unique_backup_dir(acggr_dir)
        for dst, _name in replaced:
            backup_target = backup_dir / dst.relative_to(acggr_dir)
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup_target)

    from core.config import conf_schema_keys

    copied: list[dict] = []
    for src, dst, name in plan:
        outcome = _atomic_copy(
            src, dst,
            # conf.yml / conf_img.yml: only the keys Acggr actually reads may land. CGS's own
            # settings (clip_db, rv_script, webdav, ...) have no reader here.
            keep_yaml_keys=conf_schema_keys() if name in _CONF_FILTERED else None,
        )
        entry = {
            "name": name,
            "bytes": dst.stat().st_size,
            "sha256": _sha256_file(dst),
        }
        if outcome["dropped"]:
            entry["dropped"] = outcome["dropped"]
        copied.append(entry)

    file_backup_dir = str(backup_dir) if backup_dir is not None else None
    result["backup_dir"] = file_backup_dir or (result["qconfig_merge"] or {}).get("backup_dir")
    result["copied"] = copied
    result["kept"] = kept

    # Acggr reads ONE conf.yml, but a CGS install still keeps its script
    # sections in a second conf_img.yml. ``core`` was imported (and its
    # ``Conf()`` singleton built) before this importer ran, so without a
    # refresh the freshly copied script sections would only be folded on the
    # *next* launch — and a Settings save on this launch could write defaults
    # over them. Re-running init_conf() folds conf_img.yml into the copied
    # conf.yml now and updates the shared in-memory singleton in place.
    if any(item["name"] in _CONF_FILTERED for item in copied):
        import core

        # Either file being replaced invalidates a previous fold: conf.yml may
        # have lost script sections, or conf_img.yml may carry new ones. The
        # fold itself is non-destructive (only missing keys are added), so
        # replaying it after a re-import is safe.
        from core.config.legacy_conf import MIGRATION_MARKER

        (acggr_dir / MIGRATION_MARKER).unlink(missing_ok=True)
        core.conf.file = acggr_dir / "conf.yml"
        core.conf.init_conf()

    return result