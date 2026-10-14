"""A labelled slider for an edit value: moves preview, letting go commits."""

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget

from lumina.integration import style

# How long after a key or the wheel moved the slider the change is committed
_SETTLE_MS = 400


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
        layout.setSpacing(style.px(2))
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
