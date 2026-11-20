# -*- coding: utf-8 -*-
"""Acggr version and other process-wide constants.

``__version__`` below is the single source of truth for the version.
``pyproject.toml`` declares ``dynamic = ["version"]`` and hatchling reads this
file through ``[tool.hatch.version] path = "core/variables.py"``, so the built
wheel, runtime and UI resolve the same value without a second copy.
"""

__version__ = "0.2.0-beta.0"

APP_NAME = "acggr"
# Window title, e.g. ``ACGGR-py v0.1.0``. ``-py`` is the distribution suffix from
# ``pyproject.toml`` (``[project].name = "acggr-py"``); both halves are derived so
# a version bump never leaves a stale number hard-coded in the title.
APP_TITLE = f"{APP_NAME.upper()}-py v{__version__}"

# Script-window branding. ``:/logo.png`` is the qrc alias of the repo-root
# ``logo.png`` (see ``res/app.qrc``); there is no second copy under ``res/``.
APP_LOGO_RESOURCE = ":/logo.png"

# Language code -> picker label. *Re-exported* from ``res.lang_data`` instead of
# being spelled out here, so the labels cannot drift from the whitelist
# (``SUPPORTED_LANGUAGES`` / ``DEFAULT_LANGUAGE``) the resolver reads. ``res`` is
# a leaf package that imports nothing from ``core``, so there is no cycle.
from res.lang_data import LANGUAGE_NAMES as LANG  # noqa: E402

# --- Script-only constants -------------------------------------------------
ACGG_DOC = "https://cgs.101114105.xyz"
JSONERI_PALACES_PROBE_API_URL = "https://jps.101114105.xyz"

# aria2 bootstrap download sources (GitHub preset primary + ImgBed fallback).
GITHUB_PRESET_BASE = "https://github.com/jasoneri/ComicGUISpider/releases/download/preset"
ASSETS_FALLBACK = "https://img-cgs.101114105.xyz/file/res"
IMGBED_ASSET_OBJECTS = {
    "aria2-manifest.json": "1786799417488_aria2-manifest.json",
    "aria2c-win-amd64.exe": "1786799424964_aria2c-win-amd64.exe",
    "aria2c-macos-arm64": "1786799422383_aria2c-macos-arm64",
}
