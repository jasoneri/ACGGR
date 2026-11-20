#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Qt translation bundle compiled from the tracked YAML wording at runtime.

Only ``res/i18n/<lang>.yml`` is versioned: a nested ``key -> wording`` map whose
keys are semantic translation IDs. On launch -- and again whenever the YAML
changes -- this module turns it into an *id-based* ``.ts``, runs PySide6's own
``lrelease`` on it, and hands the ``.qm`` to a plain ``QTranslator``. Nothing
compiled is ever committed.

The lookup side is stock Qt: ``qtTrId("a.b.c")`` resolves through the installed
``QTranslator``. An id-based catalogue has no ``<source>`` strings, so it never
depends on the Chinese wording in the code and a rewording costs no key churn.
``qfluentwidgets``' own text still comes from ``FluentTranslator``; a miss here
returns the id itself, which is visible rather than blank.

Why compile on the user's machine: a ``.ts``/``.qm`` pair in git would store every
string twice (and Qt's ``.ts`` XML is five lines per string). The YAML is the one
artefact a human edits; compiling it locally is what keeps the rest out of the
repository. Compiling is skipped when the hash stamp still matches, so the cost
is one ``lrelease`` run per wording change, not per launch.
"""
import hashlib
import pathlib
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

import yaml
from PySide6.QtCore import QTranslator

from res.lang_data import SOURCE_LANGUAGE

_DIR = pathlib.Path(__file__).parent


def catalogue_path(lang: str) -> pathlib.Path:
    return _DIR.joinpath(f"{lang.replace('-', '_')}.yml")


def languages() -> list:
    """Every language that ships a wording table."""
    return sorted(p.stem for p in _DIR.glob("*.yml"))


def load_table(lang: str) -> dict:
    """Flatten ``<lang>.yml`` into ``{"a.b.c": wording}``; empty when missing.

    Every language needs a table, including the one the product text reads most
    naturally in: the code carries IDs, not source sentences, so there is no such
    thing as "the language that needs no catalogue" any more.
    """
    path = catalogue_path(lang)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    flat = {}

    def walk(node, prefix):
        for key, value in node.items():
            full = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(value, full + ".")
            elif isinstance(value, str) and value:
                flat[full] = value

    walk(raw, "")
    return flat


def _tool(name: str) -> pathlib.Path:
    import PySide6

    base = pathlib.Path(PySide6.__file__).parent
    for candidate in sorted(base.glob(f"{name}*")):
        if candidate.is_file() and candidate.suffix.lower() in ("", ".exe"):
            return candidate
    raise FileNotFoundError(f"{name} not found next to PySide6: {base}")


def write_ts(path: pathlib.Path, lang: str, table: dict) -> None:
    """Serialise an id-based ``.ts``: ``<message id="...">`` and no ``<source>``."""
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<!DOCTYPE TS>",
             f'<TS version="2.1" language="{escape(lang)}" '
             f'sourcelanguage="{escape(SOURCE_LANGUAGE)}">',
             "<context>", "    <name></name>"]
    for key in sorted(table):
        lines.append(f'    <message id="{escape(key)}">')
        lines.append(f"        <translation>{escape(table[key])}</translation>")
        lines.append("    </message>")
    lines.append("</context>")
    lines.append("</TS>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stamp(lang: str, table: dict) -> str:
    digest = hashlib.sha256(lang.encode("utf-8"))
    for key in sorted(table):
        digest.update(f"{key}\x00{table[key]}\x1e".encode("utf-8"))
    return digest.hexdigest()


def _write_dirs() -> list:
    """Output candidates, most preferred first (package dir, user cache, temp)."""
    candidates = [_DIR]
    try:
        from res import app_config_dir

        candidates.append(app_config_dir().joinpath("i18n"))
    except Exception:  # noqa: BLE001 - a cache path must never break startup
        pass
    candidates.append(pathlib.Path(tempfile.gettempdir()).joinpath("acggr-i18n"))
    return candidates


def compile_catalogue(lang: str, table: dict) -> pathlib.Path:
    """Compile ``table`` into ``<lang>.qm`` (reuses the cache when unchanged)."""
    stamp = _stamp(lang, table)
    errors = []
    for out_dir in _write_dirs():
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir.joinpath(f"{lang}.qm")
            stamp_file = out_dir.joinpath(f"{lang}.hash")
            if out.exists() and stamp_file.exists() and \
                    stamp_file.read_text(encoding="utf-8").strip() == stamp:
                return out
            with tempfile.TemporaryDirectory() as td:
                ts_file = pathlib.Path(td).joinpath(f"{lang}.ts")
                write_ts(ts_file, lang, table)
                proc = subprocess.run(
                    [str(_tool("lrelease")), str(ts_file), "-qm", str(out)],
                    capture_output=True, text=True,
                )
            if proc.returncode:
                raise RuntimeError(f"lrelease rc={proc.returncode}\n{proc.stdout}{proc.stderr}")
            stamp_file.write_text(stamp, encoding="utf-8")
            return out
        except (OSError, RuntimeError) as exc:
            errors.append(f"{out_dir}: {exc}")
    raise RuntimeError("; ".join(errors))


def translator(lang: str) -> QTranslator | None:
    """A ``QTranslator`` holding ``lang``'s wording, or ``None`` when not needed.

    ``None`` means "let Qt fall back to the id": the source language, no table,
    or a failed compile. The UI then shows ids -- degraded and loud, never blank.
    """
    table = load_table(lang)
    if not table:
        return None
    try:
        qm = compile_catalogue(lang, table)
    except (OSError, RuntimeError) as exc:
        print(f"i18n: {lang} catalogue not compiled: {exc}", file=sys.stderr)
        return None
    own = QTranslator()
    if not own.load(str(qm)):
        print(f"i18n: {qm} could not be loaded", file=sys.stderr)
        return None
    return own
