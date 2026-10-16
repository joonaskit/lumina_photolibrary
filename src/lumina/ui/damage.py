"""The badge on the thumbnail of a photo whose raw data may be damaged. The grid shows
the camera's JPEG preview, which a damaged file keeps intact, so without the badge such
a photo looks fine until the Develop view decodes it."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPen

from lumina.integration import style


def paint_badge(painter, photo_rect):
    """A warning triangle on the top right corner of a thumbnail, on a disc of the
    window's card colour, so it shows on any photo."""
    side = style.px(22)
    inset = style.px(4)
    if photo_rect.width() < side + 2 * inset or photo_rect.height() < side + 2 * inset:
        return
    disc = QRectF(photo_rect.right() - inset - side, photo_rect.top() + inset, side, side)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    background = style.layer("card")
    background.setAlphaF(0.9)
    painter.setBrush(background)
    painter.drawEllipse(disc)
    box = disc.adjusted(side * 0.2, side * 0.2, -side * 0.2, -side * 0.24)
    triangle = QPainterPath(QPointF(box.center().x(), box.top()))
    triangle.lineTo(box.bottomRight())
    triangle.lineTo(box.bottomLeft())
    triangle.closeSubpath()
    painter.setBrush(style.color("warning"))
    painter.drawPath(triangle)
    # The exclamation mark, in the disc's colour
    mark = QPen(background, max(1.0, side * 0.09))
    mark.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(mark)
    x, top, height = box.center().x(), box.top(), box.height()
    painter.drawLine(QPointF(x, top + height * 0.38), QPointF(x, top + height * 0.66))
    painter.drawPoint(QPointF(x, top + height * 0.84))
    painter.restore()
