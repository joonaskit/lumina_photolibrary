"""Asking for an aspect ratio of one's own for the crop."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from lumina.integration import style


class RatioDialog(QDialog):
    def __init__(self, width=3, height=2, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom Aspect Ratio")
        layout = QVBoxLayout(self)
        layout.setSpacing(style.px(10))
        layout.addWidget(QLabel("Width and height of the frame, in any unit:"))
        row = QHBoxLayout()
        self.width_box = self._box(width)
        self.height_box = self._box(height)
        row.addWidget(self.width_box)
        row.addWidget(QLabel(":"))
        row.addWidget(self.height_box)
        layout.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _box(value):
        box = QSpinBox()
        box.setRange(1, 9999)
        box.setValue(value)
        return box

    def ratio(self):
        """(width, height) as entered."""
        return self.width_box.value(), self.height_box.value()
