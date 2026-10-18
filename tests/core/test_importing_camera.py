"""Importing from a camera on USB (#72): reading what is on it through partial reads, and
the job that copies its photos into the library."""

import hashlib
import io
import os
from datetime import datetime

import pytest
from fakes import FakeCamera
from PIL import Image
from rawfiles import make_dng

from lumina.core import db as dbm
from lumina.core import importing, tether
from lumina.core.db import Library
from lumina.core.errors import NotConnected
from lumina.core.fingerprint import content_id
from lumina.core.volumes import Mount

DRIVE = "uuid:DRIVE"
KEY = "camera:FAKE-1"


def jpeg_at(when, colour=(200, 10, 10), padding=0):
    """A JPEG taken at `when`, in the Exif IFD as a camera writes it."""
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = f"{when:%Y:%m:%d %H:%M:%S}"
    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), colour).save(buffer, "JPEG", exif=exif.tobytes())
    return buffer.getvalue() + bytes(padding)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "camera"
    path.mkdir()
    FakeCamera.put(path, "IMG_0001.JPG", jpeg_at(datetime(2026, 10, 14, 9, 30, 5)))
    FakeCamera.put(
        path, "IMG_0002.JPG", jpeg_at(datetime(2026, 10, 15, 8, 0, 1), (10, 200, 10), 300_000)
    )
    FakeCamera.put(path, "IMG_0003.JPG", jpeg_at(datetime(2026, 10, 15, 8, 5, 0), (10, 10, 200)))
    # In a second folder, as a second card slot or a new DCIM folder is
    FakeCamera.put(path, "IMG_0004.JPG", jpeg_at(datetime(2026, 10, 16, 7, 0, 0)), "101CANON")
    # Not photos, and a hidden file
    FakeCamera.put(path, "MVI_0005.MP4", b"video")
    FakeCamera.put(path, ".hidden.JPG", b"hidden")
    return path


@pytest.fixture
def camera(root):
    return lambda: FakeCamera(root)


@pytest.fixture
def drive(tmp_path):
    path = tmp_path / "drive"
    (path / "Photos").mkdir(parents=True)
    return path


@pytest.fixture
def mounts(drive, tmp_path):
    (tmp_path / "backup").mkdir()
    return [
        Mount(DRIVE, str(drive), "/", "ext4", "/dev/sdz1", "Photos drive"),
        Mount("uuid:BACKUP", str(tmp_path / "backup"), "/", "ext4", "/dev/sdy1", "Backup"),
    ]


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


@pytest.fixture
def folder(library, drive):
    library.add_folder(DRIVE, "Photos", "Photos drive")
    return str(drive / "Photos")


def on_card(root):
    return {
        os.path.join(d, n): (hashlib.sha256(open(os.path.join(d, n), "rb").read()).hexdigest())
        for d, _f, files in os.walk(root / "card")
        for n in files
    }


def queue_all(library, camera, folder, mounts, **choices):
    reading = importing.read_camera(library, camera)
    job = importing.queue(library, reading.candidates, folder, mounts, **choices)
    return reading, job


def test_reading_a_camera_lists_its_photos_with_their_times_and_ids(library, camera, root):
    reading = importing.read_camera(library, camera)

    assert [os.path.basename(c.path) for c in reading.candidates] == [
        "IMG_0001.JPG",
        "IMG_0002.JPG",
        "IMG_0003.JPG",
        "IMG_0004.JPG",
    ]
    first = reading.candidates[0]
    assert first.taken == datetime(2026, 10, 14, 9, 30, 5)
    assert first.camera == KEY
    assert first.path.startswith("/store_00020001/DCIM/100CANON/")
    assert reading.candidates[3].path.startswith("/store_00020001/DCIM/101CANON/")
    assert reading.failures == ()
    # The same id as the photo has once it is a file: the ids match across the copy
    for candidate in reading.candidates:
        name = os.path.basename(candidate.path)
        folder = "100CANON" if "100CANON" in candidate.path else "101CANON"
        assert candidate.content_id == content_id(root / "card" / "DCIM" / folder / name)


def test_reading_asks_the_camera_for_parts_not_the_whole_photo(library, camera, root):
    importing.read_camera(library, camera)

    reads = FakeCamera.log(root, "reads")
    big = [line for line in reads if "IMG_0002" in line]
    # Two parts of 64 KiB of a photo of 300 kB, not all of it
    assert len(big) == 2
    assert all(int(line.split()[2]) <= 64 * 1024 for line in big)


def test_a_photo_whose_start_does_not_tell_its_time_has_the_cameras_time(library, root, camera):
    FakeCamera.put(root, "IMG_0006.JPG", b"not a jpeg at all", mtime=1_700_000_000)

    reading = importing.read_camera(library, camera)

    undated = next(c for c in reading.candidates if c.path.endswith("IMG_0006.JPG"))
    assert undated.taken == datetime.fromtimestamp(1_700_000_000)


