import pathlib
import random

from PySide6.QtCore import QSize
from PySide6.QtGui import QPixmap, QDesktopServices
from qfluentwidgets import ImageLabel, TextBrowser, TextEdit

from core import conf, ori_path
from ui.core.timer import safe_single_shot

# Bound the background scan: a user pointing ``bg_path`` at a photo library turned
# the synchronous ``rglob`` into a 490 s UI freeze (measured, T5 C3).
_BG_SCAN_LIMIT = 200


class BgMgr:
    """Random background picture for the text browser.

    Upstream also used this class to build its splash art; standalone acggr only
    needs the background lookup, so the PIL splice stayed behind. ``custom/*.png``
    in the install dir is the only source — ``Conf`` has no ``bg_path`` field here.
    """

    def __init__(self):
        self.bg_fs = []
        self.start_bg_f, self.start_ext = self._find_start_bg()
        self.bg_f, self.ext = self._find_bg()
        self.movie = None

    def _find_bg(self):
        # str from yaml / update() would otherwise reach .rglob() and raise.
        target_dir = pathlib.Path(getattr(conf, "bg_path", "") or ori_path.joinpath("custom"))
        if str(target_dir) == "." or not target_dir.is_dir():
            return None, None
        for file in target_dir.rglob('*.png'):
            self.bg_fs.append((file.as_posix(), file.suffix))
            if len(self.bg_fs) >= _BG_SCAN_LIMIT:
                break
        if self.bg_fs:
            return random.choice(self.bg_fs)
        return None, None

    def _find_start_bg(self, kind: str = "start"):
        cus_p = ori_path.joinpath("custom")
        if not cus_p.is_dir():
            return None, None
        matched = [
            (file.as_posix(), file.suffix)
            for file in cus_p.iterdir()
            if file.is_file() and file.suffix.lower() in ('.gif', '.png')
        ]
        return random.choice(matched) if matched else (None, None)

class TextBrowserWithBg(TextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image_label = None
        self.setStyleSheet("""
            QTextBrowser {
                background-color: transparent;
            }
        """)

    def set_fixed_image(self, image_path, height=None, margin=0):
        if self.image_label:
            self.image_label.setParent(None)
            self.image_label.deleteLater()

        # 创建新的图片标签
        self.image_label = ImageLabel(self)
        self.image_label.setImage(image_path)
        if not height:
            height = int(self.height()*1.0)
        pixmap = self.image_label.pixmap()
        if pixmap.isNull() or pixmap.height() == 0:
            self.image_label = None
            return
        self.image_label.setFixedSize(int(height * (pixmap.width() / pixmap.height())), height)
        self.image_label.setScaledContents(True)

        self.image_margin = margin

        self.position_image()
        self.image_label.lower()
        self.image_label.show()

    def position_image(self):
        if not self.image_label:
            return
        rect = self.rect()
        margin = getattr(self, 'image_margin', 15)
        x = rect.width() - self.image_label.width() - margin
        y = rect.height() - self.image_label.height() - margin

        self.image_label.move(x, y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.image_label:
            safe_single_shot(0, self.position_image)


class TextEditWithBg(TextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image_label = None
        self.image_path = None
        self.image_margin = 0

    def set_fixed_image(self, image_path, height=None, margin=0):
        self.image_path = image_path
        self.image_margin = margin
        if self.image_label:
            self.image_label.setParent(None)
            self.image_label.deleteLater()
            safe_single_shot(20, lambda: self.set_fixed_image(image_path, height, margin))
        else:
            self.image_label = ImageLabel(self)
            self.image_label.hide()  
            self.image_label.setScaledContents(True)
        pixmap = QPixmap(self.image_path)
        if pixmap.isNull():
            self.image_label.hide()
            return
        self.image_label.setImage(pixmap)
        self.image_label.lower()

    def img_resize_and_repos(self):
        if not self.image_label or not self.image_path:
            return
        pixmap = self.image_label.pixmap()
        if not pixmap or pixmap.isNull() or pixmap.height() == 0:
            return
        # resize
        current_height = self.height() - self.image_margin
        new_width = int(current_height * pixmap.width() / pixmap.height())
        self.image_label.setFixedSize(QSize(new_width, current_height))
        # repos
        rect = self.rect()
        x = rect.width() - self.image_label.width() - self.image_margin
        y = rect.height() - self.image_label.height() - self.image_margin
        self.image_label.move(x, y)
        if not self.image_label.isVisible():
            self.image_label.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.image_label:
            safe_single_shot(0, self.img_resize_and_repos)
