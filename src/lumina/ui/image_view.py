"""One photo, fitted to the widget, or a message in its place."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from lumina.integration import style


class ImageView(QWidget):
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

    def paintEvent(self, _event):
        painter = QPainter(self)
        area = QRectF(self.rect()).adjusted(style.px(8), style.px(8), -style.px(8), -style.px(8))
        if self._pixmap is not None and not self._pixmap.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            size = self._pixmap.size().toSizeF()
            size.scale(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(area.topLeft(), size)
            target.moveCenter(area.center())
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
        elif self._message:
            painter.setPen(style.color("textSecondary"))
            painter.drawText(
                area, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message
            )
        painter.end()
