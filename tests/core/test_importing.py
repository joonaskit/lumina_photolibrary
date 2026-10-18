"""Importing from a card: reading it, and the job that copies its photos into the library."""

import hashlib
import os
from datetime import datetime

import pytest
from fakes import FakeExifTool
from photos import make_jpeg, make_jpeg_with_exif
from rawfiles import make_dng

from lumina.core import db as dbm
from lumina.core import importing
from lumina.core.db import Library
from lumina.core.errors import NotConnected
from lumina.core.metadata import Metadata
from lumina.core.volumes import Mount

CARD, DRIVE = "uuid:CARD", "uuid:DRIVE"


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeExifTool.reset()


class DatedExifTool(FakeExifTool):
    """Taken on the day its name says: IMG_0001.JPG on 14 October, the rest on the 15th."""

    def _one(self, path):
        day = 14 if path.endswith("IMG_0001.JPG") else 15
        return Metadata(captured=datetime(2026, 10, day, 9, 30, 5))


@pytest.fixture
def card(tmp_path):
    """A memory card as a camera writes it: DCIM/100CANON, and something that is not a
    photo beside them."""
    root = tmp_path / "card"
    shots = root / "DCIM" / "100CANON"
    shots.mkdir(parents=True)
    make_jpeg(shots / "IMG_0001.JPG", colour=(200, 10, 10))
    make_jpeg(shots / "IMG_0002.JPG", colour=(10, 200, 10))
    make_dng(shots / "IMG_0003.DNG", preview="jpeg")
    (root / "MISC").mkdir()
    (root / "MISC" / "notes.txt").write_text("not a photo")
    return root


@pytest.fixture
def drive(tmp_path):
    path = tmp_path / "drive"
    (path / "Photos").mkdir(parents=True)
    return path


@pytest.fixture
def mounts(card, drive, tmp_path):
    (tmp_path / "backup").mkdir()
    return [
        Mount(CARD, str(card), "/", "vfat", "/dev/mmcblk0p1", "EOS_DIGITAL"),
        Mount(DRIVE, str(drive), "/", "ext4", "/dev/sdz1", "Photos drive"),
        Mount("uuid:BACKUP", str(tmp_path / "backup"), "/", "ext4", "/dev/sdy1", "Backup"),
    ]


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


@pytest.fixture
def folder(library, drive):
    """Where the photos go: the library folder Photos."""
    library.add_folder(DRIVE, "Photos", "Photos drive")
    return str(drive / "Photos")


def snapshot(root):
    """Every file under `root` with its bytes' hash and its time: to see nothing changed."""
    found = {}
    for directory, _folders, files in os.walk(root):
        for name in files:
            path = os.path.join(directory, name)
            with open(path, "rb") as file:
                found[path] = (hashlib.sha256(file.read()).hexdigest(), os.stat(path).st_mtime_ns)
    return found


def read(library, card, tool=DatedExifTool):
    return importing.read_source(library, str(card), exiftool=tool)


def import_all(library, card, folder, mounts, **choices):
    reading = read(library, card)
    job = importing.queue(library, reading.candidates, folder, mounts, **choices)
    return job, importing.run(library, job, mounts)


def test_cards_are_the_mounts_with_a_dcim_folder(mounts, card):
    assert importing.find_cards(mounts) == [str(card)]


def test_reading_a_card_finds_its_photos_with_the_times_they_were_taken(library, card):
    reading = read(library, card)

    names = {os.path.basename(c.path): c for c in reading.candidates}
    assert set(names) == {"IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.DNG"}
    assert names["IMG_0001.JPG"].taken == datetime(2026, 10, 14, 9, 30, 5)
    assert names["IMG_0002.JPG"].taken == datetime(2026, 10, 15, 9, 30, 5)
    assert all(c.content_id and c.size > 0 and not c.imported for c in reading.candidates)
    assert reading.failures == ()


