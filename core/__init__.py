# -*- coding: utf-8 -*-
"""Acggr kernel layer.

Framework-independent logic: the script section (``core/script``) plus the
small set of leaf modules it depends on (config / network / sql /
website/pawchive / website/core).

Import boundary (see ``harness/docs/import-layout.md``):
- must NOT import ``app`` or ``ui``;
- ``res`` is allowed for bundled static resources and the Qt i18n bundle.

This layer keeps only the script-reachable surface: mainline site-preprocess
helpers, spider/queue plumbing and app-side helpers are out of scope.
"""
import re as _re
import asyncio
import hashlib
import contextlib
from functools import lru_cache

from core.variables import __version__, APP_NAME, APP_TITLE  # noqa: F401
from core.config import *  # noqa: F401,F403
from core.core import *  # noqa: F401,F403

temp_p = ori_path.joinpath("__temp")  # noqa: F405
temp_p.mkdir(exist_ok=True)

conf = Conf()  # noqa: F405


@lru_cache(maxsize=1)
def get_httpx_verify():
    import platform
    import ssl

    if platform.system() == "Windows":
        with contextlib.suppress(ssl.SSLError):
            ctx = ssl.create_default_context()
            if ctx.get_ca_certs():
                return ctx
    return True


class PresetHtmlEl:
    _rule = ['em', ]
    _compile = '|'.join(map(lambda _: f"<[/]?{_}>", _rule)) + "|&nbsp;"
    regex = _re.compile(_compile)

    @classmethod
    def sub(cls, string: str) -> str:
        return cls.regex.sub('', string).rstrip('.')


minus_regex = _re.compile(r'^-\d+$')


def transfer_input(_input: str) -> list:
    """6 -> [6] | 1+3+5 -> [1,3,5] | 4-6 -> [4,5,6] | 1+4-6 -> [1,4,5,6]."""
    ranges = _input.split('-')
    if len(ranges) == 1:
        return [int(_input)]
    return list(range(int(ranges[0]), int(ranges[1]) + 1))


def fin_transfer(_elect, _results_keys) -> list:
    if _elect == '0':
        return _results_keys
    if isinstance(_elect, list):
        return _elect
    if bool(minus_regex.search(_elect)):
        return sorted(_results_keys)[int(_elect):]
    return transfer_input(_elect)


def md5(_str):
    return hashlib.md5(_str.encode()).hexdigest()


def get_loop():
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


def acggr_log_dir():
    """One source of truth for Acggr's per-user log directory."""
    from core.config import acggr_log_dir as _acggr_log_dir

    return _acggr_log_dir()


def install_qfluentwidgets_notice_filter():
    """Drop the import-time QFluentWidgets Pro notice without muting other prints."""
    import builtins
    import sys

    current_print = builtins.print
    if getattr(current_print, "_acggr_qfluent_notice_filter", False):
        return

    def filtered_print(*args, **kwargs):
        output = kwargs.get("file")
        if output is None or output is sys.stdout:
            sep = kwargs.get("sep")
            if sep is None:
                sep = " "
            text = sep.join(str(arg) for arg in args)
            if "QFluentWidgets Pro is now released" in text and "qfluentwidgets.com/pages/pro" in text:
                return
        return current_print(*args, **kwargs)

    filtered_print._acggr_qfluent_notice_filter = True
    builtins.print = filtered_print


# Install at kernel-import time so every entry surface is covered, not just
# ``app``: ``import core.config.qc`` / ``ui.core.theme`` each
# pull qfluentwidgets (which prints the banner at import) and are legitimate
# standalone entry points for probes and scripts.
install_qfluentwidgets_notice_filter()
