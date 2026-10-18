"""Exporting the photos chosen in the grid: a job, its progress and its end."""

import os

from conftest import TIMEOUT
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QMessageBox

from lumina.qt_models.exporting import ExportOptions, ExportService
from lumina.ui import main_window, window_copies, window_stacking
from lumina.ui.main_window import MainWindow


def scanned(qtbot, service, drive):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    return [(photo.id, 0) for photo in service.db.photos()]


def ended(statuses):
    return [s for s in statuses if s.state in ("done", "failed", "cancelled")]


def test_an_export_is_run_as_a_job_and_ends_with_its_files(qtbot, service, drive, tmp_path):
    entries = scanned(qtbot, service, drive)
    exporter = ExportService(service)
    statuses = []
    exporter.job_changed.connect(statuses.append)
    out = tmp_path / "out"

    job = exporter.export(entries, out, ExportOptions("jpeg", 8, 90, 64))

    qtbot.waitUntil(lambda: bool(ended(statuses)), timeout=TIMEOUT)
    assert ended(statuses)[0].state == "done"
    assert ended(statuses)[0].message == "Exported 3 files"
    assert sorted(os.listdir(out)) == ["a.jpg", "b.jpg", "c.jpg"]
    assert service.db.job(job).state == "done"
    assert any(s.state == "running" and s.total == 3 for s in statuses)
    exporter.close()


def test_a_queued_export_is_resumed_by_the_next_start(qtbot, service, drive, tmp_path):
    entries = scanned(qtbot, service, drive)
    # Queued and not run: as a quit leaves it
    from lumina.core import exporting

    job = exporting.queue(
        service.db, entries[:1], str(tmp_path / "out"), ExportOptions(), mounts=service.mounts
    )
    exporter = ExportService(service)
    statuses = []
    exporter.job_changed.connect(statuses.append)

    exporter.start()

    qtbot.waitUntil(lambda: bool(ended(statuses)), timeout=TIMEOUT)
    assert service.db.job(job).state == "done"
    assert os.listdir(tmp_path / "out") == ["a.jpg"]
    exporter.close()


def test_a_cancel_drops_the_waiting_exports_and_stops_the_running_one(
    qtbot, service, drive, tmp_path
):
    entries = scanned(qtbot, service, drive)
    exporter = ExportService(service)
    statuses = []
    exporter.job_changed.connect(statuses.append)
    exporter.export(entries, tmp_path / "one", ExportOptions())
    second = exporter.export(entries, tmp_path / "two", ExportOptions())

    exporter.cancel()

    qtbot.waitUntil(lambda: bool(ended(statuses)), timeout=TIMEOUT)
    assert service.db.job(second).state == "cancelled"
    assert not (tmp_path / "two").exists()
    exporter.close()


def test_the_grid_exports_the_photos_chosen(qtbot, service, drive, tmp_path, monkeypatch):
    exporter = ExportService(service)
    window = MainWindow(service, exporter=exporter)
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    out = tmp_path / "chosen"

    class Chosen:
        class DialogCode:
            Accepted = 1

        def __init__(self, source, parent, count=1):
            assert count == 2

        def exec(self):
            return 1

        def destination_folder(self):
            return str(out)

        def options(self):
            return ExportOptions("png", 8)

        class replace:
            @staticmethod
            def isChecked():
                return False

    # Where the window uses it
    for part in (window_copies, window_stacking):
        monkeypatch.setattr(part, "ExportDialog", Chosen)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: None)
    selection = window.grid.selectionModel()
    for row in (0, 2):
        selection.select(window.model.index(row), QItemSelectionModel.SelectionFlag.Select)
    window._update_stack_actions()
    assert window.export_selected_action.isEnabled()

    window.export_selected()

    qtbot.waitUntil(lambda: out.exists() and len(os.listdir(out)) == 2, timeout=TIMEOUT)
    assert sorted(os.listdir(out)) == ["a.png", "c.png"]
    exporter.close()
