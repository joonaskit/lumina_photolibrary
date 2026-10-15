"""Star ratings: the stars on a thumbnail, and how they are written."""

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath
from PySide6.QtWidgets import QSizePolicy, QWidget

from lumina.integration import style

# The star as five points of a circle and the five between them, the inner ones this much
# smaller
_INNER = 0.42


def text(rating):
    """ "★★★" for three stars; nothing for none."""
    return "★" * rating


def _star(centre, radius):
    path = QPainterPath()
    for number in range(10):
        angle = -math.pi / 2 + number * math.pi / 5
        reach = radius if number % 2 == 0 else radius * _INNER
        point = QPointF(centre.x() + reach * math.cos(angle), centre.y() + reach * math.sin(angle))
        if number == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    return path


def paint_stars(painter, photo_rect, rating):
    """`rating` stars on the bottom left corner of a thumbnail, in the theme's warning
    colour on a pill of the window's card colour, so they show on any photo. Nothing for
    no stars, or when the thumbnail is too small for them."""
    if rating < 1:
        return
    size = style.px(11)
    gap = style.px(1)
    pad = style.px(5)
    inset = style.px(4)
    width = rating * size + (rating - 1) * gap + 2 * pad
    height = size + 2 * pad - style.px(2)
    # Room beside the flag's badge, at the other corner
    if (
        photo_rect.width() < width + style.px(22) + 3 * inset
        or photo_rect.height() < height + 2 * inset
    ):
        return
    pill = QRectF(photo_rect.left() + inset, photo_rect.bottom() - inset - height, width, height)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    background = style.layer("card")
    background.setAlphaF(0.9)
    painter.setBrush(background)
    painter.drawRoundedRect(pill, height / 2, height / 2)
    painter.setBrush(style.color("warning"))
    for number in range(rating):
        centre = QPointF(pill.left() + pad + size / 2 + number * (size + gap), pill.center().y())
        painter.drawPath(_star(centre, size / 2))
    painter.restore()


class StarRating(QWidget):
    """Five stars to click in the toolbar: the rating of the photo shown or chosen, and the
    way to change it. Clicking the star the rating is at clears it, as pressing its key
    again does. Hovering shows what a click would give."""

    # The number of the star clicked, 1 to 5
    chosen = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rating = 0
        self._hover = 0
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setToolTip("Rate the photo (keys 1 to 5); the same star again clears it")
        self.setFixedHeight(style.px(style.CONTROL_HEIGHT))

    def _cell(self):
        return style.px(22)

    def sizeHint(self):
        return QSize(5 * self._cell() + style.px(4), style.px(style.CONTROL_HEIGHT))

    def minimumSizeHint(self):
        return self.sizeHint()

    def rating(self):
        return self._rating

    def set_rating(self, rating):
        """Show `rating` stars (0 for none, also for photos that differ)."""
        if rating != self._rating:
            self._rating = rating
            self.update()

    def _star_at(self, x):
        number = int((x - style.px(2)) // self._cell()) + 1
        return number if 1 <= number <= 5 else 0

    def mouseMoveEvent(self, event):
        hover = self._star_at(event.position().x()) if self.isEnabled() else 0
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, _event):
        self._hover = 0
        self.update()

    def mouseReleaseEvent(self, event):
        number = self._star_at(event.position().x())
        if self.isEnabled() and number and event.button() == Qt.MouseButton.LeftButton:
            self.chosen.emit(number)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shown = self._hover or self._rating
        radius = self._cell() * 0.36
        for number in range(1, 6):
            centre = QPointF(style.px(2) + (number - 0.5) * self._cell(), self.height() / 2)
            path = _star(centre, radius)
            if not self.isEnabled():
                painter.setPen(style.color("textTertiary"))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            elif number <= shown:
                # What a click would give, while hovering, a little paler than what is set
                colour = style.color("warning")
                if self._hover and number > self._rating:
                    colour.setAlphaF(0.6)
                painter.setPen(colour)
                painter.setBrush(colour)
            else:
                painter.setPen(style.color("textSecondary"))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.end()
