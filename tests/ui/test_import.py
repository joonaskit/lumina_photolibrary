"""Importing from a memory card: the service, the dialog and the window."""

import hashlib
import os

import pytest
from conftest import DRIVE, TIMEOUT
from fakes import FakeExifTool
from photos import make_jpeg
from rawfiles import make_dng

from lumina.core import volumes
from lumina.qt_models import library
from lumina.qt_models.importing import ImportService, ImportStatus
from lumina.ui import main_window, settings
from lumina.ui.import_dialog import ImportDialog
from lumina.ui.job_status import describe_import

CARD = "uuid:CARD"


@pytest.fixture
def card(tmp_path):
    root = tmp_path / "EOS_DIGITAL"
    shots = root / "DCIM" / "100CANON"
    shots.mkdir(parents=True)
    make_jpeg(shots / "IMG_0001.JPG", colour=(200, 10, 10))
    make_jpeg(shots / "IMG_0002.JPG", colour=(10, 200, 10))
    make_dng(shots / "IMG_0003.DNG", preview="jpeg")
    return root


class Mounts:
    """The system's mounts, the drive of conftest, and a card while it is plugged in."""

    def __init__(self, drive, card):
        self.drive, self.card = drive, card
        self.plugged = True
        self.card_in = True

    def __call__(self):
        found = volumes.read_mounts()
        if self.plugged:
            found.append(
                volumes.Mount(DRIVE, str(self.drive), "/", "exfat", "/dev/sdz1", "Photos drive")
            )
        if self.card_in:
            found.append(
                volumes.Mount(CARD, str(self.card), "/", "vfat", "/dev/mmcblk0p1", "EOS_DIGITAL")
            )
        return found


@pytest.fixture
def mounts(drive, card):
    return Mounts(drive, card)


@pytest.fixture
def make_importer():
    made = []

    def make(service, start=True):
        importer = ImportService(service, exiftool=FakeExifTool)
        made.append(importer)
        if start:
            importer.start()
        return importer

    yield make
    for importer in made:
        importer.close()
        for future in (importer.future, importer.read_future):
            if future is not None:
                future.result(timeout=TIMEOUT / 1000)


