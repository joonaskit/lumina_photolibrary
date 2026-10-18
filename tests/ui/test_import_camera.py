"""Importing from a camera on USB (#72): the service, the dialog and the window."""

import hashlib
import io
from datetime import datetime

import pytest
from conftest import TIMEOUT
from fakes import FakeCamera
from PIL import Image

from lumina.core.dbtypes import DONE
from lumina.core.errors import CameraBusy, NotConnected
from lumina.qt_models import importing as importing_models
from lumina.qt_models.importing import ImportService
from lumina.ui import main_window
from lumina.ui.import_dialog import ImportDialog
from lumina.ui.job_status import describe_import


def jpeg_at(when, colour=(200, 10, 10)):
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = f"{when:%Y:%m:%d %H:%M:%S}"
    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), colour).save(buffer, "JPEG", exif=exif.tobytes())
    return buffer.getvalue()


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "camera"
    path.mkdir()
    FakeCamera.put(path, "IMG_0001.JPG", jpeg_at(datetime(2026, 10, 14, 9, 30, 5)))
    FakeCamera.put(path, "IMG_0002.JPG", jpeg_at(datetime(2026, 10, 14, 9, 31, 5), (10, 200, 10)))
    FakeCamera.put(path, "IMG_0003.JPG", jpeg_at(datetime(2026, 10, 15, 8, 0, 0), (10, 10, 200)))
    return path


@pytest.fixture
def make_importer(root, monkeypatch):
    monkeypatch.setattr(importing_models, "_CAMERA_RETRY", 50)
    made = []
    present = {"camera": True}

    def detect():
        return [("Fake EOS", "usb:001,002")] if present["camera"] else []

    def make(service, start=True):
        importer = ImportService(
            service, camera=lambda: FakeCamera(root), detect=detect, parent=service
        )
        made.append(importer)
        if start:
            importer.start()
        return importer

    make.present = present
    yield make
    for importer in made:
        importer.close()
        for future in (importer.future, importer.read_future):
            if future is not None:
                # A read or an import that ended in an error has been told of it already
                future.exception(timeout=TIMEOUT / 1000)


def snapshot(root):
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "card").rglob("*"))
        if p.is_file()
    }


