#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Keep ``res/i18n/<lang>.yml`` in step with the translation IDs used in code.

    ./.venv/Scripts/python.exe res/i18n/build.py --check
    ./.venv/Scripts/python.exe res/i18n/build.py --sync
    ./.venv/Scripts/python.exe res/i18n/build.py --build

The YAML files are the only translation artefact in the repository: a nested
``id -> wording`` map. Compiling them is not a build step -- ``res.i18n`` does it
on launch with PySide6's own ``lrelease``, which is what keeps ``.ts``/``.qm`` out
of git. This script maintains the *IDs*:

``--check``  read-only. Scans the sources for ``qtTrId("...")`` literals and
             reports IDs used in code but missing from every table, keys no call
             uses any more, and entries whose wording is still empty. Also
             confirms each language still compiles to a loadable ``.qm``.

``--sync``   rewrites each table from that scan: keeps existing wording, drops
             keys the code no longer uses, and appends new IDs as ``""``
             (untranslated: Qt then shows the ID itself, so the gap is visible
             instead of silently falling back to Chinese). Wording is never
             invented -- except for ``en_US``, where a new ID gets its key as a
             placeholder you are expected to fix.

``--build``  forces a recompile of every ``.qm`` (normally done at launch).

IDs are discovered by AST scan, not by ``lupdate``: an id-based catalogue has no
source strings to extract, so the ID literal in ``qtTrId()`` is the whole
contract and a plain walk of the sources is both exact and instant.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
I18N = ROOT / "res" / "i18n"

SOURCE_DIRS = ("app", "ui", "core", "res", "deploy")
SKIP_PARTS = {"__pycache__", ".venv", "__temp", ".git", "build", "dist"}
#: Generated rcc blob: no qtTrId calls, and parsing it wastes time.
EXCLUDE_FILES = {"res/material_ct.py"}

#: Floors so a broken scan cannot silently report "no IDs at all".
MIN_SOURCE_FILES = 100
MIN_IDS = 50

sys.path.insert(0, str(ROOT))
from res.lang_data import SOURCE_LANGUAGE  # noqa: E402

HEADER = """\
# acggr {lang} wording table -- the only translation file in the repository.
#
# Keys are semantic IDs, qtTrId()-style: <feature>.<widget>.<element>. They are
# stable English identifiers, so a wording can be rewritten without touching
# code, and a translator never reads a foreign sentence as a key.
#
# Compiled at runtime: `res.i18n` turns this file into an id-based .ts, runs
# PySide6's own lrelease on it and loads the .qm through a plain QTranslator.
# Nothing compiled is ever committed. `res/i18n/build.py --check` reports IDs
# used in code but missing here, and keys no qtTrId() call uses any more.
"""


def yml_path(lang: str) -> pathlib.Path:
    return I18N / f"{lang.replace('-', '_')}.yml"


def languages() -> list:
    """Every shipped language. All of them have a table: the code holds IDs."""
    return sorted(p.stem for p in I18N.glob("*.yml"))


def source_files() -> list:
    found = []
    for directory in SOURCE_DIRS:
        base = ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if SKIP_PARTS & set(path.parts) or rel in EXCLUDE_FILES:
                continue
            found.append(path)
    return sorted(found)


#: An id: dot-separated lowercase snake_case segments, at least three of them
#: (``feature.widget.element``). Three minimum is what separates a real id from
#: incidental dotted strings in the tree -- file names (``aria2.conf``,
#: ``acggr.log``), module paths (``app.GUI.cbg``), settings keys. Those were
#: produced by the code, not authored as UI copy, and must never become keys.
ID_SHAPE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){2,}$")

#: Keyword names whose string value is a translation id even though the only
#: ``qtTrId`` call is ``qtTrId(row.title_id)`` (an attribute, not a literal).
ID_KEYWORDS = frozenset({"title_id", "hint_id"})

#: Top-level domains: a dotted literal ending in one is a host name, not an id
#: (``music.163.com``, ``cdn.example.net``).
TLDS = frozenset({
    "com", "net", "org", "io", "cn", "jp", "co", "me", "dev", "app", "xyz",
    "info", "tv", "cc", "su", "top", "site", "online", "link", "ai",
})


def looks_like_id(value: str) -> bool:
    """True when ``value`` is a translation id rather than an incidental string."""
    if not ID_SHAPE.match(value):
        return False
    return value.rsplit(".", 1)[-1] not in TLDS


