# -*- coding: utf-8 -*-
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter
from qfluentwidgets import BodyLabel, CaptionLabel, GroupHeaderCardWidget


class _ElidedLabelMixin:
    """Report a tiny minimum width and paint elided text at the real width."""

    #: Layout floor. Rows keep icon + widget visible; text elides before this.
    _MIN_WIDTH = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fullText = super().text()

    def setText(self, text: str):
        self._fullText = text
        super().setText(text)

    def text(self) -> str:
        return self._fullText

    def minimumSizeHint(self):
        # Height still follows the real metrics; width is decoupled from the text
        # so the row (and the window above it) stays freely shrinkable. sizeHint
        # keeps the full text width: layouts hand the label its preferred width
        # when space allows and only squeeze it toward this floor when not.
        return QSize(self._MIN_WIDTH, super().minimumSizeHint().height())

    def sizeHint(self):
        return QSize(super().sizeHint().width(), super().minimumSizeHint().height())

    def paintEvent(self, event):
        painter = QPainter(self)
        metrics = self.fontMetrics()
        painter.setFont(self.font())
        # qfw colours the label via qss; palette resolves it for the painter.
        painter.setPen(self.palette().color(self.foregroundRole()))
        # Elide per line: the text may contain hard newlines (e.g. the pawchive
        # cookie hint), which QFontMetrics.elidedText would otherwise measure as
        # one long line and truncate far too early.
        line_height = metrics.lineSpacing()
        y = 0
        alignment = self.alignment()
        for line in self._fullText.split("\n"):
            elided = metrics.elidedText(line, Qt.ElideRight, max(self.width(), 1))
            painter.drawText(0, y, self.width(), line_height, int(alignment), elided)
            y += line_height
            if y > self.height():
                break


class ElidedBodyLabel(_ElidedLabelMixin, BodyLabel):
    """BodyLabel that elides instead of inflating the row minimum width."""


class ElidedCaptionLabel(_ElidedLabelMixin, CaptionLabel):
    """CaptionLabel that elides instead of inflating the row minimum width."""


class ElidedGroupHeaderCardWidget(GroupHeaderCardWidget):
    """``GroupHeaderCardWidget`` whose group rows elide long text.

    Drop-in base for the settings cards: every ``addGroup`` row gets an
    eliding title/content pair, so a long hint (imgPalace token, Comfy host)
    no longer pins the window's minimum width -- shrinking the window just
    elides the description.
    """

    def addGroup(self, icon, title: str, content: str, widget, stretch=0):
        group = super().addGroup(icon, title, content, widget, stretch=stretch)
        for attr, cls in (("titleLabel", ElidedBodyLabel), ("contentLabel", ElidedCaptionLabel)):
            original = getattr(group, attr)
            replacement = cls(original.text())
            replacement.setFont(original.font())
            replacement.setAlignment(original.alignment())
            replacement.setTextColor(original._lightColor, original._darkColor)
            setattr(group, attr, replacement)
            text_layout = group.textLayout
            index = text_layout.indexOf(original)
            text_layout.insertWidget(index, replacement)
            text_layout.removeWidget(original)
            original.deleteLater()
        return group
