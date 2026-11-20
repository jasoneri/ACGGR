# -*- coding: utf-8 -*-
"""Acggr run script -- the most direct way to launch the GUI.

    python acggr.py            # start the window
    python acggr.py --check    # import-only check, exit code 0, no window
    python acggr.py -h

Installed equivalents (``pyproject.toml``):

    acggr      -> app:start   (gui-script, also accepts --check)
    acggr-cli  -> app:check   (console script, import check only)

This file is deliberately a thin shell: freeze_support + delegate to
``app.start``. Startup order (resources before window, single
QApplication) lives in ``app`` so the script and the installed entry
points cannot drift apart.
"""
import sys
from multiprocessing import freeze_support
from app import start


if __name__ == "__main__":
    # The guard is mandatory: spawned children re-import this module.
    freeze_support()
    sys.exit(start())
