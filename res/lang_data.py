#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Language-dependent values that are NOT translatable strings.

Two kinds of language dependence exist in acggr and only one of them belongs in
the ``res/i18n/<lang>.yml`` wording tables:

- user-visible text -> Qt (``tr()`` / ``QCoreApplication.translate``), wording
  from ``res/i18n/<lang>.yml`` compiled to ``<lang>.qm``;
- protocol-level or per-site values -> this module, picked by language code.

``ACCEPT_LANGUAGE`` is an HTTP request header, so it must never be "translated"
(it has to stay a valid RFC 9110 header). ``PROMOTE_URL`` points at two
different sites per language, so it is not a translation of one msgid either.

Keeping them here means ``res`` stays the single home for per-language data and
the catalogue files contain nothing but real UI text.
"""
import typing as t

from PySide6.QtCore import QLocale

#: The language the product was originally written in, kept as the ``.ts``
#: ``sourcelanguage`` attribute so Qt Linguist tools know the reference locale.
#: It ships a normal catalogue like every other language: the code carries
#: translation IDs, not sentences, so nothing is free.
SOURCE_LANGUAGE = "zh_CN"

#: Languages the UI offers -- the whitelist. ``res.resolve_language`` returns a
#: member of this tuple or ``DEFAULT_LANGUAGE``, never a "closest" catalogue:
#: any other locale (``zh_TW``, ``ja_JP``, ...) routes to ``DEFAULT_LANGUAGE``.
#: A ``<lang>.yml`` that is not listed here ships wording but is not offered.
SUPPORTED_LANGUAGES: t.Tuple[str, ...] = ("en_US", SOURCE_LANGUAGE)

#: Fallback when the system locale has no shipped catalogue. Also what a
#: non-supported locale routes to.
DEFAULT_LANGUAGE = "en_US"

#: How each supported code is spelled in the picker. This is the one mapping
#: table; ``core.variables.LANG`` re-exports it so no layer keeps a second copy
#: that could drift from ``SUPPORTED_LANGUAGES``.
LANGUAGE_NAMES: t.Dict[str, str] = {
    "en_US": "English",
    "zh_CN": "简体中文",
}

ACCEPT_LANGUAGE: t.Dict[str, str] = {
    "zh_CN": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    "en_US": "en-US,en;q=0.9",
}

PROMOTE_URL: t.Dict[str, str] = {
    "zh_CN": "https://love.p6m6.com/#/register?code=NDrxqHw9",
    "en_US": "https://hxlm.io/#/register?code=sZWBMyum",
}


def system_language() -> str:
    """System locale name, e.g. ``zh_CN``."""
    return QLocale.system().name()


def pick(table: t.Mapping[str, str], lang: t.Optional[str] = None) -> str:
    """Look up ``table`` for ``lang`` (default: the resolved ``res.lang``).

    Uses the same whitelist as the UI: a code that is not supported (``zh_TW``)
    falls back to ``DEFAULT_LANGUAGE`` rather than to a same-family entry, so
    per-language data and wording can never disagree about which locale is live.
    """
    import res

    resolved = res.resolve_language(lang or _current_lang())
    return table.get(resolved) or table[DEFAULT_LANGUAGE]


def _current_lang() -> str:
    # Imported lazily: ``res/__init__`` imports this module at import time.
    import res

    return res.lang
