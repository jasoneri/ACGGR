"""One-shot move of the legacy script settings into Acggr's single conf.yml.

Folds the legacy ``conf_img.yml`` into ``conf.yml`` once before ``Conf.init_conf()`` reads it:
- script sections missing from conf.yml are copied over verbatim;
- proxies is taken from the legacy file when the current value is empty/null;
- the legacy file is kept as a user backup;
- a marker makes the move idempotent, so a later conf.yml edit is never overwritten by a stale
  conf_img.yml.
"""
from __future__ import annotations

import pathlib

CURRENT_CONF_FILE = "conf.yml"
LEGACY_CONF_FILE = "conf_img.yml"
MIGRATION_MARKER = ".conf_img_merged"

#: Script sections the fold may carry over. Must stay a subset of the Conf schema
#: (core.config.conf_schema_keys): a key folded in here that the dataclass does not declare
#: would be dropped again on the next load. (nekohouse is CGS-only; acggr never reads it.)
_SCRIPT_SECTIONS = (
    "pawchive",
    "danbooru",
    "jsoneriPalacesProbe",
    "comfy",
    "ai",
    "cgs_aria2",
    "redis",
)


def _load(path: pathlib.Path, yaml) -> dict:
    if not path.exists():
        return {}
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.FullLoader)
    return data if isinstance(data, dict) else {}


def migrate_legacy_conf():
    """Merge ``conf_img.yml`` into ``conf.yml`` once. Returns the moved keys."""
    from core.config import conf_dir, yaml

    current = conf_dir / CURRENT_CONF_FILE
    legacy = conf_dir / LEGACY_CONF_FILE
    marker = conf_dir / MIGRATION_MARKER
    if not legacy.exists() or marker.exists():
        return []

    current_data = _load(current, yaml)
    legacy_data = _load(legacy, yaml)
    moved = []
    for key, value in legacy_data.items():
        if key == "proxies":
            if not current_data.get(key) and value:
                current_data[key] = value
                moved.append(key)
            continue
        if key not in current_data and key in _SCRIPT_SECTIONS:
            current_data[key] = value
            moved.append(key)

    if current_data:
        current.write_text(
            yaml.dump(current_data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    marker.write_text("conf_img.yml folded into conf.yml\n", encoding="utf-8")
    return moved
