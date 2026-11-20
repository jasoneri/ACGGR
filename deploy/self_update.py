#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Apply a queued acggr self-update, then relaunch acggr.

Started by core.updater.launch_update with the payload JSON path. stdlib only and imports nothing
from core/app/ui: it must work while the environment it runs from is being replaced.

Sequence (each step logged): 1) read the payload (malformed → exit 2); 2) wait for the parent GUI
to exit (poll 200 ms, cap 120 s) — replacing site-packages while it holds python.exe/*.pyd fails
halfway on Windows; 3) run the install command, appending to the log; 4) on success start the
relaunch command detached; 5) on failure keep the console readable when attached.

Exit codes: 0 ok, 1 install failed, 2 bad payload, 3 nothing to install, 9 unexpected.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

#: A GUI that is stuck behind a modal dialog must not block the update forever.
WAIT_LIMIT = 120.0
WAIT_INTERVAL = 0.2

EXIT_OK = 0
EXIT_INSTALL_FAILED = 1
EXIT_BAD_PAYLOAD = 2
EXIT_NOTHING_INSTALLED = 3
EXIT_UNEXPECTED = 9

REQUIRED_KEYS = ("parent_pid", "install", "relaunch", "log", "version")

#: ``STILL_ACTIVE`` from ``GetExitCodeProcess``.
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87

#: Windows process-creation flags: no inherited console, own Ctrl+C group.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(log_path: pathlib.Path, message: str) -> None:
    """Append one timestamped line to the transcript, and echo it.

    Echoing matters on Windows: the helper owns a console the user is looking at,
    and the install command itself writes to the log file instead of the console.
    A log that cannot be opened (deleted temp dir, read-only profile) must not
    abort the update, so the failure is reported inline.
    """
    line = f"[{_stamp()}] {message}"
    print(line, flush=True)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        print(f"    (could not write {log_path}: {exc})", flush=True)


def _pid_alive_windows(pid: int) -> bool | None:
    """``psutil`` when importable, otherwise a direct ``OpenProcess`` probe.

    ``psutil`` is a project dependency but *not* a stdlib one, and this helper
    may outlive the environment that provided it -- hence the ctypes fallback.
    ``None`` means "this platform could not answer", never "dead".
    """
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:
            pass

    try:
        import ctypes
    except ImportError:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    except (AttributeError, OSError):
        return None
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return False  # no process with that pid
        if error == _ERROR_ACCESS_DENIED:
            return True  # exists, just not ours to query
        return None
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool | None:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return None
    return True


def _wait_for_exit(pid: int, log_path: pathlib.Path) -> None:
    """Block until *pid* is gone, at most ``WAIT_LIMIT`` seconds."""
    if pid <= 0:
        _log(log_path, "no parent process recorded; continuing immediately")
        return
    _log(log_path, f"waiting for parent {pid} to exit (limit {WAIT_LIMIT:.0f}s)")
    deadline = time.monotonic() + WAIT_LIMIT
    while time.monotonic() < deadline:
        alive = _pid_alive(pid)
        if alive is None:
            # No probe on this platform: degrade to the timeout cap rather than
            # grabbing files out from under a process we cannot observe.
            _log(log_path, f"cannot probe pid {pid}; waiting the full {WAIT_LIMIT:.0f}s cap")
            time.sleep(WAIT_LIMIT)
            return
        if not alive:
            _log(log_path, f"parent {pid} is gone")
            return
        time.sleep(WAIT_INTERVAL)
    _log(log_path, f"parent {pid} still alive after {WAIT_LIMIT:.0f}s; continuing anyway")


def _install(install: list, log_path: pathlib.Path, home: pathlib.Path) -> int:
    """Run the install command, its stdout/stderr appended to the transcript."""
    command = subprocess.list2cmdline(install) if os.name == "nt" else " ".join(install)
    _log(log_path, f"install start: {command}")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== install output ({_stamp()}) ===\n")
        handle.flush()
        completed = subprocess.run(
            install,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(home),
            check=False,
        )
    _log(log_path, f"install exit={completed.returncode}")
    return completed.returncode


def _relaunch(relaunch: list, log_path: pathlib.Path, home: pathlib.Path) -> None:
    """Start the new build detached: it has to outlive this helper's console.

    The new PID is logged so a supervising harness can identify *its own* window:
    matching by title would also match a user's already-running instance.
    """
    _log(log_path, f"relaunching: {relaunch[0]}")
    # DEVNULL, not inherited handles: with DETACHED_PROCESS the child has no
    # console, so ``sys.stdout`` would be ``None`` and any startup ``print``
    # would crash the freshly installed build before it can show a window.
    kwargs = {
        "cwd": str(home),
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(relaunch, **kwargs)
    _log(log_path, f"relaunched pid={process.pid}")


def _pause_if_console() -> None:
    """Hold the window open on failure -- but only when a human is looking."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return
    except (ValueError, OSError):
        return
    try:
        input("update failed; press Enter to close this window...")
    except (EOFError, KeyboardInterrupt):
        pass


def _run(payload: dict) -> int:
    log_path = pathlib.Path(str(payload["log"]))
    home = pathlib.Path.home()
    version = str(payload.get("version") or "")
    parent_pid = int(payload.get("parent_pid") or 0)
    install = [str(part) for part in (payload.get("install") or ())]
    relaunch = [str(part) for part in (payload.get("relaunch") or ())]

    _log(log_path, f"self-update start (version={version}, helper pid={os.getpid()})")
    _wait_for_exit(parent_pid, log_path)

    if not install:
        _log(log_path, "no install command for this kind of installation; nothing to do")
        return EXIT_NOTHING_INSTALLED

    code = _install(install, log_path, home)
    if code != 0:
        _log(log_path, f"update failed (exit {code}), see {log_path}")
        _pause_if_console()
        return EXIT_INSTALL_FAILED

    if not relaunch:
        _log(log_path, "install done; no relaunch command supplied")
        return EXIT_OK
    _relaunch(relaunch, log_path, home)
    _log(log_path, "update finished")
    return EXIT_OK


def main(argv: list | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(f"usage: {pathlib.Path(sys.argv[0]).name} <payload.json | ->", file=sys.stderr)
        return EXIT_BAD_PAYLOAD

    if args[0] == "-":
        # stdin hand-off: used when the caller could not write the payload file
        # (locked/unwritable log dir). The app writes the JSON and closes the
        # pipe before it exits, so one blocking read is safe.
        raw = sys.stdin.buffer.read()
        payload_source = "stdin"
    else:
        payload_path = pathlib.Path(args[0])
        try:
            raw = payload_path.read_text(encoding="utf-8").encode("utf-8")
            payload_source = str(payload_path)
        except (OSError, ValueError) as exc:
            print(f"self-update: cannot read payload {payload_path}: {exc}", file=sys.stderr)
            return EXIT_BAD_PAYLOAD
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        print(f"self-update: cannot parse payload from {payload_source}: {exc}", file=sys.stderr)
        return EXIT_BAD_PAYLOAD
    if not isinstance(payload, dict):
        print(f"self-update: payload {payload_path} is not a JSON object", file=sys.stderr)
        return EXIT_BAD_PAYLOAD
    missing = [key for key in REQUIRED_KEYS if key not in payload]
    if missing:
        print(f"self-update: payload missing {', '.join(missing)}", file=sys.stderr)
        return EXIT_BAD_PAYLOAD

    try:
        return _run(payload)
    # The whole point of this helper is that a failed update is *reported*: any
    # escape here must land in the transcript before the process disappears.
    except Exception as exc:
        _log(pathlib.Path(str(payload.get("log") or "self_update.log")),
             f"self-update crashed: {exc!r}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())