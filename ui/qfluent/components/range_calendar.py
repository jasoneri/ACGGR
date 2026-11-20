"""Fluent 日期范围日历（社区版 qfluentwidgets 没有 Pro 的 RangeCalendarPicker）。

单月视图 + 组件内"年 → 月 → 日"三级跳转（对齐 WinUI CalendarView 的 DisplayMode），保留
Material 3 range picker 的"先点起点、再点终点"模型和已选区间摘要。宿主浮窗宽度上限 460px，
双月会把日格压到 30px 以下，故单月 + 快速跳转。

跨年正确性只依赖 QDate 比较：可见月锚点是"当月 1 日"，相邻月日格参与同一套区间判定。
性能：42 日格 + 12 年格 + 12 月格全部复用，_rebuild 只改状态并 update()；主题色在 paintEvent
现取。线程约束：纯 GUI 控件，只在主线程使用。
"""
from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from functools import partial

from PySide6.QtCore import QDate, QLocale, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon as FIF, TransparentToolButton
from qfluentwidgets.common.font import getFont
from qfluentwidgets.common.style_sheet import isDarkTheme, themeColor

_CELL_WIDTH = 36
_CELL_HEIGHT = 32
_GRID_ROWS = 6
_GRID_COLS = 7
_GRID_WIDTH = _GRID_COLS * _CELL_WIDTH
_GRID_HEIGHT = _GRID_ROWS * _CELL_HEIGHT
_PERIOD_COLUMNS = 4
_PERIOD_ROWS = 3
_PERIOD_SPACING = 4
_PERIOD_WIDTH = 60
_PERIOD_HEIGHT = 52
_YEARS_PER_PAGE = _PERIOD_COLUMNS * _PERIOD_ROWS
_WEEKDAY_HEIGHT = 22
_SUMMARY_HEIGHT = 20
_MONTH_TITLE_FORMAT = "yyyy MMMM"
_MIN_YEAR = 1
_MAX_YEAR = 9999
_BAND_ALPHA = 48
_OTHER_MONTH_ALPHA = 96


class _View(Enum):
    DAYS = 0
    MONTHS = 1
    YEARS = 2


def _text_color(*, on_accent: bool = False) -> QColor:
    if on_accent:
        return QColor(255, 255, 255)
    return QColor(230, 230, 230) if isDarkTheme() else QColor(32, 32, 32)


def _muted_text_color() -> QColor:
    return QColor(255, 255, 255, 138) if isDarkTheme() else QColor(0, 0, 0, 138)


def _hover_color() -> QColor:
    return QColor(255, 255, 255, 20) if isDarkTheme() else QColor(0, 0, 0, 12)


def _band_color() -> QColor:
    accent = themeColor()
    return QColor(accent.red(), accent.green(), accent.blue(), _BAND_ALPHA)


class _DayCell(QPushButton):
    """单日格子：自绘区间底色/端点/今天描边，避免 qfluentwidgets 按钮样式表干扰。"""

    clickedDate = Signal(QDate)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(_CELL_WIDTH, _CELL_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._date = QDate()
        self._other_month = False
        self._in_range = False
        self._is_start = False
        self._is_end = False
        self._is_today = False
        self._hovered = False
        self.clicked.connect(self._emit_clicked)

    def set_date(self, date: QDate, *, other_month: bool):
        self._date = QDate(date)
        self._other_month = bool(other_month)

    def set_state(
        self,
        *,
        in_range: bool,
        is_start: bool,
        is_end: bool,
        is_today: bool,
    ):
        self._in_range = bool(in_range)
        self._is_start = bool(is_start)
        self._is_end = bool(is_end)
        self._is_today = bool(is_today)
        self.update()

    def date(self) -> QDate:
        return QDate(self._date)

    def _emit_clicked(self, _checked: bool = False):
        if self._date.isValid():
            self.clickedDate.emit(QDate(self._date))

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        accent = themeColor()
        on_accent = self._is_start or self._is_end

        if self._in_range:
            # 底色铺满整格（左右不内缩），相邻格子拼成一条连续带。
            painter.fillRect(self.rect(), _band_color())

        pill = QRectF(self.rect()).adjusted(2, 3, -2, -3)
        if on_accent:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(pill, 6, 6)
        elif self._hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_hover_color())
            painter.drawRoundedRect(pill, 6, 6)
        elif self._is_today:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(accent, 1))
            painter.drawRoundedRect(pill.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

        color = _text_color(on_accent=on_accent)
        if self._other_month and not on_accent:
            color.setAlpha(_OTHER_MONTH_ALPHA)
        font = getFont(12)
        if on_accent:
            font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self._date.day()))
        painter.end()


