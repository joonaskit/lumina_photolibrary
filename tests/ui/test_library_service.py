import time

from conftest import TIMEOUT
from fakes import FakeExifTool
from photos import make_jpeg

from lumina.core import db as dbm
from lumina.core import library as scanning
from lumina.qt_models import library


def test_an_added_folder_is_scanned(service, drive, qtbot, wait_idle):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT) as ended:
        folder = service.add_folder(drive / "Photos")

    status = ended.args[0]
    assert status.state == library.DONE
    assert status.message == "3 photos, 3 new"
    assert [f.name for f in service.folders()] == ["Photos"]
    assert service.folders()[0].connected
    assert len(service.db.photos(dbm.Filter(folder_id=folder))) == 3


def test_a_folder_not_on_any_mount_is_refused(service, tmp_path):
    import pytest

    with pytest.raises(ValueError):
        service.add_folder(tmp_path / "nothing here")


def test_an_unplugged_drive_shows_as_not_connected_and_is_scanned_when_back(
    service, drive, mounts, qtbot, wait_idle
):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")

    mounts.plugged = False
    with qtbot.waitSignal(service.mounts_changed, timeout=TIMEOUT):
        pass
    assert not service.folders()[0].connected
    photo = service.db.photos()[0]
    assert service.resolve(photo.volume_key, photo.path) is None
    # Unplugged is not deleted
    assert len(service.db.photos()) == 3

    make_jpeg(drive / "Photos" / "added while away.jpg")
    mounts.plugged = True
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT) as ended:
        pass
    assert ended.args[0].message == "4 photos, 1 new"


def test_a_job_left_running_by_a_crash_is_finished_at_the_next_start(make_service, drive, qtbot):
    first = make_service(start=False)
    folder = first.db.add_folder("uuid:TEST-DRIVE", "Photos")
    job = first.db.add_job(scanning.SCAN, folder)
    first.db.update_job(job, state=dbm.RUNNING, phase=scanning.FINDING, done=1)
    first.close()

    second = make_service(start=False)
    with qtbot.waitSignal(second.job_ended, timeout=TIMEOUT) as ended:
        second.start()

    assert ended.args[0].state == library.DONE
    assert second.db.job(job).state == dbm.DONE


def test_a_quit_in_the_middle_leaves_the_job_for_the_next_start(
    make_service, drive, qtbot, wait_idle
):
    for n in range(150):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    service = make_service()
    service.add_folder(drive / "Photos")
    FakeExifTool.delay = 0.2
    qtbot.waitUntil(lambda: len(FakeExifTool.calls) >= 1, timeout=TIMEOUT)

    service.close()
    service.future.result(timeout=TIMEOUT / 1000)

    with dbm.Library(service.path) as db:
        assert [job.state for job in db.unfinished_jobs()] == [dbm.QUEUED]
        assert db.count_pending_metadata(db.folders()[0].id) > 0


def test_cancel_stops_the_job(service, drive, qtbot):
    for n in range(150):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    FakeExifTool.delay = 0.2
    service.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: len(FakeExifTool.calls) >= 1, timeout=TIMEOUT)

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT) as ended:
        service.cancel()

    assert ended.args[0].state == library.CANCELLED
    assert service.db.unfinished_jobs() == []


def test_removing_a_folder_while_it_is_scanned_waits_for_the_scan_to_stop(service, drive, qtbot):
    for n in range(150):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    FakeExifTool.delay = 0.2
    folder = service.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: len(FakeExifTool.calls) >= 1, timeout=TIMEOUT)
    files = sorted(p.name for p in (drive / "Photos").iterdir())

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.remove_folder(folder)

    assert service.folders() == [] and service.db.photos() == []
    assert sorted(p.name for p in (drive / "Photos").iterdir()) == files


def test_progress_is_told_while_scanning(service, drive, qtbot):
    seen = []
    service.job_changed.connect(seen.append)

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")

    phases = {status.phase for status in seen}
    assert {library.FINDING, library.DETAILS} <= phases
    assert seen[-1].state == library.DONE


def test_photos_changed_is_not_told_too_often(service, drive, qtbot):
    for n in range(120):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    told = []
    service.photos_changed.connect(lambda: told.append(time.monotonic()))

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")

    # Three batches of details at once: far fewer refreshes than batches
    assert len(told) <= 3
