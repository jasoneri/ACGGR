#!/usr/bin/python
# -*- coding: utf-8 -*-
"""UI-language notifications for surfaces that are not Qt widgets.

Qt broadcasts QEvent.LanguageChange to widgets, but acggr's own widgets render qtTrId once at
construction and a language switch relaunches the process. Embedded web pages differ: AriaNg
carries its own catalogue and can repaint in place, so it must be told when the language changes.

This is the language twin of ui.core.theme.theme_mgr: a tiny synchronous listener list with no
Qt dependency, so the web layer subscribes the same way it subscribes to theme changes. Keeping
it separate means the picker only says "language is now X"; each surface decides what to do.
"""
from __future__ import annotations

import typing as t

Listener = t.Callable[[str], None]


class LanguageManager:
    """Fan a resolved language code out to whoever renders text themselves."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, callback: Listener) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def unsubscribe(self, callback: Listener) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def notify(self, lang: str) -> None:
        """Tell every listener that ``lang`` is the language to render now.

        A listener that raises must not stop the others: one broken page should
        not leave the remaining surfaces on the old wording.
        """
        for callback in tuple(self._listeners):
            try:
                callback(lang)
            except Exception:  # noqa: BLE001 - a listener must not break the fan-out
                import traceback

                traceback.print_exc()


lang_mgr = LanguageManager()