def scan_ids() -> tuple:
    """``(direct, indirect)`` id references found in the sources.

    ``direct`` are literal keys passed to ``qtTrId("a.b.c")``, to
    ``_lang_text(table, "a.b.c", ...)``, or as the value of an ``ID_KEYWORDS``
    keyword (``row = RowDescriptor(title_id="a.b.c")``, rendered later through
    ``qtTrId(row.title_id)``).

    ``indirect`` covers ids that reach ``qtTrId`` through a variable:

    - a string constant bound to a name and later passed to ``qtTrId``
      (``_COMFY_DOCS_LINK_NAME = "..."``), and
    - id-shaped literals inside an assignment's tuple/list/dict value
      (``SECTION_OPTIONS = (("body", "a.b.c", icon), ...)``).

    Both are restricted to the ``feature.widget.element`` shape (three or more
    segments). Two-segment dotted strings in this tree are file names
    (``aria2.conf``) and module paths (``app.GUI.cbg``), never UI copy, so
    requiring three segments keeps them out of the catalogue.

    Only a key referenced by neither form is stale.
    """
    files = source_files()
    if len(files) < MIN_SOURCE_FILES:
        raise SystemExit(f"SCAN FAILED source_files={len(files)} < {MIN_SOURCE_FILES}")
    direct, indirect = {}, {}

    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        direct_nodes = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # A row descriptor may carry its ids as keyword arguments
            # (``ViewerSettingRow(title_id=..., hint_id=...)``); the row builder
            # later calls ``qtTrId(setting.title_id)``, which is an attribute and
            # therefore invisible to the name-based rule below. Without this the
            # four viewer rows look stale and ``--sync`` would delete wording
            # that is still on screen.
            for keyword in node.keywords:
                if keyword.arg not in ID_KEYWORDS:
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                    direct_nodes.add(id(value))
                    direct.setdefault(value.value, []).append((rel, node.lineno))

            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            # ``qtTrId(key)`` is the normal form. Two deliberate variants exist:
            # ``_lang_text(table, key, ...)`` renders a *specific* language's
            # wording while another translator is installed (second argument),
            # and ``_set_autosave_hint(key)`` renders a footer status line
            # (first argument, empty string = clear).
            arg_index = 0
            if name == "qtTrId":
                pass
            elif name == "_lang_text":
                arg_index = 1
            elif name == "_set_autosave_hint":
                pass
            else:
                continue
            if len(node.args) <= arg_index:
                continue
            arg = node.args[arg_index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value:
                # ``""`` is the "clear the status line" call, not a key.
                direct_nodes.add(id(arg))
                direct.setdefault(arg.value, []).append((rel, node.lineno))

        # names whose assigned value is passed to qtTrId
        id_vars = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name == "qtTrId" and node.args and isinstance(node.args[0], ast.Name):
                    id_vars.add(node.args[0].id)

        candidates = set()

        def collect(node):
            if isinstance(node, (ast.Tuple, ast.List)):
                for element in node.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        candidates.add(id(element))
                    elif isinstance(element, (ast.Tuple, ast.List)):
                        collect(element)
            elif isinstance(node, ast.Dict):
                for value in node.values:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        candidates.add(id(value))

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets if isinstance(t, ast.Name)]
                if targets and targets[0].id in id_vars and \
                        isinstance(node.value, ast.Constant) and \
                        isinstance(node.value.value, str):
                    candidates.add(id(node.value))
                else:
                    collect(node.value)

        for node in ast.walk(tree):
            if id(node) in direct_nodes or id(node) not in candidates:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and looks_like_id(node.value):
                indirect.setdefault(node.value, []).append((rel, node.lineno))

    if len(direct) < MIN_IDS:
        raise SystemExit(f"SCAN FAILED ids={len(direct)} < {MIN_IDS}")
    return direct, indirect


def read_table(lang: str) -> dict:
    import yaml

    path = yml_path(lang)
    if not path.exists():
        return {}

    flat = {}

    def walk(node, prefix):
        for key, value in node.items():
            full = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(value, full + ".")
            else:
                flat[full] = "" if value is None else str(value)

    walk(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, "")
    return flat


def set_key(table: dict, key: str, value: str) -> None:
    node = table
    parts = key.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def write_table(lang: str, flat: dict) -> None:
    """Serialise one wording table, **one physical line per key**.

    A value containing a newline is emitted as a double-quoted scalar
    (``"a\\nb"``) rather than a ``|`` block. That is what keeps the two tables
    line-for-line comparable: a block scalar spends one physical line per
    newline, so a single multi-line wording in one language -- which is real
    content, and legitimate -- shifted every following line and made "the files
    have different line counts" look like a defect for the third time.

    The *value* is unchanged: ``\\n`` is an escape, not a literal, so the loaded
    wording is byte-identical to what a block scalar would have carried. Plain
    ``|`` blocks would also have re-wrapped on any width change; escapes cannot.
    """
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    def _str(dumper, data):
        style = '"' if "\n" in data else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    Dumper.add_representer(str, _str)

    table = {}
    for key in sorted(flat):
        set_key(table, key, flat[key])
    body = yaml.dump(table, Dumper=Dumper, allow_unicode=True, sort_keys=True,
                     default_flow_style=False, width=float("inf"), indent=2)
    yml_path(lang).parent.mkdir(parents=True, exist_ok=True)
    yml_path(lang).write_text(HEADER.format(lang=lang) + "\n" + body, encoding="utf-8")


