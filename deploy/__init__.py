# -*- coding: utf-8 -*-
"""Acggr deployment helpers.

The ``deploy`` package name is kept on purpose, see
``harness/docs/import-layout.md`` §2.5.

Layout notes:
- the ``QT_QPA_PLATFORM_PLUGIN_PATH`` hack referenced ``PyQt5`` and is dropped
  (this project is PySide6-only and resolves plugins from the environment);
- the default save root is ``~/Downloads/Acggr``.
"""

import pathlib
import sys


class Env:
    default_sv_path = pathlib.Path.home().joinpath("Downloads/Acggr")
    default_acggr_path = pathlib.Path.home().joinpath("Downloads/Acggr")
    shell = "powershell"
    aira2_platform_id = "win-amd64"
    aira2_binary_name = "aria2c.exe"

    def __init__(self, _p: pathlib.Path):
        self.proj_p = _p

    def env_init(self):
        ...

    @staticmethod
    def open_folder(_p):
        import os

        os.startfile(_p)

    @staticmethod
    def open_file(_f):
        import subprocess

        subprocess.run(["start", "", f"{_f}"], shell=True, check=True)


proj_path = pathlib.Path(__file__).parent.parent
curr_os_module = Env
# ``sys.platform`` instead of ``platform.system()``: the latter pulls in
# ``platform`` -> ``_wmi`` + socket, ~55ms of import time on Windows for a
# value we only use to pick the macOS branch.
if sys.platform == "darwin":
    from deploy.launcher.mac import macOS

    curr_os_module = macOS
curr_os = curr_os_module(proj_path)