def test_a_photo_that_cannot_be_read_is_named_and_the_rest_are_read(library, camera, root):
    FakeCamera.fail_reading(root, "IMG_0002.JPG")

    reading = importing.read_camera(library, camera)

    assert len(reading.candidates) == 3
    assert [path.rpartition("/")[2] for path, _why in reading.failures] == ["IMG_0002.JPG"]


def test_reading_stops_when_told_to(library, camera):
    assert importing.read_camera(library, camera, should_stop=lambda: True) is None


def test_no_camera_is_not_connected(library, root):
    FakeCamera.unplug(root)

    with pytest.raises(NotConnected, match="camera"):
        importing.read_camera(library, lambda: FakeCamera(root))


def test_a_camera_another_program_has_says_so(library, root):
    FakeCamera.make_busy(root)

    with pytest.raises(NotConnected, match="using the camera"):
        importing.read_camera(library, lambda: FakeCamera(root))


def test_the_camera_is_only_read(library, camera, root, folder, mounts):
    before = on_card(root)
    mtimes = {p: os.stat(p).st_mtime_ns for p in before}

    reading, job = queue_all(library, camera, folder, mounts)
    importing.run(library, job, mounts, make_camera=camera)

    assert on_card(root) == before
    assert {p: os.stat(p).st_mtime_ns for p in before} == mtimes
    assert FakeCamera.log(root, "released") == []


def test_the_photos_are_copied_with_the_import_layout_and_names(
    library, camera, root, folder, mounts
):
    reading, job = queue_all(library, camera, folder, mounts, naming="date")

    state = importing.run(library, job, mounts, make_camera=camera)

    assert state == dbm.DONE
    assert library.failures(job) == []
    assert library.job(job).message == "Imported 4 photos"
    copied = os.path.join(folder, "2026", "2026-10-14", "20261014-093005-IMG_0001.JPG")
    original = root / "card" / "DCIM" / "100CANON" / "IMG_0001.JPG"
    assert open(copied, "rb").read() == original.read_bytes()
    assert os.path.exists(
        os.path.join(folder, "2026", "2026-10-16", "20261016-070000-IMG_0004.JPG")
    )
    items = library.import_items(job)
    assert all(item.state == dbm.COPIED and item.source_key == KEY for item in items)
    # No part file is left
    assert not [n for d, _f, files in os.walk(folder) for n in files if n.endswith(".part")]


def test_a_large_photo_is_copied_whole_in_parts(library, camera, root, folder, mounts):
    FakeCamera.put(
        root, "IMG_0009.JPG", jpeg_at(datetime(2026, 10, 17, 9, 0, 0), padding=9_000_000)
    )
    reading, job = queue_all(library, camera, folder, mounts, layout="none")

    importing.run(library, job, mounts, make_camera=camera)

    big = root / "card" / "DCIM" / "100CANON" / "IMG_0009.JPG"
    assert (open(os.path.join(folder, "IMG_0009.JPG"), "rb").read()) == big.read_bytes()


def test_a_backup_copy_is_made_from_the_copy_not_from_the_camera_again(
    library, camera, root, folder, mounts, tmp_path
):
    FakeCamera.log(root, "reads")
    reading = importing.read_camera(library, camera)
    reads_for_listing = len(FakeCamera.log(root, "reads"))
    job = importing.queue(
        library, reading.candidates, folder, mounts, layout="none", backup=str(tmp_path / "backup")
    )

    importing.run(library, job, mounts, make_camera=camera)

    for name in ("IMG_0001.JPG", "IMG_0002.JPG"):
        assert (tmp_path / "backup" / name).read_bytes() == open(
            os.path.join(folder, name), "rb"
        ).read()
    reads = FakeCamera.log(root, "reads")[reads_for_listing:]
    # Each photo crossed once: no photo's whole was asked for twice
    assert len([line for line in reads if "IMG_0002" in line and line.split()[1] == "0"]) == 1


def test_photos_imported_are_known_as_imported_before(library, camera, root, folder, mounts):
    reading, job = queue_all(library, camera, folder, mounts)
    importing.run(library, job, mounts, make_camera=camera)

    again = importing.read_camera(library, camera)

    assert all(c.imported for c in again.candidates)


def test_a_photo_that_will_not_read_is_named_and_the_rest_are_imported(
    library, camera, root, folder, mounts
):
    reading, job = queue_all(library, camera, folder, mounts)
    FakeCamera.fail_reading(root, "IMG_0003.JPG")

    state = importing.run(library, job, mounts, make_camera=camera)

    assert state == dbm.DONE
    assert library.job(job).message == "Imported 3 photos, 1 could not be copied"
    failures = library.failures(job)
    assert [os.path.basename(path) for path, _why in failures] == ["IMG_0003.JPG"]
    assert [i.state for i in library.import_items(job)].count(dbm.NOT_COPIED) == 1


