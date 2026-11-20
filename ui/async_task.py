"""
异步任务管理器 - 流程化耗时操作处理
提供类似微服务的便捷接入方式，支持 QThread 处理、回调和可视化状态提示
"""
import asyncio
from dataclasses import dataclass, field
import inspect
import math
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Qt, QThread, Signal, qtTrId
from PySide6.QtWidgets import QApplication, QWidget
from ui.core.timer import safe_single_shot
from loguru import logger
from qfluentwidgets import InfoBar, InfoBarPosition, PushButton, StateToolTip


def resolve_infobar_host(fallback: QWidget) -> QWidget:
    """提示落点 = 前台窗自报的承载面；无前台窗、或前台非宿主窗（弹层）时回落 fallback。

    触发方在哪扇窗，用户就正看着哪扇窗：acggrCell 在 ToolWorkspaceWindow 内、该窗挡在
    SpiderGUI 前，提示若仍打 SpiderGUI，点 cell 的用户全程看不见。承载面由窗自己声明
    （``infobar_surface``），本模块不认窗类型。落点必须是窗内子 frame 而非窗本身——
    InfoBar 按 parent 的 rect 定位、按 parent().width() 定换行，挂窗上会压住窗头。
    前台是 fallback 自身的窗（如页内 parent 挂在工作区窗）时返回 fallback，页内定位不变。
    模态弹层不声明承载面，故弹层在台前时提示不挂弹层（弹层窄、且随弹层一同隐藏）。
    """
    active_window = QApplication.activeWindow()
    if active_window is None:
        return fallback
    active_window = active_window.window()
    if active_window is fallback.window():
        return fallback
    surface_getter = getattr(active_window, "infobar_surface", None)
    if surface_getter is None:
        return fallback
    surface = surface_getter()
    return surface if surface is not None else fallback


def summarize_error_message(message: object, *, max_length: int = 180) -> str:
    if isinstance(message, BaseException):
        detail = str(message).strip()
        text = f"{type(message).__name__}: {detail}" if detail else type(message).__name__
    else:
        text = str(message or "").strip()

    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = " ".join(first_line.split())
    if not first_line:
        first_line = "Unknown error"

    clipped = len(first_line) > max_length
    if clipped:
        first_line = first_line[: max_length - 3].rstrip() + "..."

    has_hidden_detail = clipped or len(text.splitlines()) > 1
    if not has_hidden_detail:
        return first_line
    from core import acggr_log_dir

    return qtTrId("common.error.log_detail").format(
        summary=first_line, log_path=acggr_log_dir())


def _exception_first_line(exc: BaseException, *, max_length: int = 180) -> str:
    """异常 → 一行可读文本（保留原始消息，页面可继续做 ``in`` 判断）。"""
    detail = str(exc).strip()
    text = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = " ".join(first_line.split()) or type(exc).__name__
    if len(first_line) > max_length:
        first_line = first_line[: max_length - 3].rstrip() + "..."
    return first_line


def _exception_detail(exc: BaseException) -> str:
    if exc.__traceback__ is not None:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return f"{type(exc).__name__}: {exc}"


