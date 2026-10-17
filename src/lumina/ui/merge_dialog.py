"""Asking how to merge a bracket into one HDR photo, or stitch a panorama."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QLabel,
    QSizePolicy,
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


class _Picture(QLabel):
    """A picture shown as large as it fits, or a message in its place."""

    def __init__(self):
        super().__init__()
        self._pixmap = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(style.px(480), style.px(200))

    def show_picture(self, image):
        self._pixmap = QPixmap.fromImage(image)
        self._fit()

    def show_message(self, text):
        self._pixmap = None
        self.setPixmap(QPixmap())
        self.setText(text)

    def has_picture(self):
        return self._pixmap is not None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        if self._pixmap is not None:
            inside = self.contentsRect().size()
            self.setPixmap(
                self._pixmap.scaled(
                    inside,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )


class PanoramaDialog(QDialog):
    def __init__(self, frames, preview=None, parent=None):
        """`frames`: the photos to stitch (merge.Frame), as taken. `preview`: a
        PanoramaPreview of them, started when the dialog is shown and closed when it is
        done, or None for none."""
        super().__init__(parent)
        self.setWindowTitle("Stitch Panorama")
        self._preview = preview
        self._shown = False
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
        if preview is not None:
            self.picture = _Picture()
            self.status = QLabel()
            self.status.setWordWrap(True)
            layout.addWidget(self.picture, 1)
            layout.addWidget(self.status)
            self.projection_box.currentIndexChanged.connect(self._changed)
            self.crop_box.toggled.connect(self._changed)
            preview.progress.connect(self._progress)
            preview.picture.connect(self._picture)
            preview.failed.connect(self._failed)
            self.finished.connect(lambda _result: preview.close())
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

    def showEvent(self, event):
        super().showEvent(event)
        if self._preview is not None and not self._shown:
            self._shown = True
            self._preview.start(self.projection(), self.crop())

    def _changed(self, *_args):
        if self._shown:
            self._preview.start(self.projection(), self.crop())

    def _progress(self, text):
        self.status.setText(text)
        if not self.picture.has_picture():
            self.picture.show_message(text)

    def _picture(self, image):
        self.picture.show_picture(image)
        self.status.setText(
            "A preview from small copies of the photos. The result is larger and finer."
        )

    def _failed(self, text):
        self.picture.show_message(text)
        self.status.setText("The photos cannot be stitched as they are.")

    def _accepted(self):
        settings.set_panorama_choices(self.projection(), self.crop())
        self.accept()

    def projection(self):
        return self.projection_box.currentData()

    def crop(self):
        return self.crop_box.isChecked()
