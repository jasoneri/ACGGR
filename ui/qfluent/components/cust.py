from __future__ import annotations

from PySide6 import QtWidgets
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QImageReader, QMovie, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy

from qfluentwidgets import (
    FluentIcon, FluentIconBase, HyperlinkButton, ImageLabel,
    IndeterminateProgressBar, InfoBar, InfoBarIcon, InfoBarPosition,
    LineEdit, LineEditButton, FlyoutViewBase,
)


class CustEdit(LineEdit):
    custSignal = Signal(str)
    clearSignal = Signal()
    icon: FluentIconBase = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.btn = LineEditButton(self.icon, self)
        self.hBoxLayout.addWidget(self.btn, 0, Qt.AlignRight)
        self.setSizePolicy(QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding))
        self.setClearButtonEnabled(True)
        self.setTextMargins(0, 0, 59, 0)
        self.btn.clicked.connect(self.do)
        self.returnPressed.connect(self.do)
        self.clearButton.clicked.connect(self.clearSignal)

    def setClearButtonEnabled(self, enable: bool):
        self._isClearButtonEnabled = enable
        self.setTextMargins(0, 0, 28 * enable + 30, 0)

    def do(self):
        """self logic"""


class LinkEdit(CustEdit):
    """ Search line edit """
    icon = FluentIcon.LINK

    def do(self):
        text = self.text().strip()
        if text:
            self.custSignal.emit(text)
        else:
            self.clearSignal.emit()


class AcceptEdit(CustEdit):
    """ Search line edit """
    icon = FluentIcon.ACCEPT

    def do(self):
        self.custSignal.emit(self.text().strip())


class FlexImageLabel(ImageLabel):
    def setImage(self, image=None):
        self.image = image or QImage()
        if isinstance(image, str):
            reader = QImageReader(image)
            if reader.supportsAnimation():
                self.setMovie(QMovie(image))
            else:
                self.image = reader.read()
        elif isinstance(image, QPixmap):
            self.image = image.toImage()
        self.update()

    def sizeHint(self):
        return QSize(0, 0)

    def minimumSizeHint(self):
        return QSize(0, 0)


class CustomInfoBar:
    @staticmethod
    def show(title, content, parent, url, url_name, _type="ERROR", **kw):
        InfoBar_kw = dict(
            icon=getattr(InfoBarIcon, _type.upper()),
            title=title, content=content,
            orient=Qt.Horizontal, isClosable=True,
            position=InfoBarPosition.BOTTOM, duration=-1,
            parent=parent
        )
        w = InfoBar(**{**InfoBar_kw, **kw})
        w.addWidget(HyperlinkButton(FluentIcon.LINK, url, url_name, parent=None))
        w.show()

    @staticmethod
    def show_custom(title, content, parent, _type="ERROR", ib_pos=InfoBarPosition.BOTTOM, duration=-1,
                    widgets=[], **kw):
        InfoBar_kw = dict(
            icon=getattr(InfoBarIcon, _type.upper()),
            title=title, content=content,
            orient=Qt.Horizontal, isClosable=True,
            position=ib_pos, duration=duration,
            parent=parent
        )
        w = InfoBar(**{**InfoBar_kw, **kw})
        for widget in widgets:
            w.addWidget(widget)
        w.show()
        return w


class IndeterminateBarFView(FlyoutViewBase):
    def __init__(self, parent=None):
        super(IndeterminateBarFView, self).__init__(parent)
        self.barLayout = QtWidgets.QHBoxLayout()
        self.barLayout.setContentsMargins(8, 0, 8, 0)
        indeterminateBar = IndeterminateProgressBar(self, start=True)
        self.barLayout.addWidget(indeterminateBar)
        self.setLayout(self.barLayout)
        self.setFixedSize(int(parent.width() * 0.93), 10)