class _PeriodCell(QPushButton):
    """年/月列表格子：选中实心、当前项描边、悬停浅底。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(_PERIOD_WIDTH, _PERIOD_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._text = ""
        self._value = 0
        self._selected = False
        self._current = False

    def set_period(self, text: str, value: int, *, selected: bool, current: bool):
        self._text = str(text)
        self._value = int(value)
        self._selected = bool(selected)
        self._current = bool(current)
        self.update()

    def value(self) -> int:
        return self._value

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        accent = themeColor()
        pill = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        font = getFont(12)
        if self._selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(pill, 6, 6)
            color = _text_color(on_accent=True)
            font.setWeight(QFont.Weight.DemiBold)
        else:
            if self.underMouse():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_hover_color())
                painter.drawRoundedRect(pill, 6, 6)
            elif self._current:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(accent, 1))
                painter.drawRoundedRect(pill.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
            color = _text_color()
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()


class _TitleButton(QPushButton):
    """月份抬头：文字 + 折叠箭头，点击进入年/月跳转。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._text = ""
        self._caret_up = False

    def set_title(self, text: str, *, caret_up: bool = False):
        self._text = str(text)
        self._caret_up = bool(caret_up)
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        if self.underMouse() or self.isDown():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_hover_color())
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)

        font = getFont(14)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        text_width = painter.fontMetrics().horizontalAdvance(self._text)
        caret_width = 9
        gap = 5
        left = (self.width() - (text_width + gap + caret_width)) / 2
        painter.setPen(_text_color())
        painter.drawText(
            QRectF(left, 0, text_width, self.height()),
            Qt.AlignmentFlag.AlignCenter,
            self._text,
        )

        center_x = left + text_width + gap + caret_width / 2
        center_y = self.height() / 2
        pen = QPen(_muted_text_color(), 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self._caret_up:
            painter.drawLine(QPointF(center_x - 4, center_y + 2), QPointF(center_x, center_y - 2))
            painter.drawLine(QPointF(center_x, center_y - 2), QPointF(center_x + 4, center_y + 2))
        else:
            painter.drawLine(QPointF(center_x - 4, center_y - 2), QPointF(center_x, center_y + 2))
            painter.drawLine(QPointF(center_x, center_y + 2), QPointF(center_x + 4, center_y - 2))
        painter.end()


class _WeekdayHeader(QWidget):
    """星期行：随日格列宽自绘，颜色在 paintEvent 现取。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(_WEEKDAY_HEIGHT)
        self._names = [
            QLocale().dayName(day, QLocale.FormatType.ShortFormat) for day in range(1, 8)
        ]
        self._labels_visible = True

    def set_labels_visible(self, visible: bool):
        self._labels_visible = bool(visible)
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        if not self._labels_visible:
            return
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        painter.setFont(getFont(11))
        painter.setPen(_muted_text_color())
        for index, name in enumerate(self._names):
            painter.drawText(
                QRectF(index * _CELL_WIDTH, 0, _CELL_WIDTH, self.height()),
                Qt.AlignmentFlag.AlignCenter,
                name,
            )
        painter.end()


class _RangeSummary(QWidget):
    """已选区间摘要：只选了一端时显示 ``…``，跨年时两端都带年份。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(_SUMMARY_HEIGHT)
        self._start = QDate()
        self._end = QDate()

    def set_dates(self, start: QDate, end: QDate):
        self._start = QDate(start) if start.isValid() else QDate()
        self._end = QDate(end) if end.isValid() else QDate()
        self.update()

    def summary_text(self) -> str:
        if not self._start.isValid() and not self._end.isValid():
            return ""
        start_text = self._start.toString("yyyy-MM-dd") if self._start.isValid() else "…"
        end_text = self._end.toString("yyyy-MM-dd") if self._end.isValid() else "…"
        return f"{start_text} → {end_text}"

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        text = self.summary_text()
        if not text:
            return
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        painter.setFont(getFont(11))
        painter.setPen(_muted_text_color())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


class RangeCalendar(QWidget):
    """Fluent 风格日期范围日历。

    ``rangeChanged(start, end)`` 只在两端都选齐时发一次；只选了一端时保持静默
    （调用方不必处理“半截范围”）。``set_range`` 只回填显示，不发信号。
    """

    rangeChanged = Signal(QDate, QDate)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RangeCalendar")
        self._start = QDate()
        self._end = QDate()
        self._month = QDate(QDate.currentDate().year(), QDate.currentDate().month(), 1)
        self._month_year = self._month.year()
        self._year_page_start = self._year_page_offset(self._month.year())
        self._view = _View.DAYS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(2)
        self.prev_button = TransparentToolButton(FIF.LEFT_ARROW, self)
        self.prev_button.setFixedSize(28, 28)
        self.title_button = _TitleButton(self)
        self.next_button = TransparentToolButton(FIF.RIGHT_ARROW, self)
        self.next_button.setFixedSize(28, 28)
        header.addWidget(self.prev_button)
        header.addWidget(self.title_button, 1)
        header.addWidget(self.next_button)
        layout.addLayout(header)

        self.weekday_header = _WeekdayHeader(self)
        layout.addWidget(self.weekday_header)

        self._day_cells = [self._create_day_cell() for _ in range(_GRID_ROWS * _GRID_COLS)]
        self._month_cells = self._create_period_cells(12, self._on_month_cell_clicked)
        self._year_cells = self._create_period_cells(_YEARS_PER_PAGE, self._on_year_cell_clicked)

        self._stack = QStackedWidget(self)
        self._stack.setFixedHeight(_GRID_HEIGHT)
        self._stack.addWidget(self._build_day_page())
        self._stack.addWidget(self._build_period_page(self._month_cells))
        self._stack.addWidget(self._build_period_page(self._year_cells))
        layout.addWidget(self._stack)

        self.summary = _RangeSummary(self)
        layout.addWidget(self.summary)

        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedWidth(_GRID_WIDTH + 16)
        self.prev_button.clicked.connect(self._step_backward)
        self.next_button.clicked.connect(self._step_forward)
        self.title_button.clicked.connect(self._on_title_clicked)
        self._rebuild()

    # ---- 公开 API ----
    def range(self) -> tuple[QDate, QDate]:
        return QDate(self._start), QDate(self._end)

    def set_range(self, start: QDate, end: QDate):
        """回填区间（不发信号）。``end`` 无效表示“只定起点”。"""
        self._start = QDate(start) if start and start.isValid() else QDate()
        self._end = QDate(end) if end and end.isValid() else QDate()
        anchor = self._end if self._end.isValid() else self._start
        if anchor.isValid():
            self._month = QDate(anchor.year(), anchor.month(), 1)
            self._month_year = self._month.year()
        self._rebuild()

    def visible_month(self) -> QDate:
        return QDate(self._month)

    def set_visible_month(self, date: QDate):
        if date.isValid():
            self._month = QDate(date.year(), date.month(), 1)
            self._month_year = self._month.year()
            self._view = _View.DAYS
            self._rebuild()

    def view_mode(self) -> str:
        """当前页面：``days`` / ``months`` / ``years``。"""
        return self._view.name.lower()

    def show_year_selector(self, anchor_year: int | None = None):
        """进入年列表（不动已选区间）。"""
        year = int(anchor_year) if anchor_year is not None else self._focus_year()
        year = max(_MIN_YEAR, min(year, _MAX_YEAR))
        self._year_page_start = self._year_page_offset(year)
        self._view = _View.YEARS
        self._rebuild()

    def show_month_selector(self, year: int | None = None):
        """进入月列表（不动已选区间）。"""
        target = int(year) if year is not None else self._focus_year()
        self._month_year = max(_MIN_YEAR, min(target, _MAX_YEAR))
        self._view = _View.MONTHS
        self._rebuild()

    def show_day_view(self):
        self._view = _View.DAYS
        self._rebuild()

    def apply_theme(self):
        """主题切换后重绘（颜色在 paintEvent 现取，这里只需刷新一次）。"""
        self.title_button.update()
        self.weekday_header.update()
        self.summary.update()
        for cell in self._day_cells:
            cell.update()
        for cell in self._month_cells:
            cell.update()
        for cell in self._year_cells:
            cell.update()

    # ---- 内部构建 ----
    def _create_day_cell(self) -> _DayCell:
        cell = _DayCell(self)
        cell.clickedDate.connect(self._on_day_clicked)
        return cell

    def _create_period_cells(
        self, count: int, handler: Callable[[_PeriodCell, bool], None]
    ) -> list[_PeriodCell]:
        cells: list[_PeriodCell] = []
        for _ in range(count):
            cell = _PeriodCell(self)
            cell.clicked.connect(partial(handler, cell))
            cells.append(cell)
        return cells

    def _build_day_page(self) -> QWidget:
        page = QWidget(self)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        host = QWidget(page)
        host.setFixedSize(_GRID_WIDTH, _GRID_HEIGHT)
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        for index, cell in enumerate(self._day_cells):
            grid.addWidget(cell, index // _GRID_COLS, index % _GRID_COLS)
        page_layout.addWidget(host, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    def _build_period_page(self, cells: list[_PeriodCell]) -> QWidget:
        page = QWidget(self)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        host = QWidget(page)
        host.setFixedSize(
            _PERIOD_COLUMNS * _PERIOD_WIDTH + (_PERIOD_COLUMNS - 1) * _PERIOD_SPACING,
            _PERIOD_ROWS * _PERIOD_HEIGHT + (_PERIOD_ROWS - 1) * _PERIOD_SPACING,
        )
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(_PERIOD_SPACING)
        for index, cell in enumerate(cells):
            grid.addWidget(cell, index // _PERIOD_COLUMNS, index % _PERIOD_COLUMNS)
        page_layout.addWidget(host, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    # ---- 刷新 ----
    def _rebuild(self):
        self._refresh_title()
        self.weekday_header.set_labels_visible(self._view == _View.DAYS)
        self._refresh_day_cells()
        self._refresh_month_cells()
        self._refresh_year_cells()
        self.summary.set_dates(self._start, self._end)
        self._stack.setCurrentIndex(self._view.value)

    def _refresh_title(self):
        if self._view == _View.YEARS:
            last_year = min(self._year_page_start + _YEARS_PER_PAGE - 1, _MAX_YEAR)
            self.title_button.set_title(
                f"{self._year_page_start} - {last_year}", caret_up=True
            )
        elif self._view == _View.MONTHS:
            self.title_button.set_title(str(self._month_year))
        else:
            self.title_button.set_title(QLocale().toString(self._month, _MONTH_TITLE_FORMAT))

    def _refresh_day_cells(self):
        first_day = QDate(self._month.year(), self._month.month(), 1)
        # Qt: Monday=1 .. Sunday=7；我们的列同样从周一开始。
        first_cell_date = first_day.addDays(-(first_day.dayOfWeek() - 1))
        today = QDate.currentDate()
        start_valid = self._start.isValid()
        end_valid = self._end.isValid()
        for index, cell in enumerate(self._day_cells):
            date = first_cell_date.addDays(index)
            cell.set_date(
                date,
                other_month=(
                    date.year() != first_day.year() or date.month() != first_day.month()
                ),
            )
            cell.set_state(
                in_range=start_valid and end_valid and self._start < date < self._end,
                is_start=start_valid and date == self._start,
                is_end=end_valid and date == self._end,
                is_today=date == today,
            )

    def _refresh_month_cells(self):
        today = QDate.currentDate()
        for index, cell in enumerate(self._month_cells):
            month = index + 1
            cell.set_period(
                QLocale().monthName(month, QLocale.FormatType.ShortFormat),
                month,
                selected=(
                    self._month.year() == self._month_year and self._month.month() == month
                ),
                current=(today.year() == self._month_year and today.month() == month),
            )

    def _refresh_year_cells(self):
        today = QDate.currentDate()
        for index, cell in enumerate(self._year_cells):
            year = self._year_page_start + index
            cell.set_period(
                str(year),
                year,
                selected=(year == self._month.year()),
                current=(year == today.year()),
            )

    # ---- 导航 ----
    def _focus_year(self) -> int:
        if self._view == _View.MONTHS:
            return self._month_year
        return self._month.year()

    def _year_page_offset(self, year: int) -> int:
        return max(_MIN_YEAR, min(year - 4, _MAX_YEAR - _YEARS_PER_PAGE + 1))

    def _shift_month(self, delta: int):
        self._month = self._month.addMonths(int(delta))
        self._month_year = self._month.year()

    def _step_backward(self):
        self._step(-1)

    def _step_forward(self):
        self._step(1)

    def _step(self, delta: int):
        if self._view == _View.YEARS:
            self._year_page_start = max(
                _MIN_YEAR,
                min(
                    self._year_page_start + delta * _YEARS_PER_PAGE,
                    _MAX_YEAR - _YEARS_PER_PAGE + 1,
                ),
            )
        elif self._view == _View.MONTHS:
            self._month_year = max(_MIN_YEAR, min(self._month_year + delta, _MAX_YEAR))
        else:
            self._shift_month(delta)
        self._rebuild()

    def _on_title_clicked(self):
        if self._view == _View.YEARS:
            self.show_day_view()
        else:
            self.show_year_selector()

    def _on_month_cell_clicked(self, cell: _PeriodCell, _checked: bool = False):
        self._month = QDate(self._month_year, cell.value(), 1)
        self._view = _View.DAYS
        self._rebuild()

    def _on_year_cell_clicked(self, cell: _PeriodCell, _checked: bool = False):
        self._month_year = cell.value()
        self._view = _View.MONTHS
        self._rebuild()

    # ---- 选择 ----
    def _on_day_clicked(self, date: QDate):
        if not date.isValid():
            return
        if date.year() != self._month.year() or date.month() != self._month.month():
            self._month = QDate(date.year(), date.month(), 1)
            self._month_year = date.year()

        if not self._start.isValid() or (self._start.isValid() and self._end.isValid()):
            # 第一次点击（或上一段已完整）：重新起一段，端点只定起点。
            self._start = QDate(date)
            self._end = QDate()
            self._rebuild()
            return

        if date < self._start:
            self._end = QDate(self._start)
            self._start = QDate(date)
        else:
            self._end = QDate(date)
        self._rebuild()
        self.rangeChanged.emit(QDate(self._start), QDate(self._end))


__all__ = ["RangeCalendar"]