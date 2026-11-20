import ctypes
import sys
from enum import Enum
from typing import Callable

# Before the qfluentwidgets import below: that import prints the Pro banner, and
# this module is a valid standalone entry point (probes, scripts) that never
# touches app.
from core import install_qfluentwidgets_notice_filter

install_qfluentwidgets_notice_filter()

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, isDarkTheme, qconfig, setTheme

from core.config.qc import acggr_cfg
from .mid import MidNodeColors, create_dark_mid_colors, create_light_mid_colors


class CustTheme(Enum):
    LIGHT = "light"
    DARK = "dark"


#: ``DWMWA_WINDOW_CORNER_PREFERENCE`` / ``DWMWCP_ROUND`` / ``DWMWCP_DEFAULT``
#: (dwmapi.h). Not exposed by PySide6, and neither qfluentwidgets nor
#: qframelesswindow sets it: the platform plugin is *not* the author of this value
#: (see ``_restore_window_corners`` for the measurements).
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2


def _restore_window_corners() -> None:
    """Give rounded corners back to the windows DWM stops rounding after a color-scheme flip.

    The loss is OS-internal, not a plugin write: a window DWM rounds by default reads back as 0
    immediately after setColorScheme() returns. Re-assert DWMWCP_ROUND on visible, non-translucent
    top-levels.

    Do NOT touch translucent top-levels (detail viewers use WA_TranslucentBackground + a 12px
    transparent margin): DWM's default for a frameless window is 0, and forcing ROUND adds a native
    edge along the whole rect that a fresh process would not have. Keep both states identical.

    Do not turn this into a pixel-level test (width rides on DPI / DWM AA); gate on the attribute
    itself (translucent 0 / visible non-translucent 2). Skip hidden windows; no-op off Win11.
    """
    if sys.platform != "win32":
        return
    app = QApplication.instance()
    if app is None:
        return
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        return
    preference = ctypes.c_int(_DWMWCP_ROUND)
    for window in app.topLevelWidgets():
        # Translucent viewers: see the docstring -- DWM's square default is correct.
        if window.testAttribute(Qt.WA_TranslucentBackground):
            continue
        # Menus/tooltips are transient popups; rounding them is meaningless.
        if window.windowType() in (Qt.Popup, Qt.ToolTip):
            continue
        if not window.isVisible():
            continue
        handle = window.winId()
        if not handle:
            continue
        try:
            dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(int(handle)),
                ctypes.c_uint(_DWMWA_WINDOW_CORNER_PREFERENCE),
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )
        except (OSError, TypeError, ValueError):
            # Never let a cosmetic DWM call break a theme switch.
            continue


class LightFontColor:
    text = "black"
    tip = "blue"
    highlight = "purple"
    success = "green"
    err = "red"


class DarkFontColor:
    text = "white"
    tip = "aqua"
    highlight = "gold"
    err = "lightcoral"
    success = "lightgreen"


class Light:
    def __init__(self):
        self.palette = QPalette()
        self.palette.setColor(QPalette.Window, QColor(245, 245, 245))
        self.palette.setColor(QPalette.WindowText, QColor(40, 40, 40))
        self.palette.setColor(QPalette.Base, QColor(255, 255, 255))
        self.palette.setColor(QPalette.AlternateBase, QColor(238, 238, 238))
        # Native QToolTip paints with exactly these two roles (QCommonStyle
        # PE_PanelTipLabel = ToolTipBase brush, text = ToolTipText). qfw's own
        # ToolTip bubble is qss-painted and ignores them, so anything that goes
        # through plain setToolTip on a widget without ToolTipFilter (most of
        # danbooru/jsoneri/settings) is rendered by Qt with this palette -- keep
        # them at the fluent tooltip colours (#f9f9f9 / #000000) instead of the
        # stock Windows pale yellow so both tooltip flavours look alike.
        self.palette.setColor(QPalette.ToolTipBase, QColor(249, 249, 249))
        self.palette.setColor(QPalette.ToolTipText, QColor(0, 0, 0))
        self.palette.setColor(QPalette.Text, QColor(40, 40, 40))
        self.palette.setColor(QPalette.Button, QColor(230, 230, 230))
        self.palette.setColor(QPalette.ButtonText, QColor(40, 40, 40))
        self.palette.setColor(QPalette.BrightText, QColor(239, 83, 80))
        self.palette.setColor(QPalette.Highlight, QColor(100, 181, 246))
        self.palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
        self.font_color = LightFontColor
        self.c = Theme.LIGHT
        self.mid_colors: MidNodeColors = create_light_mid_colors()