def test_a_photo_that_tells_its_time_in_its_first_bytes_is_not_read_by_exiftool(library, tmp_path):
    """exiftool reads a CR3 to its end, a second a photo from a card: only photos whose
    first bytes don't tell their time go to it."""
    shots = tmp_path / "card" / "DCIM" / "100CANON"
    shots.mkdir(parents=True)
    make_jpeg_with_exif(shots / "IMG_0001.JPG")
    make_jpeg(shots / "IMG_0002.JPG")

    reading = read(library, tmp_path / "card")

    taken = {os.path.basename(c.path): c.taken for c in reading.candidates}
    assert taken == {
        "IMG_0001.JPG": datetime(2024, 6, 1, 18, 30, 5, 250000),
        "IMG_0002.JPG": datetime(2026, 10, 15, 9, 30, 5),
    }
    assert FakeExifTool.calls == [[str(shots / "IMG_0002.JPG")]]


def test_without_exiftool_the_files_times_are_used(library, card):
    def missing():
        from lumina.core.metadata import MetadataUnavailable

        raise MetadataUnavailable("exiftool is not installed")

    path = card / "DCIM" / "100CANON" / "IMG_0001.JPG"
    os.utime(path, (datetime(2025, 1, 2, 3, 4, 5).timestamp(),) * 2)

    reading = read(library, card, tool=missing)

    taken = {os.path.basename(c.path): c.taken for c in reading.candidates}
    assert taken["IMG_0001.JPG"] == datetime(2025, 1, 2, 3, 4, 5)


def test_an_import_copies_the_photos_into_folders_by_day_and_leaves_the_card_alone(
    library, card, drive, folder, mounts
):
    before = snapshot(card)

    job, state = import_all(library, card, folder, mounts)

    assert state == dbm.DONE
    assert library.job(job).message == "Imported 3 photos"
    assert snapshot(card) == before
    photos = drive / "Photos"
    assert sorted(p.relative_to(photos).as_posix() for p in photos.rglob("*") if p.is_file()) == [
        "2026/2026-10-14/IMG_0001.JPG",
        "2026/2026-10-15/IMG_0002.JPG",
        "2026/2026-10-15/IMG_0003.DNG",
    ]
    original = card / "DCIM" / "100CANON" / "IMG_0002.JPG"
    copy = photos / "2026" / "2026-10-15" / "IMG_0002.JPG"
    assert copy.read_bytes() == original.read_bytes()
    # The file's time is the card's, which a scan takes for a photo without a capture time
    assert copy.stat().st_mtime_ns == original.stat().st_mtime_ns
    items = library.import_items(job)
    assert {item.state for item in items} == {dbm.COPIED}
    assert items[1].path == "Photos/2026/2026-10-15/IMG_0002.JPG"


def test_photos_imported_before_are_known_when_the_card_is_read_again(
    library, card, folder, mounts
):
    import_all(library, card, folder, mounts)
    (card / "DCIM" / "100CANON" / "IMG_0004.JPG").write_bytes(b"")
    make_jpeg(card / "DCIM" / "100CANON" / "IMG_0004.JPG", colour=(1, 2, 3))

    reading = read(library, card)

    imported = {os.path.basename(c.path): c.imported for c in reading.candidates}
    assert imported == {
        "IMG_0001.JPG": True,
        "IMG_0002.JPG": True,
        "IMG_0003.DNG": True,
        "IMG_0004.JPG": False,
    }


def test_a_photo_already_in_the_library_is_known_as_imported(library, card, drive, folder):
    [photos] = library.folders()
    library.write_scan(photos.id, library.volume_id(DRIVE), [("Photos/x.jpg", 1, 1, "2026")])
    [(photo, _path)] = library.pending_metadata(photos.id, 10)
    original = card / "DCIM" / "100CANON" / "IMG_0002.JPG"
    from lumina.core.fingerprint import content_id

    library.write_metadata([(photo, content_id(original), Metadata())])

    reading = read(library, card)

    assert [os.path.basename(c.path) for c in reading.candidates if c.imported] == ["IMG_0002.JPG"]


def test_names_can_start_with_the_time_and_the_layout_can_be_flat(
    library, card, drive, folder, mounts
):
    import_all(library, card, folder, mounts, layout="none", naming="date")

    assert sorted(p.name for p in (drive / "Photos").iterdir()) == [
        "20261014-093005-IMG_0001.JPG",
        "20261015-093005-IMG_0002.JPG",
        "20261015-093005-IMG_0003.DNG",
    ]


