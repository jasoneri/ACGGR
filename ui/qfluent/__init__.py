import types
import contextlib
from PySide6.QtCore import qtTrId
from qfluentwidgets import (
    Action, RoundMenu, FluentIcon,
)

from .components import *

__all__ = [
    'MonkeyPatch',
    'CustomFlyout', 'CustomInfoBar',
    'RangeCalendar',
    'AcggrIcon',
]


def _exec_menu(menu, event):
    menu.exec(event.globalPos())


def _create_web_context_menu(web_view):
    with contextlib.suppress(AttributeError, RuntimeError):
        return web_view.createStandardContextMenu()
    return None


def _selected_web_text(web_view):
    request_getter = getattr(web_view, "lastContextMenuRequest", None)
    if callable(request_getter):
        with contextlib.suppress(RuntimeError):
            request = request_getter()
            if request is not None:
                if text := (request.selectedText() or "").strip():
                    return text
    with contextlib.suppress(AttributeError, RuntimeError):
        if text := (web_view.selectedText() or "").strip():
            return text
    with contextlib.suppress(AttributeError, RuntimeError):
        if text := (web_view.page().selectedText() or "").strip():
            return text
    return ""


_WEB_MENU_ICON_MAP = {
    'Copy': FluentIcon.COPY.icon(),
    'Copy link address': FluentIcon.COPY.icon(),
    'Cut': FluentIcon.CUT.icon(),
    'Paste': FluentIcon.PASTE.icon(),
    'Undo': FluentIcon.CANCEL.icon(),
    'Reload': FluentIcon.SYNC.icon(),
    'Back': FluentIcon.LEFT_ARROW.icon(),
    'Forward': FluentIcon.RIGHT_ARROW.icon(),
}

def _convert_menu(native_menu, menu_builder=None, icon_map=None):
    """将原生 QMenu 转换为 Fluent 风格 RoundMenu，menu_builder 负责前置自定义项。"""
    fluent_menu = menu_builder() if menu_builder else RoundMenu()
    for action in native_menu.actions():
        if not action.isVisible():
            continue
        if action.isSeparator():
            fluent_menu.addSeparator()
            continue
        action_text = str(action.text() or "").replace("&", "").strip()
        if not action_text:
            continue
        fluent_action = Action(text=action_text, shortcut=action.shortcut(), triggered=action.trigger)
        fluent_action.setEnabled(action.isEnabled())
        if action.isCheckable():
            fluent_action.setCheckable(True)
            fluent_action.setChecked(action.isChecked())
        icon = (icon_map or {}).get(action_text)
        if icon:
            fluent_action.setIcon(icon)
        fluent_menu.addAction(fluent_action)
    return fluent_menu


class MonkeyPatch:
    @staticmethod
    def rbutton_menu_lineEdit(line_edit, extra_actions=None, sub_menu=None):
        def new_context_menu(self, event):
            def _showCompleterMenu():
                if not self.text().strip():
                    self.setText(" ")
                self._showCompleterMenu()

            menu = RoundMenu(parent=self)
            undo_action = Action(FluentIcon.CANCEL, text=qtTrId("common.action.cancel"), triggered=self.undo)
            paste_action = Action(FluentIcon.PASTE, text=qtTrId("common.menu.paste"), triggered=self.paste)
            select_all_action = Action(qtTrId("common.menu.select_all"), triggered=self.selectAll)
            show_completer = Action(
                FluentIcon.ALIGNMENT,
                text=qtTrId("settings.preset.expand"),
                triggered=_showCompleterMenu)
            if not extra_actions and not sub_menu:
                menu.addAction(show_completer)
            for action in extra_actions or []:
                menu.addAction(action)
            for _menu in sub_menu or []:
                menu.addMenu(_menu)
            menu.addSeparator()
            menu.addAction(paste_action)
            menu.addAction(undo_action)
            menu.addAction(select_all_action)

            _exec_menu(menu, event)
            event.accept()
        line_edit.contextMenuEvent = types.MethodType(new_context_menu, line_edit)

    @staticmethod
    def rbutton_menu_WebEngine(browserWindow):
        def custom_context_menu(self, event):
            native_menu = _create_web_context_menu(self)
            menu = _convert_menu(native_menu, icon_map=_WEB_MENU_ICON_MAP) if native_menu is not None else RoundMenu()
            _exec_menu(menu, event)
            event.accept()
            if native_menu is not None:
                native_menu.deleteLater()

        web_view = browserWindow.view
        web_view.contextMenuEvent = types.MethodType(custom_context_menu, web_view)

    @staticmethod
    def rbutton_menu_widget(widget, menu_builder, event_name="contextMenuEvent"):
        """给任意 widget 挂 Fluent ``RoundMenu``（``ui`` 层公共入口）。

        ``ui`` 层组件不 import ``app`` 包，所以这里只收"事件对象"：
        ``menu_builder(event)`` 拿 ``QContextMenuEvent`` 自行取
        ``globalPos()`` 并返回构造好的菜单（``None`` = 不弹）。
        与 ``rbutton_menu_tabBar`` 同款思路：PySide6 对实例级覆写既可能传
        ``(event)`` 也可能传 ``(self, event)``，``args[-1]`` 恒是事件。
        """

        def handler(*args):
            event = args[-1]
            menu = menu_builder(event)
            if menu is not None:
                _exec_menu(menu, event)
            event.accept()

        setattr(widget, event_name, handler)

    @staticmethod
    def rbutton_menu_tabBar(tab_bar, menu_builder=None):
        """tab 栏右键菜单：给每个 ``TabItem`` 挂 Fluent ``RoundMenu``。

        ``TabItem`` 会把鼠标事件 forward 给 ``TabBar.view``（qfluentwidgets 内部
        拖拽逻辑），右键 menu 事件不会落到 ScrollArea 的 viewport 上，所以不能走
        eventFilter —— 与 ``rbutton_menu_lineEdit`` 同款：实例级覆写
        ``contextMenuEvent``。item 是动态增删的，addTab/insertTab 一并包一层，
        新 item 建出来即挂菜单。``menu_builder(route_key)`` 返回构造好的菜单；
        item 命中失败（点在空隙）就不弹。
        """
        def _patch_item(item):
            # PySide6's virtual dispatch may call the instance override with
            # (event) or (self, event); accept both shapes.
            def contextMenuEvent(*args):
                event = args[-1]
                menu = menu_builder(item.routeKey()) if menu_builder else None
                if menu is not None:
                    _exec_menu(menu, event)
                event.accept()
            item.contextMenuEvent = contextMenuEvent

        for item in tab_bar.items:
            _patch_item(item)

        original_insertTab = tab_bar.insertTab

        def insertTab(tab_bar_self, index, routeKey, text, icon=None, onClick=None):
            item = original_insertTab(index, routeKey, text, icon, onClick)
            _patch_item(item)
            return item

        tab_bar.insertTab = types.MethodType(insertTab, tab_bar)
        tab_bar.addTab = lambda routeKey, text, icon=None, onClick=None: tab_bar.insertTab(-1, routeKey, text, icon, onClick)