def test_a_photo_the_camera_sends_cut_short_is_not_copied(library, camera, root, folder, mounts):
    FakeCamera.put(
        root, "IMG_0010.JPG", jpeg_at(datetime(2026, 10, 17, 1, 0, 0), padding=9_000_000)
    )
    reading, job = queue_all(library, camera, folder, mounts, layout="none")
    FakeCamera.short_reading(root, "IMG_0010.JPG")

    importing.run(library, job, mounts, make_camera=camera)

    assert not os.path.exists(os.path.join(folder, "IMG_0010.JPG"))
    assert [os.path.basename(p) for p, _why in library.failures(job)] == ["IMG_0010.JPG"]
    assert not [n for d, _f, files in os.walk(folder) for n in files if n.endswith(".part")]


def test_a_photo_that_is_not_the_one_read_is_not_copied(library, camera, root, folder, mounts):
    reading, job = queue_all(library, camera, folder, mounts, layout="none")
    path = root / "card" / "DCIM" / "100CANON" / "IMG_0001.JPG"
    path.write_bytes(path.read_bytes() + b"more")

    importing.run(library, job, mounts, make_camera=camera)

    assert [os.path.basename(p) for p, _why in library.failures(job)] == ["IMG_0001.JPG"]
    assert not os.path.exists(os.path.join(folder, "IMG_0001.JPG"))


def test_a_file_already_there_is_never_written_over(library, camera, root, folder, mounts):
    reading, job = queue_all(library, camera, folder, mounts, layout="none")
    with open(os.path.join(folder, "IMG_0001.JPG"), "wb") as file:
        file.write(b"the user's own file")

    importing.run(library, job, mounts, make_camera=camera)

    assert open(os.path.join(folder, "IMG_0001.JPG"), "rb").read() == b"the user's own file"
    original = root / "card" / "DCIM" / "100CANON" / "IMG_0001.JPG"
    assert open(os.path.join(folder, "IMG_0001-2.JPG"), "rb").read() == original.read_bytes()


def test_unplugged_in_the_middle_the_job_waits_and_goes_on_from_the_next_photo(
    library, camera, root, folder, mounts
):
    FakeCamera.put(
        root, "IMG_0002.JPG", jpeg_at(datetime(2026, 10, 15, 8, 0, 1), padding=9_000_000)
    )
    reading, job = queue_all(library, camera, folder, mounts, layout="none")
    FakeCamera.unplug_while_reading(root)

    with pytest.raises(NotConnected):
        importing.run(library, job, mounts, make_camera=camera)

    job_row = library.job(job)
    assert job_row.state != dbm.DONE
    states = [item.state for item in library.import_items(job)]
    assert states.count(dbm.COPIED) == 1 and states.count(dbm.TO_COPY) == 3
    assert not [n for d, _f, files in os.walk(folder) for n in files if n.endswith(".part")]

    FakeCamera.unplug(root, False)
    state = importing.run(library, job, mounts, make_camera=camera)

    assert state == dbm.DONE
    assert library.job(job).message == "Imported 4 photos"
    assert sorted(os.listdir(folder)) == [
        "IMG_0001.JPG",
        "IMG_0002.JPG",
        "IMG_0003.JPG",
        "IMG_0004.JPG",
    ]


def test_a_job_waits_while_the_camera_is_not_there(library, camera, root, folder, mounts):
    reading, job = queue_all(library, camera, folder, mounts)
    FakeCamera.unplug(root)

    with pytest.raises(NotConnected):
        importing.run(library, job, mounts, make_camera=camera)

    assert all(item.state == dbm.TO_COPY for item in library.import_items(job))


def test_another_camera_is_not_the_one_the_photos_are_on(library, camera, root, folder, mounts):
    reading, job = queue_all(library, camera, folder, mounts)

    class Other(FakeCamera):
        def open(self):
            info = super().open()
            return tether.CameraInfo(info.model, "OTHER-2", info.port)

    with pytest.raises(NotConnected, match="photos are on"):
        importing.run(library, job, mounts, make_camera=lambda: Other(root))


def test_cancel_stops_before_reporting_cancelled_and_removes_the_part(
    library, camera, root, folder, mounts
):
    reading, job = queue_all(library, camera, folder, mounts)
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 3

    state = importing.run(library, job, mounts, should_stop=stop, make_camera=camera)

    assert state == dbm.CANCELLED
    assert not [n for d, _f, files in os.walk(folder) for n in files if n.endswith(".part")]


def test_raw_photos_are_listed_and_dated(library, root, camera):
    make_dng(root / "card" / "DCIM" / "100CANON" / "IMG_0007.DNG", preview="jpeg")

    reading = importing.read_camera(library, camera)

    assert any(c.path.endswith("IMG_0007.DNG") for c in reading.candidates)


def test_the_camera_can_be_closed_after_a_read(library, root):
    opened = []

    class Tracked(FakeCamera):
        def close(self):
            opened.append("closed")
            super().close()

    importing.read_camera(library, lambda: Tracked(root))

    assert opened == ["closed"]
