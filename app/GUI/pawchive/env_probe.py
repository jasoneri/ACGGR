"""pawchive 环境探测（aria2 / redis）：**只允许在工作线程里执行**。

AcggrAria2Engine.is_ready() 先拿引擎 RLock、再在锁内 RPC ping，而该锁被后台下载线程
（submit / ensure / spawn / 轮询）长时间持有；主线程直接调用会在后台运行时被卡住。
redis_reachable() 也是 0.15s 网络探测。故两者都搬进线程池：

EnvProber.start() 一次发起两个探测（各自一个 QRunnable，并发跑），结果只通过 Qt 排队
信号回到 GUI 线程的槽。generation 自增用于丢弃过期结果。

本模块只认"探测"，不认识任何控件；UI 策略（节流、超时兜底、渲染）留在
app/GUI/pawchive/__init__.py。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Qt

#: 探测最长等待：超时后界面按"未知"先渲染，晚到的结果再修正（见 UI 侧 ``_on_env_timeout``）。
PROBE_TIMEOUT_MS = 3000

#: generation 里 kind 的取值。
KIND_ARIA2 = "aria2"
KIND_REDIS = "redis"


def probe_aria2() -> bool:
    """aria2 引擎是否可用（RPC ping）。任何异常都按"不可用"，绝不抛给调用方。

    **只许在工作线程调用**：函数内部会拿引擎锁（见模块 docstring）。
    """
    try:
        from core.script.aria2.engine import get_engine

        return bool(get_engine().is_ready())
    except Exception:  # noqa: BLE001 - 探测失败不该炸页面
        return False


def probe_redis() -> bool:
    """redis 端口是否在监听（0.15s 快失败）。缺失只影响命令行队列，不影响本页下载。"""
    try:
        from core.script import redis_reachable

        return bool(redis_reachable())
    except Exception:  # noqa: BLE001
        return False


class _ProbeTask(QRunnable):
    """一次单服务探测。跑在 prober 的线程池里，**不碰任何 Qt 控件/成员集合**。"""

    def __init__(self, owner: "EnvProber", generation: int, kind: str, func):
        super().__init__()
        self._owner = owner
        self._generation = generation
        self._kind = kind
        self._func = func
        self.setAutoDelete(True)

    def run(self):
        try:
            ok = bool(self._func())
        except Exception:  # noqa: BLE001 - 探测函数的失败等价于"不可用"
            ok = False
        try:
            self._owner._finished.emit(self._generation, self._kind, ok)
        except RuntimeError:
            # 宿主已销毁（关窗/退出）：信号源没了，结果已无意义，静默丢弃。
            pass


class EnvProber(QObject):
    """环境探测调度器：``start()`` 发起一轮，结果按服务分两条信号回主线程。"""

    aria2_ready = Signal(bool)
    redis_ready = Signal(bool)
    #: 内部：worker → 主线程（跨线程自动排队）。
    _finished = Signal(int, str, bool)

    def __init__(self, parent=None, *, aria2_probe=probe_aria2, redis_probe=probe_redis):
        super().__init__(parent)
        self._aria2_probe = aria2_probe
        self._redis_probe = redis_probe
        self._pool = QThreadPool(self)
        # 两个探测并发：aria2 可能卡在引擎锁上好几秒，redis 的结果不能被它拖住。
        self._pool.setMaxThreadCount(2)
        self._generation = 0
        self._finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)

    @property
    def generation(self) -> int:
        return self._generation

    def busy(self) -> bool:
        """还有在飞的探测（超时后仍可能是 True —— 超时不等于放弃，结果晚到仍可用）。"""
        return bool(self._pool.activeThreadCount())

    def start(self, *, aria2_probe=None, redis_probe=None) -> int:
        """发起一轮探测，返回本轮 generation（过期结果按它丢弃）。

        ``aria2_probe`` / ``redis_probe`` 缺省用构造时注入的那对；显式传入只为
        探针（``__temp/_v5_async_*.py``）能替换成假函数，生产路径不会传。
        """
        self._generation += 1
        generation = self._generation
        for kind, func in (
            (KIND_ARIA2, aria2_probe or self._aria2_probe),
            (KIND_REDIS, redis_probe or self._redis_probe),
        ):
            self._pool.start(_ProbeTask(self, generation, kind, func))
        return generation

    def invalidate(self) -> int:
        """作废所有在飞探测：自增 generation，晚到的结果按过期丢弃。

        用途：① 探针替换探测函数后要屏蔽真实在飞结果；② 用户切走页面/关 tab 时
        让迟到的结果不再回写 UI（否则会往已关闭的控件上画状态）。
        """
        self._generation += 1
        return self._generation

    def shutdown(self) -> None:
        """清空未开始的排队任务。**不 waitForDone**：探测可能正卡在引擎锁上，
        等它就是把手上的卡顿原封不动搬回来，线程随进程退出即可。"""
        try:
            self._pool.clear()
        except RuntimeError:
            pass

    def _on_finished(self, generation: int, kind: str, ok: bool):
        if generation != self._generation:
            return  # 过期轮次：用户已经又发起过一轮，旧结论不许覆盖新状态
        if kind == KIND_ARIA2:
            self.aria2_ready.emit(ok)
        else:
            self.redis_ready.emit(ok)


__all__ = [
    "EnvProber",
    "KIND_ARIA2",
    "KIND_REDIS",
    "PROBE_TIMEOUT_MS",
    "probe_aria2",
    "probe_redis",
]
