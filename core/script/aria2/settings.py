from __future__ import annotations

from typing import Any

import yaml

from core.script import conf as script_conf
from core.script.aria2.conf import normalize_proxy
from core.script.aria2.lock import DEFAULT_RPC_PORT

#: Rows kept in acggr's own ``download_history`` table.
DEFAULT_HISTORY_LIMIT = 1000
MAX_HISTORY_LIMIT = 100_000


def load_script_yaml() -> dict[str, Any]:
    if not script_conf.file.exists():
        return {}
    with open(script_conf.file, "r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle.read()) or {}


def get_cgs_aria2_section(config_data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = config_data if config_data is not None else load_script_yaml()
    section = data.get("cgs_aria2")
    return dict(section) if isinstance(section, dict) else {}


def get_proxy(config_data: dict[str, Any] | None = None) -> str:
    return normalize_proxy(get_cgs_aria2_section(config_data).get("proxy"))


def get_rpc_port(config_data: dict[str, Any] | None = None) -> int:
    """Fixed RPC port; a bad configured value is a config error, not a fallback."""
    raw = get_cgs_aria2_section(config_data).get("rpc_port")
    if raw in (None, ""):
        return DEFAULT_RPC_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cgs_aria2.rpc_port must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"cgs_aria2.rpc_port out of range: {port}")
    return port


def singleton_enabled(config_data: dict[str, Any] | None = None) -> bool:
    return bool(get_cgs_aria2_section(config_data).get("singleton_enabled", True))


def _coerce_history_limit(raw: object) -> int:
    """Any unusable value collapses to the default; the range is 0..MAX."""
    if isinstance(raw, bool):
        # ``bool`` is an ``int`` subclass: int(True) would silently mean a
        # one-row history instead of the default ceiling.
        return DEFAULT_HISTORY_LIMIT
    try:
        limit = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_LIMIT
    if not 0 <= limit <= MAX_HISTORY_LIMIT:
        return DEFAULT_HISTORY_LIMIT
    return limit


def get_history_limit(config_data: dict[str, Any] | None = None) -> int:
    """Rows kept in acggr's own download history; ``0`` disables history.

    The retired boolean ``cgs_aria2.keep_history`` is migrated on read: on
    meant "keep every finished download" (now the default ceiling), off meant
    "keep nothing".
    """
    section = get_cgs_aria2_section(config_data)
    raw = section.get("history_limit")
    if raw is None:
        legacy = section.get("keep_history")
        if isinstance(legacy, bool):
            return DEFAULT_HISTORY_LIMIT if legacy else 0
        return DEFAULT_HISTORY_LIMIT
    return _coerce_history_limit(raw)


def set_history_limit(value: object, *, config_data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(config_data) if config_data is not None else load_script_yaml()
    section = get_cgs_aria2_section(data)
    section["history_limit"] = _coerce_history_limit(value)
    # The on/off switch is gone: leaving it behind would resurrect the old
    # force-save semantics on an older build reading this conf.
    section.pop("keep_history", None)
    data["cgs_aria2"] = section
    return data


def set_proxy(proxy: object, *, config_data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(config_data) if config_data is not None else load_script_yaml()
    section = get_cgs_aria2_section(data)
    section["proxy"] = normalize_proxy(proxy)
    data["cgs_aria2"] = section
    return data
