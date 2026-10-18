import hashlib
import os
import shutil
import time

import pytest
from fakes import FakeExifTool
from photos import make_jpeg, make_jpeg_with_exif
from rawfiles import make_dng

from lumina.core import db as dbm
from lumina.core import library as scanning
from lumina.core.db import Library
from lumina.core.metadata import MetadataUnavailable
from lumina.core.volumes import Mount, locate

DRIVE = "uuid:TEST-DRIVE"


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeExifTool.reset()


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
    job = library.add_job(dbm.SCAN, folder)
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
    assert {library.photo(p.id).model for p in library.photos()} == {"Canon EOS 90D"}
    assert job.message == "3 photos, 3 new"


def test_a_cr3_exiftool_warns_of_damage_in_is_marked_and_left_as_it_was(library, drive):
    path = drive / "Photos" / "d.cr3"
    path.write_bytes(b"not really a CR3")
    FakeExifTool.warnings["d.cr3"] = "Truncated CTMD record"
    folder = add(library, drive)

    state, job = run(library, folder, mounted(drive))

    # Not an error: the photo is listed, with what exiftool said
    assert state == dbm.DONE and job.failures == 0
    warnings = {p.path: p.damage_warning for p in library.photos()}
    assert warnings["Photos/d.cr3"] == "Truncated CTMD record"
    assert warnings["Photos/a.jpg"] is None
    assert path.read_bytes() == b"not really a CR3"


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
    assert row.available == 0 and row.model == "Canon EOS 90D"

    photo.write_bytes(kept)
    os.utime(photo, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    run(library, folder, mounted(drive))
    assert library.photo(row.id).available == 1


def test_an_unplugged_drive_leaves_everything_as_it_was(library, drive):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    before = by_path(library, folder)
    job = library.add_job(dbm.SCAN, folder)

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


def test_a_drive_unplugged_while_details_are_read_leaves_them_to_read_when_it_is_back(
    library, drive, monkeypatch
):
    folder = add(library, drive)
    real = scanning.content_id

    def unplugged(path):
        # The drive goes as the first photo is read
        shutil.rmtree(drive / "Photos", ignore_errors=True)
        return real(path)

    monkeypatch.setattr(scanning, "content_id", unplugged)
    job = library.add_job(dbm.SCAN, folder)
    with pytest.raises(scanning.NotConnected):
        scanning.scan(library, job, folder, mounted(drive), exiftool=FakeExifTool)
    monkeypatch.undo()

    assert library.failures(job) == []
    assert not any(state[3] for state in by_path(library, folder).values())

    # Plugged in again
    (drive / "Photos" / "2022").mkdir(parents=True)
    make_jpeg(drive / "Photos" / "a.jpg")
    make_jpeg(drive / "Photos" / "2022" / "b.jpg")
    make_dng(drive / "Photos" / "2022" / "c.dng", preview="jpeg")
    state, job = run(library, folder, mounted(drive))

    assert state == dbm.DONE
    assert library.failures(job.id) == []
    assert library.count_pending_metadata(folder) == 0
    assert all(state[3] for state in by_path(library, folder).values())


def test_a_file_that_hangs_exiftool_is_named_and_the_rest_of_the_folder_is_read(library, drive):
    folder = add(library, drive)
    for n in range(60):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    hanging = str(drive / "Photos" / "more031.jpg")

    state, job = run(library, folder, mounted(drive), tool=lambda: FakeExifTool(hangs=[hanging]))

    assert state == dbm.DONE
    assert [path for path, _message in library.failures(job.id)] == [hanging]
    assert library.count_pending_metadata(folder) == 0
    # Not asked again: the next scan has nothing to read
    FakeExifTool.calls = []
    state, _job = run(library, folder, mounted(drive), tool=lambda: FakeExifTool(hangs=[hanging]))
    assert state == dbm.DONE
    assert FakeExifTool.calls == []


def test_an_exiftool_that_dies_on_every_file_still_stops_the_scan_and_blames_none(library, drive):
    folder = add(library, drive)

    state, job = run(library, folder, mounted(drive), tool=lambda: FakeExifTool(dies_after=0))

    assert state == dbm.FAILED_JOB
    assert library.failures(job.id) == []
    assert library.count_pending_metadata(folder) == 3


def test_a_scan_removes_part_files_of_ours_that_a_crash_left_and_nothing_else(library, drive):
    folder = add(library, drive)
    photos = drive / "Photos"
    old = time.time() - 2 * 3600
    stale = [".lumina-xmp-123-a.jpg.part", ".lumina-tether-4.part", ".lumina-merge-5.part"]
    stale += [".lumina-import-6-7.part", ".lumina-export-abc.part"]
    kept = [
        # Being written now
        ".lumina-import-9-9.part",
        # Not ours
        ".other-tool.part",
        ".lumina-notes.txt",
        "keep.part",
    ]
    for name in stale + kept:
        (photos / name).write_bytes(b"x")
    for name in stale + [".other-tool.part", ".lumina-notes.txt", "keep.part"]:
        os.utime(photos / name, (old, old))
    before = {p.name for p in photos.iterdir()} - set(stale)

    state, _job = run(library, folder, mounted(drive))

    assert state == dbm.DONE
    assert {p.name for p in photos.iterdir()} == before


def test_a_partial_scan_looks_only_at_the_directories_given(library, drive):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    photos = drive / "Photos"
    make_jpeg(photos / "new_top.jpg")
    make_jpeg(photos / "2022" / "new_deep.jpg")
    # Gone in both, but only one of the directories is looked at
    (photos / "a.jpg").unlink()
    (photos / "2022" / "b.jpg").unlink()

    state, job = run(library, folder, mounted(drive), only={str(photos / "2022")})

    assert state == dbm.DONE
    states = by_path(library, folder)
    available = {path.rsplit("/", 1)[-1]: s[3] for path, s in states.items()}
    assert available == {
        "a.jpg": True,  # not looked at: still believed there
        "b.jpg": False,
        "c.dng": True,
        "new_deep.jpg": True,
    }
    assert library.count_pending_metadata(folder) == 0


def test_a_partial_scan_of_a_directory_that_is_gone_makes_what_was_below_it_missing(library, drive):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    (drive / "Photos" / "2022" / "deeper").mkdir()
    make_jpeg(drive / "Photos" / "2022" / "deeper" / "d.jpg")
    run(library, folder, mounted(drive))
    shutil.rmtree(drive / "Photos" / "2022")

    run(library, folder, mounted(drive), only={str(drive / "Photos" / "2022")})

    available = {path.rsplit("/", 1)[-1]: s[3] for path, s in by_path(library, folder).items()}
    assert available == {
        "a.jpg": True,
        "b.jpg": False,
        "c.dng": False,
        "d.jpg": False,
    }


def test_a_partial_scan_does_not_walk_the_rest_of_the_folder(library, drive, monkeypatch):
    folder = add(library, drive)
    run(library, folder, mounted(drive))
    walked = []
    real = os.walk
    monkeypatch.setattr(
        os, "walk", lambda *args, **kwargs: walked.append(args) or real(*args, **kwargs)
    )
    listed = []
    real_list = os.listdir
    monkeypatch.setattr(os, "listdir", lambda path: listed.append(str(path)) or real_list(path))

    run(library, folder, mounted(drive), only={str(drive / "Photos" / "2022")})

    assert walked == []
    assert listed == [str(drive / "Photos" / "2022")]


def test_a_scan_interrupted_by_a_crash_is_found_and_finished(library, drive):
    folder = add(library, drive)
    job = library.add_job(dbm.SCAN, folder)
    # As a crash leaves it: running, half done
    library.update_job(job, state=dbm.RUNNING, phase=dbm.FINDING, done=1)

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
    assert row.metadata_state == dbm.FAILED and row.metadata_error == "File format error"
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

    assert (dbm.FINDING, 3, None) in seen
    assert (dbm.DETAILS, 3, 3) in seen
    assert (job.phase, job.done, job.total) == (dbm.DETAILS, 3, 3)


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
    assert (row.captured, row.captured_offset) == ("2024-06-01 18:30:05.250000", "+03:00")
    assert row.lens == "RF24-105mm F4 L IS USM"


class Rests:
    """Stands in for `rest`: notes it was asked to, without waiting."""

    def __init__(self):
        self.seconds = []

    def __call__(self, seconds, should_stop):
        self.seconds.append(seconds)


def test_a_large_gentle_scan_rests_between_batches_and_reads_at_low_priority(
    library, drive, monkeypatch
):
    monkeypatch.setattr(scanning, "GENTLE_FROM", 100)
    folder = add(library, drive)
    for n in range(120):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    rests = Rests()

    state, _job = run(library, folder, mounted(drive), gentle=True, rest=rests)

    assert state == dbm.DONE
    assert FakeExifTool.gentle
    # 123 photos in batches of 50: a rest after each, and none longer than the most
    assert len(rests.seconds) == 3
    assert all(0 <= seconds <= scanning._REST_MOST for seconds in rests.seconds)
    # Everything was read all the same
    assert library.count_pending_metadata(folder) == 0


def test_a_small_scan_is_not_gentle_and_nor_is_one_not_asked_to_be(library, drive, monkeypatch):
    folder = add(library, drive)
    rests = Rests()
    state, _job = run(library, folder, mounted(drive), gentle=True, rest=rests)
    # 3 photos, below GENTLE_FROM
    assert state == dbm.DONE and rests.seconds == [] and not FakeExifTool.gentle

    monkeypatch.setattr(scanning, "GENTLE_FROM", 1)
    for n in range(60):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg")
    state, _job = run(library, folder, mounted(drive), rest=rests)
    assert state == dbm.DONE and rests.seconds == [] and not FakeExifTool.gentle


def test_finding_a_large_first_import_rests_too(library, drive, monkeypatch):
    monkeypatch.setattr(scanning, "GENTLE_FROM", 400)
    monkeypatch.setattr(scanning, "_WRITE_EVERY", 100)
    folder = add(library, drive)
    for n in range(450):
        make_jpeg(drive / "Photos" / f"more{n:03}.jpg", size=(16, 16))
    rests = Rests()

    state, _job = run(library, folder, mounted(drive), gentle=True, rest=rests)

    assert state == dbm.DONE
    # The walk rested from the 400th photo on (4 writes of 100, then 453 are seen);
    # the details after each of their batches
    assert len(rests.seconds) > len(range(0, 453, 50)) - 1


def test_a_cancel_is_not_waited_for_in_a_rest():
    import time

    began = time.monotonic()
    scanning.rest(30, lambda: True)
    assert time.monotonic() - began < 1

    began = time.monotonic()
    scanning.rest(0.25, lambda: False)
    assert 0.2 <= time.monotonic() - began < 1


def test_gentle_exiftool_is_niced(tmp_path):
    import subprocess

    from lumina.core import metadata

    child = subprocess.Popen(["sleep", "30"])
    try:
        assert os.getpriority(os.PRIO_PROCESS, child.pid) == 0
        metadata.lower_priority(child.pid)
        assert os.getpriority(os.PRIO_PROCESS, child.pid) == metadata._NICE
        if shutil.which("ionice"):
            said = subprocess.run(
                ["ionice", "-p", str(child.pid)], capture_output=True, text=True
            ).stdout
            assert "idle" in said
    finally:
        child.kill()
        child.wait()


def test_a_missing_process_is_not_an_error_when_lowering_priority():
    from lumina.core import metadata

    metadata.lower_priority(2**22 + 12345)
