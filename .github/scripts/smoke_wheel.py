"""Smoke test for the built ``acggr-py`` wheel.

Runs under ``QT_QPA_PLATFORM=offscreen`` and ``ACGG_CHECK_MODE=1``: it only
verifies that the wheel installs and every layer imports cleanly. No GUI is
started.

The check-mode switch must short-circuit *before* ``QApplication``
construction inside ``app.start()``; nothing may import Qt widgets on this path.
"""

import os
import subprocess
import sys


def run_test(name, cmd, env):
    print(f"::group::{name}")
    result = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    print(f"exit_code={result.returncode}")
    print("::endgroup::")
    return result.returncode


def main():
    env = os.environ.copy()
    tests = [
        ("layer imports", [sys.executable, "-c",
                           "import app, core, ui, res"]),
        ("version consistency", [sys.executable, "-c",
                                 "import core; print(core.__version__)"]),
        ("check mode startup", [sys.executable, "-c",
                                "import app; app.start()"]),
        ("cli help (default)", ["acggr", "--help"]),
        ("cli help (uvx target)", ["acggr-py", "--help"]),
    ]

    failures = []
    for name, cmd in tests:
        if run_test(name, cmd, env) != 0:
            failures.append(name)

    if failures:
        raise SystemExit(f"Smoke tests failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
