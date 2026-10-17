"""Shooting with a camera on USB (#58): the dialog that starts a session, and the bar
above the photos while one runs, with the camera's settings, Capture and End Session."""

import os
from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
)

from lumina.integration import style
from lumina.qt_models import tether
from lumina.ui.import_dialog import LAYOUTS
from lumina.ui.tether import NO_PRESET

# The most letters of a setting's value a box is wide for ("Continuous high speed" is
# cut short; its list has it whole)
_MOST_LETTERS = 12
# Settings to a row
_PER_ROW = 3
# What a setting is called in the bar, by libgphoto2's name
_NAMES = {
    "iso": "ISO",
    "shutterspeed": "Shutter",
    "aperture": "Aperture",
    "whitebalance": "White balance",
    "imageformat": "Quality",
    "drivemode": "Drive",
}


class CameraDialog(QDialog):
    """Where a session's photos go, how the camera keeps them, and their preset."""

    def __init__(self, folders, presets, choices, parent=None):
        """`folders` are the library's connected folders (FolderInfo), `presets` the
        presets' names, `choices` what was chosen last time (ui/settings.camera_choices)."""
        super().__init__(parent)
        self.setWindowTitle("Tether Camera")
        self._folders = list(folders)
        layout = QVBoxLayout(self)
        text = QLabel(
            "Shoot with a camera connected by USB: each photo is copied into the library "
            "as it is taken and opens here. Close other programs that use the camera first."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        form = QFormLayout()
        self.folder_box = QComboBox()
        for folder in self._folders:
            self.folder_box.addItem(folder.name, folder.id)
        found = self.folder_box.findData(choices.get("folder"))
        self.folder_box.setCurrentIndex(max(found, 0))
        form.addRow("Into:", self.folder_box)
        self.layout_box = QComboBox()
        for key, words in LAYOUTS:
            self.layout_box.addItem(words, key)
        self.layout_box.setCurrentIndex(max(self.layout_box.findData(choices.get("layout")), 0))
        form.addRow("Folders:", self.layout_box)
        self.name_field = QLineEdit(choices.get("name") or "")
        self.name_field.setPlaceholderText(f"Tethered {datetime.now():%Y-%m-%d}")
        form.addRow("Session:", self.name_field)
        self.example = QLabel()
        self.example.setObjectName("secondary")
        self.example.setWordWrap(True)
        form.addRow("", self.example)
        self.card = QRadioButton("On the camera's card and on the computer")
        self.computer = QRadioButton("On the computer only (faster)")
        (self.computer if choices.get("keep") == tether.COMPUTER else self.card).setChecked(True)
        form.addRow("Keep photos:", self.card)
        form.addRow("", self.computer)
        self.warning = QLabel(
            "A photo the camera has not sent yet is lost if the camera is unplugged or "
            "Lumina stops: it is never written to the card."
        )
        self.warning.setWordWrap(True)
        form.addRow("", self.warning)
        self.preset = QComboBox()
        self.preset.addItem(NO_PRESET, None)
        for each in presets:
            self.preset.addItem(each, each)
        self.preset.setCurrentIndex(max(self.preset.findData(choices.get("preset")), 0))
        form.addRow("Preset:", self.preset)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for signal in (
            self.folder_box.currentIndexChanged,
            self.layout_box.currentIndexChanged,
            self.name_field.textChanged,
            self.computer.toggled,
        ):
            signal.connect(self._update)
        style.follow_density(self, lambda: layout.setSpacing(style.gap(10)))
        self._update()

    def chosen(self):
        """{"folder", "layout", "name", "keep", "preset"}, as camera_choices keeps them."""
        return {
            "folder": self.folder_box.currentData(),
            "layout": self.layout_box.currentData(),
            "name": self.name_field.text().strip(),
            "keep": tether.COMPUTER if self.computer.isChecked() else tether.CARD,
            "preset": self.preset.currentData(),
        }

    def _update(self):
        folder = next((f for f in self._folders if f.id == self.folder_box.currentData()), None)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(folder is not None)
        self.warning.setVisible(self.computer.isChecked())
        if folder is None:
            self.example.setText("Add a library folder to shoot into first.")
            return
        name = self.name_field.text().strip() or self.name_field.placeholderText()
        below = tether.subfolder(datetime.now(), self.layout_box.currentData())
        self.example.setText(os.path.join(folder.path, below, name, ""))


class CameraBar(QFrame):
    """The session's camera: what it is and how it stands, its settings, Capture."""

    capture_requested = Signal(bool)
    # Focus without taking a picture
    focus_requested = Signal()
    # A setting changed here: its name and value
    setting_chosen = Signal(str, str)
    fetch_again_requested = Signal()
    end_requested = Signal()
    # Live view shown, or not
    live_requested = Signal(bool)

    def __init__(self, parent=None):
        """As narrow as it can be: the window's width is at least the bar's, and a bar
        wider than the screen keeps the window from being maximized. The camera and its
        state on top; the settings below, three to a row, with Capture beside them."""
        super().__init__(parent)
        self.setObjectName("group")
        self._column = QVBoxLayout(self)
        self._top = QHBoxLayout()
        self._below = QHBoxLayout()
        self._settings = QGridLayout()
        self._shoot = QVBoxLayout()
        self._column.addLayout(self._top)
        self._column.addLayout(self._below)
        self._below.addLayout(self._settings)
        self._below.addStretch(1)
        self._below.addLayout(self._shoot)
        self.camera = QLabel()
        self.camera.setObjectName("title")
        # Wrapped, so a long reason (another program has the camera) adds height, not width
        self.state = QLabel()
        self.state.setObjectName("secondary")
        self.state.setWordWrap(True)
        self.state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._top.addWidget(self.camera)
        self._top.addWidget(self.state, 1)
        self.boxes = {}
        for number, name in enumerate(tether.SETTINGS):
            label = QLabel(_NAMES[name])
            label.setObjectName("secondary")
            box = QComboBox()
            # As wide as the value chosen, not the longest choice; the list opens wider
            box.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            box.activated.connect(lambda _i, name=name, box=box: self._chosen(name, box))
            row, column = divmod(number, _PER_ROW)
            self._settings.addWidget(label, row, 2 * column)
            self._settings.addWidget(box, row, 2 * column + 1)
            self.boxes[name] = (label, box)
        self.fetch_again = QPushButton("Fetch Again")
        self.fetch_again.setToolTip("Fetch the photos that were not saved from the card again")
        self.fetch_again.clicked.connect(self.fetch_again_requested)
        self.focus = QCheckBox("Focus first")
        self.focus.setChecked(True)
        self.capture = QPushButton("Capture")
        self.capture.setToolTip("Take a photo (Space)")
        self.capture.clicked.connect(lambda: self.capture_requested.emit(self.focus.isChecked()))
        self.autofocus = QPushButton("Focus")
        self.autofocus.setToolTip("Focus without taking a photo; in live view, at its point")
        self.autofocus.clicked.connect(self.focus_requested)
        self._buttons = QHBoxLayout()
        self._buttons.addWidget(self.autofocus)
        self._buttons.addWidget(self.capture)
        self.end = QPushButton("End Session")
        self.end.clicked.connect(self.end_requested)
        self.live = QPushButton("Live View")
        self.live.setCheckable(True)
        self.live.setToolTip("The camera's live view, to frame and focus by")
        self.live.toggled.connect(self.live_requested)
        self._top.addWidget(self.live)
        self._top.addWidget(self.fetch_again)
        self._top.addWidget(self.end)
        self._shoot.addWidget(self.focus)
        self._shoot.addLayout(self._buttons)
        style.follow_density(self, self._fit)
        self.hide()

    def show_service(self, service):
        """Show how the CameraService `service` stands; hidden while no session runs."""
        if not service.running:
            self.hide()
            return
        info = service.info
        self.camera.setText(info.model if info is not None else "Camera")
        self.state.setText(describe(service))
        self.state.setToolTip(failures_text(service.failures))
        failed = bool(service.failures) and service.connected
        self.state.setStyleSheet(
            f"color: {style.color('danger').name()};" if service.failures else ""
        )
        for name, (label, box) in self.boxes.items():
            setting = service.settings.get(name) if service.connected else None
            label.setVisible(setting is not None)
            box.setVisible(setting is not None)
            if setting is None:
                continue
            box.blockSignals(True)
            box.clear()
            box.addItems(list(setting.choices) or [setting.value])
            box.setCurrentText(setting.value)
            box.setMinimumContentsLength(min(len(setting.value), _MOST_LETTERS) + 1)
            box.view().setMinimumWidth(box.view().sizeHintForColumn(0) + style.px(24))
            box.setEnabled(not setting.readonly)
            box.blockSignals(False)
        self.fetch_again.setVisible(failed)
        self.capture.setEnabled(service.connected and not service.ending)
        self.show_focus(service)
        self.focus.setEnabled(service.connected and not service.ending)
        self.end.setEnabled(not service.ending)
        self.live.setEnabled(service.connected and not service.ending or self.live.isChecked())
        self.show()

    def show_focus(self, service):
        """Focus, for a camera that can be told to focus (which its live view's state
        says)."""
        self.autofocus.setVisible(not service.connected or service.live.view.focus)
        self.autofocus.setEnabled(service.can_focus and not service.ending)

    def set_live_shown(self, shown):
        """Whether live view is shown: the Live View button is down."""
        self.live.blockSignals(True)
        self.live.setChecked(shown)
        self.live.blockSignals(False)

    def _chosen(self, name, box):
        self.setting_chosen.emit(name, box.currentText())

    def _fit(self):
        self._column.setContentsMargins(style.gap(8), style.gap(4), style.gap(8), style.gap(4))
        self._column.setSpacing(style.gap(4))
        self._top.setSpacing(style.gap(6))
        self._below.setSpacing(style.gap(12))
        self._settings.setHorizontalSpacing(style.gap(6))
        self._settings.setVerticalSpacing(style.gap(4))
        self._shoot.setSpacing(style.gap(4))
        self._buttons.setSpacing(style.gap(6))


def describe(service):
    """The camera's state in words: why it can't shoot, or the battery, shots left and
    what this session has saved."""
    if service.message is not None:
        return service.message
    parts = []
    battery = service.settings.get("batterylevel")
    if battery is not None and battery.value:
        parts.append(f"battery {battery.value}")
    shots = service.settings.get("availableshots")
    if shots is not None and shots.value:
        parts.append(f"{_thousands(shots.value)} shots left")
    count = service.saved_count
    parts.append(f"{count} photo{'s' * (count != 1)} this session")
    if service.fetching:
        parts.append(f"fetching {next(iter(service.fetching.values()))}")
    if service.failures:
        n = len(service.failures)
        parts.append(f"{n} not saved")
    return ", ".join(parts)


def failures_text(failures):
    return "\n".join(f"{failure.name}: {failure.why}" for failure in failures.values())


def _thousands(text):
    try:
        return f"{int(text):,}".replace(",", " ")
    except ValueError:
        return text