class Dark:
    def __init__(self):
        self.palette = QPalette()
        self.palette.setColor(QPalette.Window, QColor(53, 53, 53))
        self.palette.setColor(QPalette.WindowText, QColor(255, 255, 255))
        self.palette.setColor(QPalette.Base, QColor(35, 35, 35))
        self.palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
        # White-on-white here is what made every native tooltip a blank bubble:
        # Qt draws the tip label with ToolTipBase as background and ToolTipText
        # as the text colour, so both being #ffffff rendered nothing. Match the
        # fluent dark tooltip (#2b2b2b bg + white text) instead.
        self.palette.setColor(QPalette.ToolTipBase, QColor(43, 43, 43))
        self.palette.setColor(QPalette.ToolTipText, QColor(255, 255, 255))
        self.palette.setColor(QPalette.Text, QColor(220, 220, 220))
        self.palette.setColor(QPalette.Button, QColor(53, 53, 53))
        self.palette.setColor(QPalette.ButtonText, QColor(255, 255, 255))
        self.palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
        self.palette.setColor(QPalette.Highlight, QColor(61, 174, 255))
        self.palette.setColor(QPalette.HighlightedText, QColor(0, 0, 0))
        self.font_color = DarkFontColor
        self.c = Theme.DARK
        self.mid_colors: MidNodeColors = create_dark_mid_colors()


_THEME_SCHEMES = {
    CustTheme.LIGHT: Light(),
    CustTheme.DARK: Dark(),
}
_THEME_CALLBACK_ATTR = "_cgs_theme_callback"
_THEME_CLEANUP_ATTR = "_cgs_theme_cleanup_bound"


