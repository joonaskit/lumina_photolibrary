"""One photo, fitted to the widget, or a message in its place.

A zoomable view (the viewer's) also shows the photo at 100 % and other steps: a
click switches between fitted and 100 % at the point clicked, a drag or the wheel
moves it, Ctrl and the wheel zoom in steps. The zoom is in pixels of the photo at
full size (`full`), which the image shown may not have yet: `needs_full` says
when a larger one should be fetched. The zoom and the place (as a fraction of the
photo) are kept when another photo is shown, so the same detail of each can be
compared.
"""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget

from lumina.integration import style

# Zoom steps: screen pixels a pixel of the photo at full size
ZOOM_STEPS = (0.25, 0.5, 1.0, 2.0, 4.0)
# A wheel step's movement while zoomed, unscaled pixels
_WHEEL_PAN = 60


class ImageView(QWidget):
    # Space around the image, unscaled
    inset = 8
    # Zoomed in or out, or moved
    zoom_changed = Signal()

    def __init__(self, parent=None, zoomable=False):
        super().__init__(parent)
        self._pixmap = None
        self._message = ""
        self._zoomable = zoomable
        # The photo's (width, height) at full size
        self._full = None
        # Screen pixels a pixel of the photo at full size; None: fitted
        self._zoom = None
        # The point of the photo at the middle of the view, as fractions of its size
        self._center = (0.5, 0.5)
        self._press = None
        self._dragged = False
        # The image made smaller for the fitted view, and the size it was made for
        self._scaled = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._update_cursor()

    def pixmap(self):
        return self._pixmap

    def message(self):
        return self._message

    def set_image(self, image, full=None):
        """A QImage or QPixmap to show; None for nothing. `full` is the photo's (width,
        height) at full size; without it the last photo's is kept if this one has its
        shape (stepping through a burst), else the image's own. The zoom is kept."""
        if image is not None and not isinstance(image, QPixmap):
            image = QPixmap.fromImage(image)
        self._pixmap = image
        self._message = ""
        self._scaled = None
        if image is None or image.isNull():
            self._full = None
        elif full is not None:
            self._full = (int(full[0]), int(full[1]))
        elif self._full is None or not _same_shape(self._full, (image.width(), image.height())):
            self._full = (image.width(), image.height())
        self._clamp()
        self._update_cursor()
        self.update()

    def set_message(self, text):
        self._pixmap = None
        self._scaled = None
        self._message = text
        self._update_cursor()
        self.update()

    def full_size(self):
        """The photo's (width, height) at full size, or None without one."""
        return self._full

    # Zoom

    def is_zoomed(self):
        return self._zoom is not None

    def zoom(self):
        """Screen pixels a pixel of the photo at full size: 1.0 is 100 %. Fitted, the
        zoom that fits."""
        return self._zoom if self._zoom is not None else self.fit_zoom()

    def fit_zoom(self):
        if self._full is None:
            return 1.0
        area = self._area()
        ratio = self.devicePixelRatioF()
        return min(area.width() * ratio / self._full[0], area.height() * ratio / self._full[1])

    def needs_full(self):
        """Whether the image shown has fewer pixels than the zoom shows: a larger one
        should be fetched."""
        if self._zoom is None or self._pixmap is None or self._full is None:
            return False
        wanted = self._full[0] * min(self._zoom, 1.0)
        # A little short is the same: an edit's full size is known only roughly first
        return self._pixmap.width() < wanted * 0.95

    def set_zoom(self, zoom, anchor=None):
        """Zoom to `zoom` (1.0 is 100 %), or fitted (None, or no more than fits), with
        the point of the photo at `anchor` (a point in the view, the middle without
        one) staying where it is."""
        if not self._zoomable or self._pixmap is None or self._full is None:
            return
        area = self._area()
        anchor = QPointF(anchor) if anchor is not None else area.center()
        rect = self.image_rect()
        fx = _clip((anchor.x() - rect.left()) / rect.width())
        fy = _clip((anchor.y() - rect.top()) / rect.height())
        before = (self._zoom, self._center)
        if zoom is None or zoom <= self.fit_zoom() * 1.001:
            self._zoom, self._center = None, (0.5, 0.5)
        else:
            self._zoom = zoom
            width, height = self._shown_size()
            self._center = (
                fx - (anchor.x() - area.center().x()) / width,
                fy - (anchor.y() - area.center().y()) / height,
            )
            self._clamp()
        if (self._zoom, self._center) != before:
            self._scaled = None
            self._update_cursor()
            self.update()
            self.zoom_changed.emit()

    def toggle_zoom(self, anchor=None):
        """Fitted to 100 % at `anchor`, or back to fitted."""
        self.set_zoom(None if self.is_zoomed() else 1.0, anchor)

    def reset_zoom(self):
        self.set_zoom(None)

    def zoom_in(self, anchor=None):
        current = self.zoom()
        larger = [step for step in ZOOM_STEPS if step > current * 1.001]
        if larger:
            self.set_zoom(larger[0], anchor)

    def zoom_out(self, anchor=None):
        current = self.zoom()
        smaller = [step for step in ZOOM_STEPS if step < current * 0.999]
        self.set_zoom(smaller[-1] if smaller else None, anchor)

    def pan(self, dx, dy):
        """Move the photo by (dx, dy) pixels of the view, while zoomed."""
        if self._zoom is None:
            return
        width, height = self._shown_size()
        before = self._center
        self._center = (self._center[0] - dx / width, self._center[1] - dy / height)
        self._clamp()
        if self._center != before:
            self.update()
            self.zoom_changed.emit()

    def center(self):
        """The point of the photo at the middle of the view, as fractions of its size."""
        return self._center

    def _area(self):
        inset = style.px(self.inset)
        return QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)

    def _shown_size(self):
        """(width, height) of the whole photo in the view at the zoom, in view pixels."""
        ratio = self.devicePixelRatioF()
        zoom = self.zoom()
        return self._full[0] * zoom / ratio, self._full[1] * zoom / ratio

    def _clamp(self):
        """The place within what can be shown: no space beside a photo larger than the
        view, a smaller one in the middle."""
        if self._zoom is None or self._full is None:
            return
        area = self._area()
        width, height = self._shown_size()
        place = []
        for value, shown, room in (
            (self._center[0], width, area.width()),
            (self._center[1], height, area.height()),
        ):
            if shown <= room:
                place.append(0.5)
            else:
                half = room / 2 / shown
                place.append(min(max(value, half), 1 - half))
        self._center = tuple(place)

    # Drawing

    def image_rect(self):
        """Where the whole image is drawn, fitted and centred or zoomed; None without
        one. Zoomed, it is larger than the view."""
        if self._pixmap is None or self._pixmap.isNull():
            return None
        area = self._area()
        if self._zoom is None:
            size = self._pixmap.size().toSizeF()
            size.scale(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(area.topLeft(), size)
            target.moveCenter(area.center())
            return target
        width, height = self._shown_size()
        ratio = self.devicePixelRatioF()
        # On whole screen pixels, so that 100 % is sharp
        left = round((area.center().x() - self._center[0] * width) * ratio) / ratio
        top = round((area.center().y() - self._center[1] * height) * ratio) / ratio
        return QRectF(left, top, width, height)

    def paintEvent(self, _event):
        painter = QPainter(self)
        area = self._area()
        target = self.image_rect()
        if target is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            if self._zoom is None:
                fitted = self._fitted(target)
                painter.drawPixmap(target, fitted, QRectF(fitted.rect()))
            else:
                self._paint_zoomed(painter, target, area)
            self.paint_over(painter, target)
        elif self._message:
            painter.setPen(style.color("textSecondary"))
            painter.drawText(
                area, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message
            )
        painter.end()

    def _fitted(self, target):
        """The image at the fitted size: drawing a full-size photo smaller on every paint
        is slow, so it is made smaller once."""
        ratio = self.devicePixelRatioF()
        size = QSize(round(target.width() * ratio), round(target.height() * ratio))
        if self._pixmap.width() <= size.width() * 1.5:
            return self._pixmap
        if self._scaled is None or self._scaled[0] != size:
            scaled = self._pixmap.scaled(
                size,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._scaled = (size, scaled)
        return self._scaled[1]

    def _paint_zoomed(self, painter, target, area):
        # Only the part in view: the whole would be drawn and clipped on every move
        shown = target.intersected(area)
        if shown.isEmpty():
            return
        sx = self._pixmap.width() / target.width()
        sy = self._pixmap.height() / target.height()
        source = QRectF(
            (shown.left() - target.left()) * sx,
            (shown.top() - target.top()) * sy,
            shown.width() * sx,
            shown.height() * sy,
        )
        painter.drawPixmap(shown, self._pixmap, source)

    def paint_over(self, painter, target):
        """For a subclass: draw over the image, which is at `target`."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scaled = None
        self._clamp()

    # Mouse

    def mousePressEvent(self, event):
        if not self._zoomable or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._press = event.position()
        self._dragged = False

    def mouseMoveEvent(self, event):
        if self._press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        position = event.position()
        moved = position - self._press
        if not self._dragged:
            if moved.manhattanLength() < QApplication.startDragDistance():
                return
            self._dragged = True
            self._update_cursor()
        self.pan(moved.x(), moved.y())
        self._press = position

    def mouseReleaseEvent(self, event):
        if self._press is None or event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if not self._dragged:
            self.toggle_zoom(event.position())
        self._press = None
        self._dragged = False
        self._update_cursor()

    def wheelEvent(self, event):
        if not self._zoomable or self._pixmap is None:
            super().wheelEvent(event)
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y()
            if steps > 0:
                self.zoom_in(event.position())
            elif steps < 0:
                self.zoom_out(event.position())
            event.accept()
            return
        if self._zoom is None:
            event.ignore()
            return
        # A touchpad says how far in pixels; a wheel in steps of 120
        delta = event.pixelDelta().toPointF()
        if delta.isNull():
            delta = event.angleDelta().toPointF() / 120 * style.px(_WHEEL_PAN)
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            delta = QPointF(delta.y(), delta.x())
        self.pan(delta.x(), delta.y())
        event.accept()

    def _update_cursor(self):
        if not self._zoomable or self._pixmap is None:
            self.unsetCursor()
        elif self._dragged:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._zoom is not None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)


def _clip(value):
    return min(max(value, 0.0), 1.0)


def _same_shape(a, b):
    return abs(a[0] / a[1] - b[0] / b[1]) < 0.02
