"""Live view of the tethered camera (#59): its frames, fitted, with guides to frame by
(thirds, a grid, the preset's crop, the last shot faintly over the frame), its histogram
and clipping warning, and the camera's magnification to check focus.

A click puts the camera's point there: what it magnifies, and where it focuses with
Focus on Click. Magnified, dragging moves what is magnified, as do the arrow keys.
"""

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style
from lumina.qt_models import tether

# The guides over the frame, by name and in words
GUIDES = (("none", "No guide"), ("thirds", "Thirds"), ("grid", "Grid"), ("crop", "Preset's crop"))
# The grid's lines across the frame's shorter side
_GRID = 6
# The share of pixels clipped before the histogram's corner lights up
_CLIPPED = 0.002
# How far an arrow key moves what is magnified, as a share of what is shown
_STEP = 0.25
# A press that moves less than this many pixels is a click, not a drag
_CLICK = 4
# The histogram's size, unscaled
_HISTOGRAM = (200, 90)


class Histogram(QWidget):
    """A frame's histogram: red, green and blue, and a corner lit for each end that is
    clipped."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.look = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        style.follow_density(
            self, lambda: self.setFixedSize(style.px(_HISTOGRAM[0]), style.px(_HISTOGRAM[1]))
        )

    def set_look(self, look):
        self.look = look
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect())
        fill = QColor(style.color("background"))
        fill.setAlphaF(0.75)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(area, style.radius("small"), style.radius("small"))
        if self.look is None:
            painter.end()
            return
        inner = area.adjusted(style.px(6), style.px(6), -style.px(6), -style.px(6))
        channels = (
            (self.look.red, style.palette_color("red")),
            (self.look.green, style.palette_color("green")),
            (self.look.blue, style.palette_color("blue")),
        )
        # One scale for all three: a square root keeps the shape visible beside a spike at
        # black or white, which is left out of the scale
        top = max(max(counts[1:-1]) for counts, _colour in channels) ** 0.5 or 1.0
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for counts, colour in channels:
            path = QPainterPath(QPointF(inner.left(), inner.bottom()))
            for index, count in enumerate(counts):
                height = min((count**0.5) / top, 1.0)
                path.lineTo(
                    inner.left() + index / 255 * inner.width(),
                    inner.bottom() - height * inner.height(),
                )
            path.lineTo(inner.right(), inner.bottom())
            path.closeSubpath()
            colour = QColor(colour)
            colour.setAlphaF(0.6)
            painter.setBrush(colour)
            painter.drawPath(path)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        size = style.px(8)
        for clipped, x in (
            (self.look.clipped_shadows, inner.left()),
            (self.look.clipped_highlights, inner.right() - size),
        ):
            lit = clipped >= _CLIPPED
            colour = QColor(style.color("textPrimary"))
            colour.setAlphaF(1.0 if lit else 0.25)
            painter.setBrush(colour)
            painter.drawRect(QRectF(x, inner.top(), size, size))
        painter.end()


class LiveCanvas(QWidget):
    """The frame, fitted, and what is over it."""

    # Where the point goes, as shares of the camera's whole frame
    point_chosen = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame = None
        self.message = "Live view is off"
        self.guide = "none"
        self.crop = None
        self.last_shot = None
        self.opacity = 0.0
        self.clipping = False
        # Whether the camera's point can be moved, and so is shown
        self.show_point = False
        self._pressed = None
        self._dragged = QPointF()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(style.px(160), style.px(120))

    def set_frame(self, frame):
        self.frame = frame
        self.update()

    def set_message(self, text):
        self.message = text
        self.update()

    @property
    def zoom(self):
        return self.frame.zoom if self.frame is not None else 1

    def shown(self):
        """The part of the camera's whole frame shown: (left, top, right, bottom)."""
        if self.frame is None:
            return (0.0, 0.0, 1.0, 1.0)
        return tether.magnified(self.frame.point, self.frame.zoom)

    def image_rect(self):
        """Where the frame is drawn: fitted, centred."""
        if self.frame is None:
            return QRectF()
        size = self.frame.image.size()
        area = QRectF(self.rect())
        scale = min(area.width() / size.width(), area.height() / size.height())
        width, height = size.width() * scale, size.height() * scale
        return QRectF(
            area.left() + (area.width() - width) / 2,
            area.top() + (area.height() - height) / 2,
            width,
            height,
        )

    def to_frame(self, position):
        """The point of the camera's whole frame under `position` (in the widget), or None
        off the frame."""
        target = self.image_rect()
        if target.isEmpty() or not target.contains(position):
            return None
        left, top, right, bottom = self.shown()
        x = (position.x() - target.left()) / target.width()
        y = (position.y() - target.top()) / target.height()
        return (left + x * (right - left), top + y * (bottom - top))

    def _whole_to_widget(self, target, rect):
        """`rect` (shares of the whole frame) on the widget, as magnified."""
        left, top, right, bottom = self.shown()
        width, height = right - left, bottom - top
        return QRectF(
            target.left() + (rect[0] - left) / width * target.width(),
            target.top() + (rect[1] - top) / height * target.height(),
            (rect[2] - rect[0]) / width * target.width(),
            (rect[3] - rect[1]) / height * target.height(),
        )

    # Painting

    def paintEvent(self, _event):
        painter = QPainter(self)
        if self.frame is None:
            painter.setPen(style.color("textSecondary"))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message
            )
            painter.end()
            return
        target = self.image_rect().translated(self._dragged)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self.frame.image)
        if self.last_shot is not None and self.opacity > 0:
            painter.setOpacity(self.opacity)
            painter.setClipRect(target)
            painter.drawImage(self._whole_to_widget(target, (0, 0, 1, 1)), self.last_shot)
            painter.setClipping(False)
            painter.setOpacity(1.0)
        if self.clipping and self.frame.clipping is not None:
            painter.drawImage(target, self.frame.clipping)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.guide == "crop" and self.crop is not None:
            self._paint_crop(painter, target)
        elif self.zoom == 1 and self.guide in ("thirds", "grid"):
            self._paint_lines(painter, target)
        if self.zoom > 1:
            self._paint_navigator(painter, target)
        elif self.show_point:
            self._paint_point(painter, target)
        painter.end()

    def _line(self, alpha=1.0):
        line = QColor(style.color("textPrimary"))
        line.setAlphaF(alpha)
        return line

    def _paint_lines(self, painter, target):
        halo = QColor(style.color("background"))
        halo.setAlphaF(0.35)
        if self.guide == "thirds":
            xs = [target.left() + share * target.width() for share in (1 / 3, 2 / 3)]
            ys = [target.top() + share * target.height() for share in (1 / 3, 2 / 3)]
        else:
            step = min(target.width(), target.height()) / _GRID
            xs = [target.center().x() + i * step for i in range(-_GRID * 2, _GRID * 2 + 1)]
            ys = [target.center().y() + i * step for i in range(-_GRID * 2, _GRID * 2 + 1)]
            xs = [x for x in xs if target.left() < x < target.right()]
            ys = [y for y in ys if target.top() < y < target.bottom()]
        for pen in (QPen(halo, 3), QPen(self._line(0.6), 1)):
            painter.setPen(pen)
            for x in xs:
                painter.drawLine(QPointF(x, target.top()), QPointF(x, target.bottom()))
            for y in ys:
                painter.drawLine(QPointF(target.left(), y), QPointF(target.right(), y))

    def _paint_crop(self, painter, target):
        frame = self._whole_to_widget(target, self.crop)
        # What the preset cuts away is dimmed
        shade = QColor(style.color("background"))
        shade.setAlphaF(0.55)
        outside = QPainterPath()
        outside.addRect(target)
        inside = QPainterPath()
        inside.addRect(frame)
        painter.fillPath(outside.subtracted(inside), shade)
        painter.setPen(QPen(self._line(), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(frame.intersected(target))

    def _paint_point(self, painter, target):
        # The camera's point: where it magnifies and focuses
        x, y = self.frame.point
        centre = QPointF(target.left() + x * target.width(), target.top() + y * target.height())
        half = style.px(14)
        box = QRectF(centre.x() - half, centre.y() - half, 2 * half, 2 * half)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(style.color("background"), 3))
        painter.drawRect(box)
        painter.setPen(QPen(self._line(), 1))
        painter.drawRect(box)

    def _paint_navigator(self, painter, target):
        # The whole frame, small, at the corner, and what of it is magnified
        width = style.px(96)
        height = width * target.height() / max(target.width(), 1)
        margin = style.px(10)
        whole = QRectF(target.left() + margin, target.bottom() - margin - height, width, height)
        fill = QColor(style.color("background"))
        fill.setAlphaF(0.6)
        painter.setPen(QPen(self._line(0.8), 1))
        painter.setBrush(fill)
        painter.drawRect(whole)
        left, top, right, bottom = self.shown()
        painter.setBrush(self._line(0.5))
        painter.drawRect(
            QRectF(
                whole.left() + left * whole.width(),
                whole.top() + top * whole.height(),
                (right - left) * whole.width(),
                (bottom - top) * whole.height(),
            )
        )
        painter.setPen(self._line())
        text = f"{self.zoom}×"
        painter.drawText(
            QRectF(whole.right() + margin / 2, whole.top(), width, whole.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            text,
        )

    # The mouse and the keys

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.frame is not None:
            self._pressed = event.position()
            self._dragged = QPointF()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed is not None and self.zoom > 1:
            self._dragged = event.position() - self._pressed
            self.update()

    def mouseReleaseEvent(self, event):
        if self._pressed is None or event.button() != Qt.MouseButton.LeftButton:
            return
        pressed, self._pressed = self._pressed, None
        moved = event.position() - pressed
        self._dragged = QPointF()
        self.update()
        if abs(moved.x()) + abs(moved.y()) < style.px(_CLICK):
            point = self.to_frame(pressed)
            if point is not None:
                self.point_chosen.emit(*point)
        elif self.zoom > 1:
            # Dragged: what is under the mouse moves with it
            target = self.image_rect()
            left, top, right, bottom = self.shown()
            x = (left + right) / 2 - moved.x() / target.width() * (right - left)
            y = (top + bottom) / 2 - moved.y() / target.height() * (bottom - top)
            self.point_chosen.emit(x, y)

    def keyPressEvent(self, event):
        steps = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }
        if self.zoom > 1 and event.key() in steps:
            dx, dy = steps[event.key()]
            left, top, right, bottom = self.shown()
            self.point_chosen.emit(
                (left + right) / 2 + dx * _STEP * (right - left),
                (top + bottom) / 2 + dy * _STEP * (bottom - top),
            )
            return
        super().keyPressEvent(event)


class LivePanel(QWidget):
    """The live view page: the frame, and a row of what to show over it and how much to
    magnify it."""

    live_toggled = Signal(bool)
    zoom_chosen = Signal(int)
    # Where the point goes (shares of the whole frame), and whether to focus there
    point_chosen = Signal(float, float, bool)
    clipping_toggled = Signal(bool)
    # The last shot over the frame was turned on: it is wanted
    last_shot_wanted = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        column.addLayout(row)
        self.start = QPushButton("Start Live View")
        self.start.setCheckable(True)
        self.start.toggled.connect(self._on_start)
        row.addWidget(self.start)
        self.zooms = {}
        for times in (1, 5, 10):
            button = QToolButton()
            button.setText("Fit" if times == 1 else f"{times}×")
            button.setToolTip(
                "The whole frame" if times == 1 else f"Magnified {times} times, to check focus"
            )
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.clicked.connect(lambda _on, times=times: self.zoom_chosen.emit(times))
            row.addWidget(button)
            self.zooms[times] = button
        self.focus = QCheckBox("Focus on click")
        self.focus.setToolTip("A click on the frame focuses there")
        row.addWidget(self.focus)
        row.addStretch(1)
        self.guide = QComboBox()
        for key, words in GUIDES:
            self.guide.addItem(words, key)
        self.guide.currentIndexChanged.connect(self._on_guide)
        row.addWidget(self.guide)
        self.last = QCheckBox("Last shot")
        self.last.setToolTip(
            "The last photo of the session faintly over the frame, to line up a shot again"
        )
        self.last.toggled.connect(self._on_last)
        row.addWidget(self.last)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(10, 90)
        self.opacity.setValue(40)
        self.opacity.setToolTip("How much of the last shot shows")
        self.opacity.valueChanged.connect(self._on_last)
        row.addWidget(self.opacity)
        self.histogram_box = QCheckBox("Histogram")
        self.histogram_box.setChecked(True)
        self.histogram_box.toggled.connect(
            lambda on: self.histogram.setVisible(on and self.canvas.frame is not None)
        )
        row.addWidget(self.histogram_box)
        self.clipping = QCheckBox("Clipping")
        self.clipping.setToolTip("Red where a colour is at its brightest, blue where it is black")
        self.clipping.toggled.connect(self._on_clipping)
        row.addWidget(self.clipping)
        self.canvas = LiveCanvas()
        self.canvas.point_chosen.connect(
            lambda x, y: self.point_chosen.emit(
                x, y, self.focus.isChecked() and self.canvas.zoom == 1
            )
        )
        column.addWidget(self.canvas, 1)
        self.histogram = Histogram(self.canvas)
        self.histogram.hide()
        self.state = QLabel()
        self.state.setObjectName("secondary")
        column.addWidget(self.state)
        self.opacity.setEnabled(False)

        def space():
            row.setSpacing(style.gap(6))
            column.setSpacing(style.gap(6))
            self.opacity.setFixedWidth(style.px(90))

        style.follow_density(self, space)

    # Shown

    def show_live(self, live, connected):
        """How live view stands: `live` the service's LiveState, `connected` whether
        there is a camera to show."""
        self.start.blockSignals(True)
        self.start.setChecked(live.on)
        self.start.blockSignals(False)
        self.start.setText("Stop Live View" if live.on else "Start Live View")
        self.start.setEnabled(connected)
        for times, button in self.zooms.items():
            button.setVisible(times in live.view.zooms and len(live.view.zooms) > 1)
            button.setEnabled(live.on)
            if times == live.zoom:
                button.setChecked(True)
        self.focus.setVisible(live.view.focus)
        self.canvas.show_point = live.view.point
        if not live.on:
            self.canvas.set_frame(None)
            self.histogram.hide()
            self.canvas.set_message(live.why or ("Live view is off" if connected else "No camera"))

    def show_frame(self, frame):
        if frame is None:
            return
        self.canvas.set_frame(frame)
        self.histogram.set_look(frame.look)
        self.histogram.setVisible(self.histogram_box.isChecked())
        self._place_histogram()

    def set_message(self, text):
        self.state.setText(text or "")

    def set_crop(self, crop):
        """The preset's crop on the frame (shares), or None."""
        self.canvas.crop = crop
        self.canvas.update()
        self.guide.model().item(self.guide.findData("crop")).setEnabled(crop is not None)

    def set_last_shot(self, image):
        self.canvas.last_shot = image
        self.canvas.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_histogram()

    def _place_histogram(self):
        # At the frame's top right corner
        margin = style.px(10)
        target = self.canvas.image_rect()
        if target.isEmpty():
            target = QRectF(self.canvas.rect())
        self.histogram.move(
            round(target.right() - self.histogram.width() - margin), round(target.top() + margin)
        )

    def _on_start(self, on):
        self.live_toggled.emit(on)

    def _on_guide(self):
        self.canvas.guide = self.guide.currentData()
        self.canvas.update()

    def _on_last(self):
        on = self.last.isChecked()
        self.opacity.setEnabled(on)
        self.canvas.opacity = self.opacity.value() / 100 if on else 0.0
        self.canvas.update()
        if on and self.canvas.last_shot is None:
            self.last_shot_wanted.emit()

    def _on_clipping(self, on):
        self.canvas.clipping = on
        self.canvas.update()
        self.clipping_toggled.emit(on)
