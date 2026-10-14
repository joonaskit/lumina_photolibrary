import hashlib
import os
import shutil
from datetime import datetime

import pytest
from photos import make_jpeg, make_jpeg_with_exif
from rawfiles import make_dng

from lumina.core import db as dbm
from lumina.core import library as scanning
from lumina.core.db import Library
from lumina.core.errors import PhotoError
from lumina.core.metadata import Metadata, MetadataUnavailable
from lumina.core.volumes import Mount, locate

DRIVE = "uuid:TEST-DRIVE"


class FakeExifTool:
    """Says every photo is from a 90D, except the paths in `broken`."""

    calls = []

    def __init__(self, broken=(), dies_after=None):
        self.broken = set(map(str, broken))
        self.dies_after = dies_after

    def read_many(self, paths):
        FakeExifTool.calls.append(list(paths))
        if self.dies_after is not None and len(FakeExifTool.calls) > self.dies_after:
            raise MetadataUnavailable("exiftool stopped")
        return {
            p: PhotoError(p, "File format error")
            if p in self.broken
            else Metadata(captured=datetime(2022, 5, 9, 21, 3), model="Canon EOS 90D")
            for p in paths
        }

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fresh_calls():
    FakeExifTool.calls = []


@pytest.fixture
def drive(tmp_path):
    """A "USB drive": a folder that a mount list says is the volume DRIVE."""
    path = tmp_path / "drive"
    (path / "Photos" / "2022").mkdir(parents=True)
    make_jpeg(path / "Photos" / "a.jpg")
    make_jpeg(path / "Photos" / "2022" / "b.jpg")
    make_dng(path / "Photos" / "2022" / "c.dng", preview="jpeg")
    return path


def mounted(path):
    return [Mount(DRIVE, str(path), "/", "exfat", "/dev/sdz1", "Photos drive")]


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


def add(library, drive):
    key, inside = locate(drive / "Photos", mounted(drive))
    return library.add_folder(key, inside, "Photos drive", str(drive))


def run(library, folder, mounts, tool=FakeExifTool, **kwargs):
    job = library.add_job(scanning.SCAN, folder)
    state = scanning.scan(library, job, folder, mounts, exiftool=tool, **kwargs)
    return state, library.job(job)


def by_path(library, folder):
    return {path: state for path, state in library.photo_states(folder).items()}


def test_a_first_scan_finds_the_photos_and_reads_their_details(library, drive):
    folder = add(library, drive)

    state, job = run(library, folder, mounted(drive))

    assert state == dbm.DONE and job.state == dbm.DONE
    assert set(by_path(library, folder)) == {
        "Photos/a.jpg",
        "Photos/2022/b.jpg",
        "Photos/2022/c.dng",
    }
    assert all(p.content_id for p in library.photos())
    assert {library.photo(p.id)["model"] for p in library.photos()} == {"Canon EOS 90D"}
    assert job.message == "3 photos, 3 new"


def test_unchanged_photos_are_not_read_again(library, drive):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    FakeExifTool.calls = []

    state, job = run(library, folder, mounted(drive))

    assert state == dbm.DONE
    assert FakeExifTool.calls == []
    assert job.message == "3 photos"


def test_a_changed_photo_is_read_again(library, drive):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    FakeExifTool.calls = []
    make_jpeg(drive / "Photos" / "a.jpg", size=(30, 30))
    os.utime(drive / "Photos" / "a.jpg", ns=(1, 1))

    _state, job = run(library, folder, mounted(drive))

    assert [os.path.basename(p) for batch in FakeExifTool.calls for p in batch] == ["a.jpg"]
    assert job.message == "3 photos, 1 changed"