def _exception_status(exc: BaseException) -> Optional[int]:
    for attr in ("status", "status_code"):
        value = getattr(exc, attr, None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


class TaskError(str):
    """UI 错误呈现契约：字符串值是**给用户看的一行**，堆栈只挂在属性上。

    继承 str 是为了兼容既有的 error_callback(str) 回调（f"{error}" / str(error) / in /
    .splitlines()）；error 本身不含 traceback，结构化信息读 detail（只进日志）/ hint / action。
    str(error) == summary [+ " " + hint]；debug_text 才是完整堆栈。
    """

    def __new__(
        cls,
        summary: str,
        *,
        hint: str = "",
        detail: str = "",
        exception_type: str = "",
        status: Optional[int] = None,
        action_text: str = "",
        action: Optional[Callable[[], None]] = None,
    ) -> "TaskError":
        text = f"{summary} {hint}".strip() if hint else str(summary)
        self = super().__new__(cls, text)
        self.summary = str(summary)
        self.hint = str(hint)
        self.detail = str(detail)
        self.exception_type = str(exception_type)
        self.status = status
        self.action_text = str(action_text)
        self.action = action
        return self

    @property
    def user_message(self) -> str:
        """给用户看的那一句（进 InfoBar / BodyLabel）。"""
        return str(self)

    @property
    def debug_text(self) -> str:
        """给开发看的完整堆栈（**只进日志**，禁止进 UI）。"""
        return self.detail or self.summary

    def with_action(self, action_text: str, action: Optional[Callable[[], None]]) -> "TaskError":
        self.action_text = str(action_text or "")
        self.action = action
        return self

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        summary: str = "",
        hint: str = "",
        action_text: str = "",
        action: Optional[Callable[[], None]] = None,
    ) -> "TaskError":
        return cls(
            summary=summary or _exception_first_line(exc),
            hint=hint,
            detail=_exception_detail(exc),
            exception_type=type(exc).__name__,
            status=_exception_status(exc),
            action_text=action_text,
            action=action,
        )

    @classmethod
    def coerce(
        cls,
        error: object,
        *,
        hint: str = "",
        action_text: str = "",
        action: Optional[Callable[[], None]] = None,
    ) -> "TaskError":
        """``TaskError`` / 异常 / 裸字符串 → 统一的 TaskError（幂等）。"""
        if isinstance(error, cls):
            result = error
            if hint and not result.hint:
                result.hint = str(hint)
        elif isinstance(error, BaseException):
            result = cls.from_exception(error, hint=hint)
        else:
            text = str(error or "").strip()
            result = cls(summary=summarize_error_message(text), hint=hint, detail=text)
        if action_text:
            result.with_action(action_text, action)
        return result


class AsyncTaskProgressReporter:
    def __init__(self, emit: Callable[[str], None], *, throttle_seconds: float = 0.2, min_percent_step: int = 1):
        self._emit = emit
        self._throttle_seconds = throttle_seconds
        self._min_percent_step = min_percent_step
        self._label = ""
        self._bytes_downloaded = 0
        self._total_bytes: Optional[int] = None
        self._last_emit_at = 0.0
        self._last_percent = -1

    def __call__(self, message: str):
        self._emit(message)

    def download_reset(self, *, label: Optional[str] = None):
        self._label = label or self._label
        self._bytes_downloaded = 0
        self._total_bytes = None
        self._last_emit_at = 0.0
        self._last_percent = -1

    def download_start(self, *, label: str, total_bytes: Optional[int] = None):
        self.download_reset(label=label)
        self._total_bytes = total_bytes if total_bytes is not None and total_bytes > 0 else None
        if self._total_bytes is None:
            self._emit(f"{self._label} 0B")
            return
        self._emit(f"{self._label} 0% {self._format_ratio(0, self._total_bytes)}")

    def download_advance(self, chunk_size: int, *, label: Optional[str] = None, total_bytes: Optional[int] = None):
        if chunk_size < 0:
            raise ValueError("chunk_size must be >= 0")
        if label:
            self._label = label
        if total_bytes is not None:
            self._total_bytes = total_bytes if total_bytes > 0 else None
        self._bytes_downloaded += chunk_size
        now = time.monotonic()
        if self._total_bytes is None:
            if now - self._last_emit_at < self._throttle_seconds:
                return
            self._last_emit_at = now
            self._emit(f"{self._label} {self._format_bytes(self._bytes_downloaded)}")
            return

        percent = min(100, math.floor((self._bytes_downloaded * 100) / self._total_bytes))
        should_emit = percent >= self._last_percent + self._min_percent_step or now - self._last_emit_at >= self._throttle_seconds
        if not should_emit:
            return
        self._last_emit_at = now
        self._last_percent = percent
        self._emit(f"{self._label} {percent}% {self._format_ratio(self._bytes_downloaded, self._total_bytes)}")

    def download_finish(self, *, label: Optional[str] = None):
        if label:
            self._label = label
        if self._total_bytes is None:
            self._emit(f"{self._label} done {self._format_bytes(self._bytes_downloaded)}")
            return
        done_bytes = max(self._bytes_downloaded, self._total_bytes)
        self._emit(f"{self._label} done {self._format_bytes(done_bytes)}")

    @staticmethod
    def _format_bytes(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes}B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}K"
        return f"{size_bytes / 1024 / 1024:.1f}M"

    @classmethod
    def _format_ratio(cls, downloaded_bytes: int, total_bytes: int) -> str:
        if total_bytes < 1024:
            return f"{downloaded_bytes}/{total_bytes}B"
        if total_bytes < 1024 * 1024:
            return f"{downloaded_bytes / 1024:.1f}/{total_bytes / 1024:.1f}K"
        return f"{downloaded_bytes / 1024 / 1024:.1f}/{total_bytes / 1024 / 1024:.1f}M"


