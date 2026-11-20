#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Acggr configuration layer.

Acggr is the script toolset only: one config class (``Conf``, with
``ScriptConf`` kept as a compatibility alias) and one user file
``conf_dir/conf.yml``.

``conf_dir`` is intentionally its own directory, so this app never shares or
fights over one yaml with another install on the same machine.
"""
import os
import pickle
import pathlib as p
import typing as t
from dataclasses import dataclass, asdict, field, fields as dc_fields

import yaml
from loguru import logger as lg

from res import app_config_dir
from deploy import curr_os

exc_p = ori_path = p.Path(__file__).parent.parent.parent
env = os.environ.copy()

# ``app_config_dir()`` lives in res (the leaf package) so this conf_dir and
# ``res.lang`` (the Qt translation default) share ONE env-based resolver instead
# of each rolling its own. Never go back to QStandardPaths: it folds ``QCoreApplication.applicationName()`` into the result, which made the
# config root depend on whether a QApplication existed at import time.
conf_dir = app_config_dir()
conf_dir.mkdir(parents=True, exist_ok=True)
qconfig_dir = conf_dir.joinpath("qconfig")
qconfig_dir.mkdir(parents=True, exist_ok=True)


def acggr_log_dir() -> p.Path:
    """Single source of truth for Acggr's per-user log directory (outside the repo)."""
    return conf_dir.joinpath("log")


def acggr_log_file() -> p.Path:
    """The rolling application log file inside :func:`acggr_log_dir`."""
    return acggr_log_dir().joinpath("acggr.log")


yaml.warnings({'YAMLLoadWarning': False})

#: The redis endpoint used when ``conf.yml`` says nothing. Redis is an external
#: optional service (see ``core.script.redis_reachable``), so its address is a
#: plain default rather than something the user has to supply before first run.
DEFAULT_REDIS_CONF: dict = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 0,
    "password": "",
}

#: Sections that hold user-editable dicts; ``null`` in the yaml means "unset",
#: and every reader indexes into them, so they must not stay ``None``.
_DICT_SECTIONS = ("pawchive", "danbooru", "jsoneriPalacesProbe", "comfy", "ai",
                  "cgs_aria2", "redis")

#: Default value of every section, by key. Kept next to the schema that owns them so a
#: new field cannot be added without touching this table.
DEFAULT_CONF_SECTIONS: dict = {
    "pawchive": {"cookie": ""},
    "danbooru": {"save_type": None, "download_concurrency": 5, "view_ratio": 60},
    "jsoneriPalacesProbe": {"token": ""},
    "comfy": {"host": ""},
    "ai": {"provider": {"url": None, "key": None, "model": None}},
    "cgs_aria2": {"proxy": ""},
    "redis": dict(DEFAULT_REDIS_CONF),
}

#: Whole ``conf.yml`` payload for a never-configured install, in write order.
DEFAULT_CONF_PAYLOAD: dict = {
    "sv_path": None,
    "proxies": None,
    "lang": "",
    "bg_path": "",
    **DEFAULT_CONF_SECTIONS,
}

#: Sections whose defaults are filled in per key (nested for ``ai``).
_DEFAULTED_SECTIONS = tuple(DEFAULT_CONF_SECTIONS)

#: Nested keys that are code constants now, dropped on load: the top-level prune cannot see
#: inside a section and save() writes asdict(self) back, so a stale value would otherwise
#: survive every save forever.
_DEPRECATED_NESTED_KEYS: dict = {"pawchive": ("redis_key",)}


def default_conf_text() -> str:
    """Serialised ``conf.yml`` for a never-configured install."""
    return yaml.dump(DEFAULT_CONF_PAYLOAD, allow_unicode=True, sort_keys=False)


def is_default_conf(path) -> bool:
    """True when ``path`` still holds the generated default (no user edits).

    Replaces the old byte comparison against ``res/conf_sample.yml``. Semantic
    comparison instead of bytes, so a re-dump (different key order, quoting, or
    line wrapping) of the same values still counts as untouched.
    """
    try:
        return yaml.safe_load(p.Path(path).read_text(encoding="utf-8")) == DEFAULT_CONF_PAYLOAD
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return False


def conf_schema_keys() -> frozenset:
    """Top-level ``conf.yml`` keys Acggr itself owns.

    ``Conf`` is the schema: anything it does not declare belongs to another
    program (the CGS install this app was extracted from carries
    ``cv_proj_path`` / ``clip_db`` / ``rv_script`` / ``webdav`` / ``ags_file``
    and a dozen more). Those must never be adopted, persisted, or copied over by
    the CGS importer -- see :func:`core.config.import_cgs.import_from_cgs`.
    """
    return frozenset(field.name for field in dc_fields(Conf)) | {"cookies"}


