#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Acggr resource layer (leaf package).

Holds the per-user path resolver, the bundled qrc package (``material_ct``),
the language data and the Qt translation bundle (``res/i18n``).

Translation contract: user-visible text is translated by Qt, never by a custom
gettext layer, and never by a foreign sentence used as a key. Code calls
``qtTrId("feature.widget.element")``; wording lives in the tracked
``res/i18n/<lang>.yml`` (nested ``id -> wording``), which ``res.i18n`` compiles
with PySide6's own ``lrelease`` on launch into a ``.qm`` that ``app.bootstrap``
installs with a plain ``QTranslator``. No compiled translation ever enters git.
See ``harness/docs/import-layout.md`` §2.4.
"""
import os
import sys
import pathlib

from res.lang_data import system_language


def app_config_root() -> pathlib.Path:
    """Per-user application-data root, resolved WITHOUT Qt.

    ``QStandardPaths`` folds ``QCoreApplication.applicationName()`` into its
    results, so the same call yields ``%LOCALAPPDATA%`` before a ``QApplication``
    exists, ``%LOCALAPPDATA%\\python`` after one is built, and
    ``%LOCALAPPDATA%\\acggr`` with an explicit ``setApplicationName("acggr")``.
    Resolving from the environment instead keeps every per-user path constant
    regardless of import order.

    This is the single source of truth for ``core.config.conf_dir``; res is the
    leaf package, so it is the only place both layers may depend on.
    """
    if sys.platform == "win32":
        return pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home().joinpath("AppData", "Local"))
    if sys.platform == "darwin":
        return pathlib.Path.home().joinpath("Library", "Application Support")
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home().joinpath(".config"))


def app_config_dir() -> pathlib.Path:
    """Acggr's own per-user config dir (``<root>/Acggr``); ``conf_dir``'s only source."""
    return app_config_root().joinpath("Acggr")


"""usage of `<br>`：
    1. 一行话禁止加 `br` ，换行在 `self.say()` 前解决;
    2. 多行的一段话可在最后加 `br` ，禁止在段落起始处加 `br`

    1. forbid the use of `br` in one line, solve line break before `self.say()`;
    2. add `br` at the end of a paragraph with multiple lines, forbid the use of `br` at the beginning of a paragraph
"""

_path = pathlib.Path(__file__).parent


def i18n_dir() -> pathlib.Path:
    """Directory holding the tracked ``<lang>.yml`` wording tables."""
    return _path.joinpath("i18n")


def available_languages() -> tuple:
    """Language codes acggr ships wording for.

    The tracked ``res/i18n/<lang>.yml`` set is the runtime truth; the compiled
    ``.qm`` is produced on demand. Every language needs a table -- the code
    carries translation IDs, so nothing is "free" the way a source-language
    catalogue would be.
    """
    return tuple(sorted(p.stem for p in i18n_dir().glob("*.yml")))


def supported_languages() -> tuple:
    """The languages acggr *offers* and ships wording for.

    ``available_languages`` is a directory listing; this is the product contract
    (``res.lang_data.SUPPORTED_LANGUAGES``) intersected with it, so a stray
    ``<lang>.yml`` dropped in by a translator never silently becomes a picker
    entry. Order follows ``SUPPORTED_LANGUAGES``.
    """
    from res.lang_data import SUPPORTED_LANGUAGES

    shipped = set(available_languages())
    return tuple(code for code in SUPPORTED_LANGUAGES if code in shipped) or available_languages()


def resolve_language(preferred=None) -> str:
    """Pick the shipped language for ``preferred`` (system locale by default).

    A whitelist, not a best match: only ``res.lang_data.SUPPORTED_LANGUAGES`` can
    come back. ``zh_TW`` / ``ja_JP`` / any other locale routes to
    ``DEFAULT_LANGUAGE`` (``en_US``) instead of the closest catalogue -- matching
    by primary subtag handed a Traditional-Chinese user Simplified wording, and
    guessing wrong is worse than the documented English fallback.
    """
    langs = supported_languages()
    wanted = (preferred or system_language() or "").replace("-", "_")
    if wanted in langs:
        return wanted
    from res.lang_data import DEFAULT_LANGUAGE

    if DEFAULT_LANGUAGE in langs:
        return DEFAULT_LANGUAGE
    return langs[0] if langs else DEFAULT_LANGUAGE


#: Language used before any translator is installed. ``core.config`` reads this
#: as the default for ``conf.yml``'s ``lang`` key, so it must stay import-cheap
#: and Qt-free apart from the QLocale name lookup.
lang = resolve_language()

# ``from res import res`` is the prefix every layer uses.
res = sys.modules[__name__]