_ORPHANED_TASK_THREADS: set["AsyncTaskThread"] = set()  # CGS015 IRON-2


class AsyncTaskThread(QThread):
    success_signal = Signal(object)
    #: 载荷是 :class:`TaskError`（``str`` 子类，历史 ``error_callback(str)`` 照旧可用）。
    #: 声明成 ``object`` 才不会在跨线程投递时被 Qt 降级成裸 ``str``、丢掉 detail/hint。
    error_signal = Signal(object)
    progress_signal = Signal(str)

    def __init__(self, task_func: Callable, *args, error_translator=None, **kwargs):
        super().__init__()
        self.task_func = task_func
        self.args = tuple(args)
        self.kwargs = dict(kwargs)
        self.error_translator = error_translator
        self.is_cancelled = False

    def run(self):
        try:
            if self.is_cancelled:
                return
            code_obj = getattr(self.task_func, "__code__", None)
            if code_obj is not None and "progress_callback" in code_obj.co_varnames:
                self.kwargs["progress_callback"] = AsyncTaskProgressReporter(self.emit_progress)
            result = self.task_func(*self.args, **self.kwargs)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            if not self.is_cancelled:
                self.success_signal.emit(result)
        except Exception as exc:
            if not self.is_cancelled:
                # 只投递结构化的 TaskError：堆栈留在 detail 里走日志，绝不进 UI。
                self.error_signal.emit(self._build_error(exc))

    def _build_error(self, exc: BaseException) -> TaskError:
        """异常 → TaskError。翻译器跑在 worker 线程，必须是无 UI 依赖的纯函数。"""
        if self.error_translator is not None:
            try:
                translated = self.error_translator(exc)
            except Exception:  # noqa: BLE001 - 翻译器自身出错不能吞掉原始失败
                logger.opt(exception=True).error("error_translator 失败，回落默认文案")
                translated = None
            if isinstance(translated, TaskError):
                if not translated.detail:
                    translated.detail = _exception_detail(exc)
                return translated
        return TaskError.from_exception(exc)

    def emit_progress(self, message: str):
        if not self.is_cancelled:
            self.progress_signal.emit(message)

    def cancel(self):
        # CGS015 IRON-1:只置标志,不 join。
        self.is_cancelled = True


@dataclass(slots=True)
class TaskConfig:
    task_func: Callable[..., Any]
    #: 回调入参是 :class:`TaskError`（``str`` 子类）：老写法 ``lambda e: ...`` 照旧。
    #: 需要人话+指引时读 ``e.hint`` / ``e.action``；堆栈只在 ``e.detail``。
    error_callback: Optional[Callable[[str], None]] = None
    progress_callback: Optional[Callable[[str], None]] = None
    #: 把异常翻译成「人话 + 可操作指引」；缺省时用 :meth:`TaskError.from_exception`。
    #: 入参是原始异常，返回 ``TaskError``（也可返回 ``None`` 表示交回默认）。
    error_translator: Optional[Callable[[BaseException], Optional[TaskError]]] = None
    success_callback: Optional[Callable[[Any], None]] = None
    #: ``None`` = the built-in wording; ``""`` stays deliberately blank. Resolved
    #: at display time (``execute_task``), after the translators are installed --
    #: a dataclass default is evaluated at import, before that.
    tooltip_title: Optional[str] = None
    tooltip_content: Optional[str] = None
    show_success_info: bool = True
    show_error_info: bool = True
    success_message: Optional[str] = None
    auto_hide_tooltip: bool = True
    show_tooltip: bool = True
    tooltip_position: Optional[Tuple[int, int]] = None
    tooltip_parent: Optional[QObject] = None
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)


