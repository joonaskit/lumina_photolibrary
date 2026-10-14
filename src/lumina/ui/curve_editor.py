"""The tone curve: points on a square over the histogram, dragged with the mouse.

Click to add a point, drag to move it (between its neighbours), double-click or
right-click it, or drag it well outside the square, to remove it. The two end
points move only up and down. Moving signals `moved`; letting go, `committed`.
"""

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from lumina.integration import style
from lumina.qt_models.develop import IDENTITY_CURVE, curve_values, normalize_curve

# How near the pointer must be to take a point, and how far past the edge removes it
_GRAB = 9
_REMOVE = 28
# The closest two points may come, as a share of the width
_GAP = 0.01
_HISTOGRAM_ROW = {"rgb": 3, "red": 0, "green": 1, "blue": 2}


class CurveEditor(QWidget):
    # The points while one is dragged
    moved = Signal(tuple)
    # The points when it is let go, added or removed
    committed = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = IDENTITY_CURVE
        self.channel = "rgb"
        self._histogram = None
        self._dragging = None
        self._removing = False
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumSize(style.px(200), style.px(200))
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    def sizeHint(self):
        return QSize(style.px(260), style.px(260))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return width

    def resizeEvent(self, event):
        # Square: as tall as it is wide
        self.setFixedHeight(self.width())
        super().resizeEvent(event)

    def set_curve(self, channel, points):
        """Show `channel`'s `points`, signalling nothing."""
        self.channel = channel
        self.points = normalize_curve(points)
        self.update()

    def set_histogram(self, histogram):
        """4 x 256 counts: red, green, blue, luminance."""
        self._histogram = histogram
        self.update()

    # Geometry

    def _area(self):
        inset = style.px(8)
        return QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)

    def _to_screen(self, x, y):
        area = self._area()
        return QPointF(area.left() + x * area.width(), area.bottom() - y * area.height())

    def _to_curve(self, point):
        area = self._area()
        x = (point.x() - area.left()) / area.width()
        y = (area.bottom() - point.y()) / area.height()
        return x, y

    def _point_at(self, position):
        grab = style.px(_GRAB)
        for index, (x, y) in enumerate(self.points):
            spot = self._to_screen(x, y)
            if abs(spot.x() - position.x()) <= grab and abs(spot.y() - position.y()) <= grab:
                return index
        return None

    # Painting

    def _colour(self):
        names = {"red": "red", "green": "green", "blue": "blue"}
        if self.channel in names:
            return style.palette_color(names[self.channel])
        return style.color("textPrimary")

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._area()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(style.layer("raised"))
        painter.drawRoundedRect(area, style.radius("small"), style.radius("small"))
        self._paint_histogram(painter, area)
        grid = style.color("textPrimary")
        grid.setAlphaF(0.10)
        painter.setPen(QPen(grid, 1))
        for step in (0.25, 0.5, 0.75):
            painter.drawLine(self._to_screen(step, 0), self._to_screen(step, 1))
            painter.drawLine(self._to_screen(0, step), self._to_screen(1, step))
        painter.drawLine(self._to_screen(0, 0), self._to_screen(1, 1))
        samples = max(2, int(area.width()))
        values = curve_values(self.points, samples)
        path = QPainterPath(self._to_screen(0, values[0]))
        for index in range(1, samples):
            path.lineTo(self._to_screen(index / (samples - 1), values[index]))
        colour = self._colour()
        painter.setPen(QPen(colour, style.px(2)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        radius = style.px(5)
        for index, (x, y) in enumerate(self.points):
            removing = self._removing and index == self._dragging
            painter.setPen(QPen(colour, style.px(2)))
            painter.setBrush(style.layer("raised") if removing else colour)
            painter.drawEllipse(self._to_screen(x, y), radius, radius)
        painter.end()

    def _paint_histogram(self, painter, area):
        if self._histogram is None:
            return
        counts = np.asarray(self._histogram[_HISTOGRAM_ROW[self.channel]], dtype=np.float64)
        # A square root keeps the shape visible beside a tall spike at black or white
        heights = np.sqrt(counts)
        top = heights[1:-1].max() if heights[1:-1].max() > 0 else heights.max()
        if top <= 0:
            return
        heights = np.minimum(heights / top, 1.0)
        path = QPainterPath(self._to_screen(0, 0))
        for index, height in enumerate(heights):
            path.lineTo(self._to_screen(index / 255, height * 0.9))
        path.lineTo(self._to_screen(1, 0))
        path.closeSubpath()
        fill = self._colour()
        fill.setAlphaF(0.18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawPath(path)

    # The mouse

    def mousePressEvent(self, event):
        position = event.position()
        index = self._point_at(position)
        if event.button() == Qt.MouseButton.RightButton:
            if index is not None:
                self._remove(index)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if index is None:
            x, y = self._to_curve(position)
            if not 0 < x < 1:
                return
            # On the curve where it was clicked, so adding a point changes nothing yet
            y = float(curve_values(self.points, 2001)[round(x * 2000)])
            points = [p for p in self.points if abs(p[0] - x) >= _GAP] + [(x, y)]
            self.points = normalize_curve(points)
            index = next(i for i, p in enumerate(self.points) if abs(p[0] - x) < 1e-6)
            self.moved.emit(self.points)
        self._dragging = index
        self.update()

    def mouseMoveEvent(self, event):
        if self._dragging is None:
            hovering = self._point_at(event.position()) is not None
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if hovering else Qt.CursorShape.CrossCursor
            )
            return
        x, y = self._to_curve(event.position())
        index = self._dragging
        last = len(self.points) - 1
        area = self._area()
        outside = max(
            area.left() - event.position().x(),
            event.position().x() - area.right(),
            area.top() - event.position().y(),
            event.position().y() - area.bottom(),
        )
        self._removing = 0 < index < last and outside > style.px(_REMOVE)
        if index in (0, last):
            x = self.points[index][0]
        else:
            low = self.points[index - 1][0] + _GAP
            high = self.points[index + 1][0] - _GAP
            x = min(max(x, low), high)
        points = list(self.points)
        points[index] = (round(x, 6), round(min(max(y, 0.0), 1.0), 6))
        self.points = tuple(points)
        self.moved.emit(self._without_removed())
        self.update()

    def mouseReleaseEvent(self, event):
        if self._dragging is None:
            return
        if self._removing:
            self._remove(self._dragging)
        else:
            self.committed.emit(self.points)
        self._dragging, self._removing = None, False
        self.update()

    def mouseDoubleClickEvent(self, event):
        index = self._point_at(event.position())
        if index is not None:
            self._dragging = None
            self._remove(index)

    def _remove(self, index):
        if 0 < index < len(self.points) - 1:
            self.points = self.points[:index] + self.points[index + 1 :]
            self.committed.emit(self.points)
            self.update()

    def _without_removed(self):
        if self._removing:
            return self.points[: self._dragging] + self.points[self._dragging + 1 :]
        return self.points

    def reset(self):
        self.points = IDENTITY_CURVE
        self.committed.emit(self.points)
        self.update()
