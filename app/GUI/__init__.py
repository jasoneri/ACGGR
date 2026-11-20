# ruff: noqa: E402
import sys
import pathlib
import os
import threading
import time

from PySide6 import QtWidgets
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractScrollArea, QAbstractSlider, QApplication, QDateTimeEdit,
    QDoubleSpinBox, QFrame, QHBoxLayout, QLineEdit, QPlainTextEdit, QScrollBar,
    QSizePolicy, QCompleter, QFileDialog, QTextEdit, QVBoxLayout, QStackedWidget,
    QWidget, QSpinBox,
)
from PySide6.QtCore import QCoreApplication, QRect, QSize, Qt, QTimer, qtTrId
from PySide6.QtGui import QColor, QIcon
from core import install_qfluentwidgets_notice_filter

install_qfluentwidgets_notice_filter()

from qfluentwidgets import (
    NavigationItemPosition, FluentWindow, qrouter,
    LineEdit, PasswordLineEdit,
    FluentIcon as FIF, InfoBar, InfoBarPosition,
    PushButton, ScrollArea, SpinBox, ComboBox,
    PrimaryPushSettingCard, SwitchButton, TabCloseButtonDisplayMode, Theme,
)
from qfluentwidgets.components.widgets.spin_box import SpinBoxBase

from core.variables import APP_LOGO_RESOURCE, APP_TITLE, LANG as UI_LANGUAGES, __version__ as APP_VERSION
from core import yaml, ori_path
from core.config.qc import danbooru_cfg, acggr_cfg
from core.script import conf as script_conf
from core.script.entry_descriptor import (
    DEFAULT_SCRIPT_ENTRY_SLUG, SCRIPT_ENTRY_DESCRIPTORS, ScriptEntryDescriptor, get_script_entry,
)
from core.script.image.danbooru.constants import DEFAULT_DANBOORU_SAVE_PATH
from core.script.image.danbooru.models import DanbooruRuntimeConfig
from ui.core.doh_runtime import ScriptDoHStubRuntime
from ui.core.exception_feedback import GuiExceptionFeedbackDispatcher, InfoBarExceptionPresenter
from ui.core.theme import setupTheme, theme_mgr
from core.network.doh_policy import DEFAULT_DOH_URL, proxy_url
from ui.qfluent.components.elided_card import ElidedGroupHeaderCardWidget
from ui.core.timer import safe_single_shot
from ui.async_task import summarize_error_message
from ui.qfluent.components.icons import AcggrIcon


_SCRIPT_ENTRY_DESCRIPTOR = get_script_entry(DEFAULT_SCRIPT_ENTRY_SLUG)

#: How long a "即将进行重启" toast stays readable before the relaunch fires.
#: One value for every restart trigger (language switch, CGS import): both
#: toasts show the same ring draining over the same span, and two constants
#: would drift -- 5 s felt like dead waiting next to the import's 2.5 s.
RESTART_DELAY_MS = 2500
AUTOSAVE_DEBOUNCE_MS = 600
AUTOSAVE_TOAST_MS = 1600

#: Update probe pacing. The startup probe waits for the landing page to settle
#: (it runs on a worker thread, but a cold link still competes with first paint),
#: and an answer -- including "up to date" -- is reused for a day. A manual
#: "检查更新" always bypasses the TTL: the user asked *now*.
UPDATE_CHECK_STARTUP_DELAY_MS = 3000
#: Time between "updater launched" and the app quitting: the toast must be
#: readable, but the install waits on this process, so it cannot be long.
UPDATE_QUIT_DELAY_MS = 1500
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
UPDATE_CHECK_TASK_ID = "settings_update_check"

#: How long the startup splash stays up at minimum, in milliseconds.
#: Same reason as CGS' ``splashWait``: the deferred setup can finish in a few
#: hundred ms on a warm cache, and a logo that appears and vanishes within one
#: frame reads as a glitch rather than as a sieve for the wait.
STARTUP_SPLASH_MIN_MS = 1000
#: Splash logo edge = window height * ratio, clamped to the range below.
#: Derived from the *window* (not the screen) so the logo keeps the same
#: proportion at every restored geometry.
STARTUP_SPLASH_ICON_RATIO = 0.30
STARTUP_SPLASH_ICON_MIN = 320
STARTUP_SPLASH_ICON_MAX = 480

AUTO_SAVE_BINDINGS = (
    # qfw ComboBox / EditableComboBox (QWidget wrapper with a popup button)
    (lambda w: hasattr(w, "currentIndexChanged") and hasattr(w, "addItem")
     and hasattr(w, "currentIndex"), "currentIndexChanged"),
    # qfw SwitchButton (no QAbstractButton base)
    (lambda w: hasattr(w, "checkedChanged") and hasattr(w, "isChecked"),
     "checkedChanged"),
    (lambda w: isinstance(w, QLineEdit), "textChanged"),
    # text edits outside the line-edit family (spin box internals excluded)
    (lambda w: isinstance(w, (QTextEdit, QPlainTextEdit))
     and not w.isReadOnly(), "textChanged"),
    (lambda w: isinstance(w, (QSpinBox, QDoubleSpinBox)), "valueChanged"),
    # sliders and dials, but never scrollbars
    (lambda w: isinstance(w, QAbstractSlider) and not isinstance(w, QScrollBar),
     "valueChanged"),
    (lambda w: isinstance(w, QDateTimeEdit), "dateTimeChanged"),
    # plain Qt buttons last: CheckBox / RadioButton / ToggleButton
    (lambda w: isinstance(w, QAbstractButton) and w.isCheckable(), "toggled"),
)


def _autosave_connect(widget):
    """Return a ``connect(slot)`` callable for ``widget``, or ``None``.

    Returning the connector instead of connecting here keeps
    :func:`_wire_auto_save` and its coverage audit reading from one table.
    """
    for matcher, signal_name in AUTO_SAVE_BINDINGS:
        try:
            matched = matcher(widget)
        except (RuntimeError, AttributeError):
            # Deleted C++ object, or a property that raises on this type.
            return None
        if matched:
            signal = getattr(widget, signal_name, None)
            return None if signal is None else signal.connect
    return None


def _looks_editable(widget) -> bool:
    if getattr(widget, "configItem", None) is not None:
        return False  # qfw setting card: saves through qconfig itself
    if isinstance(widget, (QScrollBar, QAbstractSlider)) and "ScrollBar" in type(widget).__name__:
        return False  # stock QScrollBar
    if type(widget).__name__.endswith("ScrollBar"):
        return False  # qfw SmoothScrollBar / ScrollBar: QWidget wrappers
    if isinstance(widget, QAbstractButton) and not widget.isCheckable():
        return False  # plain action button: no stored value
    if isinstance(widget, QScrollBar):
        return False
    for attribute in ("textChanged", "valueChanged", "currentIndexChanged",
                      "checkedChanged", "toggled", "dateTimeChanged"):
        if hasattr(widget, attribute):
            return True
    return False


def _lang_text(table: dict, key: str, *, fallback: str, **fmt) -> str:
    """Look ``key`` up in a specific language table instead of the installed one.

    The language countdown must speak the *target* language while the running
    process still has the old translator installed, so it cannot go through
    ``qtTrId``. ``fallback`` is used when the table has no wording yet.
    """
    text = table.get(key) or ""
    if not text:
        return f"{key} ({fallback})"
    try:
        return text.format(**fmt) if fmt else text
    except (KeyError, IndexError):
        return text



OFFSCREEN_FLUENT_FALLBACK = os.environ.get("QT_QPA_PLATFORM") == "offscreen"
ScriptWindowBase = QFrame if OFFSCREEN_FLUENT_FALLBACK else FluentWindow

# qfw SpinBox/CompactSpinBox lineEdits collapse to 1px when NoButtons is active
# under PySide6 >= 6.11.3: switch the box to native UpDownArrows and hide the
# *native* QSpinBox subcontrols app-wide, so qfw's own Inline SpinButton stays
# the only visible arrows. App-level qss + monkeypatch: no theme switch can undo it.
_SPINBOX_COMPAT_QSS = (
    "/* acggr-spinbox-compat */\n"
    "QSpinBox::up-button, QSpinBox::down-button {\n"
    "    width: 0px; height: 0px; border: none; background: transparent;\n"
    "}"
)


def install_spinbox_compat(app) -> None:
    """Apply once per app: qss hides native spin buttons, patch restores layout."""
    if app is None:
        return
    if _SPINBOX_COMPAT_QSS not in app.styleSheet():
        app.setStyleSheet(app.styleSheet() + "\n" + _SPINBOX_COMPAT_QSS)
    base_init = SpinBoxBase.__init__
    if getattr(base_init, "_acggr_native_symbols", False):
        return

    def init_with_native_symbols(self, parent=None):
        base_init(self, parent=parent)
        self.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)

    init_with_native_symbols._acggr_native_symbols = True
    SpinBoxBase.__init__ = init_with_native_symbols
