"""Choosing which parts of an edit to keep: for a preset, or to copy to other photos."""

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
    ("presence", "Texture, clarity, dehaze"),
    ("sharpening", "Sharpening"),
    ("noise", "Noise reduction"),
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
    "presence",
    "sharpening",
    "noise",
    "colour",
    "hsl",
    "grading",
    "curves",
    "matte",
    "vignette",
    "grain",
)


class SectionsDialog(QDialog):
    """Choosing which parts of an edit to keep, with a button to go ahead with: a preset
    (which has a name too) or settings to copy over other photos."""

    def __init__(
        self, title, prompt, action, chosen, remember, named=False, existing=(), parent=None
    ):
        """`chosen`: the sections ticked to begin with (or None for DEFAULT_SECTIONS),
        `remember(sections)` is called with what is ticked when `action` is pressed,
        `existing`: the names of the presets there are, for a named dialog."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self._existing = {name.casefold() for name in existing}
        self._remember = remember
        self._action = action
        layout = QVBoxLayout(self)
        layout.setSpacing(style.px(8))

        self.name_edit = None
        if named:
            layout.addWidget(QLabel("Name"))
            self.name_edit = QLineEdit()
            layout.addWidget(self.name_edit)
            layout.addSpacing(style.px(4))
        layout.addWidget(QLabel(prompt))
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
        if self.name_edit is not None:
            self.name_edit.textChanged.connect(lambda _text: self._update())
            self.name_edit.returnPressed.connect(self.save_button.click)
        self.setMinimumWidth(style.px(380))
        self._update()

    def name(self):
        return self.name_edit.text().strip() if self.name_edit is not None else ""

    def sections(self):
        """The sections chosen, in the order the edit has them."""
        return [name for name in develop.PRESET_SECTIONS if self.boxes[name].isChecked()]

    def set_sections(self, sections):
        for name, box in self.boxes.items():
            box.setChecked(name in sections)

    def _update(self):
        named = self.name_edit is not None
        replacing = named and self.name().casefold() in self._existing
        self.save_button.setText("Replace" if replacing else self._action)
        self.save_button.setEnabled((self.name() != "" or not named) and bool(self.sections()))

    def _accept(self):
        self._remember(self.sections())
        self.accept()


class PresetDialog(SectionsDialog):
    def __init__(self, existing=(), parent=None):
        """`existing`: the names of the presets there are, so that saving over one says so."""
        super().__init__(
            "Save Preset",
            "The preset keeps these parts of the edit:",
            "Save",
            settings.preset_sections(),
            settings.set_preset_sections,
            named=True,
            existing=existing,
            parent=parent,
        )


class CopyDialog(SectionsDialog):
    def __init__(self, title="Copy Settings", prompt=None, action="Copy", parent=None):
        super().__init__(
            title,
            prompt or "Copy these parts of the edit:",
            action,
            settings.copy_sections() or ("exposure", *DEFAULT_SECTIONS),
            settings.set_copy_sections,
            parent=parent,
        )