class ThemeManager:
    """QFluent theme adapter with inherited palette/font helpers."""

    def __init__(self):
        self._listeners: list[Callable[[CustTheme], None]] = []
        self._bootstrapped = False
        # Before any window exists: the Qt platform plugin colours native
        # window chrome (WCA_USEDARKMODECOLORS / DWMWA_USE_IMMERSIVE_DARK_MODE)
        # from QStyleHints::colorScheme() at window creation -- a fresh light
        # window on a dark-scheme system is born with dark native chrome
        # (lead_theme_criteria A1). Syncing here covers both paths: land_theme()
        # calls apply_to_app() before create_window(), and runtime switches go
        # through _on_qfluent_theme_changed below.
        self._syncing_color_scheme = False
        self._sync_color_scheme()
        qconfig.themeChanged.connect(self._on_qfluent_theme_changed)

    def _bootstrap(self):
        if self._bootstrapped:
            return
        self._bootstrapped = True
        setTheme(self.themeMode, save=False)
        self.apply_to_app()

    def _ensure_bootstrapped(self):
        if QApplication.instance() is None:
            return
        self._bootstrap()

    def _sync_color_scheme(self):
        """Make QStyleHints::colorScheme() follow the acggr theme.

        The Windows platform plugin rewrites the native dark-mode window
        attributes from styleHints().colorScheme() on every window creation and
        after event-loop spins, so a stale Dark scheme re-darkens the DWM mica
        chrome even after qfw replays setMicaEffect(False) (no Python-level
        call visible in a spy). Following the app theme keeps the native
        attributes at 0 on the light leg, at 1 on the dark leg.
        """
        if sys.platform != "win32" or self._syncing_color_scheme:
            return
        hints = QApplication.styleHints() if QApplication.instance() else None
        if hints is None or not hasattr(hints, "setColorScheme"):
            return
        self._syncing_color_scheme = True
        try:
            hints.setColorScheme(
                Qt.ColorScheme.Dark if isDarkTheme() else Qt.ColorScheme.Light
            )
        finally:
            self._syncing_color_scheme = False
        # setColorScheme() above drops the rounded corners of captioned windows
        # (OS-internal, synchronously inside the call -- the plugin never writes
        # the corner attribute, see _restore_window_corners); restore them once the
        # event loop has processed the plugin's own pass.
        QTimer.singleShot(0, _restore_window_corners)

    def _on_qfluent_theme_changed(self, _theme_mode):
        self._sync_color_scheme()
        self.apply_to_app()
        current_theme = self.currentTheme
        for callback in tuple(self._listeners):
            callback(current_theme)

    @property
    def themeMode(self) -> Theme:
        return acggr_cfg.themeMode.value

    @property
    def currentTheme(self) -> CustTheme:
        self._ensure_bootstrapped()
        return CustTheme.DARK if isDarkTheme() else CustTheme.LIGHT

    @property
    def is_dark(self) -> bool:
        return self.currentTheme == CustTheme.DARK

    @property
    def theme(self):
        return _THEME_SCHEMES[self.currentTheme]

    def get_theme(self) -> CustTheme:
        return self.currentTheme

    @property
    def font_color(self):
        return self.theme.font_color

    @property
    def mid_colors(self) -> MidNodeColors:
        return self.theme.mid_colors

    def set_theme_mode(self, theme_mode: Theme, *, save: bool = False, lazy: bool = False):
        self._ensure_bootstrapped()
        setTheme(theme_mode, save=save, lazy=lazy)

    def set_dark(self, set_dark: bool, *, save: bool = False, lazy: bool = False):
        self.set_theme_mode(Theme.DARK if set_dark else Theme.LIGHT, save=save, lazy=lazy)

    def apply_to_app(self, app: QApplication | None = None):
        target = app or QApplication.instance()
        if target is not None:
            target.setPalette(self.theme.palette)

    def subscribe(self, callback: Callable[[CustTheme], None]):
        if callback not in self._listeners:
            self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[CustTheme], None]):
        if callback in self._listeners:
            self._listeners.remove(callback)


theme_mgr = ThemeManager()


def setupTheme(widget):
    theme_mgr._ensure_bootstrapped()

    def apply_theme(_theme):
        # Only TextBrowser owners carry a per-widget document stylesheet. Other
        # mounts still get registered as listeners below; the palette itself is
        # applied app-wide by theme_mgr.apply_to_app(), not per widget.
        text_browser = getattr(widget, "textBrowser", None)
        if text_browser is None:
            return
        color = theme_mgr.font_color
        css = f"""
p.theme-text {{ color: {color.text}; }}
font.theme-tip {{ color: {color.tip}; }}
font.theme-highlight {{ color: {color.highlight}; }}
font.theme-success {{ color: {color.success}; }}
font.theme-err {{ color: {color.err}; }}
"""
        text_browser.document().setDefaultStyleSheet(css)

    previous = getattr(widget, _THEME_CALLBACK_ATTR, None)
    if previous is not None:
        theme_mgr.unsubscribe(previous)

    setattr(widget, _THEME_CALLBACK_ATTR, apply_theme)
    if not getattr(widget, _THEME_CLEANUP_ATTR, False):
        def _cleanup(_obj=None):
            callback = getattr(widget, _THEME_CALLBACK_ATTR, None)
            if callback is not None:
                theme_mgr.unsubscribe(callback)
                setattr(widget, _THEME_CALLBACK_ATTR, None)

        widget.destroyed.connect(_cleanup)
        setattr(widget, _THEME_CLEANUP_ATTR, True)

    apply_theme(theme_mgr.currentTheme)
    theme_mgr.subscribe(apply_theme)