class _OffscreenNavigationInterface(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("OffscreenNavigationInterface")
        self.setFixedWidth(148)
        self.setStyleSheet(
            """
            QFrame#OffscreenNavigationInterface {
                background: #111111;
                border-right: 1px solid rgba(255, 255, 255, 0.12);
            }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(8)
        self._top_layout = QVBoxLayout()
        self._top_layout.setSpacing(8)
        self._bottom_layout = QVBoxLayout()
        self._bottom_layout.setSpacing(8)
        layout.addLayout(self._top_layout)
        layout.addStretch(1)
        layout.addLayout(self._bottom_layout)

    def add_button(self, button, bottom: bool = False):
        target_layout = self._bottom_layout if bottom else self._top_layout
        target_layout.addWidget(button)

    def addSeparator(self):
        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet("background: rgba(255, 255, 255, 0.14); min-height: 1px; max-height: 1px; border: none;")
        self._top_layout.addWidget(separator)


class BaseServiceGroupCard(ElidedGroupHeaderCardWidget):
    """基础服务配置组卡片 - 高复用度的基类"""
    def __init__(self, parent=None, service_name="", config_key="", default_path=""):
        super().__init__(parent)
        self.setting_interface = parent
        self.service_name = service_name
        self.config_key = config_key
        self.setTitle(f"{service_name} Config")
        self.setBorderRadius(8)

        # 创建组件
        self.cookiesEdit = LineEdit()
        self.cookiesEdit.setPlaceholderText(f"{service_name} session cookie")
        self.cookiesEdit.setClearButtonEnabled(True)
        self.cookiesEdit.setMinimumWidth(400)

        # Contexts below are written as explicit ``QCoreApplication.translate``
        # with the *defining* class name on purpose: this __init__ runs for every
        # subclass (e.g. ScriptEntryGroupCard), and ``self.tr`` would splinter one
        # message across one context per subclass.
        self.pathButton = PushButton(
            qtTrId("common.save_path.select"))
        self.pathButton.setFixedWidth(120)
        self.pathButton.clicked.connect(self._onSelectFolder)

        # 当前选择的路径
        self.default_path = str(default_path or "")
        config_data = getattr(script_conf, config_key, {}) if hasattr(script_conf, config_key) else {}
        # ``sv_path`` 缺失和空串都表示"未设置"：一律用默认路径，与爬虫侧
        # ``conf.get('sv_path') or DEFAULT`` 一致，并保证说明文字不画成空白行。
        self.current_path = str(config_data.get("sv_path") or self.default_path)

        # 添加组件到分组中
        self.pathCard = self.addGroup(
            FIF.DOWNLOAD, qtTrId("common.save_path.title"),
            self.current_path, self.pathButton)
        self.addGroup(FIF.VPN, qtTrId("script.entry.cookie.title"),
                      qtTrId("script.entry.cookie.hint"), self.cookiesEdit)

    def _onSelectFolder(self):
        folder = QFileDialog.getExistingDirectory(
            self, qtTrId("script.entry.select_dir_title").format(service=self.service_name))
        if folder:
            wanted_p = pathlib.Path(folder)
            cgs_path = ori_path.parent if ori_path.parent.joinpath("scripts/CGS.py").exists() else ori_path
            cgs_flag = str(wanted_p).startswith(str(cgs_path))
            drive_flag = len(wanted_p.parts) == 1 and wanted_p.drive
            if cgs_flag or drive_flag:
                InfoBar.error(
                    title='', content=qtTrId("settings.save.invalid_path"),
                    orient=Qt.Horizontal, isClosable=True, position=InfoBarPosition.TOP,
                    duration=5000, parent=self.setting_interface
                )
                return
            self.current_path = folder
            self.pathCard.setContent(self.current_path)
            # Directory picking is one discrete action with no debounce value:
            # persist now so the chosen path survives closing the window at once.
            self.setting_interface.flush_auto_save()

    def getCookieText(self):
        """获取Cookie文本"""
        return self.cookiesEdit.text().strip()

    def setCookieText(self, text):
        """设置Cookie文本"""
        self.cookiesEdit.setText(text)

    def getCurrentPath(self):
        """获取当前路径"""
        return self.current_path

    def setCurrentPath(self, path):
        self.current_path = str(path or self.default_path)
        self.pathCard.setContent(self.current_path)


class ScriptEntryGroupCard(BaseServiceGroupCard):
    """ScriptEntry 配置组卡片"""
    def __init__(self, parent=None, descriptor: ScriptEntryDescriptor | None = None):
        descriptor = descriptor or _SCRIPT_ENTRY_DESCRIPTOR
        super().__init__(parent, descriptor.display_name, descriptor.slug, descriptor.save_path_default)


class DanbooruGroupCard(ElidedGroupHeaderCardWidget):
    #: ``(translation id, config value)``. The id is resolved with ``qtTrId`` when
    #: the box is built, so the labels follow the UI language while the tuple
    #: itself stays plain data.
    SAVE_TYPE_OPTIONS = (
        ("danbooru.config.save_mode.default", None),
        ("danbooru.config.save_mode.by_search_tag", "search_tag"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setting_interface = parent

        self.setTitle(qtTrId("danbooru.config.title"))
        self.setBorderRadius(8)

        danbooru_conf = getattr(script_conf, "danbooru", {}) or {}
        self.current_path = danbooru_conf.get("save_path", DEFAULT_DANBOORU_SAVE_PATH)

        self.downloadConcurrencyEdit = SpinBox(self)
        self.downloadConcurrencyEdit.setRange(1, 10)
        self.downloadConcurrencyEdit.setValue(int(danbooru_conf.get("download_concurrency", 3)))

        self.saveTypeBox = ComboBox(self)
        for label_id, value in self.SAVE_TYPE_OPTIONS:
            self.saveTypeBox.addItem(qtTrId(label_id), userData=value)
        self.setSaveType(danbooru_conf.get("save_type"))

        self.pathButton = PushButton(qtTrId("common.save_path.select"))
        self.pathButton.setFixedWidth(120)
        self.pathButton.clicked.connect(self._onSelectFolder)

        self.pathCard = self.addGroup(
            FIF.DOWNLOAD, qtTrId("common.save_path.title"),
            self.current_path, self.pathButton)
        self.addGroup(
            AcggrIcon.SV_TYPE,
            qtTrId("danbooru.config.save_mode.title"),
            qtTrId("danbooru.config.save_mode_hint"),
            self.saveTypeBox,
        )
        self.addGroup(
            FIF.SPEED_HIGH,
            qtTrId("danbooru.config.concurrency"),
            qtTrId("danbooru.config.concurrency_hint"),
            self.downloadConcurrencyEdit,
        )
        # viewer 通用设置（pin 初值 / 缩放 / 显示清晰度档位）已按 PRD R5 迁到 SettingInterface
        # 「通用 > viewer 卡组」，本卡只留站点专属的 hideUnpreviewable。共享定义仍在
        # app/GUI/viewer/settings.py，两个 viewer 都读同一份。
        self.hideUnpreviewableSwitch = SwitchButton(self)
        self.hideUnpreviewableSwitch.setOnText("")
        self.hideUnpreviewableSwitch.setOffText("")
        self.hideUnpreviewableSwitch.setChecked(bool(danbooru_cfg.hideUnpreviewable.value))
        self.hideUnpreviewableSwitch.checkedChanged.connect(self._onHideUnpreviewableChanged)
        self.addGroup(
            FIF.FILTER,
            qtTrId("danbooru.config.hide_unpreviewable"),
            qtTrId("danbooru.config.hide_unpreviewable_hint"),
            self.hideUnpreviewableSwitch,
        )

    def _onHideUnpreviewableChanged(self, checked: bool):
        danbooru_cfg.hideUnpreviewable.value = bool(checked)
        danbooru_cfg.save()
        # Live-update every open tab: the danbooru interface is constructed
        # lazily on first visit, so it may not exist yet (nothing to refresh).
        interface = getattr(self.setting_interface, "danbooruInterface", None)
        if interface is not None:
            interface.apply_unpreviewable_filter()

    def _onSelectFolder(self):
        folder = QFileDialog.getExistingDirectory(self, qtTrId("danbooru.config.select_dir_title"))
        if folder:
            wanted_p = pathlib.Path(folder)
            cgs_path = ori_path.parent if ori_path.parent.joinpath("scripts/CGS.py").exists() else ori_path
            cgs_flag = str(wanted_p).startswith(str(cgs_path))
            drive_flag = len(wanted_p.parts) == 1 and wanted_p.drive
            if cgs_flag or drive_flag:
                InfoBar.error(
                    title='', content=qtTrId("settings.save.invalid_path"),
                    orient=Qt.Horizontal, isClosable=True, position=InfoBarPosition.TOP,
                    duration=5000, parent=self.setting_interface
                )
                return
            self.current_path = folder
            self.pathCard.setContent(self.current_path)
            # Same reasoning as ScriptEntryGroupCard._onSelectFolder.
            self.setting_interface.flush_auto_save()

    def getCurrentPath(self):
        return self.current_path

    def setCurrentPath(self, path):
        self.current_path = path
        self.pathCard.setContent(path)

    def getDownloadConcurrency(self):
        return self.downloadConcurrencyEdit.value()

    def setDownloadConcurrency(self, concurrency):
        self.downloadConcurrencyEdit.setValue(int(concurrency or 3))

    def getSaveType(self):
        return self.saveTypeBox.currentData()

    def setSaveType(self, save_type):
        index = self.saveTypeBox.findData(save_type)
        self.saveTypeBox.setCurrentIndex(index if index >= 0 else 0)


class AiGroupCard(ElidedGroupHeaderCardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setting_interface = parent
        self.setTitle(qtTrId("settings.ai.title"))
        self.setBorderRadius(8)

        self.urlEdit = LineEdit(self)
        self.urlEdit.setPlaceholderText(f"{qtTrId('common.example_prefix')} https://api.openai.com/v1")
        self.urlEdit.setClearButtonEnabled(True)
        self.urlEdit.setMinimumWidth(320)

        self.keyEdit = PasswordLineEdit(self)
        self.keyEdit.setPlaceholderText(qtTrId("settings.ai.key_placeholder"))
        self.keyEdit.setClearButtonEnabled(True)
        self.keyEdit.setMinimumWidth(320)

        self.modelEdit = LineEdit(self)
        self.modelEdit.setPlaceholderText(qtTrId("settings.ai.model_placeholder"))
        self.modelEdit.setClearButtonEnabled(True)
        # Same width as the two rows above: a narrower box on the third row of
        # the same card reads as an unfinished field.
        self.modelEdit.setMinimumWidth(320)

        self.addGroup(AcggrIcon.URL, qtTrId("settings.ai.base_url"),
                      qtTrId("settings.ai.base_url_hint"), self.urlEdit)
        self.addGroup(AcggrIcon.KEY, qtTrId("settings.ai.api_key"),
                      qtTrId("settings.ai.api_key_hint"), self.keyEdit)
        self.addGroup(FIF.ROBOT, qtTrId("settings.ai.model"),
                      qtTrId("settings.ai.model_hint"), self.modelEdit)

    def set_provider(self, *, url: object = None, key: object = None, model: object = None):
        self.urlEdit.setText(str(url or ""))
        self.keyEdit.setText(str(key or ""))
        self.modelEdit.setText(str(model or ""))

    def get_provider_fields(self) -> dict:
        from core.script.ai.kernel import AiProviderMgr

        return AiProviderMgr.normalize_fields(
            url=self.urlEdit.text(),
            key=self.keyEdit.text(),
            model=self.modelEdit.text(),
        )


class ImgPalaceGroupCard(ElidedGroupHeaderCardWidget):
    """imgPalace job token only — no enable switch; non-empty token = capability on."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setting_interface = parent
        self.setTitle("imgPalace")
        self.setBorderRadius(8)

        self.tokenEdit = PasswordLineEdit(self)
        self.tokenEdit.setPlaceholderText(qtTrId("settings.jsoneri.token_placeholder"))
        self.tokenEdit.setClearButtonEnabled(True)
        self.tokenEdit.setMinimumWidth(320)
        self.addGroup(
            AcggrIcon.KEY,
            qtTrId("settings.jsoneri.token_label"),
            qtTrId("settings.jsoneri.token_hint"),
            self.tokenEdit,
        )

    def set_token(self, token: object = None):
        self.tokenEdit.setText(str(token or ""))

    def get_token(self) -> str:
        return self.tokenEdit.text().strip()


class ComfyGroupCard(ElidedGroupHeaderCardWidget):
    """Comfy host only — empty host hides Comfy rows; no silent 8188 fallback in app."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setting_interface = parent
        self.setTitle("ComfyUI")
        self.setBorderRadius(8)

        self.hostEdit = LineEdit(self)
        self.hostEdit.setPlaceholderText(f"{qtTrId('common.example_prefix')} http://127.0.0.1:8188")
        self.hostEdit.setClearButtonEnabled(True)
        self.hostEdit.setMinimumWidth(320)
        self.addGroup(
            AcggrIcon.URL,
            qtTrId("settings.comfy.host_label"),
            qtTrId("settings.comfy.host_hint"),
            self.hostEdit,
        )

    def set_host(self, host: object = None):
        self.hostEdit.setText(str(host or ""))

    def get_host(self) -> str:
        return self.hostEdit.text().strip()


class _SettingFillScrollArea(ScrollArea):
    """ScrollArea whose vertical sizeHint is 0 so parent layout can stretch it to fill remainder."""

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(hint.width(), 0)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(hint.width(), 0)


class SettingInterface(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.parent_window = parent
        #: CGS config dir discovered at startup (``""`` = nothing importable).
        #: Passed in, not probed here: the platform-config scan already ran at
        #: startup, before the window existed.
        self.cgs_import_source = str(getattr(parent, "cgs_import_source", "") or "")
        #: Update probe state. The task manager is built on first use (it owns a
        #: toast stack and an InfoBar host, which a never-checking session should
        #: not pay for), and the startup timer is a member so it dies with the page.
        self._update_task_mgr = None
        #: Set when a manual click lands while the automatic probe is in flight.
        self._manual_update_check_pending = False
        self._update_check_timer = None
        self.setObjectName("SettingInterface")
        self.setupUi()

    def setupUi(self):
        _translate = QCoreApplication.translate
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(0, 0, 0, 0)
        self.outer_layout.setSpacing(0)

        # --- tab strip: general + one tab per nav business -------------------
        # Deliberately the *same* widget tree as WebInterface's strip (shared
        # AccentTabBar + strip QSS), just static: no close button, no add
        # button. Each tab carries the sidebar icon of the entry it mirrors, so
        # the page reads as "the settings of that nav entry".
        from ui.qfluent.components.accent_tabs import AccentTabBar

        self.tab_strip = QFrame(self)
        self.tab_strip.setObjectName("SettingTabStrip")
        strip_layout = QHBoxLayout(self.tab_strip)
        strip_layout.setContentsMargins(6, 2, 6, 2)
        strip_layout.setSpacing(0)
        self.tab_bar = AccentTabBar(self.tab_strip)
        self.tab_bar.setScrollable(False)
        self.tab_bar.setTabMaximumWidth(200)
        self.tab_bar.setTabShadowEnabled(False)
        self.tab_bar.setAddButtonVisible(False)
        self.tab_bar.setCloseButtonDisplayMode(TabCloseButtonDisplayMode.NEVER)
        self.tab_bar.enableTransparentBackground()
        self.tab_bar.itemLayout.setContentsMargins(0, 5, 0, 5)
        self.tab_bar.itemLayout.setSpacing(6)
        #: apply_tab_strip_style() finds the bar through this attribute.
        self.tab_strip.tab_bar = self.tab_bar
        strip_layout.addWidget(self.tab_bar, 1)
        # Styled by _apply_tab_strip_theme() once the page is assembled; it is
        # also the theme-change slot, so there is one code path, not two.

        # Tabs (key, icon, title), left-to-right in sidebar order.
        # - cbg sits between pawchive and jsoneri in the sidebar but has no tab:
        #   its single setting lives on its own interface.
        # - ``general`` is not a nav entry, so it leads.
        # Icon/title reuse the same values ``initNavigation`` uses, so the tab
        # strip cannot drift from the sidebar.
        self._setting_tabs = (
            ("general", FIF.SETTING, qtTrId("settings.tab.general")),
            ("danbooru", QIcon(":/danbooru.svg"), "Danbooru"),
            (_SCRIPT_ENTRY_DESCRIPTOR.slug, QIcon(_SCRIPT_ENTRY_DESCRIPTOR.icon),
             _SCRIPT_ENTRY_DESCRIPTOR.display_name),
            ("jsoneriPalaces", FIF.CLOUD, "jsoneriPalaces"),
        )

        self.pages_stack = QStackedWidget(self)
        # Each tab owns page -> scroll area -> content -> cards. One scroll area
        # per page (not a shared one re-parented on switch): re-parenting made
        # the cards end up as siblings of the scroll area instead of its
        # content, so the area was an empty strip, the page could not scroll,
        # and cards butted together edge-to-edge.
        self._page_layouts: dict[str, QVBoxLayout] = {}
        for key, icon, title in self._setting_tabs:
            page = QFrame(self.pages_stack)
            page.setObjectName(f"SettingPage_{key}")
            self.tab_bar.addTab(key, title, icon=icon)
            self.pages_stack.addWidget(page)

            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.setSpacing(0)

            scroll = _SettingFillScrollArea(page)
            scroll.setObjectName(f"SettingScrollArea_{key}")
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            # content sizeHint must not inflate ScriptWindow min height
            # (scriptWinRect owns geometry).
            scroll.setMinimumHeight(0)
            scroll.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
            if hasattr(scroll, "enableTransparentBackground"):
                scroll.enableTransparentBackground()

            content = QWidget(scroll)
            content.setObjectName(f"SettingPageContent_{key}")
            content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            layout = QVBoxLayout(content)
            # Margins + spacing are what make a card read as a card: a gap on
            # every side, and a visible gap between neighbours.
            layout.setContentsMargins(16, 12, 16, 16)
            layout.setSpacing(12)
            scroll.setWidget(content)

            page_layout.addWidget(scroll, 1)
            page._scroll = scroll
            page._content = content
            self._page_layouts[key] = layout

        #: The general page's scroll area, kept under the historical name.
        self.scroll_area = self.pages_stack.widget(0)._scroll

        self.outer_layout.addWidget(self.tab_strip)
        self.outer_layout.addWidget(self.pages_stack, 1)

        self.tab_bar.currentChanged.connect(self._switch_setting_page_by_index)

        self._build_general_page()
        self._build_danbooru_page()
        self._build_jsoneri_palaces_page()
        self._build_pawchive_page()
        self.tab_bar.setCurrentTab(self._setting_tabs[0][0])

        self._auto_save_wired = False
        self._loading_settings = False
        self._autosave_toast = None
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(AUTOSAVE_DEBOUNCE_MS)
        self._auto_save_timer.timeout.connect(self._auto_save_now)

        # Every card above is constructed, so the tree is complete: subscribe to
        # all editable controls at once instead of wiring each one.
        self._wire_auto_save()

        self._sync_theme_box()
        theme_mgr.subscribe(self._sync_theme_box)
        # The strip paints its own background from the QSS template, so it does
        # not follow the app palette: without this subscription a theme flip
        # left a dark strip over a light page (WebInterface subscribes for the
        # same reason).
        self._apply_tab_strip_theme()
        theme_mgr.subscribe(self._apply_tab_strip_theme)
        # The transparent-background qss leaves the scroll viewport with its own
        # (construction-time) palette — Qt then resolves scroll_content's palette
        # through that viewport, so a theme change would freeze the page background.
        # Re-apply the app palette to the three widgets after every theme change.
        self._apply_scroll_palette()
        theme_mgr.subscribe(self._apply_scroll_palette)
        # Lambda, not a bound method: PySide6 does not deliver a signal to a
        # bound-method slot of the object being destroyed, so
        # ``self.destroyed.connect(self._on_destroyed_unsubscribe_theme)`` would
        # never fire and the listener would leak for the process lifetime.
        self.destroyed.connect(lambda _obj=None: self._on_destroyed_unsubscribe_theme())
        #: An in-flight probe outlives its page if the user closes the window
        #: mid-probe. Cancel it (flag only -- the repo's IRON-1 rule forbids a
        #: join here) so the worker's result is dropped instead of delivered to
        #: a half-destroyed interface.
        self.destroyed.connect(lambda _obj=None: self._stop_update_probe())

        self._sync_import_card_visibility()
        self._schedule_startup_update_check()

    def _schedule_startup_update_check(self) -> None:
        """Arm the one startup probe (never fires twice per page instance).

        A member ``QTimer`` parented to this frame rather than ``singleShot``:
        the settings page is the landing page at startup, so the timer must die
        with the window instead of firing into a half-destroyed interface when
        the user closes within the delay.
        """
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(UPDATE_CHECK_STARTUP_DELAY_MS)
        timer.timeout.connect(self._auto_check_for_update)
        timer.start()
        self._update_check_timer = timer

    def _page_layout(self, key: str) -> QVBoxLayout:
        """Card layout of one tab (built in ``setupUi``)."""
        return self._page_layouts[key]

    def _switch_setting_page_by_index(self, index: int) -> None:
        """Adapt ``TabBar.currentChanged(int)`` to the key-based switcher.

        Only a *user* click emits ``currentChanged``; ``setCurrentIndex`` is
        silent in qfw. So this handler never calls back into the tab bar --
        that would loop -- and programmatic switches go through
        :meth:`_switch_setting_page` alone.
        """
        if 0 <= index < len(self._setting_tabs):
            self._switch_setting_page(self._setting_tabs[index][0], sync_tab=False)

    def _switch_setting_page(self, key: str, *, sync_tab: bool = True) -> None:
        """Show one settings tab. Each page owns its scroll area, so switching
        is just a stack index change."""
        index = next(i for i, (k, *_r) in enumerate(self._setting_tabs) if k == key)
        if sync_tab and self.tab_bar.currentIndex() != index:
            # setCurrentIndex is silent (no currentChanged), so this cannot
            # re-enter the click handler.
            self.tab_bar.setCurrentIndex(index)
        self.pages_stack.setCurrentIndex(index)

    def _build_general_page(self) -> None:
        """General tab: proxy/DoH, language, theme, aria2, AI, then the import row.

        Every row is a qfw ``SettingCard`` so this page reads like the rest of
        the app's settings. The import action sits at the very bottom, past a
        stretch: it is a one-shot migration, not a setting, and pinning it to the
        end keeps it out of the way of the values people actually change.
        """
        from ui.qfluent.components.setting_cards import LineEditSettingCard, WidgetSettingCard

        import res

        layout = self._page_layout("general")
        #: Pending language-switch restart (bar + 1 s ticker); see
        #: :meth:`_start_language_countdown`.
        self._language_restart_timer = None
        self._language_restart_bar = None

        # One group card, exactly like "Aria2 download engine" / "AI provider"
        # below it: a header with the group name and its rows inside. Not a
        # ``SettingCardGroup`` -- that is just a heading label, so the rows fell
        # out of the card look the rest of the page uses.
        self.general_card = ElidedGroupHeaderCardWidget(self)
        self.general_card.setTitle(qtTrId("settings.general.title"))
        self.general_card.setBorderRadius(8)

        # --- language -------------------------------------------------------
        # ``conf.lang == ""`` is the "follow the system" state
        # (``resolve_language("")`` falls back to the system locale), so the
        # first item simply stores the empty string.
        self.langBox = ComboBox()
        self.langBox.setMinimumWidth(160)
        self.langBox.addItem(qtTrId("settings.general.language_follow_system"), userData="")
        for code in res.supported_languages():
            self.langBox.addItem(UI_LANGUAGES.get(code, code), userData=code)
        self.general_card.addGroup(
            FIF.LANGUAGE, qtTrId("settings.general.language"),
            qtTrId("settings.general.language_hint"), self.langBox,
        )
        self.langBox.currentIndexChanged.connect(self._on_language_combo_changed)
        self._sync_language_box()

        # --- theme ----------------------------------------------------------
        # Three states, not a boolean: qconfig's themeMode is
        # ``OptionsConfigItem(Light, Dark, Auto)`` and Auto is "follow the
        # system". The old SwitchButton collapsed it to on/off, so choosing
        # "follow system" was simply not expressible.
        self.themeBox = ComboBox()
        self.themeBox.setMinimumWidth(160)
        # Literal qtTrId calls (not a loop over keys): the i18n scanner reads
        # direct calls, so a computed key would silently drop out of the tables.
        self.themeBox.addItem(qtTrId("settings.general.theme_follow_system"), userData=Theme.AUTO)
        self.themeBox.addItem(qtTrId("settings.general.theme_light"), userData=Theme.LIGHT)
        self.themeBox.addItem(qtTrId("settings.general.theme_dark"), userData=Theme.DARK)
        self.general_card.addGroup(
            FIF.QUIET_HOURS, qtTrId("settings.general.theme_toggle"),
            qtTrId("settings.general.theme_hint"), self.themeBox,
        )
        self.themeBox.currentIndexChanged.connect(self._on_theme_combo_changed)
        self._sync_theme_box()

        # --- proxy ----------------------------------------------------------
        self.proxyEdit = LineEdit()
        # "例如: URL" tells the user the grey text is an example to replace,
        # not a value already filled in.
        self.proxyEdit.setPlaceholderText(
            f"{qtTrId('common.example_prefix')} {qtTrId('settings.general.proxy_example')}")
        self.proxyEdit.setMinimumWidth(320)
        self.proxyEdit.setClearButtonEnabled(True)
        self.imgProxiesEdit = self.proxyEdit  # kept for the load/save collectors
        # History completer: the store owns MRU order, de-duplication and the
        # cap; the page only binds it to the field.
        self.proxyCompleter = self._install_history_completer(
            self.proxyEdit, acggr_cfg.histories.proxy,
        )
        # History is recorded at the commit point (focus loss / Enter), the same
        # moment the value is settled -- never per keystroke.
        self.proxyEdit.editingFinished.connect(self._on_proxy_committed)
        self.general_card.addGroup(
            FIF.GLOBE, qtTrId("settings.general.proxy_label"),
            qtTrId("settings.general.proxy_hint"), self.proxyEdit,
        )

        # --- DoH ------------------------------------------------------------
        # Same shape as the proxy row above: a plain input, committed on
        # editingFinished (focus loss / Enter). The old flow was a button that
        # opened a flyout with its own edit box and a separate save button --
        # two clicks and a floating window to change one string, and it looked
        # nothing like the row right above it.
        self.dohEdit = LineEdit()
        self.dohEdit.setPlaceholderText(f"{qtTrId('common.example_prefix')} {DEFAULT_DOH_URL}")
        self.dohEdit.setMinimumWidth(320)
        self.dohEdit.setClearButtonEnabled(True)
        self.dohEdit.setText(acggr_cfg.doh.get_url())
        # Same history completer as the proxy row.
        self.dohCompleter = self._install_history_completer(
            self.dohEdit, acggr_cfg.histories.doh,
        )
        self.dohEdit.editingFinished.connect(self._on_doh_edited)
        self.general_card.addGroup(
            FIF.WIFI, qtTrId("settings.general.doh"),
            qtTrId("settings.general.doh_hint"), self.dohEdit,
        )

        layout.addWidget(self.general_card)

        # --- viewer card group (PRD R5) -------------------------------------
        # All viewer-wide settings (pin / view_ratio / image source mode) live in
        # one card under the general tab; danbooru's page keeps only site rows.
        # build_viewer_settings walks the shared description table, so a new row
        # added there (e.g. source_mode) shows up here automatically.
        from app.GUI.viewer.settings import build_viewer_settings, viewer_store

        self.viewer_card = ElidedGroupHeaderCardWidget(self)
        self.viewer_card.setTitle(qtTrId("settings.general.viewer_card"))
        self.viewer_card.setBorderRadius(8)
        self.viewer_setting_rows = build_viewer_settings(self.viewer_card, viewer_store())
        layout.addWidget(self.viewer_card)

        # Cards keep SettingInterface as parent so setting_interface wiring stays valid.
        # CGS016: this card pulls the aria2 bootstrap chain (~950ms) — import it inside the
        # deferred Settings page instead of paying it during app.GUI package init.
        from app.GUI.settings_aria2 import Aria2GroupCard

        self.aria2_group_card = Aria2GroupCard(self)
        self.ai_card = AiGroupCard(self)
        # Download proxy / AI base URL: same history contract as the rows above
        # -- remembered values, committed on focus loss or Enter.
        self.downloadProxyCompleter = self._install_history_completer(
            self.aria2_group_card.proxy_edit, acggr_cfg.histories.download_proxy,
        )
        self.aria2_group_card.proxy_edit.editingFinished.connect(
            self._on_download_proxy_committed)
        self.aiUrlCompleter = self._install_history_completer(
            self.ai_card.urlEdit, acggr_cfg.histories.ai_url,
        )
        self.ai_card.urlEdit.editingFinished.connect(self._on_ai_url_committed)
        layout.addWidget(self.aria2_group_card)
        layout.addWidget(self.ai_card)
        layout.addStretch(1)

        self.importCard = PrimaryPushSettingCard(
            qtTrId("settings.general.import_from_cgs"),
            FIF.SAVE_AS,
            qtTrId("settings.general.import_from_cgs_title"),
            qtTrId("settings.general.import_from_cgs_hint"),
            parent=self,
        )
        self.importCard.clicked.connect(self._on_export_from_cgs)
        # Kept under the old name: the hide-once-imported logic and probes use it.
        self.exportFromCGSBtn = self.importCard
        layout.insertWidget(0, self.importCard)

        # --- update surfaces -------------------------------------------------
        # Imported here, not at module import: this page is the landing page, and
        # the banner module pulls the desktop-services/browser helpers that only
        # a "there is an update" click needs. Lazy at build time is enough.
        from app.GUI.settings_update import UpdateAvailableCard, UpdateCheckCard

        # Index 0: the banner is the one thing that must be seen without
        # scrolling. It is hidden until a probe actually finds a newer release,
        # so an up-to-date or offline launch shows no placeholder hole.
        self.update_available_card = UpdateAvailableCard(
            self,
            on_skip=self._on_skip_update_version,
            on_install=self._on_install_update,
        )
        layout.insertWidget(0, self.update_available_card)
        # Last real row, just before the stretch: a manual probe belongs next to
        # the values, not in a separate "about" surface.
        self.update_check_card = UpdateCheckCard(
            self, current_version=APP_VERSION, on_check=self._on_manual_update_check,
        )
        layout.insertWidget(layout.count() - 1, self.update_check_card)

    def _build_danbooru_page(self) -> None:
        """Danbooru tab: save path/type/concurrency + ComfyUI host (danbooru-only)."""
        self.danbooru_group_card = DanbooruGroupCard(self)
        self.comfy_card = ComfyGroupCard(self)
        self.comfyHostCompleter = self._install_history_completer(
            self.comfy_card.hostEdit, acggr_cfg.histories.comfy_host,
        )
        self.comfy_card.hostEdit.editingFinished.connect(self._on_comfy_host_committed)
        layout = self._page_layout("danbooru")
        layout.addWidget(self.danbooru_group_card)
        layout.addWidget(self.comfy_card)
        layout.addStretch(0)

    def _build_jsoneri_palaces_page(self) -> None:
        """jsoneriPalaces tab: probe + imgPalace token + storage root."""
        from app.GUI.settings_jsoneri import JsoneriGroupCard

        self.jsoneri_group_card = JsoneriGroupCard(self)
        self.imgpalace_card = ImgPalaceGroupCard(self)
        layout = self._page_layout("jsoneriPalaces")
        layout.addWidget(self.jsoneri_group_card)
        layout.addWidget(self.imgpalace_card)
        layout.addStretch(0)

    def _build_pawchive_page(self) -> None:
        """Pawchive tab: entry cookie + save path + redis (endpoint/password)."""
        from app.GUI.settings_redis import RedisGroupCard

        self.script_entry_group_card = ScriptEntryGroupCard(self)
        self.redis_group_card = RedisGroupCard(self)
        layout = self._page_layout("pawchive")
        layout.addWidget(self.script_entry_group_card)
        layout.addWidget(self.redis_group_card)
        layout.addStretch(0)

    def _sync_language_box(self):
        """Tick the combo to match the *stored* choice, not the resolved one.

        The distinction matters: ``conf.lang == ""`` (follow system) and an
        explicit value that happens to equal the system locale look identical
        after resolution, but they are different states and the combo must show
        which one is stored.
        """
        stored = str(getattr(script_conf, "lang", "") or "")
        index = self.langBox.findData(stored)
        if index < 0:
            index = 0  # unknown/legacy value -> follow system
        blocked = self.langBox.blockSignals(True)
        try:
            self.langBox.setCurrentIndex(index)
        finally:
            self.langBox.blockSignals(blocked)

    def _on_language_combo_changed(self, _index: int):
        """Persist the picked language and offer the restart."""
        self._on_language_selected(str(self.langBox.currentData() or ""))

    def _on_language_selected(self, lang: str):
        """Persist the choice, then restart acggr into the new language.

        ``lang`` may be ``""`` (follow the system); resolution to a concrete
        catalogue happens at every launch in ``app.bootstrap``. Not a hot switch:
        widgets render ``qtTrId(...)`` at construction and none implements
        ``retranslateUi``, so only a fresh process applies the new catalogue. The
        countdown text comes from the target catalogue, not ``qtTrId``, which
        still answers in the old language.
        """
        import res
        import res.i18n

        stored = str(getattr(script_conf, "lang", "") or "")
        # Compare the *stored* values: switching from an explicit zh_CN to
        # "follow system" is a real change even when the system is also zh_CN,
        # because the next launch must stop pinning it.
        if lang == stored:
            return
        # ``lang`` is a first-class dataclass field, so update() persists it the
        # same way save_conf() persists every other setting.
        script_conf.update(lang=lang)
        self._sync_language_box()
        # The concrete catalogue the next launch will use (may differ from the
        # empty stored value when following the system).
        resolved = res.resolve_language(lang)
        self._rebuild_language_checks(resolved)
        # Qt widgets cannot follow (see the docstring), but embedded web pages
        # can: AriaNg ships its own catalogue and repaints from it, so it must
        # switch now rather than after the restart. It takes the *resolved*
        # code: an empty stored value means "whatever the system says".
        from ui.core.lang import lang_mgr

        lang_mgr.notify(resolved)

        # Everything below works with the resolved catalogue -- an empty
        # ``lang`` has no table of its own.
        target = res.i18n.load_table(resolved)
        seconds = RESTART_DELAY_MS // 1000
        # Same toast as the autosave feedback: rises from the bottom-left and
        # leaves to the left. ``duration=-1`` keeps it up for the countdown;
        # the ring on the left drains instead of a seconds number in the text.
        from qfluentwidgets import InfoBarIcon
        from ui.qfluent.components.toast import ToastCountdownRing, show_toast

        bar = show_toast(
            self,
            _lang_text(target, "settings.language.restart_countdown",
                       fallback=resolved),
            icon=InfoBarIcon.WARNING, duration=-1, is_closable=True,
        )
        ring = ToastCountdownRing(bar)
        ring.start(seconds)
        self._start_language_countdown(bar, ring, target, resolved)

    def _start_language_countdown(self, bar, ring, target: dict, lang: str):
        """Relaunch after the countdown; closing the toast cancels.

        Only one countdown may be pending: picking another language while this
        one is running retires the previous bar and timer, otherwise the first
        timer would relaunch the process mid-countdown of the second.

        The ring animates on its own (``ring.start`` above); the timer here only
        decides *when* to relaunch.
        """
        from PySide6.QtCore import QTimer

        import app.restart

        self._retire_language_countdown()

        cancelled = {"v": False}
        timer = QTimer(self)
        timer.setInterval(RESTART_DELAY_MS)
        timer.setSingleShot(True)

        def _fire():
            timer.stop()
            ring.stop()
            if cancelled["v"]:
                return
            try:
                app.restart.spawn_replacement()
            except OSError as exc:
                self._show_save_error(
                    _lang_text(target, "settings.language.restart_failed",
                               fallback=lang), exc)
                return
            QApplication.instance().quit()

        def _cancel():
            # A closed toast must not relaunch: the user changed their mind.
            cancelled["v"] = True
            timer.stop()

        bar.closedSignal.connect(_cancel)
        # Qt deletes the bar once it slides out; drop our handle at that moment
        # (``_retire_language_countdown``) so the next switch cannot close a freed C++ object.
        bar.destroyed.connect(lambda _obj=None, _bar=bar: self._forget_language_bar(_bar))
        self._language_restart_timer = timer
        self._language_restart_bar = bar
        timer.timeout.connect(_fire)
        timer.start()

    def _retire_language_countdown(self) -> None:
        """Cancel the pending countdown and close its toast, safely.

        Both handles are dropped *before* touching the widget, and the close is
        guarded: an ``InfoBar`` deletes itself after sliding out and nothing
        clears ``_language_restart_bar`` on that path. The stale handle made
        ``previous_bar.close()`` raise ``RuntimeError: Internal C++ object
        (InfoBar) already deleted`` from *inside* the switch, before
        ``timer.start()`` -- so the language was persisted but the restart
        silently never happened.
        """
        timer = getattr(self, "_language_restart_timer", None)
        bar = getattr(self, "_language_restart_bar", None)
        self._language_restart_timer = None
        self._language_restart_bar = None
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass
        if bar is not None:
            try:
                bar.close()
            except RuntimeError:
                pass

    def _forget_language_bar(self, bar) -> None:
        """Drop our handle on ``bar`` once Qt has deleted it."""
        if getattr(self, "_language_restart_bar", None) is bar:
            self._language_restart_bar = None

    def _wire_auto_save(self):
        """Subscribe once to every editable control in the page.

        Generic rather than per-control: ``save_conf()`` collects the whole
        page, so a new setting only needs to exist in a card. **Matching is an
        ordered table, not an isinstance chain**: qfluentwidgets' ``ComboBox``
        subclasses ``QPushButton``, so a ``QAbstractButton`` branch placed first
        would bind ``toggled`` instead of ``currentIndexChanged``. Any unrecognised
        editable widget is reported by :meth:`_audit_auto_save_coverage`.
        """
        if self._auto_save_wired:
            return
        self._auto_save_wired = True

        for widget in self.findChildren(QWidget):
            connect = _autosave_connect(widget)
            if connect is not None:
                connect(self._schedule_auto_save)

        self._audit_auto_save_coverage()

    def _audit_auto_save_coverage(self):
        """Warn about editable widgets that no binding could claim.

        Unknown widget types are logged at construction so a new control that
        never saves is a visible defect instead of a silent user report.
        """
        missed = []
        for widget in self.findChildren(QWidget):
            if _autosave_connect(widget) is not None:
                continue
            if not _looks_editable(widget):
                continue
            missed.append(f"{type(widget).__name__}({widget.objectName() or '-'})")
        if missed:
            self._gui_logger().warning(
                "[ScriptSettings] editable widgets without an autosave binding "
                f"({len(missed)}): {', '.join(sorted(set(missed)))}. "
                "Add a matcher to AUTO_SAVE_BINDINGS or bind manually."
            )
        return missed

    def _schedule_auto_save(self, *_args):
        """Restart the debounce timer; the write itself happens in _auto_save_now."""
        if self._loading_settings:
            # ``show_self`` backfills every control on open and on refresh; those
            # programmatic setText/setValue calls must not write the file back.
            return
        self._auto_save_timer.start()

    def _auto_save_now(self):
        """Debounced flush. Silent on success: the page has no Save button to
        reassure the user, and a toast per keystroke pause would be noise."""
        if self._loading_settings:
            return
        self.save_conf(announce=False)

    def flush_auto_save(self):
        """Persist a pending edit immediately (used before the window closes)."""
        if self._auto_save_timer.isActive():
            self._auto_save_timer.stop()
            self._auto_save_now()

    def _set_autosave_hint(self, key: str, *, level: str = "success", **fmt):
        """Show a transient toast for the autosave result (empty key = nothing).

        Every save re-announces: *replace* the visible toast rather than
        suppress or stack it, so one bar stays on screen, its text and icon
        swap to the newest result, and its timer restarts.
        """
        if not key:
            return
        self._present_toast(qtTrId(key).format(**fmt), level=level)

    def _present_toast(self, text: str, *, level: str = "success", duration_ms: int = None):
        """Put ``text`` on the single bottom-left toast; returns the live bar.

        Callers that need to decorate the toast (the restart countdown's ring)
        take the return value rather than reaching into ``_autosave_toast``
        afterwards -- the attribute is cleared by ``closedSignal`` on the same
        event-loop turn a short toast can expire, so a stale read races.

        ``duration_ms`` overrides the default: the import toast must outlive its
        own restart delay, otherwise the ring (a child of the toast) dies before
        the countdown finishes and the restart callback races a deleted object.
        """
        from qfluentwidgets import InfoBarIcon

        from ui.qfluent.components.toast import refresh_toast, show_toast

        icons = {
            "success": InfoBarIcon.SUCCESS,
            "warning": InfoBarIcon.WARNING,
            "error": InfoBarIcon.ERROR,
            "info": InfoBarIcon.INFORMATION,
        }
        icon = icons.get(level, InfoBarIcon.INFORMATION)
        duration = AUTOSAVE_TOAST_MS if duration_ms is None else int(duration_ms)
        existing = self._autosave_toast
        if existing is not None and self._toast_is_live(existing):
            refresh_toast(existing, text, icon, duration_ms=duration)
            return existing
        toast = show_toast(self, text, icon=icon, duration=duration)
        self._autosave_toast = toast
        toast.closedSignal.connect(
            lambda: setattr(self, "_autosave_toast", None))
        return toast

    @staticmethod
    def _toast_is_live(toast) -> bool:
        """True while ``toast`` is still on screen and not already leaving."""
        try:
            return toast.isVisible() and not getattr(toast, "_acggr_leaving", False)
        except RuntimeError:
            # deleteLater already ran on the C++ object.
            return False

    def _rebuild_language_checks(self, lang: str):
        """Re-sync the combo after a switch.

        Kept as a separate step because the countdown path also calls it once
        the concrete language is known; ``_sync_language_box`` reads the stored
        value, so a resolved code here is only used for the countdown wording.
        """
        self._sync_language_box()

    def _apply_tab_strip_theme(self, _theme=None):
        """Repaint the settings tab strip for the current theme.

        Shared implementation with WebInterface (``apply_tab_strip_style``), so
        the two strips cannot drift; only the call site differs.
        """
        from ui.qfluent.components.accent_tabs import apply_tab_strip_style

        apply_tab_strip_style(self.tab_strip)

    def _on_destroyed_unsubscribe_theme(self, _obj=None):
        theme_mgr.unsubscribe(self._sync_theme_box)
        theme_mgr.unsubscribe(self._apply_scroll_palette)
        theme_mgr.unsubscribe(self._apply_tab_strip_theme)

    def _apply_scroll_palette(self, _theme=None):
        """Keep every page's scroll chain in sync with the app palette.

        Each tab owns its own scroll area, so this walks all of them: the
        transparent-background qss leaves each viewport with its
        construction-time palette, and Qt resolves the content widget's palette
        through that viewport.
        """
        pal = QApplication.instance().palette()
        for key in self._page_layouts:
            page = self.pages_stack.widget(
                next(i for i, (k, *_r) in enumerate(self._setting_tabs) if k == key))
            scroll = page._scroll
            scroll.setPalette(pal)
            scroll.viewport().setPalette(pal)
            page._content.setPalette(pal)

    def _on_theme_combo_changed(self, _index: int):
        """Apply the picked mode. ``Auto`` is a real value, not a fallback.

        ``theme_mgr.set_theme_mode`` writes through ``qconfig`` (``save=True``)
        and re-applies the palette, so Light/Dark/Auto all persist the same way.
        """
        mode = self.themeBox.currentData()
        if mode is None:
            return
        theme_mgr.set_theme_mode(mode, save=True)

    def _sync_theme_box(self, _theme=None):
        """Mirror the stored mode into the combo without re-issuing the change.

        Reads ``theme_mgr.themeMode`` (the qconfig value) rather than
        ``is_dark``: with Auto the resolved theme follows the system, so
        ``is_dark`` cannot tell "Auto on a dark system" from "Dark".
        """
        mode = theme_mgr.themeMode
        index = self.themeBox.findData(mode)
        if index < 0:
            index = 0
        blocked = self.themeBox.blockSignals(True)
        try:
            self.themeBox.setCurrentIndex(index)
        finally:
            self.themeBox.blockSignals(blocked)

    # ---- update detection ---------------------------------------------------

    def _update_task_manager(self):
        """Page-owned ``AsyncTaskManager``, built on first use.

        The settings page is the landing page, so this is the first thing every
        launch constructs; the manager eagerly allocates a tooltip stack and an
        InfoBar host. Deferring it keeps a launch that never checks (or whose
        probe is cached) from paying for them.
        """
        if self._update_task_mgr is None:
            from ui.async_task import AsyncTaskManager

            self._update_task_mgr = AsyncTaskManager(self)
        return self._update_task_mgr

    def _on_manual_update_check(self) -> None:
        from core.update import update_check_enabled

        if not update_check_enabled():
            # An explicit click deserves the real reason, not the network-error
            # toast: the probe is disabled by the environment, not broken.
            self._present_toast(
                qtTrId("settings.update.check_disabled"), level="warning")
            return
        self._start_update_check(manual=True)

    def _stop_update_probe(self) -> None:
        """Flag the in-flight probe as cancelled while this page is destroyed.

        Deliberately NOT ``manager.cancel_all_tasks()``: that path raises an
        InfoBar ("任务已取消") on this page, and ``destroyed`` fires when the C++
        widget is already gone -- the InfoBar then dies on a deleted parent. The
        per-thread ``cancel()`` only flips a flag, which is all a disappearing
        page needs (IRON-1: no join here either).
        """
        manager, self._update_task_mgr = self._update_task_mgr, None
        if manager is None:
            return
        for thread in list(getattr(manager, "current_tasks", {}).values()):
            try:
                thread.cancel()
            except RuntimeError:
                continue  # already reaped by Qt

    def _auto_check_for_update(self) -> None:
        """Startup probe: paint the cached answer, hit the network only if stale.

        Order matters. The cache is rendered first because it is what the user
        already saw last session (and it works offline); the network probe then
        runs only when the TTL expired, so a launch loop of quick restarts makes
        exactly one request per day.
        """
        from core.update import update_check_enabled

        if not update_check_enabled():
            return
        info = self._cached_update_info()
        if info is not None:
            self._show_update_available(info, scroll=False)
        else:
            self._sync_update_badge(False)
        if self._update_check_is_fresh():
            return
        # Probe even when a cached banner is on screen: the cache is the *last*
        # known release, and a newer one must still be discovered.
        self._start_update_check(manual=False)

    def _update_check_is_fresh(self) -> bool:
        try:
            last = float(acggr_cfg.updateLastCheckAt.value or 0)
        except (TypeError, ValueError):
            return False
        return last > 0 and (time.time() - last) < UPDATE_CHECK_INTERVAL_SECONDS

    def _cached_update_info(self):
        """Rehydrate the last accepted release, or ``None`` when it is moot.

        Re-validated against the *running* version: the cache is a snapshot from
        an older build, and after the user upgraded it must not resurface as
        "newer than 0.1.0" forever. The skip decision is re-applied here too,
        which is what keeps a dismissed version dismissed across launches.
        """
        from core.update import UpdateInfo, is_update_available

        version = str(acggr_cfg.updateCachedVersion.value or "").strip()
        if not version:
            return None
        skipped = str(acggr_cfg.updateSkippedVersion.value or "").strip()
        if version == skipped:
            return None
        if not is_update_available(APP_VERSION, version):
            return None
        return UpdateInfo(
            version=version,
            notes=str(acggr_cfg.updateCachedNotes.value or ""),
            published_at=str(acggr_cfg.updateCachedPublishedAt.value or ""),
            release_url=str(acggr_cfg.updateCachedReleaseUrl.value or ""),
            source="cache",
        )

    def _start_update_check(self, *, manual: bool) -> None:
        manager = self._update_task_manager()
        if manager.is_task_running(UPDATE_CHECK_TASK_ID):
            # The in-flight probe answers for both; queueing a second one would
            # only race the first result into the same widgets. The click is
            # still an explicit request, so the running probe is upgraded to a
            # manual one and answers with the user-visible toast.
            if manual:
                self._manual_update_check_pending = True
                self._set_update_check_busy(True)
                self._present_toast(qtTrId("settings.update.checking"), level="info")
            return
        if manual:
            self._set_update_check_busy(True)
        started = manager.execute_simple_task(
            self._probe_update_release,
            success_callback=lambda info: self._on_update_probe_result(info, manual=manual),
            error_callback=lambda error: self._on_update_probe_error(error, manual=manual),
            # Silent by contract: a background probe must never raise an InfoBar
            # of its own, and a manual check reports through the toast below.
            show_success_info=False, show_error_info=False, show_tooltip=False,
            task_id=UPDATE_CHECK_TASK_ID,
        )
        if not started:
            self._set_update_check_busy(False)

    @staticmethod
    def _probe_update_release():
        """Worker-thread body: network + parsing only, no widgets.

        Reads the proxy/DoH values at call time (the user may have just changed
        them on this page) and returns a :class:`ProbeOutcome`, which carries the
        "did any source answer" bit the caller needs to avoid erasing a banner
        on an offline launch.
        """
        from core import conf as core_conf
        from core.update import probe_updates

        return probe_updates(
            proxies=getattr(core_conf, "proxies", None) or [],
            doh_url=acggr_cfg.doh.get_url(),
        )

    def _on_update_probe_result(self, outcome, *, manual: bool) -> None:
        # A click that arrived while the automatic probe was in flight turns that
        # result into the manual one (see ``_start_update_check``).
        manual = bool(manual or self._manual_update_check_pending)
        self._manual_update_check_pending = False
        self._set_update_check_busy(False)
        if not getattr(outcome, "checked", False):
            # Offline / source down: do not stamp the TTL (retry next launch) and
            # do not clear the cache -- the cached banner is still the best known
            # answer. A manual attempt gets the only user-visible feedback.
            self._gui_logger().debug("[Settings] update check: no source reachable")
            if manual:
                self._present_toast(
                    qtTrId("settings.update.check_failed"), level="warning")
            return
        try:
            acggr_cfg.updateLastCheckAt.value = int(time.time())
        except (TypeError, ValueError):
            pass
        info = getattr(outcome, "info", None)
        if info is None:
            # A *successful* "nothing newer" answer invalidates both the cache
            # and any banner already on screen; otherwise the stored answer and
            # the one the user is looking at would contradict each other.
            self._clear_cached_update()
            acggr_cfg.save()
            card = getattr(self, "update_available_card", None)
            if card is not None:
                card.clear_update()
            self._sync_update_badge(False)
            if manual:
                self._present_toast(
                    qtTrId("settings.update.up_to_date").format(version=APP_VERSION),
                    level="info",
                )
            return
        self._cache_update_info(info)
        acggr_cfg.save()
        skipped = str(acggr_cfg.updateSkippedVersion.value or "").strip()
        if not manual and info.version == skipped:
            # Dismissed this version: honour silently on the automatic path
            # (make sure no banner for another version lingers). An explicit
            # check still shows the banner -- the user asked again.
            card = getattr(self, "update_available_card", None)
            if card is not None:
                card.clear_update()
            self._sync_update_badge(False)
            return
        self._show_update_available(info, scroll=manual)

    def _on_update_probe_error(self, error, *, manual: bool) -> None:
        manual = bool(manual or self._manual_update_check_pending)
        self._manual_update_check_pending = False
        self._set_update_check_busy(False)
        self._gui_logger().warning(f"[Settings] update check failed: {error}")
        if manual:
            self._present_toast(qtTrId("settings.update.check_failed"), level="warning")

    def _set_update_check_busy(self, busy: bool) -> None:
        card = getattr(self, "update_check_card", None)
        if card is not None:
            card.set_checking(busy)

    def _cache_update_info(self, info) -> None:
        acggr_cfg.updateCachedVersion.value = str(info.version or "")
        acggr_cfg.updateCachedNotes.value = str(info.notes or "")
        acggr_cfg.updateCachedPublishedAt.value = str(info.published_at or "")
        acggr_cfg.updateCachedReleaseUrl.value = str(info.release_url or "")

    def _clear_cached_update(self) -> None:
        acggr_cfg.updateCachedVersion.value = ""
        acggr_cfg.updateCachedNotes.value = ""
        acggr_cfg.updateCachedPublishedAt.value = ""
        acggr_cfg.updateCachedReleaseUrl.value = ""

    def _show_update_available(self, info, *, scroll: bool) -> None:
        card = getattr(self, "update_available_card", None)
        if card is None:
            return
        card.apply_update(info, current_version=APP_VERSION)
        # Passive half: the sidebar badge lives outside Settings, so a user who
        # never opens this page still learns that a build exists.
        self._sync_update_badge(True)
        self._notify_version_once(info)
        if scroll:
            # A manual check is triggered from the row at the bottom of the
            # page; the answer is at the top. Bring it into view rather than
            # leaving the user staring at an unchanged row.
            self._switch_setting_page("general")
            bar = self.scroll_area.verticalScrollBar()
            bar.setValue(bar.minimum())

    def _sync_update_badge(self, visible: bool) -> None:
        """Ask the main window to add/remove the Settings badge (no window = no-op)."""
        window = getattr(self, "parent_window", None)
        if window is None:
            return
        if visible:
            show = getattr(window, "show_update_badge", None)
            if callable(show):
                show()
        else:
            hide = getattr(window, "hide_update_badge", None)
            if callable(hide):
                hide()

    def _notify_version_once(self, info) -> None:
        """One toast per released version, then silence.

        The badge is the standing reminder; this is the only interruption, and it
        is keyed by version so a restart does not repeat it. ``NotifiedVersion``
        is separate from ``SkippedVersion`` on purpose: dismissing the toast is
        not "skip this version", so the badge must stay.
        """
        version = str(getattr(info, "version", "") or "").strip()
        if not version:
            return
        if str(acggr_cfg.updateNotifiedVersion.value or "").strip() == version:
            return
        acggr_cfg.updateNotifiedVersion.value = version
        acggr_cfg.save()
        self._present_toast(
            qtTrId("settings.update.notified").format(version=version),
            level="info", duration_ms=6000,
        )

    def _on_install_update(self, version: str) -> None:
        """Install ``version`` and restart into it (CGS flow, detached child).

        The install cannot run in this process: replacing the venv's
        site-packages under a live interpreter is what forces CGS to wait for
        the parent PID. So the dialog only *launches* a detached updater, then
        the app quits normally (``QApplication.quit`` keeps the aria2/atexit
        hand-over intact) and the child installs the pinned version and
        relaunches the app.
        """
        from core import acggr_log_dir
        from core.updater import detect_install, launch_update, payload_for

        target = detect_install(version)
        # ``upgradable`` is the one place the "can this install update itself"
        # rule lives: a source checkout / frozen bundle / missing uv has no
        # command, and pretending otherwise would leave a broken payload.
        if not target.upgradable:
            self._gui_logger().warning(
                f"[Settings] self-update unavailable: kind={target.kind} reason={target.reason}")
            self._present_toast(
                qtTrId("settings.update.install_unsupported"), level="warning", duration_ms=8000)
            return

        import app.restart as restart

        payload = payload_for(
            target, restart.relaunch_command(),
            acggr_log_dir().joinpath(f"update-{version}.log"),
        )
        try:
            launch_update(payload)
        except OSError:
            self._gui_logger().exception("[Settings] could not start the updater")
            self._present_toast(
                qtTrId("settings.update.install_failed"), level="error", duration_ms=8000)
            return

        self._present_toast(
            qtTrId("settings.update.installing").format(version=version),
            level="info", duration_ms=UPDATE_QUIT_DELAY_MS,
        )
        QTimer.singleShot(UPDATE_QUIT_DELAY_MS, self._quit_for_update)

    @staticmethod
    def _quit_for_update() -> None:
        application = QApplication.instance()
        if application is not None:
            application.quit()

    def _on_skip_update_version(self, version: str) -> None:
        """Permanent mute for one version: card, badge and toast all stand down."""
        acggr_cfg.updateSkippedVersion.value = str(version or "")
        acggr_cfg.save()
        card = getattr(self, "update_available_card", None)
        if card is not None:
            card.clear_update()
        self._sync_update_badge(False)
        self._present_toast(
            qtTrId("settings.update.skipped_toast").format(version=version), level="info")

    def _import_marker_path(self):
        from app import CGS_IMPORT_MARKER
        from core.config import conf_dir

        return conf_dir.joinpath(CGS_IMPORT_MARKER)

    def _sync_import_card_visibility(self) -> None:
        card = getattr(self, "importCard", None)
        if card is None:
            return
        if self.cgs_import_source and not self._import_marker_path().exists():
            card.show()
        else:
            card.hide()

    def _on_export_from_cgs(self):
        try:
            from core.config.import_cgs import import_from_cgs

            result = import_from_cgs(overwrite=False)
        except Exception as error:
            self._show_save_error(qtTrId("settings.import_from_cgs.failed"), error)
            return

        skipped = result.get("skipped")
        copied = result.get("copied") or []
        merged = result.get("merged") or []
        if skipped == "no_cgs":
            # The install vanished between startup and the click: retire the card
            # instead of leaving a row that can no longer do anything.
            self.cgs_import_source = ""
            self._sync_import_card_visibility()
            self._present_toast(
                qtTrId("settings.import_from_cgs.not_found"), level="warning")
            return
        if skipped == "already_imported":
            # Every target already existed: the decision was made before this
            # launch, so record it and retire the card.
            self._write_import_marker(skipped=skipped, copied=0)
            self._sync_import_card_visibility()
            self._present_toast(
                qtTrId("settings.import_from_cgs.already_imported"), level="warning")
            return
        self._write_import_marker(skipped=skipped, copied=len(copied) + len(merged))
        self._sync_import_card_visibility()
        toast = self._present_toast(
            qtTrId("settings.import_from_cgs.imported").format(
                count=len(copied) + len(merged)),
            level="success",
            # Outlive the restart delay, or the countdown ring (a child of the
            # toast) dies mid-drain and the restart callback races a deleted
            # object.
            duration_ms=RESTART_DELAY_MS + 400,
        )
        self._schedule_restart_after_import(toast)

    def _write_import_marker(self, *, skipped, copied: int) -> None:
        """Record that the user chose to import, so the card never comes back."""
        import time

        try:
            self._import_marker_path().write_text(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"skipped={skipped} copied={copied}\n",
                encoding="utf-8",
            )
        except OSError as error:
            # A read-only config dir must not turn a successful import into a
            # failure: the card simply shows again next launch.
            self._gui_logger().warning(f"[ScriptSettings] could not write CGS import marker: {error}")

    def _schedule_restart_after_import(self, toast=None):
        """Relaunch after the import toast has been readable for a moment.

        Same visual contract as the language switch: one ring on the toast
        drains for the whole delay (animated, not stepped), and a refused
        process start must be reported, not swallowed by the event loop.
        """
        from PySide6.QtCore import QTimer

        import app.restart
        from ui.qfluent.components.toast import ToastCountdownRing

        application = QApplication.instance()
        if application is None:
            return
        ring = None
        if toast is not None and self._toast_is_live(toast):
            ring = ToastCountdownRing(toast)
            ring.start(RESTART_DELAY_MS / 1000)

        def _relaunch():
            if ring is not None:
                ring.stop()
            # Same contract as the language countdown: a refused process start
            # must be reported, not swallowed by the event loop.
            try:
                app.restart.schedule_relaunch_and_quit(application)
            except OSError as error:
                self._show_save_error(
                    qtTrId("settings.import_from_cgs.restart_failed"), error)

        QTimer.singleShot(RESTART_DELAY_MS, _relaunch)

    # No resizeEvent/showEvent height juggling: each page is
    # ``page -> scroll area -> content`` in a plain QVBoxLayout, so the scroll
    # area fills the page on its own. The old manual ``setMaximumHeight``
    # arithmetic existed only to work around the cards being siblings of the
    # scroll area instead of its content.

    def _schedule_download_engine_restart(self, proxy: str):
        """Coalesce aria2 restarts: at most one worker in flight, one pending re-run.

        ``proxy`` is resolved by the caller *on the UI thread* and handed over
        explicitly: the worker must not read ``conf.yml`` itself, because
        ``script_conf.update()`` rewrites that file from the UI thread and a
        concurrent read can observe the truncated (empty) window.
        """
        self._engine_restart_proxy = proxy
        if getattr(self, "_engine_restart_thread", None) is not None and self._engine_restart_thread.is_alive():
            self._engine_restart_pending = True
            return
        self._engine_restart_pending = False
        self._engine_restart_thread = threading.Thread(
            target=self._run_download_engine_restart, name="acggr-aria2-restart", daemon=True,
        )
        self._engine_restart_thread.start()

    def _run_download_engine_restart(self):
        from core.script.aria2 import get_engine

        while True:
            try:
                get_engine().restart(proxy=self._engine_restart_proxy)
            except Exception as engine_error:
                self._gui_logger().warning(f"[ScriptSettings] aria2 restart after settings save: {engine_error}")
            if not self._engine_restart_pending:
                return
            self._engine_restart_pending = False

    def show_self(self):
        """加载配置文件内容到各个编辑框"""
        with open(script_conf.file, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f.read()) or {}

        # Backfilling fires textChanged/valueChanged on every control. Without
        # this guard the page would immediately schedule a write of the values
        # it just read, and -- worse -- would persist partial state while the
        # remaining cards are still unpopulated.
        self._loading_settings = True
        try:
            self._load_settings_into_widgets(config_data)
        finally:
            self._loading_settings = False
        self._set_autosave_hint("")

    def _load_settings_into_widgets(self, config_data: dict):
        """Assign ``config_data`` to every control. Caller owns the load guard."""
        from core.script.aria2.settings import get_history_limit, get_proxy

        self.imgProxiesEdit.setText(','.join(config_data.get('proxies') or []))
        self.aria2_group_card.set_proxy_text(get_proxy(config_data))
        self.aria2_group_card.set_history_limit(get_history_limit(config_data))
        script_entry_config = config_data.get(_SCRIPT_ENTRY_DESCRIPTOR.config_section, {})
        self.script_entry_group_card.setCookieText(script_entry_config.get('cookie', ''))
        self.script_entry_group_card.setCurrentPath(script_entry_config.get('sv_path', ''))
        runtime_config = DanbooruRuntimeConfig.from_mapping(config_data.get('danbooru', {}), doh_url=acggr_cfg.doh.get_url())
        self.danbooru_group_card.setCurrentPath(runtime_config.save_path)
        self.danbooru_group_card.setSaveType(runtime_config.save_type)
        self.danbooru_group_card.setDownloadConcurrency(runtime_config.download_concurrency)
        from core.script.ai.kernel import AiProviderMgr

        provider = AiProviderMgr(config_data.get("ai") or {}).provider
        self.ai_card.set_provider(url=provider.url, key=provider.key, model=provider.model)

        jsoneri_config = config_data.get("jsoneriPalacesProbe") or {}
        self.imgpalace_card.set_token(jsoneri_config.get("token") if isinstance(jsoneri_config, dict) else "")
        comfy_config = config_data.get("comfy") or {}
        self.comfy_card.set_host(comfy_config.get("host") if isinstance(comfy_config, dict) else "")
        redis_config = config_data.get("redis") or {}
        if isinstance(redis_config, dict) and redis_config:
            self.redis_group_card.set_redis_address(
                f"{redis_config.get('host') or ''}:{redis_config.get('port') or ''}")
            self.redis_group_card.set_password(redis_config.get("password") or "")
        else:
            self.redis_group_card.set_redis_address(None)
            self.redis_group_card.set_password(None)

    def _gui_logger(self):
        # Standalone repo: there is no host window to borrow a logger from, so
        # the Script settings page owns its own. Prefix kept identical to upstream.
        from loguru import logger

        return logger

    def _show_save_error(self, prefix: str, error: BaseException, *, log: bool = True):
        if log:
            self._gui_logger().exception(f"[ScriptSettings] {prefix}")
        self._present_toast(
            f"{prefix}: {summarize_error_message(error)}", level="error")

    def _install_history_completer(self, line_edit, history):
        """Bind a field to an ``MRUHistory`` store.

        The store owns the values (MRU order, de-duplication, cap, persistence);
        this only renders them, so a new remembered field is one call site.
        """
        completer = QCompleter(history.get(), line_edit)
        completer.setFilterMode(Qt.MatchStartsWith)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        line_edit.setCompleter(completer)
        return completer

    def _refresh_history_completer(self, line_edit, history) -> None:
        """Re-seed a history completer after its store changed."""
        completer = line_edit.completer()
        if completer is not None:
            completer.model().setStringList(history.get())

    def _on_proxy_committed(self):
        """Record the proxy at the *commit* point, not on every keystroke.

        History must mean "values the user actually settled on". The autosave
        path runs on a 600 ms debounce over ``textChanged``, so typing
        ``socks5://127.0.0.1:1080`` with a pause in the middle would otherwise
        push ``socks5://127.0`` into the list as if it were a used proxy -- the
        field would then offer its own half-typed garbage back.

        This is also why the write is not in ``save_conf``: that collector runs
        for every edit of every control, which is the wrong granularity for a
        *memory of choices*. Competitors solve it the same way -- a config write
        per ``editingFinished`` (or ``leaveEvent``), never per keystroke.
        """
        text = self.proxyEdit.text().strip()
        if not text:
            return
        values = []
        rejection = None
        for part in text.split(","):
            candidate = part.strip()
            if not candidate:
                continue
            try:
                values.append(proxy_url(candidate))
            except ValueError as error:
                # Incomplete/garbage entry: it must never become a remembered
                # value. The *report* lives here, at the commit point, because
                # ``save_conf`` runs on the autosave debounce and would fire
                # per typing pause instead of per edit.
                rejection = error
        if rejection is not None:
            self._show_save_error(
                qtTrId("settings.general.proxy_invalid"), rejection, log=False)
        if not values:
            return
        history = acggr_cfg.histories.proxy
        # Several proxies can be committed at once: newest first, rest as stored.
        history.set([*values, *history.get()])
        self._refresh_history_completer(self.proxyEdit, history)

    def _on_download_proxy_committed(self):
        """Record the aria2 download proxy at the commit point (single value).

        Validated with the same ``proxy_url`` as the access proxy: a half-typed
        or malformed endpoint must never become a suggestion. The field text
        stays as typed -- aria2 normalizes it on save -- and only the canonical
        form is remembered.
        """
        text = self.aria2_group_card.get_proxy_text()
        if not text:
            return
        try:
            canonical = proxy_url(text)
        except ValueError as error:
            self._show_save_error(
                qtTrId("settings.general.proxy_invalid"), error, log=False)
            return
        history = acggr_cfg.histories.download_proxy
        history.add(canonical)
        self._refresh_history_completer(self.aria2_group_card.proxy_edit, history)

    def _on_ai_url_committed(self):
        """Remember the AI base URL; the provider layer validates on save.

        ``AiProviderMgr.normalize_fields`` accepts any non-empty string, so
        there is nothing to reject here -- the field's own contract is "typed
        and settled", which is exactly what belongs in history.
        """
        text = self.ai_card.urlEdit.text().strip()
        if not text:
            return
        history = acggr_cfg.histories.ai_url
        history.add(text)
        self._refresh_history_completer(self.ai_card.urlEdit, history)

    def _on_comfy_host_committed(self):
        """Remember the ComfyUI host (same contract as the AI base URL)."""
        text = self.comfy_card.get_host()
        if not text:
            return
        history = acggr_cfg.histories.comfy_host
        history.add(text)
        self._refresh_history_completer(self.comfy_card.hostEdit, history)

    def _on_doh_edited(self):
        """Commit the DoH field (editingFinished: focus loss or Enter).

        ``editingFinished`` also fires when focus leaves without a change, so an
        unchanged value returns early instead of rewriting qconfig and
        restarting the DNS stub for nothing.

        An invalid value is rejected *and* reverted: the field must never show a
        URL that was not stored, or the next edit would appear to be based on a
        value the app is not actually using.
        """
        raw = self.dohEdit.text().strip()
        current = acggr_cfg.doh.get_url()
        if raw == current:
            return
        try:
            saved = acggr_cfg.doh.set_url(raw)
        except Exception as error:
            self._show_save_error(qtTrId("settings.general.doh_invalid"), error)
            self.dohEdit.setText(current)
            return
        self.dohEdit.setText(saved)
        # ``set_url`` already recorded the value through the shared store;
        # refresh so the entry just saved is offered next time without a restart.
        self._refresh_history_completer(self.dohEdit, acggr_cfg.histories.doh)
        self._save_doh_config(saved)
        self._set_autosave_hint("settings.save.autosaved")

    def _save_doh_config(self, doh_url: str):
        danbooru = getattr(self.parent_window, "danbooruInterface", None)
        if danbooru is not None:
            danbooru.refresh_runtime_settings()
        if hasattr(self.parent_window, "doh_stub_runtime"):
            self.parent_window.doh_stub_runtime.ensure(doh_url)

    def save_conf(self, announce: bool = True):
        """Collect the whole page and persist it to conf.yml.

        ``announce`` is what separates the two entry points: the automatic path
        passes ``False`` (a status line, no toast) because it runs after every
        edit pause, while an explicit caller keeps the success/warning InfoBar.

        The collector below is the single list of what this page owns. A new
        setting belongs in one place only -- its card -- plus one line here;
        nothing else has to learn about saving.
        """
        try:
            # 读取现有配置
            with open(script_conf.file, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f.read()) or {}

            from core.script.aria2 import get_engine
            from core.script.aria2.settings import set_history_limit, set_proxy

            proxies_text = self.imgProxiesEdit.text().strip()
            parsed_proxies = []
            rejected = []
            if proxies_text:
                for part in proxies_text.split(','):
                    candidate = part.strip()
                    if not candidate:
                        continue
                    try:
                        parsed_proxies.append(proxy_url(candidate))
                    except ValueError:
                        rejected.append(candidate)
                if not rejected:
                    config_data['proxies'] = parsed_proxies or None
            else:
                config_data['proxies'] = None

            config_data = set_proxy(self.aria2_group_card.get_proxy_text(), config_data=config_data)
            config_data = set_history_limit(self.aria2_group_card.get_history_limit(), config_data=config_data)

            config_section = _SCRIPT_ENTRY_DESCRIPTOR.config_section
            # 更新脚本入口配置
            if config_section not in config_data:
                config_data[config_section] = {}
            config_data[config_section]['cookie'] = self.script_entry_group_card.getCookieText()
            config_data[config_section]['sv_path'] = self.script_entry_group_card.getCurrentPath()
            config_data[config_section].pop('redis_key', None)

            if 'danbooru' not in config_data:
                config_data['danbooru'] = {}
            config_data['danbooru']['save_path'] = self.danbooru_group_card.getCurrentPath()
            config_data['danbooru']['save_type'] = self.danbooru_group_card.getSaveType()
            config_data['danbooru']['download_concurrency'] = self.danbooru_group_card.getDownloadConcurrency()
            config_data['danbooru'].pop('doh_url', None)
            config_data['danbooru'].pop('redis_key', None)
            config_data['danbooru'].pop('page_size', None)

            config_data['ai'] = {
                "provider": self.ai_card.get_provider_fields(),
            }

            existing_jsoneri = config_data.get("jsoneriPalacesProbe")
            if not isinstance(existing_jsoneri, dict):
                existing_jsoneri = {}
            else:
                existing_jsoneri = dict(existing_jsoneri)
            existing_jsoneri["token"] = self.imgpalace_card.get_token()
            config_data["jsoneriPalacesProbe"] = existing_jsoneri

            config_data["comfy"] = {
                "host": self.comfy_card.get_host(),
            }

            from app.GUI.settings_redis import parse_redis_address

            try:
                parsed_redis = parse_redis_address(self.redis_group_card.redisEdit.text())
            except ValueError:
                parsed_redis = None
            if parsed_redis is not None:
                existing_redis = config_data.get("redis")
                merged_redis = dict(existing_redis) if isinstance(existing_redis, dict) else {}
                merged_redis.update(parsed_redis)
                # The address row is ``host:port`` only; the password keeps
                # whatever the card committed on its own editingFinished.
                merged_redis["password"] = self.redis_group_card.get_password()
                config_data["redis"] = merged_redis

            script_conf.update(**config_data)
            # LLM presence is a process-wide capability bit (fav-translate + comfy_nl_row).
            from core.script.ai.kernel import AiProviderConfigSession

            AiProviderConfigSession.instance().reload(config_data.get("ai") or {})
            from core.script.aria2.settings import get_proxy

            self._schedule_download_engine_restart(get_proxy(config_data))
            danbooru = getattr(self.parent_window, "danbooruInterface", None)
            if danbooru is not None:
                danbooru.refresh_runtime_settings()
            jsoneri = getattr(self.parent_window, "jsoneriPalacesProbeInterface", None)
            if jsoneri is not None and hasattr(jsoneri, "refresh_config"):
                jsoneri.refresh_config(fetch=False)

            if announce:
                InfoBar.success(
                    title='', content=qtTrId("settings.save.succeeded"),
                    orient=Qt.Horizontal, isClosable=True, position=InfoBarPosition.BOTTOM,
                    duration=2500, parent=self
                )
            else:
                self._set_autosave_hint("settings.save.autosaved")
        except Exception as e:
            # A failed autosave is louder than a silent one: the toast carries
            # the short message, the InfoBar keeps the full error text.
            self._set_autosave_hint("settings.save.autosave_failed", level="error")
            self._show_save_error(qtTrId("settings.save.failed"), e)


class AcggrMainWindow(ScriptWindowBase):
    # Startup-only lazy factories: construct on first visit, then keep alive for the session.
    # Lazy is NOT continuous unload/delete of real interfaces.
    _INTERFACE_FACTORIES = {
        "danbooru": ("app.GUI.danbooru", "DanbooruInterface"),
        **{descriptor.slug: descriptor.interface_factory for descriptor in SCRIPT_ENTRY_DESCRIPTORS},
        "cbg": ("app.GUI.cbg", "CbgInterface"),
        "jsoneri": ("app.GUI.jsoneri", "JsoneriPalacesProbeInterface"),
        "web": ("app.GUI.web", "WebInterface"),
        "settings": (None, "SettingInterface"),
    }
    _ROUTE_KEYS = {
        "danbooru": "DanbooruInterface",
        **{descriptor.slug: descriptor.route_key for descriptor in SCRIPT_ENTRY_DESCRIPTORS},
        "cbg": "CbgInterface",
        "jsoneri": "JsoneriPalacesProbeInterface",
        "web": "WebInterface",
        "settings": "SettingInterface",
    }

    def __init__(
        self,
        parent=None,
        *,
        script_entry_state: dict | None = None,
        feedback_dispatcher: GuiExceptionFeedbackDispatcher | None = None,
        cgs_import_source: str = "",
    ):
        super().__init__()
        # ``parent`` is accepted for call-site compatibility only. In the
        # standalone repo there is no host main window: this window IS the app.
        self.script_entry_state = script_entry_state or {}
        self.cgs_import_source = str(cgs_import_source or "")
        if OFFSCREEN_FLUENT_FALLBACK:
            self._setup_offscreen_shell()
        self._mounted_keys: set[str] = set()
        self._deferred_nav_keys: set[str] = set()
        self._fluent_stack_hooked = False
        #: Landing sidebar key, resolved by ``initNavigation`` and materialized by
        #: ``start_post_first_paint_setup`` (``None`` = nothing to land on).
        self._initial_route_key: str | None = None
        self._post_first_paint_setup_started = False
        #: Child-widget splash covering this window during the deferred setup.
        #: ``None`` means "no splash" (off-screen fallback, missing logo, failure).
        self._startup_splash = None
        self._splash_created_at: float | None = None
        self._splash_shown_at: float | None = None
        self._splash_finish_scheduled = False
        # Self-owned dispatcher: no host window exists to hand one down, so the
        # window builds its own and registers the image viewer surface on it.
        self._feedback_dispatcher = feedback_dispatcher or GuiExceptionFeedbackDispatcher()
        self._exception_feedback_scope = None
        self.doh_stub_runtime = ScriptDoHStubRuntime(self)
        self.danbooruInterface = None
        for descriptor in SCRIPT_ENTRY_DESCRIPTORS:
            setattr(self, descriptor.attr_name, None)
        self.cbgInterface = None
        self.jsoneriPalacesProbeInterface = None
        self.webInterface = None
        self.settingInterface = None
        #: ``key -> sidebar widget`` for the entries that exist; the update badge
        #: anchors to the Settings entry, so it needs the real nav widget.
        self._nav_widgets: dict[str, QWidget] = {}
        self._update_badge = None
        self.doh_stub_runtime.ensure_from_config()
        install_spinbox_compat(QApplication.instance())

        # Light shell only: sidebar entries + geometry + splash. The landing
        # interface, the window title/icon and the splash dismissal all wait for
        # ``start_post_first_paint_setup`` (see ``app.start``).
        self.initNavigation()
        self.initWindow()
        self._create_startup_splash()
        safe_single_shot(0, self.doh_stub_runtime.flush_warning)
        QTimer.singleShot(0, self, lambda: __import__("app.GUI.web", fromlist=["premount_web_shell"]).premount_web_shell(self))  # A: idle pre-mount of the light Web shell after show()

    def _setup_offscreen_shell(self):
        self.setObjectName("OffscreenScriptWindow")
        self.setStyleSheet("QFrame#OffscreenScriptWindow { background: #f6f6f7; }")
        self._offscreen_nav_buttons: dict[str, PushButton] = {}
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.navigationInterface = _OffscreenNavigationInterface(self)
        self.stackedWidget = QStackedWidget(self)
        self.main_layout.addWidget(self.navigationInterface)
        self.main_layout.addWidget(self.stackedWidget, 1)

    def initNavigation(self):
        """Register the sidebar entries only; **no** interface is constructed here.

        Records the entry set and the landing key;
        :meth:`start_post_first_paint_setup` materializes the landing key once the
        window (and its splash) is on screen. Landing page: Settings when it is
        visible, otherwise the first visible top entry.
        """
        # 入口常驻：可用性不靠"隐藏入口"表达，而由 interface 自身的遮罩层表达
        # （点进去才检测；不可用则 blur 遮罩 + infobar + progressBar）。
        entries = (
            ("danbooru", "danbooru_visible", ":/danbooru.svg", "Danbooru", NavigationItemPosition.TOP),
            *(
                (descriptor.slug, descriptor.visibility_key, descriptor.icon, descriptor.display_name, NavigationItemPosition.TOP)
                for descriptor in SCRIPT_ENTRY_DESCRIPTORS
            ),
            ("cbg", "cbg_visible", ":/cbg.svg", "Cbg", NavigationItemPosition.TOP),
            ("jsoneri", "jsoneri_palaces_probe_visible", FIF.CLOUD, "jsoneriPalacesProbe", NavigationItemPosition.TOP),
        )
        first_key = None
        for key, visibility_key, icon, text, position in entries:
            if not self._script_entry_visible(visibility_key):
                continue
            if first_key is None:
                first_key = key
            self._register_nav_entry(key, icon, text, position, interface=None)

        self._initial_route_key = first_key
        if self._script_entry_visible("settings_visible"):
            self.navigationInterface.addSeparator()
            self._register_nav_entry(
                "web", FIF.GLOBE, "Web", NavigationItemPosition.BOTTOM, interface=None,
            )
            self._register_nav_entry(
                "settings", FIF.SETTING, "Settings", NavigationItemPosition.BOTTOM, interface=None,
            )
            # Settings is the landing page whether or not a top entry exists
            # (that was the pre-split behaviour; only construction moved).
            self._initial_route_key = "settings"

    def start_post_first_paint_setup(self):
        """Do the heavy part of startup once the window is on screen.

        Called by :func:`app.start` after ``show()`` + one
        ``processEvents()`` pass (while the splash covers the window). Builds the
        landing interface, switches the stack to it, restores the window
        title/icon, then dismisses the splash. Idempotent: the first call wins.
        """
        if self._post_first_paint_setup_started:
            return
        self._post_first_paint_setup_started = True
        try:
            key = self._initial_route_key
            if key is not None:
                if not OFFSCREEN_FLUENT_FALLBACK and not self._fluent_stack_hooked:
                    # Normally connected by ``_register_nav_entry`` when the first
                    # interface is added; nothing is added at registration time
                    # any more, so hook it before the landing page lands.
                    self.stackedWidget.currentChanged.connect(self._onCurrentInterfaceChanged)
                    self._fluent_stack_hooked = True
                interface = self._materialize_interface(key)
                self.switchTo(interface)
                if not OFFSCREEN_FLUENT_FALLBACK:
                    self.navigationInterface.setCurrentItem(self._route_key_for(key))
                    qrouter.setDefaultRouteKey(self.stackedWidget, self._route_key_for(key))
                    if hasattr(self, "_updateStackedBackground"):
                        self._updateStackedBackground()
            self._apply_window_identity()
        finally:
            self.finish_startup_splash()

    def _apply_window_identity(self):
        """Set the window title and icon.

        Deliberately not in ``initWindow``: the first ``adjustSize`` (which loads
        the font library, ~400 ms) belongs to the deferred phase, and the splash is
        a child widget, so the window needs no title of its own to show it.
        """
        self.setWindowIcon(QIcon(APP_LOGO_RESOURCE))
        self.setWindowTitle(APP_TITLE)

    def _create_startup_splash(self):
        """Cover this window with a logo splash while the deferred setup runs.

        The splash is a **child widget of this window** (``qfluentwidgets``
        ``SplashScreen``), never a second top-level window: one process, one main
        window handle, so the taskbar / Alt-Tab / window enumeration show a single
        entry, and the title bar, icon and geometry are already the real ones.

        Anything that can fail here (missing ``logo.png``, a broken icon, an
        import error) leaves ``_startup_splash`` as ``None`` and startup continues
        without a splash.
        """
        if OFFSCREEN_FLUENT_FALLBACK:
            return None
        try:
            logo_path = ori_path.joinpath("logo.png")
            logo = QIcon(str(logo_path))
            if not logo_path.exists() or logo.isNull():
                return None
            from qfluentwidgets import SplashScreen

            splash = SplashScreen(logo, self)
            # CGS hides exactly the three title-bar buttons; everything else about
            # the window stays the main window's own.
            for button_name in ("minBtn", "maxBtn", "closeBtn"):
                button = getattr(splash.titleBar, button_name, None)
                if button is not None:
                    button.hide()
            edge = int(min(
                STARTUP_SPLASH_ICON_MAX,
                max(STARTUP_SPLASH_ICON_MIN, self.height() * STARTUP_SPLASH_ICON_RATIO),
            ))
            splash.setIconSize(QSize(edge, edge))
            # Fill the window immediately: the base class only resizes itself from
            # the parent's Resize events, and geometry is already final here.
            splash.resize(self.size())
            splash.raise_()
            self._startup_splash = splash
            self._splash_created_at = time.monotonic()
            return splash
        except Exception:  # noqa: BLE001 - a splash failure must never block startup
            from loguru import logger

            logger.exception("[ScriptWindow] startup splash unavailable; starting without it")
            self._startup_splash = None
            return None

    def finish_startup_splash(self):
        """Dismiss the splash, no sooner than ``STARTUP_SPLASH_MIN_MS``. Idempotent."""
        if self._splash_finish_scheduled:
            return
        self._splash_finish_scheduled = True
        reference = self._splash_shown_at or self._splash_created_at
        if reference is None:
            self._dismiss_startup_splash()
            return
        remaining_ms = int(STARTUP_SPLASH_MIN_MS - (time.monotonic() - reference) * 1000)
        if remaining_ms <= 0:
            self._dismiss_startup_splash()
        else:
            QTimer.singleShot(remaining_ms, self, self._dismiss_startup_splash)

    def _dismiss_startup_splash(self):
        splash = self._startup_splash
        self._startup_splash = None
        if splash is None:
            return
        try:
            splash.finish()
        except RuntimeError:
            return
        splash.deleteLater()

    def showEvent(self, event):
        # Stamp the moment the splash actually becomes visible, so the
        # minimum-visible time is measured on screen and not from construction.
        super().showEvent(event)
        if self._startup_splash is not None and self._splash_shown_at is None:
            self._splash_shown_at = time.monotonic()

    def _attr_name_for_key(self, key: str) -> str:
        for descriptor in SCRIPT_ENTRY_DESCRIPTORS:
            if descriptor.slug == key:
                return descriptor.attr_name
        return {
            "danbooru": "danbooruInterface",
            "cbg": "cbgInterface",
            "jsoneri": "jsoneriPalacesProbeInterface",
            "web": "webInterface",
            "settings": "settingInterface",
        }[key]

    def _route_key_for(self, key: str) -> str:
        return self._ROUTE_KEYS[key]

    def _construct_interface(self, key: str):
        """Import+construct once. Real interfaces stay alive; never deleted for lazy policy."""
        if key in self._mounted_keys:
            return getattr(self, self._attr_name_for_key(key))
        module_path, class_name = self._INTERFACE_FACTORIES[key]
        if module_path is None:
            interface_cls = SettingInterface
        else:
            import importlib

            module = importlib.import_module(module_path)
            interface_cls = getattr(module, class_name)
        interface = interface_cls(self)
        setattr(self, self._attr_name_for_key(key), interface)
        self._mounted_keys.add(key)
        self._deferred_nav_keys.discard(key)
        # Wire the theme at construction time (not from a host window) so every
        # mount joins the switch-subscription chain regardless of entry order.
        setupTheme(interface)

        if key == "settings":
            interface.show_self()
        if key == "danbooru" and self._exception_feedback_scope is None:
            self._exception_feedback_scope = self._feedback_dispatcher.register_scope(
                owner=self,
                surfaces=(interface.image_viewer,),
                presenter=InfoBarExceptionPresenter(),
            )
        return interface

    def _materialize_interface(self, key: str):
        """Idempotent first-visit materialize: construct if needed, mount into stack once, keep alive."""
        interface = self._construct_interface(key)
        if self.stackedWidget.indexOf(interface) < 0:
            if not OFFSCREEN_FLUENT_FALLBACK:
                interface.setProperty("isStackedTransparent", False)
            self.stackedWidget.addWidget(interface)
            if not OFFSCREEN_FLUENT_FALLBACK and hasattr(self, "_updateStackedBackground"):
                self._updateStackedBackground()
        return interface

    def switchTo(self, interface):
        """Switch stacked page. Offscreen shell has no FluentWindow.switchTo."""
        if interface is None:
            raise ValueError("switchTo requires a live interface widget")
        if OFFSCREEN_FLUENT_FALLBACK:
            if self.stackedWidget.indexOf(interface) < 0:
                self.stackedWidget.addWidget(interface)
            self.stackedWidget.setCurrentWidget(interface)
            for nav_key, button in getattr(self, "_offscreen_nav_buttons", {}).items():
                mounted = getattr(self, self._attr_name_for_key(nav_key), None) if nav_key in self._ROUTE_KEYS else None
                button.setChecked(mounted is interface)
            return interface
        return super().switchTo(interface)

    def _ensure_and_switch(self, key: str):
        interface = self._materialize_interface(key)
        self.switchTo(interface)
        return interface

    def _nav_click_handler(self, key: str):
        """Fluent/QPushButton clicked(bool) passes a positional flag; must not bind over `key`."""

        def _on_nav_clicked(_trigger_by_user: bool = False):
            return self._ensure_and_switch(key)

        return _on_nav_clicked

    def _register_nav_entry(self, key: str, icon, text: str, position, *, interface: QFrame | None):
        """Register sidebar entry. With interface: first-visit already constructed; without: deferred."""
        route_key = self._route_key_for(key)
        if interface is None:
            self._deferred_nav_keys.add(key)
        else:
            if not OFFSCREEN_FLUENT_FALLBACK:
                interface.setProperty("isStackedTransparent", False)
            self.stackedWidget.addWidget(interface)

        if OFFSCREEN_FLUENT_FALLBACK:
            button = PushButton(text, self.navigationInterface)
            button.setCheckable(True)
            button.setMinimumHeight(40)
            button.setStyleSheet(
                """
                PushButton {
                    background: rgba(255, 255, 255, 0.04);
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    border-radius: 10px;
                    color: white;
                    text-align: left;
                    padding-left: 12px;
                }
                PushButton:checked {
                    background: rgba(255, 255, 255, 0.14);
                }
                """
            )
            self.navigationInterface.add_button(button, bottom=position == NavigationItemPosition.BOTTOM)
            self._offscreen_nav_buttons[key] = button
            self._nav_widgets[key] = button
            button.clicked.connect(self._nav_click_handler(key))
            if interface is not None and self.stackedWidget.count() == 1:
                self.stackedWidget.setCurrentWidget(interface)
                button.setChecked(True)
            return

        nav_item = self.navigationInterface.addItem(
            routeKey=route_key,
            icon=icon,
            text=text,
            onClick=self._nav_click_handler(key),
            position=position,
            tooltip=text,
        )
        if nav_item is not None:
            # qfw returns the item widget; keeping it is what lets a badge anchor
            # to the entry (``addItem`` returns None on some older builds).
            self._nav_widgets[key] = nav_item
        if interface is not None and self.stackedWidget.count() == 1:
            if not self._fluent_stack_hooked:
                self.stackedWidget.currentChanged.connect(self._onCurrentInterfaceChanged)
                self._fluent_stack_hooked = True
            self.navigationInterface.setCurrentItem(route_key)
            qrouter.setDefaultRouteKey(self.stackedWidget, route_key)
            if hasattr(self, "_updateStackedBackground"):
                self._updateStackedBackground()

    def show_update_badge(self) -> bool:
        """Breathing ``carbon:upgrade`` badge on the Settings entry.

        This is the *passive* half of update detection: a user who never opens
        Settings still sees that a new build exists. Idempotent -- the probe runs
        on every launch and the cache can re-render a banner, but only one badge
        may exist. Returns whether a badge is on screen.
        """
        if self._update_badge is not None:
            return True
        target = self._nav_widgets.get("settings")
        if target is None:
            return False
        try:
            from PySide6.QtGui import QIcon

            from ui.qfluent.components.badge import CustomBadge

            icon = QIcon(":/carbon-upgrade.svg")
            if icon.isNull():
                self._log_update_warning(":/carbon-upgrade.svg missing; badge skipped")
                return False
            badge = CustomBadge.make_ani_icon(
                self.navigationInterface, icon, size=(18, 18), target=target)
        except Exception:  # noqa: BLE001 - a badge must never break startup
            self._log_update_warning("badge unavailable", with_traceback=True)
            return False
        badge.show()
        self._update_badge = badge
        return True

    @staticmethod
    def _log_update_warning(message: str, *, with_traceback: bool = False) -> None:
        from loguru import logger

        if with_traceback:
            logger.opt(exception=True).warning(f"[Update] {message}")
        else:
            logger.warning(f"[Update] {message}")

    def hide_update_badge(self) -> None:
        """Retire the badge (version skipped, user already on it, or up to date)."""
        badge, self._update_badge = self._update_badge, None
        if badge is None:
            return
        try:
            badge.hide()
            badge.deleteLater()
        except RuntimeError:
            pass  # nav rebuilt / window already torn down

    def _script_entry_visible(self, visibility_key: str) -> bool:
        return bool(self.script_entry_state.get(visibility_key, True))

    def addSubInterface(self, interface, icon, text, position=NavigationItemPosition.TOP):
        """弃用：零调用者（导航统一走 _register_nav_entry）；删除时机待定。"""
        route_key = interface.objectName()
        key = next((entry_key for entry_key, name in self._ROUTE_KEYS.items() if name == route_key), None)
        if key is None:
            if OFFSCREEN_FLUENT_FALLBACK:
                raise ValueError(f"offscreen ScriptWindow cannot add unknown interface {route_key!r}")
            return super().addSubInterface(interface, icon, text, position)
        if key not in self._mounted_keys:
            setattr(self, self._attr_name_for_key(key), interface)
            self._mounted_keys.add(key)
        self._register_nav_entry(key, icon, text, position, interface=interface)
        return interface

    @staticmethod
    def _normalized_window_rect(x: int, y: int, width: int, height: int) -> QRect:
        """Keep restored geometry usable when a prior monitor was disconnected."""
        min_width, min_height = 640, 420
        width = max(min_width, int(width))
        height = max(min_height, int(height))
        proposed = QRect(int(x), int(y), width, height)
        screens = QApplication.screens()
        if not screens:
            return proposed

        def _visible_enough(rect: QRect) -> bool:
            for screen in screens:
                intersection = screen.availableGeometry().intersected(rect)
                # Require a usable title-bar / content strip so taskbar-only ghosts fail.
                if intersection.width() >= min(120, rect.width()) and intersection.height() >= 48:
                    return True
            return False

        if _visible_enough(proposed):
            return proposed

        primary = QApplication.primaryScreen() or screens[0]
        available = primary.availableGeometry()
        fitted_width = min(width, available.width())
        fitted_height = min(height, available.height())
        centered = QRect(
            available.x() + max(0, (available.width() - fitted_width) // 2),
            available.y() + max(0, (available.height() - fitted_height) // 2),
            fitted_width,
            fitted_height,
        )
        return centered

    def _disable_mica_backdrop(self):
        """Paint the window background in Qt instead of letting DWM do it.

        The title bar / navigation panel are qss-transparent so DWM colour shows
        through; on a dark system with an explicit Light theme that leaves dark
        chrome around a light Qt layer. Turning Mica off puts
        ``_normalBackgroundColor()`` in charge: qfw then paints
        ``setCustomBackgroundColor()`` on every theme change, so both legs stay
        consistent and no native attribute can fight it.
        """
        if OFFSCREEN_FLUENT_FALLBACK:
            return
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor(QColor(243, 243, 243), QColor(32, 32, 32))

    def initWindow(self):
        self._disable_mica_backdrop()
        # Minimum size set explicitly, not inherited from content: interface
        # construction is deferred, so an empty window would otherwise be shown
        # shrinkable and clip every page once the splash is gone. Same band as
        # ``_normalized_window_rect`` so restore never fights it.
        self.setMinimumSize(640, 420)
        saved_rect = acggr_cfg.scriptWinRect.value
        if saved_rect and len(saved_rect) >= 4:
            x, y, width, height = (int(value) for value in saved_rect[:4])
            geometry = self._normalized_window_rect(x, y, width, height)
            if geometry.x() != x or geometry.y() != y or geometry.width() != width or geometry.height() != height:
                from loguru import logger

                logger.warning(
                    f"[ScriptWindow] restored geometry off-screen or invalid "
                    f"saved={[x, y, width, height]} -> applied="
                    f"[{geometry.x()}, {geometry.y()}, {geometry.width()}, {geometry.height()}]"
                )
            self.setGeometry(geometry)
        else:
            # No host window to mirror: the 850x600 default is the only path.
            self.resize(850, 600)
        # Title, icon and ``settingInterface.show_self()`` are deferred to
        # ``start_post_first_paint_setup``: restoring geometry is cheap and must
        # happen before the splash measures itself against ``self.height()``.

    def server_mode_switch_blockers(self) -> list[str]:
        blockers = []
        script_entry_interfaces = tuple(
            getattr(self, descriptor.attr_name, None) for descriptor in SCRIPT_ENTRY_DESCRIPTORS
        )
        for interface in (self.danbooruInterface, *script_entry_interfaces, self.cbgInterface):
            if interface is None:
                continue
            blockers.extend(interface.server_mode_switch_blockers())
        return list(dict.fromkeys(blockers))

    def closeEvent(self, event):
        event.accept()
        # Autosave is debounced, so an edit made just before closing is still
        # pending. Flush it while the widgets (and their values) still exist.
        setting = getattr(self, "settingInterface", None)
        if setting is not None:
            try:
                setting.flush_auto_save()
            except Exception:  # noqa: BLE001 - closing must never be blocked
                pass
        if self._exception_feedback_scope is not None:
            self._exception_feedback_scope.close()
            self._exception_feedback_scope = None
        geometry = self._normalized_window_rect(self.x(), self.y(), self.width(), self.height())
        # Persist only a screen-safe rect so a disconnected monitor cannot trap the next open.
        acggr_cfg.scriptWinRect.value = [geometry.x(), geometry.y(), geometry.width(), geometry.height()]
        acggr_cfg.save()
        if self.danbooruInterface is not None:
            self.danbooruInterface.image_viewer.hide()
        if self.jsoneriPalacesProbeInterface is not None:
            self.jsoneriPalacesProbeInterface.close_service_window()


if __name__ == '__main__':
    import res.material_ct
    app = QApplication(sys.argv)
    w = AcggrMainWindow()
    w.show()
    app.exec()