def test_a_file_with_the_name_already_there_is_never_written_over(
    library, card, drive, folder, mounts
):
    reading = read(library, card)
    job = importing.queue(library, reading.candidates, folder, mounts, layout="none")
    # Appears after the import was planned: found when the copy is put in place
    other = drive / "Photos" / "IMG_0001.JPG"
    other.write_bytes(b"someone else's photo")

    assert importing.run(library, job, mounts) == dbm.DONE

    assert other.read_bytes() == b"someone else's photo"
    copy = drive / "Photos" / "IMG_0001-2.JPG"
    assert copy.read_bytes() == (card / "DCIM" / "100CANON" / "IMG_0001.JPG").read_bytes()
    assert library.import_items(job)[0].path == "Photos/IMG_0001-2.JPG"


def test_a_copy_already_there_is_taken_as_the_copy_not_made_again(
    library, card, drive, folder, mounts
):
    first, _state = import_all(library, card, folder, mounts, layout="none")
    # The same photos again, as when the job was stopped by a crash just after a copy
    # was put in place, before the database heard of it
    reading = read(library, card)
    job = importing.queue(library, reading.candidates, folder, mounts, layout="none")

    assert importing.run(library, job, mounts) == dbm.DONE

    assert sorted(p.name for p in (drive / "Photos").iterdir()) == [
        "IMG_0001.JPG",
        "IMG_0002.JPG",
        "IMG_0003.DNG",
    ]
    assert [item.path for item in library.import_items(job)] == [
        item.path for item in library.import_items(first)
    ]


def test_a_backup_copy_goes_to_the_second_folder_laid_out_the_same(
    library, card, drive, folder, mounts, tmp_path
):
    backup = tmp_path / "backup" / "Card copies"

    import_all(library, card, folder, mounts, backup=str(backup))

    copy = backup / "2026" / "2026-10-14" / "IMG_0001.JPG"
    assert copy.read_bytes() == (card / "DCIM" / "100CANON" / "IMG_0001.JPG").read_bytes()
    assert len([p for p in backup.rglob("*") if p.is_file()]) == 3


def test_a_copy_that_reads_back_wrong_is_named_and_the_rest_are_copied(
    library, card, drive, folder, mounts, monkeypatch
):
    real = importing._hash

    def flaky(path, drop_cache=False):
        # The second photo's part file reads back other than it was written
        return "different" if path.endswith("-2.part") else real(path, drop_cache)

    monkeypatch.setattr(importing, "_hash", flaky)

    job, state = import_all(library, card, folder, mounts, layout="none")

    assert state == dbm.DONE
    record = library.job(job)
    assert record.message == "Imported 2 photos, 1 could not be copied"
    [(path, message)] = library.failures(job)
    assert path.endswith("IMG_0002.JPG")
    assert "not the same" in message
    left = sorted(p.name for p in (drive / "Photos").iterdir())
    assert left == ["IMG_0001.JPG", "IMG_0003.DNG"]
    assert [item.state for item in library.import_items(job)] == [
        dbm.COPIED,
        dbm.NOT_COPIED,
        dbm.COPIED,
    ]


def test_a_cancel_stops_leaving_no_part_file_and_the_job_resumes_without_copying_twice(
    library, card, drive, folder, mounts
):
    reading = read(library, card)
    job = importing.queue(library, reading.candidates, folder, mounts, layout="none")
    asked = []

    def stop_in_the_second():
        asked.append(1)
        # The first photo's chunks and the check before the second photo
        return len(asked) > 3

    assert importing.run(library, job, mounts, should_stop=stop_in_the_second) == dbm.CANCELLED
    assert [p.name for p in (drive / "Photos").iterdir() if p.name.startswith(".")] == []
    copied = [item for item in library.import_items(job) if item.state == dbm.COPIED]
    assert 1 <= len(copied) < 3

    library.update_job(job, state=dbm.QUEUED)
    assert importing.run(library, job, mounts) == dbm.DONE
    assert sorted(p.name for p in (drive / "Photos").iterdir()) == [
        "IMG_0001.JPG",
        "IMG_0002.JPG",
        "IMG_0003.DNG",
    ]


def test_a_card_that_is_not_mounted_leaves_the_job_waiting(library, card, folder, mounts):
    reading = read(library, card)
    job = importing.queue(library, reading.candidates, folder, mounts)

    with pytest.raises(NotConnected):
        importing.run(library, job, mounts[1:])

    assert library.job(job).state == dbm.QUEUED


