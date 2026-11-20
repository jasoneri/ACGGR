# -*- coding: utf-8 -*-
"""Acggr application layer.

Holds the standalone window and its UI assembly: ScriptWindow and every entry it mounts
(danbooru / pawchive / cbg / jsoneri / aria2 settings).
Import boundary: may import ``ui`` / ``core`` / ``res``; must NOT import any legacy upstream module.
Entry surfaces, all funnelled through :func:`start`: ``acggr`` gui-script, ``python acggr.py``
and ``python -m``-style calls → :func:`start`; ``acggr-cli`` / ``--check`` → :func:`check`.
Window startup: ``create_window`` builds the light shell plus a splash child;
:func:`prewarm_webengine` absorbs QtWebEngine's one-time HWND rebuild while hidden;
``start_post_first_paint_setup`` runs after first paint to build the landing interface.
"""
import os
import sys
import time

# Install the notice filter *before* anything can import qfluentwidgets: the
# notice is printed at `qfluentwidgets.common.config` import time, and
# `ui.core.theme` imports qfluentwidgets at module scope. Installing the
# filter any later than this leaves the banner unavoidable on every launch.
from core import install_qfluentwidgets_notice_filter

install_qfluentwidgets_notice_filter()

#: CI smoke switch (see ``harness/docs/import-layout.md`` §3).
#: ``start()`` degrades to :func:`check` when it is ``1``.
CHECK_MODE_ENV = "ACGG_CHECK_MODE"

#: Marker written into ``core.config.conf_dir`` once the *user* has imported
#: CGS's config from the Settings page. Never written by startup code: importing
#: is a user decision (that is what the button is for), so this only records that
#: the decision was already made and the one-shot card can retire.
CGS_IMPORT_MARKER = ".cgs_import_done"

#: Marker for the additive data sync (download history, Comfy job snapshots and
#: qconfig favourites/history). Separate from ``CGS_IMPORT_MARKER`` because these
#: dimensions were not part of the original import flow: installs that imported
#: config -- or ran the first history-only backfill -- still need one sync run.
#: The marker also stores a fingerprint of CGS's data files, so a CGS install
#: that keeps being used is re-synced on the launch after it changes.
CGS_DATA_MERGE_MARKER = ".cgs_merge_done"

USAGE = """\
usage: acggr [--check | -h | --help]

  (no argument)  start the Acggr window
  --check        import-only smoke check: exits 0 without creating a window
  -h, --help     show this message
"""


def _register_resources():
    """Register the compiled qrc bundle before any icon is looked up.

    res.material_ct calls qInitResources() at import time, so the bundle registers exactly
    once per process (a repeat import hits sys.modules and is a no-op). Skipping leaves every
    ``:/...`` icon blank inside ScriptWindow.

    Do NOT importlib.reload this module: re-registering the same qt_resource_data blob then
    constructing a QApplication crashes PySide6.
    """
    import res.material_ct  # noqa: F401


def _install_translators(app):
    """Install qfluentwidgets' and acggr's Qt translators on ``app``.

    Called from :func:`bootstrap` right after the QApplication exists and before
    any window is constructed. ``check()`` deliberately never reaches here: it
    is import-only and must not need a QApplication or a GUI translator.

    Language uses ``conf.lang`` (the persisted value, defaulting to
    ``res.lang``) and falls back through :func:`res.resolve_language`; no new
    language-switch UI is introduced. Translators are kept alive as attributes
    of ``app`` -- a QTranslator that is garbage-collected silently stops
    translating.
    """
    from PySide6.QtCore import QCoreApplication, QLocale
    from qfluentwidgets import FluentTranslator

    import res
    import res.i18n
    from core import conf

    lang = res.resolve_language(getattr(conf, "lang", None))
    fluent = FluentTranslator(QLocale(lang))
    # Compiled from res/i18n/<lang>.yml on the spot (lupdate -> lrelease); the
    # .qm is cached and never committed. ``None`` = source language / no table.
    own = res.i18n.translator(lang)
    QCoreApplication.installTranslator(fluent)
    if own is not None:
        QCoreApplication.installTranslator(own)
    app._acggr_translators = (fluent, own) if own is not None else (fluent,)
    return lang


def _install_acggr_logging():
    """Install the single file sink and the process-wide uncaught-exception hook."""
    from loguru import logger

    from core.config import acggr_log_file
    from ui.core.exception_feedback import install_gui_exception_hook

    log_file = acggr_log_file()
    existing_ids = [
        handler_id
        for handler_id, handler in logger._core.handlers.items()
        if str(getattr(getattr(handler, "_sink", None), "_file_path", "")) == str(log_file)
    ]
    if not existing_ids:
        logger.add(
            log_file,
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | [{name}]: {message}",
            retention="5 days",
            encoding="utf-8",
        )
    install_gui_exception_hook(logger=logger)
    return log_file