class _TooltipEntry:
    __slots__ = ("tooltip", "parent", "custom_position")

    def __init__(self, tooltip: StateToolTip, parent: QObject, custom_position: bool):
        self.tooltip = tooltip
        self.parent = parent
        self.custom_position = custom_position


class TaskTooltipStack:
    TOOLTIP_VERTICAL_SPACING = 64
    TOOLTIP_TOP_MARGIN = 20
    TOOLTIP_RIGHT_MARGIN = 30
    CLOSE_DELAY_MS = 1000

    def __init__(self, default_parent: QWidget):
        self._default_parent = default_parent
        self.tooltips: Dict[str, StateToolTip] = {}
        self._entries: Dict[str, _TooltipEntry] = {}

    def show(self, task_id: str, title: str, content: str, position: Optional[Tuple[int, int]] = None, parent: Optional[QObject] = None):
        # 显式 parent 优先（照搬）；否则与 infobar 同一条落点规则：loading 提示也跟当前前台窗。
        tooltip_parent = parent if parent is not None else resolve_infobar_host(self._default_parent)

        tooltip = StateToolTip(title, content, tooltip_parent)
        self.tooltips[task_id] = tooltip
        self._entries[task_id] = _TooltipEntry(tooltip=tooltip, parent=tooltip_parent, custom_position=position is not None)

        if position is None:
            self._rearrange_parent(tooltip_parent)
        else:
            tooltip.move(position[0], position[1])

        tooltip.setState(False)
        tooltip.show()

    def update(self, task_id: str, content: str):
        tooltip = self.tooltips.get(task_id)
        if tooltip is not None:
            tooltip.setContent(content)

    def complete(self, task_id: str, auto_hide: bool):
        tooltip = self.tooltips.get(task_id)
        if tooltip is None:
            return
        if auto_hide:
            tooltip.setState(True)
            safe_single_shot(self.CLOSE_DELAY_MS, lambda tid=task_id: self.close(tid))
            return
        tooltip.setContent(qtTrId("common.task.completed"))

    def close(self, task_id: str):
        entry = self._entries.pop(task_id, None)
        tooltip = self.tooltips.pop(task_id, None)
        if tooltip is None:
            return

        try:
            tooltip.close()
            if entry is not None:
                self._rearrange_parent(entry.parent)
        except RuntimeError:
            # 宿主窗已销毁（WA_DeleteOnClose：如开 scriptWin 前关掉工作区窗），tooltip 作为
            # 其子对象一并被删；延迟 close 打到已释放对象时静默收尾。
            pass

    def cleanup(self):
        for tooltip in list(self.tooltips.values()):
            tooltip.close()
        self.tooltips.clear()
        self._entries.clear()

    def _rearrange_parent(self, parent: QObject):
        auto_entries = [
            entry
            for entry in self._entries.values()
            if entry.parent is parent and not entry.custom_position
        ]
        for index, entry in enumerate(auto_entries):
            x = parent.width() - entry.tooltip.width() - self.TOOLTIP_RIGHT_MARGIN
            y = self.TOOLTIP_TOP_MARGIN + index * self.TOOLTIP_VERTICAL_SPACING
            entry.tooltip.move(x, y)