class ConfCookie:
    """Cookie配置管理类，承担所有cookie的缓存、管理、展示和保存"""
    support_key = ['pawchive']
    pickle_file = conf_dir.joinpath("cookies.pkl")

    def __init__(self, cookies_data=None):
        self.cache = cookies_data or self.empty_cache()  # 所有cookie类型的缓存
        self.current_type = self.support_key[0]
        self.load_from_pickle()

    def empty_cache(self):
        return {cookie_type: {} for cookie_type in self.support_key}

    def switch(self, cookie_type):
        """切换当前选中的cookie类型"""
        if cookie_type in self.support_key:
            self.current_type = cookie_type
            return True
        return False

    def show(self):
        """返回当前选中的cookie配置用于显示"""
        return self.cache.get(self.current_type, {})

    def update_current(self, cookie_data):
        if isinstance(cookie_data, dict):
            self.cache[self.current_type] = cookie_data
        else:
            self.cache[self.current_type] = {}
        self.save_to_pickle()

    def save_to_pickle(self):
        with open(self.pickle_file, 'wb') as f:
            pickle.dump(self.cache, f)

    def load_from_pickle(self):
        if self.pickle_file.exists():
            with open(self.pickle_file, 'rb') as f:
                self.cache = pickle.load(f)
            return True
        return False

    def get(self, name):
        return self.cache.get(name, {})


class BaseConf:
    file = None

    def __init__(self, path=None, iname=None):
        self.init_conf()

    def init_conf(self):  # 脱敏，储存路径和代理等用外部文件读
        ...

    @classmethod
    def duel_conf(cls, ori_conf_yml, iname=None):
        """One file, one singleton: ``iname`` is accepted and ignored.

        Both spellings (``Conf()`` and ``ScriptConf(iname="img")``) must resolve to the SAME
        object — otherwise whoever is constructed last owns the schema again.
        """
        return "_instance", conf_dir.joinpath(ori_conf_yml.name)

    def __new__(cls, *args, path: t.Optional[p.Path] = None, iname: str = None, **kwargs):
        _ori_conf_yml = (path or ori_path).joinpath("conf.yml")
        _instance, file = cls.duel_conf(_ori_conf_yml, iname)
        if not hasattr(cls, _instance):
            setattr(cls, _instance, object.__new__(cls))
            getattr(cls, _instance).file = file
        return getattr(cls, _instance)


