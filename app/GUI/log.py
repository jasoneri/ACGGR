"""Process logger for the standalone script app.

Upstream, every script-window module reached the main window's loguru logger
through ``self.gui.log``; that host window no longer exists, so the loguru
logger is exposed here under one name. Message prefixes are unchanged
(``[ScriptWindow]``, ``[Danbooru]``, ``[ScriptSettings]``, ...) so existing log
greps and user-supplied log excerpts keep working.
"""
from loguru import logger


def script_logger():
    return logger
