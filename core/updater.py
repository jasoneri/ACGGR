#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Self-update plumbing: "how is this build installed, and how do I replace it".

core.update answers *whether* a newer release exists; this module turns the running layout into
the one command that upgrades it, and hands it to a helper process that waits for us to exit.
A helper process is required on Windows: the running interpreter holds open handles on its .exe,
pythonXY.dll and every mapped .pyd, so an in-place install fails halfway.
Install kind matters: uv tool / pipx own their metadata (a plain pip install corrupts it; a
missing uv is reported unknown, not fallen back); editable/source checkout has no wheel to
upgrade ("update yourself"); plain venv uses python -m pip install -U.
Nothing here imports Qt / app / the network client; the decision table is unit-testable.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import pathlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "InstallTarget",
    "detect_install",
    "payload_for",
    "launch_update",
    "DEFAULT_DIST",
]

#: ``[project].name`` in ``pyproject.toml``. The distribution name is what pip,
#: uv and pipx resolve; it is *not* the import name (``app``/``core``).
DEFAULT_DIST = "acggr-py"
#: Metadata candidates in probe order: the real distribution first, then the
#: bare project name some older local installs registered.
DIST_CANDIDATES = (DEFAULT_DIST, "acggr")

#: The payload is written next to the log, so a user reporting a failed update
#: can hand over both the transcript and the exact command that was attempted.
PAYLOAD_NAME = "update_payload.json"

_UV_TOOL_MARKER = "/uv/tools/"
_UV_TOOL_ENV = "UV_TOOL_DIR"
_PIPX_MARKER = "/pipx/venvs/"


@dataclass(frozen=True, slots=True)
class InstallTarget:
    """The installation this process runs from, and how to upgrade it.

    ``kind`` is the only thing the UI has to branch on: ``uv-tool`` / ``pipx`` /
    ``pip`` can be upgraded in place, while ``source`` and ``unknown`` cannot --
    the caller shows ``reason`` instead of an "update now" button.
    """

    kind: str
    dist: str
    tool: str
    version: str
    install_argv: tuple[str, ...]
    interpreter: str
    reason: str = ""

    @property
    def upgradable(self) -> bool:
        return self.kind in {"uv-tool", "pipx", "pip"} and bool(self.install_argv)


def _norm(path: object) -> str:
    """Forward-slash form of *path*.

    Layout detection is string work (installed paths arrive with ``\\`` on
    Windows and ``/`` elsewhere), so exactly one normalisation happens at the
    boundary and every comparison below sees the same shape.
    """
    return str(path).replace("\\", "/")


def _which(name: str) -> str | None:
    """``shutil.which`` behind a seam the self-check can drive.

    The uv/pipx launcher is frequently *not* on the app's PATH (uv lives in its
    own toolchain dir and the app was started from a shortcut), which is a
    supported outcome -- see ``reason`` -- not an error to work around.
    """
    return shutil.which(name)


def _dist_info() -> tuple[str, dict]:
    """Installed distribution name plus its PEP 610 ``direct_url.json`` payload.

    The *metadata* name wins over the string we probed with: a wheel that
    declares ``Name: acggr`` must be upgraded as ``acggr``, or ``uv tool install``
    would install a second distribution next to the running one. When nothing is
    installed at all (bare checkout on ``sys.path``), fall back to the project
    name from ``pyproject.toml``.
    """
    for candidate in DIST_CANDIDATES:
        try:
            dist = importlib.metadata.distribution(candidate)
        except importlib.metadata.PackageNotFoundError:
            continue
        except Exception:  # broken metadata must never break update detection
            continue
        try:
            name = str(dist.metadata.get("Name") or "").strip()
        except Exception:
            name = ""
        payload: dict = {}
        try:
            raw = dist.read_text("direct_url.json")
        except Exception:
            raw = None
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict):
                payload = data
        return (name or candidate), payload
    return DEFAULT_DIST, {}


def _is_editable(direct_url: dict) -> bool:
    """True for a PEP 610 editable install (``pip/uv install --editable``)."""
    info = direct_url.get("dir_info")
    return isinstance(info, dict) and bool(info.get("editable"))


def _tool_from_layout(exe_path: str, markers: tuple[str, ...]) -> str | None:
    """Registered tool name for *exe_path*, or ``None`` when the layout misses.

    ``<marker><tool>/Scripts/python.exe`` (Windows) and
    ``<marker><tool>/bin/python`` (POSIX) both place the tool directory right
    after the marker, so the segment after the last matching marker is the name.
    Case is preserved: uv/pipx register the name the user typed.
    """
    lowered = exe_path.lower()
    for marker in markers:
        index = lowered.find(marker.lower())
        if index >= 0:
            return exe_path[index + len(marker):].split("/", 1)[0]
    return None


def _uv_tool_name(exe_path: str) -> str | None:
    """uv tool name, honouring a relocated ``UV_TOOL_DIR``.

    The default ``%APPDATA%\\uv\\tools`` renders as ``/uv/tools/`` in the
    normalised path, but users (and CI) relocate it, in which case the env var is
    the only prefix that matches.
    """
    markers = [_UV_TOOL_MARKER]
    tool_dir = str(os.environ.get(_UV_TOOL_ENV, "") or "").strip()
    if tool_dir:
        markers.insert(0, _norm(tool_dir).rstrip("/") + "/")
    return _tool_from_layout(exe_path, tuple(markers))


