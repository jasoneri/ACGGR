#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Relaunch acggr as a fresh process, for settings that need a new interpreter.

Only language currently needs this: widgets render qtTrId once at construction and none
implements Qt's LanguageChange/retranslateUi contract, so already-built text cannot follow a
new catalogue; rebuilding the window in-session forces a top-level HWND rebuild. A new
process replays app.bootstrap, which installs the translator for conf.lang.

Not os.execv: that replaces the current process image, so atexit hooks that hand over or shut
down the aria2 engine never run and a stale instance record is left behind. A detached child
plus a normal parent exit keeps that shutdown path intact. The child is detached on purpose
(must outlive the parent, must not inherit a console).
"""
from __future__ import annotations

import os
import sys

#: Exit code the caller passes to ``app.exec_``'s caller when a restart was
#: requested. Non-zero so a supervising shell can tell it apart from a plain
#: window close; the detached child is already running either way.
RESTART_EXIT_CODE = 75


def relaunch_command() -> list:
    """Argv that re-runs this application the way it was started.

    Three launch shapes, not interchangeable:
    - frozen bundle (PyInstaller): sys.executable is the app itself; argv is just it.
    - ``python acggr.py``: rerun sys.argv[0] with the current interpreter, drop nothing.
    - installed ``acggr`` gui-script: sys.argv[0] is the .exe shim, so the shim itself is the
      command (re-running the interpreter on it would execute a binary).
    Paths are absolutised: the child starts in an unspecified working directory.
    """
    extra = list(sys.argv[1:])
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]

    entry = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if not entry:
        # ``python -m acggr``: no script path to re-run.
        return [sys.executable, "-m", "acggr", *extra]
    if not entry.lower().endswith((".py", ".pyw")):
        # console/gui script shim (``acggr``/``acggr-cli``)
        return [entry, *extra]
    return [sys.executable, entry, *extra]


def spawn_replacement() -> int:
    """Start a detached copy of acggr; return its pid.

    Raises ``OSError`` when the platform refuses to start the process, so the
    caller can report it instead of quitting into nothing.
    """
    from PySide6.QtCore import QProcess

    command = relaunch_command()
    program, args = command[0], command[1:]
    # Qt picks the platform-appropriate detach flags; ``startDetached`` also
    # survives the parent exiting, which ``subprocess.Popen`` without flags does
    # not guarantee on Windows when the parent owns the console.
    ok, pid = QProcess.startDetached(program, args)
    if not ok:
        raise OSError(f"could not start {program!r}")
    return pid


def schedule_relaunch_and_quit(app, *, delay_ms: int = 0) -> None:
    """Quit ``app`` after spawning the replacement (optionally after a delay)."""
    from PySide6.QtCore import QTimer

    def _go():
        spawn_replacement()
        app.quit()

    if delay_ms > 0:
        QTimer.singleShot(delay_ms, _go)
    else:
        _go()
