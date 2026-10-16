"""Asking how to merge a bracket into one HDR photo, or stitch a panorama."""

from PySide6.QtCore import Qt
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


# Projections, as the panorama names them, in words, and what each is for
PROJECTIONS = (
    ("cylindrical", "Cylindrical", "Verticals stay upright: for a row of photos turning around"),
    ("spherical", "Spherical", "For a very wide view, or several rows of photos"),
    (
        "perspective",
        "Perspective",
        "Straight lines stay straight, the sides stretched: for views under about 120°",
    ),
)


class PanoramaDialog(QDialog):
    def __init__(self, frames, parent=None):
        """`frames`: the photos to stitch (merge.Frame), as taken."""
        super().__init__(parent)
        self.setWindowTitle("Stitch Panorama")
        layout = QVBoxLayout(self)
        layout.setSpacing(style.px(10))
        layout.addWidget(QLabel(f"Stitch these {len(frames)} photos into one panorama:"))
        listed = QLabel("\n".join(frame.name for frame in frames))
        listed.setContentsMargins(style.px(12), 0, 0, 0)
        layout.addWidget(listed)
        form = QFormLayout()
        self.projection_box = QComboBox()
        for index, (name, words, tip) in enumerate(PROJECTIONS):
            self.projection_box.addItem(words, name)
            self.projection_box.setItemData(index, tip, Qt.ItemDataRole.ToolTipRole)
        chosen = self.projection_box.findData(settings.panorama_projection())
        self.projection_box.setCurrentIndex(max(chosen, 0))
        self.projection_box.setToolTip(
            "How the view around is laid flat. " + PROJECTIONS[max(chosen, 0)][2]
        )
        self.projection_box.currentIndexChanged.connect(
            lambda index: self.projection_box.setToolTip(
                "How the view around is laid flat. " + PROJECTIONS[index][2]
            )
        )
        form.addRow("Projection:", self.projection_box)
        self.crop_box = QCheckBox("Crop to a rectangle")
        self.crop_box.setToolTip("Leave out the uneven edges that no photo covers")
        self.crop_box.setChecked(settings.panorama_crop())
        form.addRow("", self.crop_box)
        layout.addLayout(form)
        note = QLabel(
            "The result is saved beside the first photo as a TIFF of linear light, its "
            "exposures made alike. The photos themselves are not changed."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        stitch = buttons.addButton("Stitch", QDialogButtonBox.ButtonRole.AcceptRole)
        stitch.setDefault(True)
        buttons.accepted.connect(self._accepted)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accepted(self):
        settings.set_panorama_choices(self.projection(), self.crop())
        self.accept()

    def projection(self):
        return self.projection_box.currentData()

    def crop(self):
        return self.crop_box.isChecked()