class TaskInfoBarCenter:
    """错误/警告提示的唯一出口。

    两条纪律（用户 2026-11-15 现场：一整屏 traceback 糊住页面）：

    - **UI 只显示人话**：``error()`` 的正文永远是 :attr:`TaskError.user_message`，
      traceback 只写日志。传裸 ``str`` / 异常也安全（内部 coerce）。
    - **不做滚动日志**：同一条错误在 ``ERROR_DEDUPE_WINDOW_MS`` 内重复出现，
      只在已有 InfoBar 上把计数改成 ``（×N）``，不再新开一条。
    """

    #: 同文案合并窗口；窗口内重复只加计数，避免连点连刷。
    ERROR_DEDUPE_WINDOW_MS = 5000

    def __init__(self, parent: QWidget):
        self._parent = parent
        self.infobars: List[InfoBar] = []
        #: 文案 → (InfoBar, 首次出现时间, 已出现次数)
        self._recent_errors: Dict[str, Tuple[InfoBar, float, int]] = {}

    def success(self, message: str):
        # 兼容旧行为：该开关存在，但当前 app 没有成功弹层设计。
        return None

    def warning(self, message: str):
        self._show(factory=InfoBar.warning, title=qtTrId("common.warning.title"),
                   content=message, duration=6000)

    def info(self, message: str):
        self._show(factory=InfoBar.info, title="", content=message, duration=2000)

    def error(self, message: object, show_popup: bool = True, *, action_text: str = "",
              action: Optional[Callable[[], None]] = None) -> Optional[InfoBar]:
        """展示一条错误。

        - ``message`` 可以是 :class:`TaskError` / 异常 / 裸字符串；
        - 完整堆栈走 ``logger.error``，UI 只拿 :attr:`TaskError.user_message`；
        - ``action_text`` + ``action`` 会给 InfoBar 挂一个按钮（如「去设置」）。
        """
        task_error = TaskError.coerce(message, action_text=action_text, action=action)
        logger.error(task_error.debug_text)
        if not show_popup:
            return None
        text = task_error.user_message
        if task_error.action_text and callable(task_error.action):
            return self._show_error_with_action(task_error, text)
        return self._show_deduped_error(text)

    # ---- 内部 ----

    def _show_deduped_error(self, text: str) -> Optional[InfoBar]:
        now = time.monotonic()
        entry = self._recent_errors.get(text)
        if entry is not None:
            infobar, first_at, count = entry
            if (now - first_at) * 1000 <= self.ERROR_DEDUPE_WINDOW_MS and self._bar_alive(infobar):
                self._set_bar_text(
                    infobar,
                    f"{text}{qtTrId('common.task.repeat_suffix').format(count=count + 1)}",
                )
                self._recent_errors[text] = (infobar, first_at, count + 1)
                return infobar
        infobar = self._show(factory=InfoBar.error, title="", content=text, duration=-1)
        if infobar is not None:
            self._recent_errors[text] = (infobar, now, 1)
        return infobar

    def _show_error_with_action(self, task_error: "TaskError", text: str) -> Optional[InfoBar]:
        """带按钮的错误条：按钮只在第一份上挂，合并计数沿用同一根。"""
        key = f"{text}\n{task_error.action_text}"
        now = time.monotonic()
        entry = self._recent_errors.get(key)
        if entry is not None:
            infobar, first_at, count = entry
            if (now - first_at) * 1000 <= self.ERROR_DEDUPE_WINDOW_MS and self._bar_alive(infobar):
                self._set_bar_text(
                    infobar,
                    f"{text}{qtTrId('common.task.repeat_suffix').format(count=count + 1)}",
                )
                self._recent_errors[key] = (infobar, first_at, count + 1)
                return infobar
        infobar = self._show(factory=InfoBar.error, title="", content=text, duration=-1)
        if infobar is None:
            return None
        try:
            button = PushButton(task_error.action_text, infobar)
            button.clicked.connect(lambda _=False, bar=infobar, fn=task_error.action: self._run_error_action(bar, fn))
            infobar.addWidget(button)
            infobar.adjustSize()
        except RuntimeError:
            pass
        self._recent_errors[key] = (infobar, now, 1)
        return infobar

    @staticmethod
    def _run_error_action(infobar: InfoBar, action):
        try:
            infobar.close()
        finally:
            try:
                action()
            except Exception:  # noqa: BLE001 - 动作失败不能把 UI 再炸一次
                logger.opt(exception=True).error("错误提示按钮动作失败")

    @staticmethod
    def _bar_alive(infobar: InfoBar) -> bool:
        try:
            infobar.objectName()
            return True
        except RuntimeError:
            return False

    @staticmethod
    def _set_bar_text(infobar: InfoBar, text: str):
        # InfoBar 的换行由 _adjustText() 在 show/resize 时重算，这里同步改内容再重排。
        infobar.content = text
        try:
            infobar._adjustText()  # noqa: SLF001 - qfluentwidgets 无公开的 setContent
        except RuntimeError:
            pass

    def cleanup(self):
        for infobar in list(self.infobars):
            infobar.close()
        self.infobars.clear()
        self._recent_errors.clear()

    def _show(self, factory: Callable[..., Optional[InfoBar]], title: str, content: str, duration: int):
        infobar = factory(
            title=title, content=content, orient=Qt.Horizontal, isClosable=True, position=InfoBarPosition.TOP,
            duration=duration, parent=resolve_infobar_host(self._parent),
        )
        if infobar is None:
            return None
        self.infobars.append(infobar)
        infobar.closedSignal.connect(lambda bar=infobar: self._cleanup_closed(bar))
        return infobar

    def _cleanup_closed(self, infobar: InfoBar):
        if infobar in self.infobars:
            self.infobars.remove(infobar)
        for key, (bar, _first_at, _count) in list(self._recent_errors.items()):
            if bar is infobar:
                self._recent_errors.pop(key, None)