def test_a_card_pulled_in_the_middle_leaves_the_rest_to_copy_when_it_is_back(
    library, card, drive, folder, mounts, monkeypatch
):
    reading = read(library, card)
    job = importing.queue(library, reading.candidates, folder, mounts, layout="none")
    real = importing.copy_file
    calls = []

    def pulled(source, part, should_stop):
        calls.append(source)
        if len(calls) == 2:
            raise OSError(5, "Input/output error", source)
        return real(source, part, should_stop)

    monkeypatch.setattr(importing, "copy_file", pulled)

    with pytest.raises(NotConnected):
        importing.run(library, job, mounts, read_mounts=lambda: mounts[1:])

    assert [item.state for item in library.import_items(job)] == [
        dbm.COPIED,
        dbm.TO_COPY,
        dbm.TO_COPY,
    ]
    assert library.failures(job) == []

    monkeypatch.undo()
    assert importing.run(library, job, mounts) == dbm.DONE
    assert [item.state for item in library.import_items(job)] == [dbm.COPIED] * 3


def test_a_failed_backup_copy_does_not_make_the_copy_in_the_library_a_failure(
    library, card, drive, folder, mounts, tmp_path, monkeypatch
):
    real = importing.copy_file

    def full_backup(source, part, should_stop):
        if os.sep + "backup" + os.sep in part:
            raise OSError(28, "No space left on device", part)
        return real(source, part, should_stop)

    monkeypatch.setattr(importing, "copy_file", full_backup)

    job, state = import_all(
        library, card, folder, mounts, layout="none", backup=str(tmp_path / "backup" / "b")
    )

    assert state == dbm.DONE
    assert [item.state for item in library.import_items(job)] == [dbm.COPIED] * 3
    assert sorted(p.name for p in (drive / "Photos").iterdir()) == [
        "IMG_0001.JPG",
        "IMG_0002.JPG",
        "IMG_0003.DNG",
    ]
    failures = library.failures(job)
    assert len(failures) == 3
    assert all("not backed up" in message for _path, message in failures)


def test_without_the_space_nothing_is_copied(library, card, drive, folder, mounts, monkeypatch):
    class Usage:
        free = 10

    monkeypatch.setattr(importing.shutil, "disk_usage", lambda _path: Usage)

    job, state = import_all(library, card, folder, mounts)

    assert state == dbm.FAILED_JOB
    assert library.job(job).message.startswith(f"Not enough space in {drive / 'Photos'}")
    assert list((drive / "Photos").iterdir()) == []


def test_a_card_that_cannot_be_written_is_read_and_copied(library, card, drive, folder, mounts):
    shots = card / "DCIM" / "100CANON"
    for path in shots.iterdir():
        path.chmod(0o444)
    shots.chmod(0o555)
    try:
        _job, state = import_all(library, card, folder, mounts)
    finally:
        shots.chmod(0o755)

    assert state == dbm.DONE
    assert len([p for p in (drive / "Photos").rglob("*") if p.is_file()]) == 3


def test_copy_file_names_the_photo_when_it_cannot_be_read(tmp_path):
    with pytest.raises(OSError):
        importing.copy_file(str(tmp_path / "gone.CR3"), str(tmp_path / "x.part"))


def test_an_import_needs_photos_and_a_connected_folder(library, card, folder, mounts):
    reading = read(library, card)

    with pytest.raises(ValueError, match="No photos"):
        importing.queue(library, (), folder, mounts)
    with pytest.raises(ValueError, match="No mounted filesystem holds"):
        importing.queue(library, reading.candidates, folder, mounts[:1])
    with pytest.raises(ValueError, match="is not in the library"):
        importing.queue(library, reading.candidates, str(card), mounts)
    with pytest.raises(ValueError, match="a folder of their own"):
        importing.queue(library, reading.candidates, folder, mounts, backup=folder)


def test_photos_can_go_to_a_folder_inside_a_library_folder(library, card, drive, folder, mounts):
    reading = read(library, card)
    inside = drive / "Photos" / "Cards"

    job = importing.queue(library, reading.candidates, str(inside), mounts, layout="none")

    assert library.job(job).folder_id == library.folders()[0].id
    assert importing.run(library, job, mounts) == dbm.DONE
    assert sorted(p.name for p in inside.iterdir()) == [
        "IMG_0001.JPG",
        "IMG_0002.JPG",
        "IMG_0003.DNG",
    ]
