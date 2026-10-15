"""Saving the edit as a preset: its name, and which parts of the edit it keeps."""

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from lumina.integration import style
from lumina.qt_models import develop
from lumina.ui import settings

# In the order of the Develop view, with what each is called there
_LIGHT_AND_COLOUR = (
    ("exposure", "Exposure"),
    ("white_balance", "White balance"),
    ("basic", "Contrast, highlights, shadows, whites, blacks"),
    ("colour", "Vibrance and saturation"),
    ("hsl", "HSL"),
    ("grading", "Colour grading"),
    ("curves", "Tone curve"),
    ("matte", "Matte"),
    ("vignette", "Vignette"),
    ("grain", "Grain"),
)
_GEOMETRY = (
    ("rotate", "Rotation"),
    ("flip", "Mirroring"),
    ("straighten", "Straightening"),
    ("crop", "Crop"),
)
# What a first preset keeps: the look, not the exposure of this photo or its framing
DEFAULT_SECTIONS = (
    "white_balance",
    "basic",
    "colour",
    "hsl",
    "grading",
    "curves",
    "matte",
    "vignette",
    "grain",
)


class PresetDialog(QDialog):
    def __init__(self, existing=(), parent=None):
        """`existing`: the names of the presets there are, so that saving over one says so."""
        super().__init__(parent)
        self.setWindowTitle("Save Preset")
        self._existing = {name.casefold() for name in existing}
        layout = QVBoxLayout(self)
        layout.setSpacing(style.px(8))

        layout.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit()
        layout.addWidget(self.name_edit)

        layout.addSpacing(style.px(4))
        layout.addWidget(QLabel("The preset keeps these parts of the edit:"))
        chosen = settings.preset_sections()
        if chosen is None:
            chosen = DEFAULT_SECTIONS
        self.boxes = {}
        for group in (_LIGHT_AND_COLOUR, _GEOMETRY):
            for section, label in group:
                box = QCheckBox(label)
                box.setChecked(section in chosen)
                box.toggled.connect(lambda _on: self._update())
                self.boxes[section] = box
                layout.addWidget(box)
            if group is _LIGHT_AND_COLOUR:
                layout.addSpacing(style.px(4))

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save
        )
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.name_edit.textChanged.connect(lambda _text: self._update())
        self.name_edit.returnPressed.connect(self.save_button.click)
        self.setMinimumWidth(style.px(380))
        self._update()

    def name(self):
        return self.name_edit.text().strip()

    def sections(self):
        """The sections chosen, in the order the edit has them."""
        return [name for name in develop.PRESET_SECTIONS if self.boxes[name].isChecked()]

    def set_sections(self, sections):
        for name, box in self.boxes.items():
            box.setChecked(name in sections)

    def _update(self):
        replacing = self.name().casefold() in self._existing
        self.save_button.setText("Replace" if replacing else "Save")
        self.save_button.setEnabled(bool(self.name()) and bool(self.sections()))

    def _accept(self):
        settings.set_preset_sections(self.sections())
        self.accept()