def bootstrap(argv=None, *, apply_theme=True):
    """Process-level startup shared by every entry surface.

    Order matters: freeze_support → single file log sink + exception hook → resources →
    single QApplication → translators → theme landing. Resources come first because icons
    are resolved while widgets are built. Returns the app instance.

    apply_theme=False postpones land_theme so the caller can probe the CGS install first;
    landing the theme imports core.config.qc and its import-time qconfig.load.:func:`start`
    is the only in-repo caller and passes False; the default keeps documented behaviour.
    """
    from multiprocessing import freeze_support

    from PySide6.QtWidgets import QApplication

    freeze_support()
    _install_acggr_logging()
    _register_resources()
    app = QApplication.instance() or QApplication(list(sys.argv if argv is None else argv))
    _install_translators(app)
    if apply_theme:
        land_theme(app)
    return app


def land_theme(app=None):
    """Land the saved theme on the application.

    Kept separate from :func:`bootstrap` rather than as a side effect of
    whichever interface is constructed first: the palette must already be final
    before any window is built, and must not depend on entry visibility or
    construction order.

    Kept after :func:`import_cgs_config_once` because importing
    ``ui.core.theme`` pulls ``core.config.qc`` (see ``bootstrap``).
    """
    from PySide6.QtWidgets import QApplication

    from ui.core.theme import theme_mgr

    app = app or QApplication.instance()
    theme_mgr.apply_to_app(app)
    return app


def import_cgs_config_once():
    """Report whether CGS's config is available -- never import it.

    Importing is a *user* decision: the Settings page ships a one-shot card for
    exactly that, so doing it silently here would make the card pointless (the
    user never gets to choose, and a CGS install they wanted to keep separate is
    already merged). Startup only needs to know whether the card is worth
    showing at all, which is :func:`cgs_import_available`.

    Returns the CGS config directory as a string, or ``""`` when there is
    nothing to offer. Kept under the historical name so call sites that only
    care about "is CGS present" do not have to change.
    """
    try:
        from core.config.import_cgs import find_cgs_conf_dir

        found = find_cgs_conf_dir()
        return str(found) if found is not None else ""
    except Exception:  # noqa: BLE001 - startup must survive any probe failure
        from loguru import logger

        logger.exception("[acggr] CGS config probe failed; window startup continues")
        return ""


def _cgs_source_fingerprint(cgs_conf_dir, cgs_record_db) -> str:
    """Cheap fingerprint of every CGS file the merge reads.

    ``record.db`` runs in WAL mode, so fresh rows can live in ``-wal`` while the
    main file's mtime stays put; both files are part of the fingerprint. Missing
    files still contribute a marker, so a CGS install that is deleted and later
    recreated is not mistaken for unchanged.
    """
    import hashlib

    digest = hashlib.sha256()
    paths = []
    if cgs_record_db is not None:
        paths.append(cgs_record_db)
        paths.append(cgs_record_db.with_name(cgs_record_db.name + "-wal"))
    if cgs_conf_dir is not None:
        paths.extend(sorted(cgs_conf_dir.glob("qconfig/qc*.json")))
    for path in paths:
        try:
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size};".encode())
        except OSError:
            digest.update(f"{path.name}:missing;".encode())
    return digest.hexdigest()


def merge_cgs_data_once():
    """Sync every CGS dimension Acggr reads whenever CGS's data changed.

    Config *file* import stays a user decision; the dimensions with no UI surface of their own
    are synced at startup: danbooru download history (danbooru_md5_table), Comfy job snapshots
    and qconfig payloads (favourites, tag groups, search history, DoH/proxy).
    Every merge is additive and target-first (Acggr wins; CGS only fills missing), so it is
    idempotent and safe to retry after a partial crash. The marker fingerprints CGS's record.db
    (+WAL) and qconfig files: unchanged sources skip the work entirely.
    Must run *before* land_theme (ui.core.theme pulls core.config.qc, whose module-level
    qconfig.load would otherwise win over freshly merged files). Never creates the marker when
    no CGS install is found; a merge failure is logged and retried next launch.
    """
    from core.config import conf_dir

    try:
        from core.config.import_cgs import (
            find_cgs_conf_dir,
            find_cgs_record_db,
            merge_cgs_qconfigs,
            merge_cgs_record_history,
        )

        cgs_conf_dir = find_cgs_conf_dir()
        cgs_record_db = find_cgs_record_db()
        if cgs_conf_dir is None and cgs_record_db is None:
            return None
        fingerprint = _cgs_source_fingerprint(cgs_conf_dir, cgs_record_db)
        marker = conf_dir.joinpath(CGS_DATA_MERGE_MARKER)
        if marker.exists():
            try:
                stored_fingerprint = marker.read_text(encoding="utf-8").split()[0]
            except (OSError, IndexError):
                stored_fingerprint = ""
            if stored_fingerprint == f"fp={fingerprint}":
                return None
        records = merge_cgs_record_history(cgs_conf_dir or cgs_record_db)
        qconfigs = (
            merge_cgs_qconfigs(cgs_conf_dir) if cgs_conf_dir is not None else None
        )
    except Exception:  # noqa: BLE001 - data backfill must never block startup
        from loguru import logger

        logger.exception("[acggr] CGS data merge failed; next launch retries")
        return None

    merged_files = (qconfigs or {}).get("files") or []
    snapshots = (records or {}).get("comfy_job_snapshots") or {}
    try:
        marker.write_text(
            f"fp={fingerprint} "
            f"at={time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"danbooru_inserted={(records or {}).get('danbooru', {}).get('inserted', 0)} "
            f"snapshots_inserted={snapshots.get('inserted', 0)} "
            f"snapshots_updated={snapshots.get('updated', 0)} "
            f"qconfig_files={','.join(item['name'] for item in merged_files) or '-'}\n",
            encoding="utf-8",
        )
    except OSError:
        # A read-only config dir must not break startup; the merge is idempotent,
        # so a retry next launch simply re-runs it.
        pass
    return {"records": records, "qconfigs": qconfigs}