def read_camera(qtbot, importer):
    got = []
    importer.read_camera(got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    return got[0]


def test_cameras_on_usb_are_found(service, make_importer):
    importer = make_importer(service)

    assert importer.cameras() == [("Fake EOS", "usb:001,002")]
    make_importer.present["camera"] = False
    assert importer.cameras() == []


def test_reading_the_camera_lists_its_photos(service, make_importer, qtbot, root):
    importer = make_importer(service)

    reading = read_camera(qtbot, importer)

    assert [c.path.rpartition("/")[2] for c in reading.candidates] == [
        "IMG_0001.JPG",
        "IMG_0002.JPG",
        "IMG_0003.JPG",
    ]
    assert not any(c.imported for c in reading.candidates)


def test_a_camera_that_is_not_there_is_told_not_connected(service, make_importer, qtbot, root):
    importer = make_importer(service)
    FakeCamera.unplug(root)

    error = read_camera(qtbot, importer)

    assert isinstance(error, NotConnected)


def test_the_dialog_lists_the_camera_beside_the_cards_and_reads_it_when_chosen(
    service, make_importer, qtbot, root, drive
):
    importer = make_importer(service)
    service.add_folder(drive / "Photos")
    dialog = ImportDialog(importer, service.folders())
    qtbot.addWidget(dialog)

    # Not read before it is asked for: it is held while it is read
    assert dialog.source_box.itemData(0) == "camera:usb:001,002"
    assert dialog.reading is None and "choose the camera" in dialog.read_label.text()
    assert FakeCamera.log(root, "reads") == []

    dialog.set_source("camera:usb:001,002")
    qtbot.waitUntil(lambda: dialog.reading is not None, timeout=TIMEOUT)

    assert dialog.read_label.text().startswith("3 photos")
    assert dialog.days.count() == 2
    assert len(dialog.chosen()) == 3
    assert dialog.import_button.isEnabled()


def test_the_dialog_says_when_another_program_has_the_camera(
    service, make_importer, qtbot, root, drive
):
    importer = make_importer(service)
    service.add_folder(drive / "Photos")
    dialog = ImportDialog(importer, service.folders())
    qtbot.addWidget(dialog)
    FakeCamera.make_busy(root)

    dialog.set_source("camera:usb:001,002")
    qtbot.waitUntil(lambda: "Could not read Fake EOS" in dialog.read_label.text(), timeout=TIMEOUT)

    assert "using the camera" in dialog.read_label.text()
    assert not dialog.import_button.isEnabled()


def test_importing_from_the_camera_puts_the_photos_in_the_library(
    service, make_importer, qtbot, root, drive, wait_idle
):
    importer = make_importer(service)
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    before = snapshot(root)
    reading = read_camera(qtbot, importer)
    statuses = []
    importer.job_changed.connect(statuses.append)

    importer.import_photos(reading.candidates, str(drive / "Photos"), "day", "original")
    qtbot.waitUntil(lambda: not importer.is_busy(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: len(service.db.photos()) == 6, timeout=TIMEOUT)
    wait_idle(service)

    assert snapshot(root) == before
    assert statuses[-1].state == DONE
    assert describe_import(statuses[-1]) == "Imported 3 photos into Photos"
    # The folder had three photos of its own
    names = sorted(p.name for p in (drive / "Photos").rglob("IMG_*"))
    assert names == ["IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.JPG"]
    # Read again: nothing is new
    again = read_camera(qtbot, importer)
    assert all(c.imported for c in again.candidates)


def test_an_import_waits_for_the_camera_and_goes_on_when_it_is_plugged_in(
    service, make_importer, qtbot, root, drive, wait_idle
):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    importer = make_importer(service)
    reading = read_camera(qtbot, importer)
    statuses = []
    importer.job_changed.connect(statuses.append)
    FakeCamera.unplug(root)

    importer.import_photos(reading.candidates, str(drive / "Photos"), "none", "original")
    qtbot.waitUntil(lambda: any(s.message for s in statuses), timeout=TIMEOUT)

    waiting = next(s for s in statuses if s.message)
    assert "Waiting for the camera" in describe_import(waiting)
    assert not list((drive / "Photos").glob("IMG_*"))

    # No mount changes when a camera is plugged in: it is looked for now and then
    FakeCamera.unplug(root, False)
    qtbot.waitUntil(lambda: statuses[-1].state == DONE, timeout=TIMEOUT)
    assert len(list((drive / "Photos").glob("IMG_*"))) == 3


def test_the_camera_is_not_read_for_the_dialog_while_an_import_has_it(
    service, make_importer, qtbot, root, drive, monkeypatch
):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    importer = make_importer(service)
    reading = read_camera(qtbot, importer)
    # Hold the camera as a running import does
    assert importer._camera_lock.acquire(blocking=False)
    try:
        error = read_camera(qtbot, importer)
    finally:
        importer._camera_lock.release()

    assert isinstance(error, CameraBusy)
    assert "importing from the camera" in str(error)
    assert reading.candidates


def test_the_window_imports_from_a_camera_with_the_dialog(
    service, make_importer, qtbot, root, drive, monkeypatch, wait_idle
):
    importer = make_importer(service)
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    window = main_window.MainWindow(service, importer=importer)
    qtbot.addWidget(window)

    def run(dialog):
        from PySide6.QtTest import QTest

        dialog.set_source("camera:usb:001,002")
        while dialog.reading is None:
            QTest.qWait(20)
        dialog.set_destination(str(drive / "Photos"))
        dialog._accepted()
        return ImportDialog.DialogCode.Accepted

    monkeypatch.setattr(ImportDialog, "exec", run)

    window.importing.import_photos()
    qtbot.waitUntil(lambda: not importer.is_busy(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: len(service.db.photos()) == 6, timeout=TIMEOUT)

    assert window.import_status.label.text() == "Imported 3 photos into Photos"