@dataclass
class Conf(BaseConf):
    """The one ``conf.yml`` schema.

    Every field here is read by some acggr module -- there is deliberately no
    field kept "just in case", because ``init_conf`` prunes anything else out of
    the file (see :func:`conf_schema_keys`).
    """
    sv_path: t.Union[p.Path, str] = curr_os.default_sv_path
    proxies: list = field(default_factory=list)
    lang: str = ''
    #: Random-background root for the text-browser pages (``BgMgr``). Empty =
    #: fall back to ``<install>/custom``.
    bg_path: str = ''
    pawchive: dict = field(default_factory=dict)
    danbooru: dict = field(default_factory=dict)
    jsoneriPalacesProbe: dict = field(default_factory=dict)
    comfy: dict = field(default_factory=dict)
    ai: dict = field(default_factory=dict)
    cgs_aria2: dict = field(default_factory=dict)
    redis: dict = field(default_factory=dict)
    cookies = None

    def __init__(self, path=None, iname=None):
        self.init_conf()

    @staticmethod
    def _merge_section_defaults(yml_config: dict, sample_config: dict, section_name: str) -> dict:
        section_defaults = (sample_config.get(section_name) or {}).copy()
        section_config = (yml_config.get(section_name) or {}).copy()
        if section_name == "ai":
            provider_defaults = (section_defaults.get("provider") or {}).copy()
            provider_config = (section_config.get("provider") or {}).copy()
            for key, value in provider_defaults.items():
                provider_config.setdefault(key, value)
            section_config["provider"] = provider_config
            for key, value in section_defaults.items():
                if key == "provider":
                    continue
                section_config.setdefault(key, value)
        else:
            for key, value in section_defaults.items():
                section_config.setdefault(key, value)
        yml_config[section_name] = section_config
        return yml_config

    def init_conf(self):
        from core.config.legacy_conf import migrate_legacy_conf

        # No shipped template any more: a fresh install is written straight from
        # ``DEFAULT_CONF_PAYLOAD`` so there is exactly one definition of "what a
        # default looks like" (see ``DEFAULT_CONF_SECTIONS``).
        if not self.file.exists():
            self.file.write_text(default_conf_text(), encoding="utf-8")
        # After the default file exists, so a leftover ``conf_img.yml`` folds into a
        # complete file instead of defining a partial schema of its own.
        migrate_legacy_conf()
        with open(self.file, 'r', encoding='utf-8') as fp:
            yml_config = yaml.load(fp.read(), Loader=yaml.FullLoader) or {}
        for section_name in _DEFAULTED_SECTIONS:
            yml_config = self._merge_section_defaults(yml_config, DEFAULT_CONF_SECTIONS, section_name)
        # Foreign top-level keys are dropped, not adopted: ``conf.yml`` is
        # acggr's own file, and a config imported from the upstream program
        # carries a dozen keys nothing here reads. Adopting them would make
        # ``asdict`` write them back on every save, which is how they survived
        # the migration in the first place. The prune is persisted once, so the
        # leftovers actually leave the file instead of lingering until the next
        # settings save (nothing here ever reads them back).
        schema = conf_schema_keys()
        dirty = False
        foreign = sorted(key for key in yml_config if key not in schema)
        if foreign:
            lg.info(f"[conf] dropping non-acggr keys from {self.file.name}: {foreign}")
            for key in foreign:
                yml_config.pop(key, None)
            dirty = True
        for section_name, key_names in _DEPRECATED_NESTED_KEYS.items():
            section = yml_config.get(section_name)
            if not isinstance(section, dict):
                continue
            for key in key_names:
                if section.pop(key, None) is not None:
                    dirty = True
        if dirty:
            try:
                self.file.write_text(
                    yaml.dump(yml_config, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
            except OSError as exc:  # a read-only config dir must not break launch
                lg.warning(f"[conf] could not rewrite {self.file.name}: {exc}")
        # Every top-level key the schema declares must exist before the loop
        # below reads it back: ``Conf.__init__`` is overridden to call
        # ``init_conf()`` only, so the dataclass field defaults are never applied
        # to the instance on their own. A partial ``conf.yml`` -- a hand-written
        # one, or CGS's, whose unknown keys are pruned on import -- would
        # otherwise raise ``AttributeError`` on the first missing key and take
        # the whole process down at import time.
        for key, default in DEFAULT_CONF_PAYLOAD.items():
            yml_config.setdefault(key, default)
        # ``""`` is the "follow the system" state, and it is the default: a fresh
        # install must not pin today's OS locale into conf.yml, otherwise the
        # picker's first item can never come back into effect on its own.
        yml_config['lang'] = yml_config.get('lang') or ''
        for k, v in yml_config.items():
            # 跳过cookie相关字段，由ConfCookie处理
            if k != "cookies":
                setattr(self, k, v if v is not None else getattr(self, k, None))
        self.sv_path = p.Path(self.sv_path)
        for name in _DICT_SECTIONS:
            if getattr(self, name, None) is None:
                setattr(self, name, {})
        if self.proxies is None:
            self.proxies = []
        self.cookies = ConfCookie()

    def update(self, **kwargs):
        """Persist ``kwargs`` (plus every other field) to ``conf.yml``.

        The file is rewritten from the schema, not merged into whatever is on
        disk: a key this class does not declare is another program's, and
        carrying it forward is exactly how the upstream leftovers survived.
        """
        for k, v in kwargs.items():
            if k == "sv_path":
                # ``sv_path: null`` is a real state ("not chosen yet"), and
                # ``init_conf`` resolves it without writing it back. Settings
                # callers hand the whole loaded file straight to ``update``, so a
                # bare ``Path(None)`` raised TypeError here and surfaced as "save
                # failed" on every autosave of an untouched install. Keep the
                # resolved path instead.
                if v is None or not str(v).strip():
                    continue
                setattr(self, k, p.Path(v))
                continue
            setattr(self, k, v)
        self.save()

    def save(self):
        """Rewrite ``conf.yml`` from the schema (cookies live in their pickle)."""
        props = asdict(self)
        props["sv_path"] = str(props["sv_path"])
        self.file.write_text(
            yaml.dump(props, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )


#: Compatibility alias — the script layer and ``core.config.qc`` still spell it
#: ``ScriptConf``; it is the very same class, hence the very same singleton.
ScriptConf = Conf