def _pipx_tool_name(exe_path: str) -> str | None:
    return _tool_from_layout(exe_path, (_PIPX_MARKER,))


def detect_install(version: str, *, exe: str | None = None) -> InstallTarget:
    """Decide how *version* should be installed for the running environment.

    ``exe`` is injectable so tests (and diagnostics) can describe a layout that
    is not this machine's; production callers omit it and get ``sys.executable``.
    """
    interpreter = _norm(exe or sys.executable)
    dist, direct_url = _dist_info()
    pinned = f"{dist}=={version}"

    tool = _uv_tool_name(interpreter)
    if tool is not None:
        uv = _which("uv")
        if uv is None:
            # Deliberately not a pip fallback: uv owns that venv, see module doc.
            return InstallTarget(
                kind="unknown", dist=dist, tool=tool, version=version,
                install_argv=(), interpreter=interpreter,
                reason="uv executable not found",
            )
        return InstallTarget(
            kind="uv-tool", dist=dist, tool=tool, version=version,
            install_argv=(_norm(uv), "tool", "install", "--force", pinned),
            interpreter=interpreter,
        )

    tool = _pipx_tool_name(interpreter)
    if tool is not None:
        pipx = _which("pipx")
        if pipx is None:
            return InstallTarget(
                kind="unknown", dist=dist, tool=tool, version=version,
                install_argv=(), interpreter=interpreter,
                reason="pipx executable not found",
            )
        return InstallTarget(
            kind="pipx", dist=dist, tool=tool, version=version,
            install_argv=(_norm(pipx), "install", "--force", pinned),
            interpreter=interpreter,
        )

    if getattr(sys, "frozen", False):
        # A PyInstaller bundle has no site-packages to install into; its own
        # installer replaces it. Pip on the bundle exe would just fail.
        return InstallTarget(
            kind="unknown", dist=dist, tool="", version=version,
            install_argv=(), interpreter=interpreter,
            reason="running from a frozen bundle",
        )

    if _is_editable(direct_url):
        return InstallTarget(
            kind="source", dist=dist, tool="", version=version,
            install_argv=(), interpreter=interpreter,
            reason="running from a source checkout",
        )

    return InstallTarget(
        kind="pip", dist=dist, tool="", version=version,
        install_argv=(interpreter, "-m", "pip", "install", "-U", pinned),
        interpreter=interpreter,
    )


def payload_for(
    target: InstallTarget,
    relaunch_argv: Sequence[str],
    log_path: pathlib.Path,
) -> dict:
    """Process hand-off payload for ``deploy/self_update.py``.

    ``parent_pid`` is *this* process: the helper must not touch ``site-packages``
    before it is gone (see module doc). ``relaunch`` comes from the caller
    (``app.restart.relaunch_command``) so the replacement starts exactly the way
    the user started this one.
    """
    return {
        "parent_pid": os.getpid(),
        "install": list(target.install_argv),
        "relaunch": [str(part) for part in relaunch_argv],
        "log": str(log_path),
        "version": target.version,
    }


def launch_update(payload: dict, *, python: str | None = None) -> subprocess.Popen:
    """Start the helper that installs *payload* after this process exits.

    ``OSError`` propagates on purpose: the caller reports "could not start the
    updater" while it still owns a window, instead of quitting into nothing.

    The helper gets its own console on Windows (the user asked for an update and
    should see the progress, as the upstream script project does); elsewhere it
    simply gets its own session so a closed terminal cannot take it down.
    """
    log_path = pathlib.Path(str(payload["log"]))
    payload_path = log_path.parent / PAYLOAD_NAME
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:
        # The log dir can be unwritable or held by another instance (a locked
        # file, a redirected home). The payload is an *attachment* for bug
        # reports -- the helper also accepts it on stdin, so degrade instead of
        # aborting the whole update because paperwork failed.
        payload_path = None

    # Lazy import: ``deploy`` pulls the platform launcher helpers, and this
    # module is imported by the settings page on every launch.
    import deploy

    script = pathlib.Path(deploy.__file__).parent / "self_update.py"
    command = [python or sys.executable, str(script)]
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    popen_kwargs = {}
    if payload_path is None:
        # stdin hand-off: the helper reads the same JSON from "-" and the app
        # process is on its way out, so the pipe lifetime is not an issue.
        command.append("-")
        popen_kwargs["stdin"] = subprocess.PIPE
        popen_kwargs["stdin_data"] = encoded  # consumed below
    else:
        command.append(str(payload_path))
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        popen_kwargs["start_new_session"] = True
    stdin_data = popen_kwargs.pop("stdin_data", None)
    process = subprocess.Popen(command, **popen_kwargs)
    if stdin_data is not None:
        try:
            process.stdin.write(stdin_data)
            process.stdin.close()
        except OSError:
            pass  # the helper died early; its own error path reports it
    return process