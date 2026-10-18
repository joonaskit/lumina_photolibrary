import time

import pytest
from conftest import TIMEOUT
from fakes import FakeExifTool
from photos import make_jpeg

from lumina.core import db as dbm
from lumina.core.dbtypes import CANCELLED, DETAILS, DONE, FINDING
from lumina.qt_models import library


def test_an_added_folder_is_scanned(service, drive, qtbot, wait_idle):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT) as ended:
        folder = service.add_folder(drive / "Photos")

    status = ended.args[0]
    assert status.state == DONE
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
    job = first.db.add_job(dbm.SCAN, folder)
    first.db.update_job(job, state=dbm.RUNNING, phase=dbm.FINDING, done=1)
    first.close()

    second = make_service(start=False)
    with qtbot.waitSignal(second.job_ended, timeout=TIMEOUT) as ended:
        second.start()

    assert ended.args[0].state == DONE
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

    assert ended.args[0].state == CANCELLED
    assert service.db.unfinished_jobs() == []


def test_removing_a_folder_while_it_is_scanned_waits_for_the_scan_to_stop(service, drive, qtbot):
    for n in range(150):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    FakeExifTool.delay = 0.2
    folder = service.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: len(FakeExifTool.calls) >= 1, timeout=TIMEOUT)
    files = sorted(p.name for p in (drive / "Photos").iterdir())

    service.remove_folder(folder)
    qtbot.waitUntil(lambda: service.folders() == [], timeout=TIMEOUT)

    assert service.db.photos() == []
    assert sorted(p.name for p in (drive / "Photos").iterdir()) == files


def test_progress_is_told_while_scanning(service, drive, qtbot):
    seen = []
    service.job_changed.connect(seen.append)

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")

    phases = {status.phase for status in seen}
    assert {FINDING, DETAILS} <= phases
    assert seen[-1].state == DONE


def test_photos_changed_is_not_told_too_often(service, drive, qtbot):
    for n in range(120):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    told = []
    service.photos_changed.connect(lambda: told.append(time.monotonic()))

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")

    # Three batches of details at once: far fewer refreshes than batches
    assert len(told) <= 3


def _fake_thumbnails(cid, count, age):
    """`count` files of 1000 bytes in the cache, as thumbnails of `cid`, last used `age`
    seconds ago."""
    import os

    from lumina.core import thumbs

    root = thumbs.ThumbnailCache(thumbs.default_cache_dir()).root / cid[:2]
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for number in range(count):
        path = root / f"{cid}-{100 + number}.webp"
        path.write_bytes(b"x" * 1000)
        used = time.time() - age
        os.utime(path, (used, used))
        paths.append(path)
    return paths


def _scanned_on_and_off_a_drive(service, drive, mounts, tmp_path, qtbot):
    """The ids of the photos of a drive, then unplugged, and of one on this computer."""
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    local = tmp_path / "local"
    local.mkdir()
    make_jpeg(local / "here.jpg", size=(120, 80), colour=(1, 2, 3))
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(local)
    photos = service.db.photos()
    here = next(p.content_id for p in photos if p.path.endswith("here.jpg"))
    away = {p.content_id for p in photos if not p.path.endswith("here.jpg")}
    mounts.plugged = False
    with qtbot.waitSignal(service.mounts_changed, timeout=TIMEOUT):
        pass
    return here, away


def test_the_thumbnail_cache_is_pruned_and_a_drive_not_connected_is_kept_longest(
    service, drive, mounts, tmp_path, qtbot, wait_idle
):
    here, away = _scanned_on_and_off_a_drive(service, drive, mounts, tmp_path, qtbot)
    wait_idle(service)
    # The photo here was used last, the others long ago: by use alone it would stay
    kept = [path for cid in away for path in _fake_thumbnails(cid, 2, 9000)]
    gone = _fake_thumbnails(here, 3, 10)
    assert len(away) == 3

    with qtbot.waitSignal(service.thumbnails_pruned, timeout=TIMEOUT) as pruned:
        service.set_thumbnail_limit(6700)
    qtbot.waitUntil(lambda: not service._pruning, timeout=TIMEOUT)

    # 9000 bytes to 6030 or less: the three of the photo here go, none of the others
    assert not any(path.exists() for path in gone)
    assert all(path.exists() for path in kept)
    assert (pruned.args[0].removed, pruned.args[0].remaining) == (3, 6000)
    # The library itself is untouched
    assert len(service.db.photos()) == 4


def test_no_limit_prunes_nothing_and_a_limit_is_kept_by_the_service(
    service, drive, tmp_path, qtbot, wait_idle
):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    cid = service.db.photos()[0].content_id
    paths = _fake_thumbnails(cid, 5, 9000)

    assert service.thumbnail_limit is None
    service.prune_thumbnails()
    qtbot.wait(100)
    assert all(path.exists() for path in paths)

    with qtbot.waitSignal(service.thumbnails_pruned, timeout=TIMEOUT):
        service.set_thumbnail_limit(2000)
    assert service.thumbnail_limit == 2000
    assert sum(path.exists() for path in paths) < 5


def test_the_usage_is_counted_off_the_main_thread(service, drive, qtbot, wait_idle):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    _fake_thumbnails(service.db.photos()[0].content_id, 3, 10)
    told = []

    service.thumbnail_usage(told.append)
    qtbot.waitUntil(lambda: bool(told), timeout=TIMEOUT)

    # The scan's own thumbnails may be there too
    assert told[0] >= 3000


def test_a_scan_is_gentle_when_the_service_says_so_and_the_library_is_large(
    service, drive, qtbot, monkeypatch
):
    from lumina.core import library as core_library

    monkeypatch.setattr(core_library, "GENTLE_FROM", 5)
    monkeypatch.setattr(core_library, "_REST_RATIO", 0.0)
    for n in range(12):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    assert service.gentle_scans

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    assert FakeExifTool.gentle

    FakeExifTool.reset()
    service.gentle_scans = False
    for n in range(12):
        make_jpeg(drive / "Photos" / f"again{n:03}.jpg")
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.rescan()
    assert not FakeExifTool.gentle


def test_the_time_left_is_told_from_the_rate_of_the_details(service, qtbot, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: now[0])
    job = 7

    # The walk has no end to tell; the details tell nothing until there is a rate
    assert service._time_left(job, FINDING, 300, None) is None
    assert service._time_left(job, DETAILS, 0, 1000) is None
    now[0] += 2
    assert service._time_left(job, DETAILS, 200, 1000) is None
    now[0] += 8
    # 400 read in 10 seconds: 600 left take 15
    assert service._time_left(job, DETAILS, 400, 1000) == pytest.approx(15)
    # Few read: not told, a new job or phase starts again
    assert service._time_left(8, DETAILS, 0, 1000) is None
    now[0] += 20
    assert service._time_left(8, DETAILS, 50, 1000) is None


def test_the_raw_files_of_a_camera_are_found_on_a_thread(service, drive, qtbot):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    found, failed = [], []

    service.raw_files_of("Canon EOS 90D", 5, found.append, failed.append)

    qtbot.waitUntil(lambda: bool(found or failed), timeout=TIMEOUT)
    assert failed == []
    assert [path.rsplit("/", 1)[-1] for path in found[0]] == ["c.dng"]
