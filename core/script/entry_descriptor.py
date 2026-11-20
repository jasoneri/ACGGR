from __future__ import annotations

from dataclasses import dataclass

from core.website.pawchive import DEFAULT_PAWCHIVE_SAVE_PATH

#: Redis queue key the pawchive entry pushes into / pops from.
#:
#: A code constant, not ``conf.yml.pawchive.redis_key``: the value has never had
#: a settings row and never changes, so keeping it in the user file only made it
#: look configurable (and forced an index-access read that raised ``KeyError``
#: whenever the file predated the key).
PAWCHIVE_REDIS_KEY = "pawchive"


@dataclass(frozen=True, slots=True)
class ScriptEntryDescriptor:
    """
    Static identity of a Script-page entry.

    ``interface_factory`` is a declarative ``(module, class)`` ref, not the class
    itself, so building the registry imports no ``app`` module. ``db_relative_path``
    stays a plain string so this module needs no ``core`` import at all.
    """
    slug: str
    display_name: str
    attr_name: str
    route_key: str
    visibility_key: str
    visibility_payload_key: str
    interface_factory: tuple[str, str]
    icon: str
    save_path_default: str
    config_section: str
    qconfig_file: str
    asset_logical_name: str
    db_relative_path: str | None
    db_errors_flag: str
    check_skipped_flag: str
    redis_key: str
    requires_redis: bool


# Tuple order defines the Script nav order.
SCRIPT_ENTRY_DESCRIPTORS: tuple[ScriptEntryDescriptor, ...] = (
    ScriptEntryDescriptor(
        slug="pawchive",
        display_name="Pawchive",
        attr_name="pawchiveInterface",
        route_key="PawchiveInterface",
        visibility_key="pawchive_visible",
        visibility_payload_key="pawchive_visible",
        interface_factory=("app.GUI.pawchive", "PawchiveInterface"),
        icon=":/pawchive.ico",
        save_path_default=DEFAULT_PAWCHIVE_SAVE_PATH,
        config_section="pawchive",
        qconfig_file="qc_pawchive.json",
        asset_logical_name="pawchive.db",
        db_relative_path=None,  # DB now lives in conf_dir; descriptor uses None as marker
        db_errors_flag="pawchive_db_errors",
        check_skipped_flag="pawchive_data_check_skipped",
        redis_key=PAWCHIVE_REDIS_KEY,
        requires_redis=False,
    ),
)

DEFAULT_SCRIPT_ENTRY_SLUG = SCRIPT_ENTRY_DESCRIPTORS[0].slug


def get_script_entry(slug: str) -> ScriptEntryDescriptor:
    for descriptor in SCRIPT_ENTRY_DESCRIPTORS:
        if descriptor.slug == slug:
            return descriptor
    raise ValueError(f"unknown script entry slug: {slug!r}")
