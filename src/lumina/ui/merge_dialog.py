"""Asking how to merge a bracket into one HDR photo."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from lumina.integration import style
from lumina.ui import settings

# Deghosting choices, as the merge names them, and in words
DEGHOSTING = (("off", "Off"), ("low", "Low"), ("medium", "Medium"), ("high", "High"))


class MergeDialog(QDialog):
    def __init__(self, frames, parent=None):
        """`frames`: the photos to merge (merge.Frame), darkest first."""
        super().__init__(parent)
        self.setWindowTitle("Merge to HDR")
        layout = QVBoxLayout(self)
        layout.setSpacing(style.px(10))
        layout.addWidget(QLabel(f"Merge these {len(frames)} photos into one HDR photo:"))
        listed = QLabel(
            "\n".join(
                f"{frame.name}   {frame.exposure}" if frame.exposure else frame.name
                for frame in frames
            )
        )
        listed.setContentsMargins(style.px(12), 0, 0, 0)
        layout.addWidget(listed)
        form = QFormLayout()
        self.deghost_box = QComboBox()
        for name, words in DEGHOSTING:
            self.deghost_box.addItem(words, name)
        self.deghost_box.setToolTip(
            "Leave out of each place the photos where something moved there: "
            "people, leaves, water. Higher is stricter"
        )
        chosen = self.deghost_box.findData(settings.merge_deghost())
        self.deghost_box.setCurrentIndex(max(chosen, 0))
        form.addRow("Deghosting:", self.deghost_box)
        self.align_box = QCheckBox("Align the photos")
        self.align_box.setToolTip("For photos taken by hand: the camera moved a little")
        self.align_box.setChecked(settings.merge_align())
        form.addRow("", self.align_box)
        layout.addLayout(form)
        note = QLabel(
            "The result is saved beside the first photo as a TIFF of linear light, which "
            "keeps the brightest parts to bring back in the Develop view. The photos "
            "themselves are not changed."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        merge = buttons.addButton("Merge", QDialogButtonBox.ButtonRole.AcceptRole)
        merge.setDefault(True)
        buttons.accepted.connect(self._accepted)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accepted(self):
        settings.set_merge_choices(self.deghost(), self.align())
        self.accept()

    def deghost(self):
        return self.deghost_box.currentData()

    def align(self):
        return self.align_box.isChecked()
