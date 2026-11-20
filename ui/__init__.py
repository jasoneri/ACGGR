# -*- coding: utf-8 -*-
"""Acggr UI infrastructure layer.

This repo owns its copy of the shared GUI infrastructure outright (PRD Q5-A:
fork is an accepted long-term cost, no subtree sync):
``ui.qfluent.components`` + ``.icons``, ``ui.core.theme`` (+ qss
templates), ``ui.async_task``, ``ui.browser_window`` +
``ui.core.browser``, ``ui.core.{timer,font,doh_runtime,
exception_feedback,anim}``.

Import boundary (see ``harness/docs/import-layout.md``):
- may import ``core`` and ``res``;
- must NOT import ``app`` (no circular dependency: app -> ui -> core).
"""