def create_window(**kwargs):
    """Construct the ``ScriptWindow`` (heavy ``app.GUI`` import deferred)."""
    from app.GUI import AcggrMainWindow

    return AcggrMainWindow(**kwargs)


def prewarm_webengine(window):
    """Absorb QtWebEngine's one-time native-surface rebuild before ``show()``.

    The first QWebEngineView that renders forces Qt to re-create the top-level HWND (~300 ms
    blank window, lost DWM dark/mica attributes); the rebuild happens once per process
    regardless of trigger or widget flags. Pre-warming (a throwaway view that has rendered)
    is the only fix: later views then produce zero lifecycle events on the main window.

    This MUST run between ``create_window`` and ``show()`` with the main window as parent, so
    the one unavoidable rebuild happens while the window is invisible.
    """
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    probe = QWebEngineView(window)
    probe.hide()
    loop = QEventLoop()
    loaded = []

    def _on_loaded(ok):
        loaded.append(ok)
        loop.quit()

    probe.loadFinished.connect(_on_loaded)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(loop.quit)
    watchdog.start(2000)
    probe.load(QUrl("about:blank"))
    if not loaded:
        loop.exec()
    watchdog.stop()
    probe.setParent(None)
    probe.deleteLater()
    QApplication.processEvents()

    # Persisted-profile creation is the one-time cost (Chromium builds disk cache / cookie
    # store / interceptors) that otherwise lands on the first page with a web panel. Do it
    # while the splash still covers the window: shared_browser_window_profile() is a process
    # singleton, later carriers reuse it. A failure must not block startup.
    try:
        from ui.core.browser.profile import shared_browser_window_profile

        shared_browser_window_profile()
    except Exception:  # noqa: BLE001 - 预热失败只损失性能，不影响功能
        pass
    return bool(loaded)


def check():
    """Import-only smoke check: no ``QApplication``, no ``ScriptWindow`` instance.

    Verifies that every layer imports and that the qrc bundle registers, then
    returns ``0``. Used by ``python acggr.py --check``, the ``acggr-cli``
    console script and ``ACGG_CHECK_MODE=1`` CI smoke runs
    (``.github/scripts/smoke_wheel.py``).
    """
    import core  # noqa: F401
    import res  # noqa: F401
    import ui  # noqa: F401

    _register_resources()
    from app.GUI import AcggrMainWindow  # noqa: F401

    return 0


def start(argv=None):
    """Entry point registered as the ``acggr`` gui-script.

    Also the implementation behind ``python acggr.py``. Extra arguments are
    handed to ``QApplication`` (so Qt's own flags like ``-platform`` still work);
    only ``--check`` / ``-h`` / ``--help`` are intercepted.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print(USAGE, end="")
        return 0
    if "--check" in args or os.environ.get(CHECK_MODE_ENV) == "1":
        # CI smoke (``.github/scripts/smoke_wheel.py``): import check only.
        return check()

    app = bootstrap(apply_theme=False)
    # Probe only: whether an importable CGS install exists decides whether the
    # Settings card is worth showing. Deliberately NOT an import -- that stays a
    # user action (see ``import_cgs_config_once``). Must still run *before*
    # ``land_theme``: that imports ``core.config.qc``.
    cgs_source = import_cgs_config_once()
    merge_cgs_data_once()
    land_theme(app)
    # Light shell: sidebar entries, geometry, and the splash child widget -- no
    # interface is constructed yet (see ``AcggrMainWindow.initNavigation``).
    window = create_window(cgs_import_source=cgs_source)
    # Must stay between ``create_window`` and ``show()``: this is the only window
    # in which the HWND rebuild can be absorbed. See ``prewarm_webengine``.
    prewarm_webengine(window)
    window.show()
    # One pass so the splash (a child of ``window``) is actually painted before
    # the deferred work starts; without it the heavy phase would run first and the
    # splash would only ever be seen for its tail.
    app.processEvents()
    window.start_post_first_paint_setup()
    return app.exec()


def main(argv=None):
    """Thin alias so ``acggr.py`` can stay a 3-line shell."""
    return start(argv)


def __getattr__(name):
    """``from app import ScriptWindow`` (``harness/docs/import-layout.md`` §2.1) without
    paying the heavy ``app.GUI`` import at ``app`` import time."""
    if name == "ScriptWindow":
        from app.GUI import AcggrMainWindow as _ScriptWindow

        globals()[name] = _ScriptWindow
        return _ScriptWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
