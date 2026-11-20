"""MRU history lists backed by qconfig items.

Every settings field that remembers past values follows the same contract: the
value is recorded only at the commit point (focus loss / Enter), de-duplicated
newest-first, and capped. Keeping that contract in one class means adding a URL
row costs one line -- ``MRUHistory(item)`` -- instead of another copy of
read/normalize/merge/save.
"""
from __future__ import annotations

from loguru import logger
from qfluentwidgets import ConfigItem, qconfig


def _strip(value: object) -> str:
    return str(value or "").strip()


class MRUHistory:
    """An MRU list stored in one qconfig ``ConfigItem``.

    ``normalize`` maps a raw entry to its canonical form and may raise
    ``ValueError`` for junk: such entries are dropped on read and never stored
    on write. With no ``normalize`` the entry is merely stripped, which is the
    right default when the value is already canonical by the time it commits.
    """

    #: Newest-first entries kept -- the one place the cap is defined.
    limit = 5

    def __init__(self, item: ConfigItem, *, owner=None, normalize=None, limit=None):
        self._item = item
        #: Optional QConfig owner; without it the global ``qconfig`` is saved.
        self._owner = owner
        self._normalize = normalize or _strip
        self._limit = self.limit if limit is None else max(1, int(limit))

    def get(self) -> list[str]:
        """Remembered values in MRU order, de-duplicated and capped."""
        items: list[str] = []
        for raw in list(self._item.value or []):
            try:
                canonical = self._normalize(raw)
            except (TypeError, ValueError):
                continue
            if canonical and canonical not in items:
                items.append(canonical)
        return items[: self._limit]

    def set(self, values) -> list[str]:
        """Replace the list (normalized, de-duplicated, capped) and persist.

        Always saves, even when the list itself did not change: callers may have
        updated a sibling item (the DoH URL) on the same commit, and that write
        must land too.
        """
        items: list[str] = []
        for raw in values or []:
            try:
                canonical = self._normalize(raw)
            except (TypeError, ValueError):
                continue
            if canonical and canonical not in items:
                items.append(canonical)
        items = items[: self._limit]
        if items != list(self._item.value or []):
            self._item.value = items
        self._save()
        return items

    def add(self, value) -> list[str]:
        """Bump one value to the front; a rejected value leaves the list as-is."""
        try:
            canonical = self._normalize(value)
        except (TypeError, ValueError):
            return self.get()
        if not canonical:
            return self.get()
        return self.set([canonical, *self.get()])

    def _save(self) -> None:
        try:
            if self._owner is not None:
                self._owner.save()
            else:
                qconfig.save()
        except Exception as error:  # noqa: BLE001 - a history write must not break the page
            logger.warning(
                f"[History] could not persist {getattr(self._item, 'name', '?')}: {error}"
            )