def snapshot(root):
    found = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        found[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return found


def read_card(qtbot, importer, card):
    got = []
    importer.read(str(card), got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    return got[0]


def test_the_cards_connected_are_found_and_one_plugged_in_is_told(
    make_service, make_importer, mounts, card, qtbot
):
    mounts.card_in = False
    service = make_service()
    importer = make_importer(service)
    assert importer.cards() == []

    mounts.card_in = True
    with qtbot.waitSignal(importer.card_arrived, timeout=TIMEOUT) as arrived:
        service.check_mounts()

    assert arrived.args == [str(card)]
    assert importer.cards() == [str(card)]


def test_reading_a_card_lists_its_photos_and_what_was_imported_before(
    service, make_importer, card, qtbot, wait_idle
):
    importer = make_importer(service)

    reading = read_card(qtbot, importer, card)

    assert sorted(os.path.basename(c.path) for c in reading.candidates) == [
        "IMG_0001.JPG",
        "IMG_0002.JPG",
        "IMG_0003.DNG",
    ]
    assert not any(c.imported for c in reading.candidates)


def test_the_dialog_offers_the_new_photos_by_day_and_shows_where_they_go(
    service, make_importer, card, drive, qtbot
):
    importer = make_importer(service)
    folder = service.add_folder(drive / "Photos")
    dialog = ImportDialog(importer, service.folders())
    qtbot.addWidget(dialog)

    qtbot.waitUntil(lambda: dialog.reading is not None, timeout=TIMEOUT)

    assert dialog.source_box.currentData() == str(card)
    assert dialog.days.count() == 1
    assert "3 photos" in dialog.days.item(0).text()
    assert len(dialog.chosen()) == 3
    assert dialog.destination() == str(drive / "Photos")
    assert dialog.example.text().endswith(
        os.path.join("Photos", "2022", "2022-05-09", "IMG_0001.JPG")
    )
    assert dialog.import_button.isEnabled()
    assert service.folder_holding(str(drive / "Photos" / "2022")) == folder

    dialog.days.item(0).setCheckState(main_window.Qt.CheckState.Unchecked)
    assert dialog.chosen() == [] and not dialog.import_button.isEnabled()


def accept_import(monkeypatch, destination, backup=None, layout="year-day", naming="original"):
    """The import dialog, accepted as soon as it has read the card."""

    def run(dialog):
        from PySide6.QtTest import QTest

        while dialog.reading is None:
            QTest.qWait(20)
        dialog.set_destination(str(destination))
        dialog.set_backup(str(backup) if backup else None)
        dialog.layout_box.setCurrentIndex(dialog.layout_box.findData(layout))
        dialog.naming_box.setCurrentIndex(dialog.naming_box.findData(naming))
        dialog._accepted()
        return ImportDialog.DialogCode.Accepted

    monkeypatch.setattr(ImportDialog, "exec", run)


def test_importing_from_the_window_copies_the_card_and_the_photos_show_in_the_library(
    service, make_importer, card, drive, qtbot, monkeypatch, wait_idle, tmp_path
):
    importer = make_importer(service)
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    window = main_window.MainWindow(service, importer=importer)
    qtbot.addWidget(window)
    before = snapshot(card)
    backup = tmp_path / "backup"
    backup.mkdir()
    accept_import(monkeypatch, drive / "Photos", backup=backup)

    window.import_photos()
    qtbot.waitUntil(lambda: not importer.is_busy(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: len(service.db.photos()) == 6, timeout=TIMEOUT)
    wait_idle(service)

    assert snapshot(card) == before
    day = drive / "Photos" / "2022" / "2022-05-09"
    assert sorted(p.name for p in day.iterdir()) == ["IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.DNG"]
    assert len([p for p in backup.rglob("*") if p.is_file()]) == 3
    assert window.import_status.label.text() == "Imported 3 photos into Photos"
    assert settings.import_choices()["backup_on"] is True
    # Read again, the card has nothing new
    reading = read_card(qtbot, importer, card)
    assert all(c.imported for c in reading.candidates)


def test_a_folder_outside_the_library_is_added_to_import_into(
    service, make_importer, card, drive, qtbot, monkeypatch, wait_idle
):
    importer = make_importer(service)
    window = main_window.MainWindow(service, importer=importer)
    qtbot.addWidget(window)
    destination = drive / "Imported"
    destination.mkdir()
    accept_import(monkeypatch, destination, layout="none")

    window.import_photos()
    qtbot.waitUntil(lambda: not importer.is_busy(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: len(service.db.photos()) == 3, timeout=TIMEOUT)

    assert [f.name for f in service.folders()] == ["Imported"]
    assert sorted(p.name for p in destination.iterdir()) == [
        "IMG_0001.JPG",
        "IMG_0002.JPG",
        "IMG_0003.DNG",
    ]


def test_an_import_waits_for_its_card_and_resumes_at_the_next_start(
    make_service, make_importer, mounts, card, drive, qtbot, wait_idle
):
    service = make_service()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    importer = make_importer(service, start=False)
    reading = read_card(qtbot, importer, card)
    from lumina.core import importing

    importing.queue(
        service.db, reading.candidates, str(drive / "Photos"), service.mounts, layout="none"
    )
    mounts.card_in = False
    service.check_mounts()
    statuses = []
    importer.job_changed.connect(statuses.append)

    importer.start()
    qtbot.waitUntil(lambda: bool(statuses), timeout=TIMEOUT)
    assert statuses[-1].state == library.QUEUED
    assert "Waiting for" in describe_import(statuses[-1])
    assert not any((drive / "Photos").glob("IMG_*"))

    mounts.card_in = True
    service.check_mounts()
    qtbot.waitUntil(lambda: statuses[-1].state == library.DONE, timeout=TIMEOUT)
    assert len(list((drive / "Photos").glob("IMG_*"))) == 3


def test_an_import_in_words():
    def status(state, phase=None, done=0, total=None, message=None, queued=0):
        return ImportStatus("Photos", state, phase, done, total, message, (), queued)

    assert describe_import(status(library.QUEUED)) == "Waiting to import into Photos"
    assert describe_import(status(library.RUNNING, "copying", 2, 9, queued=1)) == (
        "Importing into Photos: 2 of 9  ·  1 more waiting"
    )
    assert describe_import(status(library.DONE, message="Imported 9 photos")) == (
        "Imported 9 photos into Photos"
    )
    assert describe_import(status(library.CANCELLED)) == "Stopped importing into Photos"
    assert describe_import(status(library.FAILED, message="Not enough space")) == (
        "Not enough space"
    )