def test_a_deleted_photo_is_marked_missing_and_comes_back(library, drive):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    photo = drive / "Photos" / "a.jpg"
    kept = photo.read_bytes()
    stat = photo.stat()
    photo.unlink()

    _state, job = run(library, folder, mounted(drive))
    row = library.photo(by_path(library, folder)["Photos/a.jpg"][0])

    assert job.message == "3 photos, 1 missing"
    assert row["available"] == 0 and row["model"] == "Canon EOS 90D"

    photo.write_bytes(kept)
    os.utime(photo, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    run(library, folder, mounted(drive))
    assert library.photo(row["id"])["available"] == 1


def test_an_unplugged_drive_leaves_everything_as_it_was(library, drive):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    before = by_path(library, folder)
    job = library.add_job(scanning.SCAN, folder)

    with pytest.raises(scanning.NotConnected):
        scanning.scan(library, job, folder, [], exiftool=FakeExifTool)

    assert by_path(library, folder) == before
    assert library.job(job).state == dbm.QUEUED


def test_a_drive_mounted_somewhere_else_keeps_its_photos(library, drive, tmp_path):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    before = by_path(library, folder)
    elsewhere = tmp_path / "mounted elsewhere"
    shutil.copytree(drive, elsewhere)
    shutil.rmtree(drive)
    FakeExifTool.calls = []

    _state, job = run(library, folder, mounted(elsewhere))

    assert by_path(library, folder) == before
    assert FakeExifTool.calls == []
    assert job.message == "3 photos"


def test_a_cancelled_scan_marks_nothing_missing(library, drive):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    (drive / "Photos" / "a.jpg").unlink()

    state, job = run(library, folder, mounted(drive), should_stop=lambda: True)

    assert state == dbm.CANCELLED and job.state == dbm.CANCELLED
    assert all(state[3] for state in by_path(library, folder).values())


def test_a_cancel_while_reading_details_keeps_what_was_read(library, drive):
    folder = add(library, drive)
    for n in range(120):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    asked = []

    def stop_after_first_batch():
        asked.append(1)
        return len(FakeExifTool.calls) >= 1

    state, _job = run(library, folder, mounted(drive), should_stop=stop_after_first_batch)

    assert state == dbm.CANCELLED
    assert 0 < library.count_pending_metadata(folder) < 123
    # Started again, it reads only what is left
    FakeExifTool.calls = []
    state, _job = run(library, folder, mounted(drive))
    assert state == dbm.DONE
    assert sum(len(batch) for batch in FakeExifTool.calls) < 123
    assert library.count_pending_metadata(folder) == 0


def test_a_scan_interrupted_by_a_crash_is_found_and_finished(library, drive):
    folder = add(library, drive)
    job = library.add_job(scanning.SCAN, folder)
    # As a crash leaves it: running, half done
    library.update_job(job, state=dbm.RUNNING, phase=scanning.FINDING, done=1)

    unfinished = library.unfinished_jobs()
    assert [j.id for j in unfinished] == [job]
    state = scanning.scan(library, job, folder, mounted(drive), exiftool=FakeExifTool)

    assert state == dbm.DONE and library.unfinished_jobs() == []


def test_a_folder_that_cannot_be_read_does_not_make_its_photos_missing(library, drive):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    locked = drive / "Photos" / "2022"
    locked.chmod(0)
    try:
        state, job = run(library, folder, mounted(drive))
    finally:
        locked.chmod(0o755)

    assert state == dbm.DONE
    assert all(state[3] for state in by_path(library, folder).values())
    assert library.failures(job.id)[0][0] == str(locked)
    assert "1 could not be read" in job.message


def test_a_folder_gone_from_a_connected_drive_fails_and_keeps_the_photos(library, drive):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    shutil.rmtree(drive / "Photos")

    state, job = run(library, folder, mounted(drive))

    assert state == dbm.FAILED_JOB
    assert str(drive / "Photos") in job.message
    states = by_path(library, folder)
    assert len(states) == 3 and not any(state[3] for state in states.values())


def test_a_photo_whose_details_cannot_be_read_is_named(library, drive):
    folder = add(library, drive)
    broken = drive / "Photos" / "a.jpg"

    state, job = run(library, folder, mounted(drive), tool=lambda: FakeExifTool(broken=[broken]))

    assert state == dbm.DONE
    assert library.failures(job.id) == [(str(broken), "File format error")]
    row = library.photo(by_path(library, folder)["Photos/a.jpg"][0])
    assert row["metadata_state"] == dbm.FAILED and row["metadata_error"] == "File format error"
    assert "1 could not be read" in job.message


def test_without_exiftool_photos_are_listed_and_read_later(library, drive):
    folder = add(library, drive)

    def missing():
        raise MetadataUnavailable("exiftool is not installed")

    state, job = run(library, folder, mounted(drive), tool=missing)

    assert state == dbm.DONE
    assert "exiftool is not installed" in job.message
    assert library.count_pending_metadata(folder) == 3
    run(library, folder, mounted(drive))
    assert library.count_pending_metadata(folder) == 0


def test_exiftool_stopping_fails_the_job_and_keeps_the_rest_for_later(library, drive):
    folder = add(library, drive)
    for n in range(60):
        make_jpeg(drive / "Photos" / f"more{n:02}.jpg")

    state, job = run(library, folder, mounted(drive), tool=lambda: FakeExifTool(dies_after=1))

    assert state == dbm.FAILED_JOB and "exiftool stopped" in job.message
    assert library.count_pending_metadata(folder) > 0


def test_progress_is_reported_and_saved(library, drive):
    folder = add(library, drive)
    seen = []

    _state, job = run(library, folder, mounted(drive), report=lambda *args: seen.append(args[:3]))

    assert (scanning.FINDING, 3, None) in seen
    assert (scanning.DETAILS, 3, 3) in seen
    assert (job.phase, job.done, job.total) == (scanning.DETAILS, 3, 3)


def test_scanning_leaves_every_file_as_it_was(library, drive, exiftool):
    make_jpeg_with_exif(drive / "Photos" / "exif.jpg")
    files = sorted(p for p in drive.rglob("*") if p.is_file())
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}
    folder = add(library, drive)

    # The real exiftool here, not the fake
    state, _job = run(library, folder, mounted(drive), tool=lambda: exiftool)

    assert state == dbm.DONE
    after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files}
    assert after == before
    assert sorted(p for p in drive.rglob("*") if p.is_file()) == files
    row = library.photo(by_path(library, folder)["Photos/exif.jpg"][0])
    assert (row["captured"], row["captured_offset"]) == ("2024-06-01 18:30:05.250000", "+03:00")
    assert row["lens"] == "RF24-105mm F4 L IS USM"
