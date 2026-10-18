"""A labelled slider for an edit value: moves preview, letting go commits. And what keeps
a scroll down a panel of them from moving them on the way."""

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style

# How long after a key or the wheel moved the slider the change is committed
_SETTLE_MS = 400


def pass_wheel(widget, event):
    """Scroll the scroll area `widget` is in with a wheel `event` it does not take."""
    parent = widget.parentWidget()
    while parent is not None and not isinstance(parent, QAbstractScrollArea):
        parent = parent.parentWidget()
    if parent is None:
        # On to the parents
        event.ignore()
    else:
        QApplication.sendEvent(parent.viewport(), event)


class _WheelGuard(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and not watched.hasFocus():
            pass_wheel(watched, event)
            return True
        return False


def wheel_when_focused(widget):
    """Let `widget` (a slider, a combo box) take the wheel only once it has the focus,
    clicked or tabbed to: a wheel over it otherwise scrolls what it is in, and a scroll
    down a panel does not change each control it passes. The wheel no longer gives it
    the focus either."""
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    guard = _WheelGuard(widget)
    widget.installEventFilter(guard)
    return guard


class ScrollStrip(QWidget):
    """A thin strip down the side of a scrolled panel with nothing in it to hit: the
    pointer rests there while the wheel scrolls the panel. Part of the card, with a
    hint of it under the pointer."""

    # Unscaled, before the density
    WIDTH = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setToolTip("Scroll here to leave the controls as they are")
        style.follow_density(self, lambda: self.setFixedWidth(style.gap(self.WIDTH)))

    def event(self, event):
        if event.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverLeave):
            self.update()
        return super().event(event)

    def wheelEvent(self, event):
        pass_wheel(self, event)

    def paintEvent(self, _event):
        if not self.underMouse():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = self.palette().color(QPalette.ColorRole.WindowText)
        colour.setAlphaF(0.06)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        inset = style.gap(4)
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        radius = min(style.radius("small"), rect.width() / 2)
        painter.drawRoundedRect(rect, radius, radius)


class ValueSlider(QWidget):
    # While dragging, and as keys or the wheel move it: the value
    moved = Signal(float)
    # The change is done (let go, settled, or reset by a double click): the value
    committed = Signal(float)

    def __init__(self, title, low, high, default=0.0, step=0.01, decimals=2, unit="", parent=None):
        super().__init__(parent)
        self._step, self._decimals, self._unit = step, decimals, unit
        self.default = default
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        style.follow_density(self, lambda: layout.setSpacing(style.gap(2)))
        row = QHBoxLayout()
        self.title = QLabel(title)
        self.value_label = QLabel()
        self.value_label.setObjectName("secondary")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self.title)
        row.addStretch(1)
        row.addWidget(self.value_label)
        layout.addLayout(row)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(round(low / step), round(high / step))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(max(1, round((high - low) / step / 20)))
        self.slider.setToolTip("Double-click to reset")
        # Around zero: no fill from the left end, which would read as "half on"
        self.slider.setProperty("bipolar", low < 0 < high)
        self.slider.installEventFilter(self)
        self.title.installEventFilter(self)
        wheel_when_focused(self.slider)
        layout.addWidget(self.slider)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(_SETTLE_MS)
        self._settle.timeout.connect(lambda: self.committed.emit(self.value()))
        self.slider.valueChanged.connect(self._on_changed)
        self.slider.sliderReleased.connect(lambda: self.committed.emit(self.value()))
        self.set_value(default)

    def value(self):
        return round(self.slider.value() * self._step, 6)

    def set_value(self, value):
        """Show `value`, signalling nothing."""
        self.slider.blockSignals(True)
        self.slider.setValue(round(value / self._step))
        self.slider.blockSignals(False)
        self._show()

    def _show(self):
        value = self.value()
        sign = "+" if value > 0 and self.slider.minimum() < 0 else ""
        self.value_label.setText(f"{sign}{value:.{self._decimals}f}{self._unit}")

    def _on_changed(self, _raw):
        self._show()
        self.moved.emit(self.value())
        if not self.slider.isSliderDown():
            # A key, the wheel or a click beside the handle: done once it settles
            self._settle.start()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonDblClick:
            self._settle.stop()
            self.set_value(self.default)
            self.moved.emit(self.value())
            self.committed.emit(self.value())
            return True
        return False
