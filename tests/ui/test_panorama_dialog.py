"""The dialog that asks how to stitch a panorama, with its preview."""

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QPushButton

from lumina.qt_models.merge import Frame
from lumina.ui.merge_dialog import PanoramaDialog

FRAMES = [Frame(i, f"IMG_000{i}.dng", "") for i in (1, 2, 3)]


class FakePreview(QObject):
    progress = Signal(str)
    picture = Signal(QImage)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.started = []
        self.closed = 0

    def start(self, projection, crop):
        self.started.append((projection, crop))

    def close(self):
        self.closed += 1


def image(width=300, height=100):
    made = QImage(width, height, QImage.Format.Format_RGB888)
    made.fill(QColor(200, 100, 50))
    return made


@pytest.fixture
def preview():
    return FakePreview()


@pytest.fixture
def dialog(qtbot, preview):
    made = PanoramaDialog(FRAMES, preview)
    qtbot.addWidget(made)
    return made


def test_without_a_preview_the_dialog_is_as_it_was(qtbot):
    plain = PanoramaDialog(FRAMES)
    qtbot.addWidget(plain)

    plain.show()
    plain.projection_box.setCurrentIndex(1)

    assert not hasattr(plain, "picture")
    assert plain.projection() == "spherical"


def test_the_preview_starts_when_the_dialog_is_shown_not_before(dialog, preview):
    assert preview.started == []

    dialog.show()

    assert preview.started == [("cylindrical", True)]


def test_a_new_projection_or_crop_asks_for_a_new_preview(dialog, preview):
    dialog.show()

    dialog.projection_box.setCurrentIndex(dialog.projection_box.findData("perspective"))
    dialog.crop_box.setChecked(False)

    assert preview.started == [
        ("cylindrical", True),
        ("perspective", True),
        ("perspective", False),
    ]


def test_it_says_what_it_is_doing_then_shows_the_picture(dialog, preview):
    dialog.show()

    preview.progress.emit("Reading photo 2 of 3…")
    assert dialog.picture.text() == "Reading photo 2 of 3…" and not dialog.picture.has_picture()
    assert dialog.status.text() == "Reading photo 2 of 3…"
    preview.picture.emit(image())

    assert dialog.picture.has_picture()
    pixmap = dialog.picture.pixmap()
    # As large as it fits, in its shape
    assert pixmap.width() / pixmap.height() == pytest.approx(3.0, rel=0.05)
    assert pixmap.width() <= dialog.picture.width()
    assert "preview" in dialog.status.text()


def test_a_newer_picture_stays_while_the_next_is_made(dialog, preview):
    dialog.show()
    preview.picture.emit(image())

    preview.progress.emit("Putting the photos together…")

    assert dialog.picture.has_picture()
    assert dialog.status.text() == "Putting the photos together…"


def test_a_failure_is_told_in_the_picture_place(dialog, preview):
    dialog.show()
    preview.picture.emit(image())

    preview.failed.emit("IMG_0003.dng does not overlap the other photos")

    assert not dialog.picture.has_picture()
    assert dialog.picture.text() == "IMG_0003.dng does not overlap the other photos"
    # The photos can still be tried
    stitch = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Stitch")
    assert stitch.isEnabled()


def test_the_preview_is_closed_with_the_dialog(dialog, preview):
    dialog.show()

    dialog.reject()

    assert preview.closed == 1
