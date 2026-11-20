"""Update prompts for the Settings page: the "new version" banner and the manual
"check for update" row.

UI-only module: ``core.update`` owns the version decision and ``core.updater`` owns
the install command. Nothing here touches the network, the config store or a thread --
the checker, install launch, "skip this version" persistence and InfoBar feedback
belong to ``SettingInterface``; keeping the widgets dumb makes them smoke-testable.

The banner is a ``PrimaryPushSettingCard`` whose trailing (accent) button opens a
custom modal with the release notes, "skip this version" parked on the left. The
primary action *installs*: ``on_install`` launches ``core.updater`` (detached process
that waits for this interpreter to exit); the dialog itself never runs a command.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, qtTrId
from PySide6.QtWidgets import QHBoxLayout
from qfluentwidgets import (
    FluentIcon as FIF,
    MessageBoxBase,
    PrimaryPushSettingCard,
    PushButton,
    SubtitleLabel,
    TextBrowser,
    TransparentToolButton,
)

from ui.qfluent.components.icons import AcggrIcon
from ui.qfluent.components.setting_cards import WidgetSettingCard

if TYPE_CHECKING:
    # Type-only import: ``core.update`` pulls httpx + the network stack on
    # import, and a settings widget must not pay that (nor a cycle risk) just to
    # be imported. ``UpdateInfo`` is consumed as attributes only.
    from core.update import UpdateInfo


#: ``published_at`` -> ``YYYY-MM-DD``. Matched rather than blindly sliced: a
#: non-ISO value ("unknown", "2026") must stay readable instead of losing its
#: tail to a fixed 10-char cut.
_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

#: Commit SHA suffix the release tooling appends to every generated note, e.g.
#: ``... (0123456789abcdef0123456789abcdef01234567)``.
_COMMIT_SUFFIX_RE = re.compile(r"\s*\(\s*[0-9a-f]{40}.*\)")


def _is_prerelease_version(version: str) -> bool:
    """Channel of ``version`` (beta/rc vs final), from the version policy.

    Imported lazily and failure-tolerant: the classifier lives in
    ``core.update``, which pulls httpx at import time, and this module is
    imported while the settings page is built (startup path). A miss only
    downgrades the dialog heading to the stable wording.
    """
    try:
        from core.update import is_prerelease

        return bool(is_prerelease(version))
    except Exception:  # noqa: BLE001 - a heading must not break the dialog
        return False


def _release_day(published_at: str) -> str:
    """``2026-11-30T12:00:00Z`` -> ``2026-11-30``; anything else left as-is."""
    raw = str(published_at or "").strip()
    match = _ISO_DAY_RE.match(raw)
    return match.group(0) if match else raw


def _hint_text(current_version: str, published_at: str) -> str:
    """One hint, two keys: an empty date must not leave a dangling separator.

    ``"当前 1.0.0 · 发布于 "`` reads as a rendering bug, so the no-date variant
    is a separate translatable string instead of a conditional suffix glued at
    runtime (translators own the whole sentence, both branches).
    """
    day = _release_day(published_at)
    if not day:
        return qtTrId("settings.update.available_hint_no_date").format(current=current_version)
    return qtTrId("settings.update.available_hint").format(current=current_version, date=day)


def _clean_notes(notes: str) -> str:
    """Release body -> the part a user wants to read in a 360px box.

    Two steps, both from the previous GUI:

    - everything after a ``---`` rule is release-tooling footer (asset list,
      checksum table), not notes;
    - a trailing ``(40-hex-sha)`` is bookkeeping a reader cannot use.
    """
    note = str(notes or "").strip()
    note = note.split("\n---")[0]
    note = _COMMIT_SUFFIX_RE.sub("", note)
    return note.strip()


class UpdateAvailableCard(PrimaryPushSettingCard):
    """Top-of-page banner: "a newer acggr exists" plus a details button.

    Hidden by default so an offline launch, an up-to-date build and a skipped
    version look identical: no banner, no placeholder row.
    """

    def __init__(
        self,
        parent=None,
        *,
        on_skip: Callable[[str], None] | None = None,
        on_install: Callable[[str], None] | None = None,
    ):
        super().__init__(
            text=qtTrId("settings.update.view_details"),
            icon=FIF.UPDATE,
            title="",
            content="",
            parent=parent,
        )
        self.setObjectName("UpdateAvailableCard")
        self._on_skip = on_skip
        self._on_install = on_install
        self._info: UpdateInfo | None = None
        self._dialog: UpdateDetailsDialog | None = None
        # ``clicked`` (not ``button.clicked``): PushSettingCard already forwards
        # the trailing button's signal, so the card stays wireable even if qfw
        # ever swaps the widget behind that button.
        self.clicked.connect(self._show_details)
        # The cleared state *is* the initial state (hidden + empty texts), so a
        # later clear_update cannot drift from what construction looks like.
        self.clear_update()

    def apply_update(self, info: UpdateInfo, *, current_version: str) -> None:
        """Fill the banner from ``info`` and reveal it.

        Idempotent: a re-check (the user pressed "检查更新" again) only rewrites
        the two texts. The widget, its layout and the button signal are never
        rebuilt, so repeat calls cannot double-connect the details button.
        """
        self._info = info
        self.setTitle(qtTrId("settings.update.available_title").format(version=info.version))
        self.setContent(_hint_text(current_version, info.published_at))
        self.show()

    def clear_update(self) -> None:
        """Hide and forget the last result (up to date, skipped, offline).

        The texts are blanked, not just the widget hidden: a hidden widget can
        still be read by accessibility tooling (and by the screenshot helper),
        and it must not advertise a version the user already dismissed.
        """
        self._info = None
        self.setTitle("")
        self.setContent("")
        self.hide()

    def _show_details(self) -> None:
        """Open one modal details box, and never two.

        The button is disabled for the whole ``exec()``: a modal box does not
        block the rest of the UI in a way that prevents a second click landing
        on this button, and two boxes would stack two masks over the window.
        """
        info = self._info
        if info is None or self._dialog is not None:
            return
        self.button.setEnabled(False)
        dialog = UpdateDetailsDialog(
            info,
            self.window(),
            on_skip=self._on_skip,
            on_install=self._on_install,
        )
        # Held in an attribute for the lifetime of the modal loop: a local-only
        # reference lets PySide6 drop the Python wrapper while C++ still shows
        # the box.
        self._dialog = dialog
        try:
            dialog.exec()
        finally:
            self._dialog = None
            self.button.setEnabled(True)
            # No WA_DeleteOnClose: with exec() that attribute frees the dialog
            # while this frame still touches it. deleteLater() runs after the
            # event loop resumes, once nothing here can reach the object.
            dialog.deleteLater()


class UpdateDetailsDialog(MessageBoxBase):
    """Modal release-notes box for one ``UpdateInfo``.

    ``MessageBoxBase`` rather than ``QMessageBox``: the notes need a scrollable
    read-only area rendered as Markdown, and the third action ("skip this
    version") has no place in a stock message box.

    ``parent`` is positional and required (unlike ``MessageBoxBase``): the mask
    is sized from the host window (``parent.width()`` in ``MaskDialogBase``), so
    ``None`` would fail deep inside qfw instead of failing here.
    """

    #: Preferred dialog body width; the notes box gets it minus the 24px view
    #: margins. A wider host window wins (see ``__init__``).
    WIDTH = 560
    #: Notes reading area: never a one-line sliver, never taller than the box.
    MIN_NOTES_HEIGHT = 120
    MAX_NOTES_HEIGHT = 360

    def __init__(
        self,
        info: UpdateInfo,
        parent,
        *,
        on_skip: Callable[[str], None] | None = None,
        on_install: Callable[[str], None] | None = None,
    ):
        super().__init__(parent)
        self._info = info
        self._on_skip = on_skip
        self._on_install = on_install

        # Two literal calls, not a computed key: the i18n scanner reads
        # ``qtTrId("...")`` literals by AST, so a ``title_key`` variable would
        # silently drop both wordings from the tables.
        if _is_prerelease_version(info.version):
            title = qtTrId("settings.update.dialog_preview").format(version=info.version)
        else:
            title = qtTrId("settings.update.dialog_stable").format(version=info.version)
        self.titleLabel = SubtitleLabel(title, self)

        # Dismissal is an icon affordance, not a third text button: "稍后"-style
        # wording made the user ask what it even means, and the banner behind the
        # mask already carries the decision (it stays until acted on).
        self.closeButton = TransparentToolButton(AcggrIcon.CLOSE, self)
        self.closeButton.setToolTip(qtTrId("settings.update.close"))
        self.closeButton.clicked.connect(self.reject)
        self.headerLayout = QHBoxLayout()
        self.headerLayout.setContentsMargins(0, 0, 0, 0)
        self.headerLayout.setSpacing(8)
        self.headerLayout.addWidget(self.titleLabel, 1)
        self.headerLayout.addWidget(self.closeButton, 0, Qt.AlignTop)

        self.browser = TextBrowser(self)
        # Notes carry links (compare-URL, issue refs). Handing them to the OS
        # browser keeps this box a read-only summary instead of turning it into
        # an embedded browser with no back button.
        self.browser.setOpenExternalLinks(True)
        self._fill_notes(str(info.notes or ""))

        self.viewLayout.addLayout(self.headerLayout)
        self.viewLayout.addWidget(self.browser)

        # Same sizing rule as the previous GUI: the box covers most of the host
        # window, but never shrinks below the width the notes were laid out for.
        min_width = int(parent.width() * 0.8)
        self.widget.setMinimumWidth(min_width)
        width = max(self.WIDTH, min_width)
        self.widget.setFixedWidth(width)

        # The primary action is the update itself, not a page to read. Dismissal is
        # covered by the close icon above; there is no separate "later" button.
        self.yesButton.setText(qtTrId("settings.update.install_now"))
        self.cancelButton.hide()
        # Skipping a version is a decision, not a way to close the box, so it
        # stays parked on the far left.
        self.skipButton = PushButton(qtTrId("settings.update.skip_version"), self.buttonGroup)
        self.skipButton.clicked.connect(self._on_skip_clicked)
        self.buttonLayout.insertWidget(0, self.skipButton, 0, Qt.AlignVCenter)
        # Hiding the cancel button leaves qfw's stretch on the primary, which
        # made "立即更新" fill half the footer. Keep it a normal-width button on
        # the right: skip | <space> | primary.
        primary_index = self.buttonLayout.indexOf(self.yesButton)
        self.buttonLayout.setStretch(primary_index, 0)
        self.buttonLayout.insertStretch(primary_index, 1)

        self._fit_notes_height(width)

    def _fill_notes(self, notes: str) -> None:
        """Markdown into the browser, with a plain-text path as a safety net.

        Qt's own ``setMarkdown`` renders the headings/bullets the release tooling
        emits; no third-party Markdown dependency is added for one dialog. An
        empty body is a real state (some releases ship no notes) and gets its
        own wording instead of a blank box.
        """
        note = _clean_notes(notes)
        if not note:
            self.browser.setPlainText(qtTrId("settings.update.notes_empty"))
            return
        try:
            self.browser.setMarkdown(note)
        except AttributeError:  # Qt build without QTextDocument::setMarkdown
            self.browser.setPlainText(note)

    def _fit_notes_height(self, width: int) -> None:
        """Grow to the content, but cap it.

        The layout height is computed once from the document: a fixed height is
        what keeps the buttons on screen for a long changelog while the notes
        area scrolls. ``setTextWidth`` is applied here because the real viewport
        width is not known before the dialog is shown.
        """
        document = self.browser.document()
        document.setTextWidth(min(width, 520))
        needed = int(document.size().height()) + 12
        self.browser.setFixedHeight(
            max(self.MIN_NOTES_HEIGHT, min(needed, self.MAX_NOTES_HEIGHT)))

    def validate(self) -> bool:
        """Hand the install off, then let the box accept.

        Hooked into ``validate`` (called by ``MessageBoxBase`` before
        ``accept``) instead of ``yesButton.clicked``: the qfw click handler is
        private, and validate runs inside it -- the launch happens before the
        dialog closes, without re-wiring the accept path.

        The callback is fire-and-forget: it spawns the detached updater and asks
        the app to quit. Returning ``True`` only means "close this box".
        """
        version = str(self._info.version or "")
        if self._on_install is not None and version:
            self._on_install(version)
        return True

    def _on_skip_clicked(self) -> None:
        # Disabled before the callback so a double click cannot persist the same
        # skip twice (or persist a version the dialog is about to forget).
        self.skipButton.setEnabled(False)
        version = str(self._info.version)
        if self._on_skip is not None:
            self._on_skip(version)
        self.accept()


class UpdateCheckCard(WidgetSettingCard):
    """The "版本 / 检查更新" row, typically last in the settings page.

    ``current_version`` is a parameter rather than a read of
    ``core.variables``: the caller already knows the running build, and the row
    should not pull the version module just to print one line.
    """

    def __init__(
        self,
        parent=None,
        *,
        current_version: str = "",
        on_check: Callable[[], None] | None = None,
    ):
        self.button = PushButton(qtTrId("settings.update.check_button"))
        super().__init__(
            FIF.SYNC,
            qtTrId("settings.update.check_title"),
            qtTrId("settings.update.check_hint").format(version=current_version),
            self.button,
            parent,
        )
        self.setObjectName("UpdateCheckCard")
        self._on_check = on_check
        self._checking = False
        if on_check is not None:
            # ``clicked`` emits a bool; routed through a slot so ``on_check``
            # keeps its documented zero-argument signature.
            self.button.clicked.connect(self._request_check)

    def _request_check(self) -> None:
        if self._checking or self._on_check is None:
            return
        self._on_check()

    def set_checking(self, checking: bool) -> None:
        """Busy state: disabled button plus "checking..." label.

        Both parts matter -- the label explains the wait, the disabled button is
        what actually stops a second request from being queued.
        """
        self._checking = bool(checking)
        self.button.setText(
            qtTrId("settings.update.checking") if self._checking
            else qtTrId("settings.update.check_button")
        )
        self.button.setEnabled(not self._checking)