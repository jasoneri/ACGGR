"""站点无关的 viewer 设置层（danbooru / pawchive / 未来站点共用）。

PRD R3.2/R4：缩放、适配基准、默认置顶属于 viewer，不属于站点。
1. 描述层 VIEWER_SETTINGS（ViewerSetting 表）说明 viewer 通用设置，不持有 QWidget / 站点对象。
2. 构建层 add_viewer_setting / build_viewer_settings 按描述表长出同一套行。
读值入口：viewer_view_ratio / viewer_view_ratio_percent / viewer_default_pin。
归属：viewer 通用（view_ratio / isDanbooruViewerDafaultPin）进表；站点数据/列表（save_path /
save_type / download_concurrency / hideUnpreviewable / zoom_index）留下。
持久化纪律：不改 key、不搬文件；store_attr 即既有 ConfigItem 属性名，title_id / hint_id 沿用既有
danbooru.config.* 的 qtTrId（本层无 res/i18n 所有权）。
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal, qtTrId

#: 行类型：开关（SwitchButton，落在 ``addGroup`` 的一行里）。
ROW_SWITCH = "switch"
#: 行类型：区间卡（RangeSettingCard，追加在 group 行之后，带分隔线）。
ROW_RATIO = "ratio"
#: 行类型：下拉框（ComboBox，落在 ``addGroup`` 的一行里，用于三档枚举）。
ROW_COMBO = "combo"

#: 本层新增的 qtTrId 键登记处。``res/i18n/build.py --sync`` 通过 AST 扫描代码里的
#: id 字面量维护各语言词条表；下面这些 id 同时作为 ``title_id`` / ``hint_id`` 出现在
#: :data:`VIEWER_SETTINGS` 里，被 build.py 的 ``ID_KEYWORDS`` 规则直接识别。此元组再显式
#: 登记一次，双重保险：无论扫描规则怎么变，这些 id 都不会被 ``--sync`` 当作 stale 删掉。
#: 注：文案由 res/i18n 词条表提供，未登记译文时 Qt 会显示 id 本身（不会静默回退中文）。
_VIEWER_I18N_KEYS = (
    "danbooru.config.viewer_source_mode",
    "danbooru.config.viewer_source_mode_hint",
    "danbooru.config.viewer_source_mode_auto",
    "danbooru.config.viewer_source_mode_on",
    "danbooru.config.viewer_source_mode_off",
)

#: **只在 store 完全不提供该 key 时**使用的回落值。正常路径下的默认值不在这里：
#: 它由 ``DanbooruConfig.view_ratio`` 这个 ConfigItem 的出厂默认决定（当前来自
#: conf 的 ``view_ratio``，``_default_danbooru_view_ratio()`` 里 ``or 65`` 只是
#: 这条链的兜底），区间由 RangeValidator(30, 85) 约束。这里的 65 与 pawchive
#: 旧私有常量一致，仅为"store 缺 key 也不至于算出 0% 面积"这一种退化情形。
FALLBACK_VIEW_RATIO_PERCENT = 65
#: 同上：viewer 默认置顶的回落值（danbooru 与 pawchive 旧默认都是 True）。
FALLBACK_DEFAULT_PIN = True


@dataclass(frozen=True, slots=True)
class ViewerSetting:
    """一条 viewer 通用设置的描述（纯数据，可安全跨站点复用）。

    ``key`` 是 viewer 层的逻辑键（``default_pin`` / ``view_ratio``），站点无关；
    ``store_attr`` 是持久化载体上 ConfigItem 的属性名，必须与既有 key 逐字一致
    ——那是"不得改 key"的落点，不是可以顺手重命名的字段。
    """

    #: 逻辑键，供 :func:`viewer_setting_value` 等读值入口使用。
    key: str
    #: 所属 qconfig 对象上的 ConfigItem 属性名（既有持久化 key，不可改）。
    store_attr: str
    #: 标题 / 副标题的翻译 id（沿用既有文案，不新增 id）。
    title_id: str
    hint_id: str
    #: ``FluentIcon`` 成员名；构建行时才解析，避免本模块导入 qfluentwidgets。
    icon: str = "PIN"
    #: :data:`ROW_SWITCH` 或 :data:`ROW_RATIO`。
    kind: str = ROW_SWITCH


#: viewer 共享设置表。顺序 = 设置页里的自然分组顺序（先开关组，再区间卡）。
VIEWER_SETTINGS: tuple[ViewerSetting, ...] = (
    ViewerSetting(
        key="default_pin",
        store_attr="isDanbooruViewerDafaultPin",
        title_id="danbooru.config.viewer_default_pin",
        hint_id="danbooru.config.viewer_default_pin_hint",
        icon="PIN",
    ),
    ViewerSetting(
        key="view_ratio",
        store_attr="view_ratio",
        title_id="danbooru.config.display_scale",
        hint_id="danbooru.config.display_scale_hint",
        icon="ZOOM",
        kind=ROW_RATIO,
    ),
    ViewerSetting(
        key="source_mode",
        store_attr="viewerSourceMode",
        title_id="danbooru.config.viewer_source_mode",
        hint_id="danbooru.config.viewer_source_mode_hint",
        icon="VIEW",
        kind=ROW_COMBO,
    ),
)

_VIEWER_SETTINGS_BY_KEY: dict[str, ViewerSetting] = {s.key: s for s in VIEWER_SETTINGS}


def get_viewer_setting(key: str) -> ViewerSetting:
    """按逻辑键取描述；拼错键是编程错误，直接 ``KeyError`` 而不是静默跳过。"""
    try:
        return _VIEWER_SETTINGS_BY_KEY[key]
    except KeyError:
        known = ", ".join(sorted(_VIEWER_SETTINGS_BY_KEY))
        raise KeyError(f"unknown viewer setting {key!r}; known: {known}") from None


def viewer_store() -> t.Any:
    """共享 viewer 设置的持久化载体。

    目前只有 danbooru 的 qconfig 定义过这两条 key；按"不改 key、不搬文件"的约束，
    pawchive 也读同一份 —— 这正是 R3.2 要的"设置属于共享 viewer，不属于站点"。
    延迟导入：纯读值调用方（viewer 构造）不应该为了 import 本模块就加载 qconfig。
    """
    from core.config.qc import danbooru_cfg

    return danbooru_cfg


def viewer_setting_item(setting: t.Union[ViewerSetting, str], store: t.Any = None):
    """``store`` 上对应的 ConfigItem；store 不提供这条设置时返回 ``None``。"""
    resolved = get_viewer_setting(setting) if isinstance(setting, str) else setting
    return getattr(store if store is not None else viewer_store(), resolved.store_attr, None)


def available_viewer_settings(store: t.Any = None) -> tuple[ViewerSetting, ...]:
    """``store`` 真正提供的描述子集（站点没定义某条 key 就不显示那行）。"""
    return tuple(s for s in VIEWER_SETTINGS if viewer_setting_item(s, store) is not None)


def viewer_setting_value(key: str, store: t.Any = None, default=None):
    """读一条 viewer 设置的当前值；store 没有这条 key → ``default``。"""
    item = viewer_setting_item(key, store)
    return default if item is None else item.value


def viewer_view_ratio_percent(store: t.Any = None) -> int:
    """图片展示缩放（面积比，%）—— danbooru / pawchive 的 fit 基准同一个来源。"""
    raw = viewer_setting_value("view_ratio", store, FALLBACK_VIEW_RATIO_PERCENT)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return FALLBACK_VIEW_RATIO_PERCENT


def viewer_view_ratio(store: t.Any = None) -> float:
    """面积比的 0..1 形式，供 fit 计算直接乘（``sqrt`` 由调用方按需做）。"""
    return viewer_view_ratio_percent(store) / 100


def viewer_default_pin(store: t.Any = None) -> bool:
    """新会话打开 viewer 时的 pin（置顶）初值；只当初值用，不回写。"""
    return bool(viewer_setting_value("default_pin", store, FALLBACK_DEFAULT_PIN))


#: source_mode 的合法档位与下拉框顺序（auto / on / off）。
_SOURCE_MODES: tuple[str, ...] = ("auto", "on", "off")


def viewer_source_mode(store: t.Any = None) -> str:
    """viewer 显示清晰度档位：``"auto"`` / ``"on"`` / ``"off"``（非法值回 ``"auto"``）。"""
    raw = viewer_setting_value("source_mode", store, "auto")
    return raw if raw in _SOURCE_MODES else "auto"


# ---- source_mode 热切换广播（站点无关）----
# 档位改动必须让**已打开**的 viewer 立即按新档重出图（用户现场诉求：改成原图却
# 还是缩略图，因为没人把变更推给活着的浮窗）。两站纪律都禁止彼此 import，所以
# 广播放共享层：设置下拉落盘后 emit，各站 viewer 控制器构造时 subscribe。
class _ViewerSourceModeBus(QObject):
    #: 新档位（``"auto"`` / ``"on"`` / ``"off"``）。同线程直连，无需 QCoreApplication。
    changed = Signal(str)

_bus: t.Optional[_ViewerSourceModeBus] = None


def _get_bus() -> _ViewerSourceModeBus:
    global _bus
    if _bus is None:
        _bus = _ViewerSourceModeBus()
    return _bus


def subscribe_source_mode_changed(handler) -> None:
    """订阅 source_mode 变更；viewer 控制器在构造时挂一次（直接连接，同 GUI 线程）。"""
    _get_bus().changed.connect(handler)


def emit_source_mode_changed(value: str) -> None:
    """设置侧落盘后广播新档位；无人订阅时是安全的空 emit。"""
    _get_bus().changed.emit(str(value))


def _persist_switch(setting: ViewerSetting, store, checked: bool) -> None:
    """开关立即落盘（与改造前 ``_onViewerDefaultPinChanged`` 完全一致）。"""
    item = getattr(store, setting.store_attr, None)
    if item is None:
        return
    item.value = bool(checked)
    save = getattr(store, "save", None)
    if callable(save):
        save()


def _build_switch_row(card, setting: ViewerSetting, item, store):
    from qfluentwidgets import SwitchButton

    switch = SwitchButton(card)
    switch.setOnText("")
    switch.setOffText("")
    switch.setChecked(bool(item.value))
    switch.checkedChanged.connect(
        lambda checked, s=setting, st=store: _persist_switch(s, st, checked)
    )
    return switch


#: 下拉框三档的文案 id，顺序与 :data:`_SOURCE_MODES` 一一对应。
_SOURCE_MODE_LABEL_IDS = (
    "danbooru.config.viewer_source_mode_auto",
    "danbooru.config.viewer_source_mode_on",
    "danbooru.config.viewer_source_mode_off",
)


def _persist_combo(setting: ViewerSetting, store, value: str) -> None:
    """下拉档位立即落盘（与 :func:`_persist_switch` 同一套纪律）。

    ``source_mode`` 落盘后向共享 bus 广播：活着的 viewer 控制器据此按新档重出
    当前项（热切换，用户现场"改成原图还是缩略图"的根因就是这条广播缺失）。
    """
    item = getattr(store, setting.store_attr, None)
    if item is None:
        return
    item.value = value
    save = getattr(store, "save", None)
    if callable(save):
        save()
    if setting.key == "source_mode":
        emit_source_mode_changed(value)


def _build_combo_row(card, setting: ViewerSetting, item, store):
    from qfluentwidgets import ComboBox

    combo = ComboBox(card)
    combo.addItems([qtTrId(label_id) for label_id in _SOURCE_MODE_LABEL_IDS])
    current = item.value if item.value in _SOURCE_MODES else "auto"
    combo.setCurrentIndex(_SOURCE_MODES.index(current))
    combo.currentIndexChanged.connect(
        lambda idx, s=setting, st=store: _persist_combo(s, st, _SOURCE_MODES[idx])
    )
    return combo


def _build_ratio_row(card, setting: ViewerSetting, item, store):
    from qfluentwidgets import FluentIcon, RangeSettingCard

    return RangeSettingCard(
        item, getattr(FluentIcon, setting.icon),
        qtTrId(setting.title_id), qtTrId(setting.hint_id), card,
    )


#: 行类型 → 构建器（卡片工厂表）。新增一种控件只在这里加一行，站点卡不用改。
VIEWER_SETTING_BUILDERS = {
    ROW_SWITCH: _build_switch_row,
    ROW_RATIO: _build_ratio_row,
    ROW_COMBO: _build_combo_row,
}


def _add_switch_row(card, setting: ViewerSetting, widget) -> None:
    from qfluentwidgets import FluentIcon

    card.addGroup(
        getattr(FluentIcon, setting.icon),
        qtTrId(setting.title_id),
        qtTrId(setting.hint_id),
        widget,
    )


def _add_combo_row(card, setting: ViewerSetting, widget) -> None:
    """下拉框与开关一样占一个 ``addGroup`` 行，摆放逻辑相同。"""
    _add_switch_row(card, setting, widget)


def _add_ratio_row(card, setting: ViewerSetting, widget) -> None:
    # 区间卡不是 group 行：它是 ``groupLayout`` 里的一个 widget。改造前的做法是
    # 先把上一 group 行的分隔线打开，再 addWidget —— 原样保留，外观不变。
    if getattr(card, "groupWidgets", None):
        card.groupWidgets[-1].setSeparatorVisible(True)
    card.groupLayout.addWidget(widget)


_ROW_ADDERS = {
    ROW_SWITCH: _add_switch_row,
    ROW_RATIO: _add_ratio_row,
    ROW_COMBO: _add_combo_row,
}


def make_viewer_setting_widget(card, key: t.Union[ViewerSetting, str], store: t.Any = None):
    """只构建控件、不摆进布局；store 不提供该设置 → ``None``。"""
    setting = get_viewer_setting(key) if isinstance(key, str) else key
    item = viewer_setting_item(setting, store)
    if item is None:
        return None
    builder = VIEWER_SETTING_BUILDERS[setting.kind]
    return builder(card, setting, item, store if store is not None else viewer_store())


def add_viewer_setting(card, key: t.Union[ViewerSetting, str], store: t.Any = None):
    """在 ``card`` 里按描述表长出一行 viewer 通用设置，返回该控件。

    拆成"构建"与"摆放"两步，是为了让调用方能在**中间插入站点自己的行**并保持
    原有行序（danbooru 卡就是这样：pin → hideUnpreviewable → 缩放卡）。
    """
    setting = get_viewer_setting(key) if isinstance(key, str) else key
    widget = make_viewer_setting_widget(card, setting, store)
    if widget is None:
        return None
    _ROW_ADDERS[setting.kind](card, setting, widget)
    return widget


def build_viewer_settings(card, store: t.Any = None) -> dict[str, t.Any]:
    """把 ``store`` 支持的所有 viewer 通用设置按表序追加进 ``card``。

    给"整页都是 viewer 设置"的站点页用的一行入口；有交错行的页面用
    :func:`add_viewer_setting` 逐条控制位置。返回 ``{逻辑键: 控件}``。
    """
    rows: dict[str, t.Any] = {}
    for setting in available_viewer_settings(store):
        widget = add_viewer_setting(card, setting, store)
        if widget is not None:
            rows[setting.key] = widget
    return rows


__all__ = [
    "FALLBACK_DEFAULT_PIN",
    "FALLBACK_VIEW_RATIO_PERCENT",
    "ROW_COMBO",
    "ROW_RATIO",
    "ROW_SWITCH",
    "VIEWER_SETTINGS",
    "VIEWER_SETTING_BUILDERS",
    "ViewerSetting",
    "add_viewer_setting",
    "available_viewer_settings",
    "build_viewer_settings",
    "get_viewer_setting",
    "make_viewer_setting_widget",
    "subscribe_source_mode_changed",
    "emit_source_mode_changed",
    "viewer_default_pin",
    "viewer_source_mode",
    "viewer_setting_item",
    "viewer_setting_value",
    "viewer_store",
    "viewer_view_ratio",
    "viewer_view_ratio_percent",
]