class AsyncTaskManager(QObject):
    """异步任务管理器 - 只负责任务入口、线程生命周期与结果分发"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.current_tasks: Dict[str, AsyncTaskThread] = {}
        # 模块级注册表,非实例属性 —— CGS015 IRON-2。
        self._zombies: set[AsyncTaskThread] = _ORPHANED_TASK_THREADS
        self._tooltip_stack = TaskTooltipStack(parent)
        self._infobar_center = TaskInfoBarCenter(parent)
        self.current_tooltips = self._tooltip_stack.tooltips
        self.current_infobars = self._infobar_center.infobars
        self._active = True

    def execute_task(self, task_id: str, config: TaskConfig) -> bool:
        if self.is_task_running(task_id):
            self._infobar_center.warning(
                qtTrId("common.task.running").format(task_id=task_id))
            return False

        try:
            thread = AsyncTaskThread(
                config.task_func, *config.args,
                error_translator=config.error_translator, **config.kwargs,
            )
            self.current_tasks[task_id] = thread
            thread.success_signal.connect(
                lambda result, tid=task_id, task_config=config, worker=thread:
                    self._handle_success(tid, result, task_config, thread=worker)
            )
            thread.error_signal.connect(
                lambda error, tid=task_id, task_config=config, worker=thread:
                    self._handle_error(tid, error, task_config, thread=worker)
            )
            thread.progress_signal.connect(
                lambda progress, tid=task_id, task_config=config, worker=thread:
                    self._handle_progress(tid, progress, task_config, thread=worker)
            )
            if config.show_tooltip:
                self._tooltip_stack.show(
                    task_id,
                    qtTrId("common.task.processing")
                    if config.tooltip_title is None else config.tooltip_title,
                    qtTrId("common.task.please_wait")
                    if config.tooltip_content is None else config.tooltip_content,
                    config.tooltip_position,
                    config.tooltip_parent,
                )
            thread.start()
            return True
        except Exception as exc:
            self._infobar_center.error(
                TaskError.from_exception(exc, summary=qtTrId("common.task.start_failed")))
            self.current_tasks.pop(task_id, None)
            return False

    def execute_simple_task(
        self,
        task_func: Callable[..., Any],
        success_callback: Optional[Callable] = None,
        error_callback: Optional[Callable] = None,
        progress_callback: Optional[Callable] = None,
        error_translator: Optional[Callable[[BaseException], Optional["TaskError"]]] = None,
        tooltip_title: Optional[str] = None,
        tooltip_content: Optional[str] = None,
        show_success_info: bool = True,
        show_error_info: bool = True,
        success_message: Optional[str] = None,
        auto_hide_tooltip: bool = True,
        tooltip_position: Optional[Tuple[int, int]] = None,
        show_tooltip: bool = True,
        tooltip_parent: Optional[QObject] = None,
        task_id: Optional[str] = None,
        *args,
        **kwargs,
    ) -> bool:
        if task_id is None:
            task_id = f"task_{task_func.__name__}_{int(time.time() * 1000000)}"

        return self.execute_task(
            task_id,
            TaskConfig(
                task_func=task_func, success_callback=success_callback, error_callback=error_callback,
                progress_callback=progress_callback, error_translator=error_translator,
                tooltip_title=tooltip_title, tooltip_content=tooltip_content,
                show_success_info=show_success_info, show_error_info=show_error_info, success_message=success_message,
                auto_hide_tooltip=auto_hide_tooltip, show_tooltip=show_tooltip, tooltip_position=tooltip_position,
                tooltip_parent=tooltip_parent, args=args, kwargs=kwargs,
            ),
        )

    def cancel_task(self, task_id: str) -> bool:
        thread = self.current_tasks.pop(task_id, None)
        if thread is None:
            return False
        if not thread.isRunning():
            # 未运行:交回既有 GC 路径,保持「不主动清理已结束项」的既有语义。
            self.current_tasks[task_id] = thread
            return False

        thread.cancel()
        self._retire_thread(thread)
        self._tooltip_stack.complete(task_id, auto_hide=True)
        self._infobar_center.info(qtTrId("common.task.cancelled"))
        return True

    def _retire_thread(self, thread: AsyncTaskThread):
        """解绑运行中的线程:移入注册表保活,并挂回收槽。见 CGS015 IRON-2/IRON-3。"""
        self._zombies.add(thread)
        thread.finished.connect(lambda t=thread: self._reap_zombie(t))

    def _reap_zombie(self, thread: AsyncTaskThread):
        # CGS015 IRON-3:只做跨线程安全的 set 操作 + deleteLater,不触碰 current_tasks/tooltip。
        self._zombies.discard(thread)
        thread.deleteLater()

    def cancel_all_tasks(self):
        for task_id in list(self.current_tasks.keys()):
            self.cancel_task(task_id)

    def is_task_running(self, task_id: str) -> bool:
        thread = self.current_tasks.get(task_id)
        return bool(thread and thread.isRunning())

    def get_running_tasks(self) -> list:
        return [task_id for task_id, thread in self.current_tasks.items() if thread.isRunning()]

    def cleanup(self):
        self._active = False
        self.cancel_all_tasks()
        self._tooltip_stack.cleanup()
        self._infobar_center.cleanup()
        # CGS015 IRON-4:只清活动表,不得清 _zombies。
        self.current_tasks.clear()

    def reset(self):
        self.cleanup()
        self._active = True

    def _handle_success(self, task_id: str, result: Any, config: TaskConfig, *, thread: Optional[AsyncTaskThread] = None):
        if thread is not None and thread.is_cancelled:
            # 取消前已入队、取消后才投递的信号:静默丢弃,不驱动 UI。
            self._cleanup_task(task_id)
            return
        if not self._active:
            self._cleanup_task(task_id)
            return

        self._tooltip_stack.complete(task_id, config.auto_hide_tooltip)
        if config.show_success_info:
            self._infobar_center.success(
                qtTrId("common.task.done")
                if config.success_message is None else config.success_message)
        try:
            if config.success_callback is not None:
                config.success_callback(result)
        finally:
            self._cleanup_task(task_id)

    def _handle_error(self, task_id: str, error: object, config: TaskConfig, *, thread: Optional[AsyncTaskThread] = None):
        if thread is not None and thread.is_cancelled:
            self._cleanup_task(task_id)
            return
        if not self._active:
            self._cleanup_task(task_id)
            return

        task_error = TaskError.coerce(error)
        self._tooltip_stack.complete(task_id, auto_hide=True)
        self._infobar_center.error(
            task_error,
            show_popup=config.show_error_info,
            action_text=task_error.action_text,
            action=task_error.action,
        )
        try:
            if config.error_callback is not None:
                # TaskError 是 str 子类：旧 ``error_callback(str)`` 签名零改动可用，
                # 想拿 hint/action 的新页面直接读属性。
                config.error_callback(task_error)
        finally:
            self._cleanup_task(task_id)

    def _handle_progress(self, task_id: str, progress: str, config: TaskConfig, *, thread: Optional[AsyncTaskThread] = None):
        if thread is not None and thread.is_cancelled:
            return
        if not self._active:
            return

        self._tooltip_stack.update(task_id, progress)
        if config.progress_callback is not None:
            config.progress_callback(progress)

    def _cleanup_task(self, task_id: str):
        thread = self.current_tasks.get(task_id)
        if thread is not None and not thread.isRunning():
            del self.current_tasks[task_id]
