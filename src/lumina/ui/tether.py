"""Tethering a folder (#57): the dialog that starts it, and what the status bar shows
while it is on."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style

NO_PRESET = "None"


class TetherDialog(QDialog):
    """Tether a folder, with a preset for every new photo or none."""

    def __init__(self, name, presets, preset=None, parent=None):
        """`name` is the folder's, `presets` the presets' names, `preset` the one chosen
        last time (None: none)."""
        super().__init__(parent)
        self.setWindowTitle("Tether Folder")
        layout = QVBoxLayout(self)
        text = QLabel(
            f"New photos written into “{name}” open as they arrive, for shooting with a "
            "capture program such as gphoto2 or the camera maker's own."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        form = QFormLayout()
        self.preset = QComboBox()
        self.preset.addItem(NO_PRESET, None)
        for each in presets:
            self.preset.addItem(each, each)
        found = self.preset.findData(preset) if preset is not None else -1
        self.preset.setCurrentIndex(max(found, 0))
        form.addRow("Preset for new photos:", self.preset)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Tether")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        style.follow_density(self, lambda: layout.setSpacing(style.gap(10)))

    def chosen_preset(self):
        return self.preset.currentData()


class TetherStatus(QWidget):
    """The status bar's “Tethered to Studio: 12 new photos”, with Stop; hidden while off."""

    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, style.px(8), 0)
        layout.setSpacing(style.px(8))
        self.label = QLabel()
        self.stop = QToolButton()
        self.stop.setText("Stop")
        self.stop.setToolTip("Stop tethering")
        self.stop.setAutoRaise(True)
        self.stop.clicked.connect(self.stop_requested)
        layout.addWidget(self.label)
        layout.addWidget(self.stop)
        self.hide()

    def show_state(self, name, count, preset):
        """Tethered to the folder `name` (None: not tethered)."""
        if name is None:
            self.hide()
            return
        self.label.setText(describe(name, count, preset))
        self.show()


def describe(name, count, preset):
    photos = "no new photos yet" if count == 0 else f"{count} new photo{'s' * (count != 1)}"
    text = f"Tethered to {name}: {photos}"
    return f"{text}, preset {preset}" if preset else text