def _check_line_parity() -> int:
    """Assert the tables are one line per key, and therefore line-comparable.

    Every language holds the same key set, so with one physical line per key the
    files have identical line counts *by construction*, and a difference means
    exactly one thing: something hand-edited a table without re-running
    ``--sync`` (or a future serializer change reintroduced block scalars). This
    is the check that was missing the previous three times the question came up --
    the answer was always "it is a formatting artefact", which is not a durable
    answer to a question that keeps being asked.
    """
    problems = 0
    counts = {}
    for lang in languages():
        text = yml_path(lang).read_text(encoding="utf-8")
        counts[lang] = len(text.splitlines())
        # A multi-line value would make one key occupy several physical lines.
        blocks = [n for n, line in enumerate(text.splitlines(), 1)
                  if line.rstrip().endswith(("|", "|-", "|+", ">", ">-", ">+"))]
        if blocks:
            print(f"{lang}: FAIL block scalar at line(s) {blocks[:5]}; "
                  "run `res/i18n/build.py --sync` to escape newlines")
            problems += len(blocks)
    if len(set(counts.values())) > 1:
        print(f"line counts differ {counts}; run `res/i18n/build.py --sync`")
        problems += 1
    else:
        print(f"line parity: ok ({next(iter(counts.values()))} lines each)")
    return problems


def check() -> int:
    direct, indirect = scan_ids()
    live = set(direct) | set(indirect)
    used = {**indirect, **direct}
    problems = 0
    for lang in languages():
        table = read_table(lang)
        missing = sorted(set(direct) - set(table))
        stale = sorted(set(table) - live)
        empty = sorted(k for k, v in table.items() if not v)
        ok = not (missing or stale or empty)
        print(f"{lang}: {'ok' if ok else 'FAIL'} keys={len(table)} "
              f"missing={len(missing)} stale={len(stale)} empty={len(empty)}")
        for label, keys in (("missing", missing), ("stale", stale), ("empty", empty)):
            for key in keys[:10]:
                where = used.get(key)
                loc = f"  ({where[0][0]}:{where[0][1]})" if where else ""
                print(f"  {label}: {key}{loc}")
            if len(keys) > 10:
                print(f"  {label}: ... {len(keys) - 10} more")
        problems += len(missing) + len(stale) + len(empty)

    problems += _check_line_parity()

    # a table that exists must still compile
    import res.i18n as bundle

    for lang in languages():
        table = bundle.load_table(lang)
        if not table:
            print(f"{lang}: FAIL no wording loaded")
            problems += 1
            continue
        try:
            qm = bundle.compile_catalogue(lang, table)
        except (OSError, RuntimeError) as exc:
            print(f"{lang}: FAIL compile: {exc}")
            problems += 1
            continue
        if qm.stat().st_size <= 17:
            print(f"{lang}: FAIL empty .qm ({qm})")
            problems += 1

    print(f"ids={len(direct)} indirect={len(set(indirect) - set(direct))} "
          f"sources={len(source_files())} langs={len(languages())} problems={problems}")
    if problems:
        print("tables out of sync; run `res/i18n/build.py --sync`.")
    return 0 if problems == 0 else 1


def sync() -> int:
    direct, indirect = scan_ids()
    used = set(direct) | set(indirect)
    for lang in languages():
        old = read_table(lang)
        flat = {key: old.get(key, "") for key in used}
        write_table(lang, flat)
        empty = sum(1 for v in flat.values() if not v)
        print(f"{lang}: keys={len(flat)} untranslated={empty} -> {yml_path(lang).name}")
    if not languages():
        print("no wording tables yet; add res/i18n/<lang>.yml")
    return 0


def build() -> int:
    import res.i18n as bundle

    rc = 0
    for lang in languages():
        table = bundle.load_table(lang)
        if not table:
            print(f"{lang}: FAIL no wording loaded")
            rc = 1
            continue
        try:
            qm = bundle.compile_catalogue(lang, table)
        except (OSError, RuntimeError) as exc:
            print(f"{lang}: FAIL {exc}")
            rc = 1
            continue
        print(f"{lang}: keys={len(table)} qm_bytes={qm.stat().st_size} -> {qm}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--build", action="store_true")
    args = ap.parse_args()
    if sum((args.check, args.sync, args.build)) != 1:
        ap.error("exactly one of --check / --sync / --build is required")
    if args.check:
        return check()
    return sync() if args.sync else build()


if __name__ == "__main__":
    raise SystemExit(main())
