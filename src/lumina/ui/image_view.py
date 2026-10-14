"""One photo, fitted to the widget, or a message in its place."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from lumina.integration import style


class ImageView(QWidget):
    # Space around the image, unscaled
    inset = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._message = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def pixmap(self):
        return self._pixmap

    def message(self):
        return self._message

    def set_image(self, image):
        """A QImage or QPixmap to show; None for nothing."""
        if image is not None and not isinstance(image, QPixmap):
            image = QPixmap.fromImage(image)
        self._pixmap = image
        self._message = ""
        self.update()

    def set_message(self, text):
        self._pixmap = None
        self._message = text
        self.update()

    def image_rect(self):
        """Where the image is drawn, fitted and centred; None without one."""
        if self._pixmap is None or self._pixmap.isNull():
            return None
        inset = style.px(self.inset)
        area = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        size = self._pixmap.size().toSizeF()
        size.scale(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QRectF(area.topLeft(), size)
        target.moveCenter(area.center())
        return target

    def paintEvent(self, _event):
        painter = QPainter(self)
        inset = style.px(self.inset)
        area = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        target = self.image_rect()
        if target is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
            self.paint_over(painter, target)
        elif self._message:
            painter.setPen(style.color("textSecondary"))
            painter.drawText(
                area, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message
            )
        painter.end()

    def paint_over(self, painter, target):
        """For a subclass: draw over the image, which is at `target`."""
