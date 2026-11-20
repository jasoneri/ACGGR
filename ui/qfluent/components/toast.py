#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Bottom-left toasts that rise in and slide out left.

qfluentwidgets' BOTTOM_LEFT manager slides the bar in from the left edge and only fades out;
acggr wants rise-from-below / leave-left for transient page feedback, so this module registers
its own manager for that position and swaps the exit animation.

Everything else stays stock: the widget is InfoBar, it still stacks/re-flows through
InfoBarManager, callers keep closedSignal. Replaced: BottomLeftRiseManager (position/slide
vectors for BOTTOM_LEFT, a plain dict write effective for future InfoBars) and show_toast
(builds the bar and patches its private fade-out hook with a horizontal exit).
"""
from __future__ import annotations

import typing as t

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QVariantAnimation
from PySide6.QtWidgets import QWidget
from qfluentwidgets import InfoBar, InfoBarIcon, InfoBarPosition, ProgressRing
from qfluentwidgets.components.widgets.info_bar import InfoBarManager

#: Distance the bar travels while leaving (px). Matches the slide-in feel.
SLIDE_OUT_DISTANCE = 220
SLIDE_OUT_MS = 180


class BottomLeftRiseManager(InfoBarManager):
    """Stack at the bottom-left, rise in from below, re-flow like the stock one.

    Position math is copied from ``BottomLeftInfoBarManager`` on purpose: the
    stacking order (newest bar closest to the bottom, older ones pushed up) is
    the behaviour users already know, and re-deriving it would drift.
    """

    def _pos(self, infoBar: InfoBar, parentSize=None) -> QPoint:
        parent = infoBar.parent()
        parentSize = parentSize or parent.size()

        y = parentSize.height() - infoBar.height() - self.margin
        index = self.infoBars[parent].index(infoBar)
        for bar in self.infoBars[parent][0:index]:
            y -= (bar.height() + self.spacing)

        return QPoint(self.margin, y)

    def _slideStartPos(self, infoBar: InfoBar) -> QPoint:
        """Start below the final spot: the bar rises into place."""
        pos = self._pos(infoBar)
        return QPoint(pos.x(), pos.y() + infoBar.height() + self.margin)


def register_bottom_left_rise() -> None:
    """Install the rise-in manager for ``BOTTOM_LEFT`` (idempotent).

    ``register`` refuses to overwrite an existing key, so the dict is written
    directly; that is the documented shape of ``InfoBarManager.managers``.
    """
    InfoBarManager.managers[InfoBarPosition.BOTTOM_LEFT] = BottomLeftRiseManager


def _patch_slide_out(info_bar: InfoBar) -> None:
    """Make the bar leave to the left instead of only fading.

    ``InfoBar.__fadeOut`` is name-mangled and called from a single-shot timer, so
    an instance attribute wins over the class method (verified: assigning
    ``bar._InfoBar__fadeOut`` replaces the call made by the timer). The opacity
    animation is still run at the end, so the bar cannot pop out mid-slide.
    """
    original = info_bar._InfoBar__fadeOut

    def slide_out():
        if getattr(info_bar, "_acggr_leaving", False):
            return
        info_bar._acggr_leaving = True
        ani = QPropertyAnimation(info_bar, b"pos", info_bar)
        ani.setDuration(SLIDE_OUT_MS)
        ani.setEasingCurve(QEasingCurve.InQuad)
        ani.setStartValue(info_bar.pos())
        ani.setEndValue(QPoint(info_bar.pos().x() - SLIDE_OUT_DISTANCE, info_bar.pos().y()))

        def _finish():
            # ``InfoBar.closeEvent`` calls ``e.ignore()`` (it only emits
            # ``closedSignal`` and defers to deleteLater), so ``close()`` alone
            # leaves the widget visible. Hide explicitly, then run the stock
            # fade-out, which is what emits the signal and schedules deletion.
            info_bar.hide()
            original()

        ani.finished.connect(_finish)
        ani.start(QPropertyAnimation.DeleteWhenStopped)
        info_bar._acggr_slide_out = ani  # keep alive until finished

    info_bar._InfoBar__fadeOut = slide_out


def _arm_exit_timer(info_bar: InfoBar, duration_ms: int) -> None:
    """Own the dismissal timer so it can be restarted when the text changes.

    ``InfoBar.showEvent`` arms ``QTimer.singleShot(self.duration, fadeOut)`` once
    and nothing can cancel it, so a recycled toast would keep the *original*
    deadline and vanish mid-update. This timer is cancellable, and it calls the
    patched ``__fadeOut`` (i.e. the slide-out), not a bare fade.
    """
    from PySide6.QtCore import QTimer

    timer = getattr(info_bar, "_acggr_exit_timer", None)
    if timer is not None:
        timer.stop()
    timer = QTimer(info_bar)
    timer.setSingleShot(True)
    timer.timeout.connect(info_bar._InfoBar__fadeOut)
    timer.start(max(0, duration_ms))
    info_bar._acggr_exit_timer = timer


def refresh_toast(info_bar: InfoBar, content: str, icon=None, *, duration_ms: int = 1600) -> None:
    """Update a live toast in place and restart its deadline.

    This is the "replace, do not stack, do not suppress" pattern: the bar stays
    where it is (no re-slide, no flicker), the newest result is what the user
    reads, and the timer starts over so a rapid burst of saves cannot dismiss
    the last one early.
    """
    info_bar.contentLabel.setText(content)
    info_bar.contentLabel.adjustSize()
    info_bar.adjustSize()
    if icon is not None:
        # ``InfoIconWidget`` has no setter: it paints from its ``icon`` attribute.
        info_bar.iconWidget.icon = icon
        info_bar.iconWidget.update()
    _arm_exit_timer(info_bar, duration_ms)


def show_toast(
    parent: QWidget,
    content: str,
    *,
    icon: InfoBarIcon = InfoBarIcon.INFORMATION,
    duration: int = 2500,
    title: str = "",
    is_closable: bool = False,
    extra: t.Optional[dict] = None,
) -> InfoBar:
    """Show one bottom-left toast that rises in and slides out left.

    Returns the bar so callers can keep a reference (needed for live text, as the
    language countdown does).
    """
    register_bottom_left_rise()
    # ``duration=-1`` disables the stock ``QTimer.singleShot`` in showEvent: it
    # cannot be cancelled, and a recycled toast would keep its original deadline.
    # Dismissal is owned by ``_arm_exit_timer`` instead (armed below, restarted
    # by ``refresh_toast``).
    bar = InfoBar(
        icon=icon, title=title, content=content,
        # Horizontal is what a toast is: one line of text beside the icon.
        # ``orient=None`` made InfoBar build a *vertical* textLayout, so the
        # countdown ring (inserted left of the text) pushed the content onto a
        # second line under the icon -- visibly broken stacking.
        orient=Qt.Horizontal, isClosable=is_closable, position=InfoBarPosition.BOTTOM_LEFT, duration=-1, parent=parent,
        **(extra or {}),
    )
    _patch_slide_out(bar)
    bar.show()
    # ``duration < 0`` from the caller means "stay until closed" -- the language
    # countdown owns that dismissal, so no exit timer is armed.
    if duration >= 0:
        _arm_exit_timer(bar, duration)
    return bar


class ToastCountdownRing(QWidget):
    """Deterministic ring that drains continuously over a toast's countdown.

    Attached to the left of an InfoBar's content, it replaces a seconds counter. One animation
    runs for the whole countdown (100 → 0, linear easing) so it hits 0 exactly with the countdown.
    ProgressRing rather than indeterminate: a countdown is determinate progress, and an empty
    ring reads as time running out without a number in the message.
    """

    SIZE = 22
    STROKE_WIDTH = 3

    def __init__(self, info_bar: InfoBar):
        super().__init__(info_bar)
        self._info_bar = info_bar
        self.setFixedSize(self.SIZE, self.SIZE)
        # ``useAni=False`` is load-bearing. qfw's ProgressBar ships its own
        # 150 ms interpolation on every ``setValue`` and *starts at 0*, so the
        # default ring grows 0->100 on creation (a fill nobody asked for) and
        # then fights our own drain animation frame by frame -- the ring lagged
        # behind real time and stuttered. Without the internal animation,
        # ``setValue`` paints immediately and our linear drain tracks real time.
        self.ring = ProgressRing(self, useAni=False)
        self.ring.setTextVisible(False)
        self.ring.setRange(0, 100)
        self.ring.setValue(100)
        self.ring.setStrokeWidth(self.STROKE_WIDTH)
        self.ring.setFixedSize(self.SIZE, self.SIZE)
        # Insert before the title/content labels: ring, then text.
        layout = info_bar.textLayout
        layout.insertWidget(0, self, 0, Qt.AlignVCenter)

        # Linear on purpose: the ring must track real time so the value shown
        # when the countdown ends is exactly 0.
        self._ani = QVariantAnimation(self)
        self._ani.setEasingCurve(QEasingCurve.Linear)
        self._ani.valueChanged.connect(lambda value: self.ring.setValue(int(value)))

    def start(self, total_s: int) -> None:
        """Drain from full to empty across ``total_s`` seconds of real time."""
        total_ms = max(0, int(total_s) * 1000)
        self._ani.stop()
        if total_ms <= 0:
            self.ring.setValue(0)
            return
        self.ring.setValue(100)
        self._ani.setDuration(total_ms)
        self._ani.setStartValue(100.0)
        self._ani.setEndValue(0.0)
        self._ani.start()

    def stop(self) -> None:
        """Freeze the ring where it is (the bar was closed / restart fired).

        Safe to call after the bar died: the ring is a child of the toast, so
        closing it tears the animation down with it, and a callback that fires
        late (the restart timer) must not raise on the deleted C++ object.
        """
        try:
            self._ani.stop()
        except RuntimeError:
            pass
