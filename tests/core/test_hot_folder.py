"""A tethered folder (#57): new photos taken in as each is complete, without a walk."""

import io
import os
import queue
import threading
import time

import pytest
from fakes import FakeExifTool
from photos import make_jpeg

from lumina.core import db as dbm
from lumina.core import hot_folder
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
    path = tmp_path / "drive"
    (path / "Studio").mkdir(parents=True)
    make_jpeg(path / "Studio" / "old.jpg")
    return path


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


@pytest.fixture
def studio(library, drive):
    """The folder Studio, scanned, as (folder, volume id, where it is)."""
    mounts = [Mount(DRIVE, str(drive), "/", "exfat", "/dev/sdz1", "Photos drive")]
    key, inside = locate(drive / "Studio", mounts)
    folder_id = library.add_folder(key, inside, "Photos drive", str(drive))
    job = library.add_job(dbm.SCAN, folder_id)
    assert scanning.scan(library, job, folder_id, mounts, exiftool=FakeExifTool) == dbm.DONE
    folder = library.folder(folder_id)
    return folder, library.volume_id(key), str(drive / "Studio")


def jpeg_bytes(colour=(5, 5, 5)):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 32), colour).save(out, "JPEG")
    return out.getvalue()


def paths(library, folder):
    return set(library.photo_states(folder.id))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# Settling


def test_a_file_is_complete_once_its_size_and_time_have_held_still(tmp_path):
    clock = Clock()
    path = tmp_path / "shot.jpg"
    path.write_bytes(b"x" * 10)
    settling = hot_folder.Settling(settle=0.5, clock=clock)
    info = os.stat(path)
    settling.add(str(path), info.st_size, info.st_mtime_ns)

    clock.now = 0.4
    assert settling.check() == []
    # It grew: the wait starts again
    with open(path, "ab") as file:
        file.write(b"y" * 10)
    clock.now = 0.6
    assert settling.check() == []
    clock.now = 1.0
    assert settling.check() == []
    clock.now = 1.1
    info = os.stat(path)
    assert settling.check() == [(str(path), 20, info.st_mtime_ns)]
    assert len(settling) == 0


def test_an_empty_file_is_never_complete_and_one_that_went_is_let_go_of(tmp_path):
    clock = Clock()
    empty, gone = tmp_path / "empty.jpg", tmp_path / "gone.jpg"
    empty.write_bytes(b"")
    gone.write_bytes(b"x")
    settling = hot_folder.Settling(settle=0.5, clock=clock)
    for path in (empty, gone):
        info = os.stat(path)
        settling.add(str(path), info.st_size, info.st_mtime_ns)
    gone.unlink()

    clock.now = 5.0
    assert settling.check() == []
    assert len(settling) == 1


# Which files are new


def test_new_files_are_the_photos_the_library_has_not_got_as_they_are(library, studio):
    folder, volume, base = studio
    make_jpeg(os.path.join(base, "new.jpg"))
    make_jpeg(os.path.join(base, ".hidden.jpg"))
    with open(os.path.join(base, "notes.txt"), "w") as file:
        file.write("not a photo")
    os.mkdir(os.path.join(base, "sub.jpg"))

    found = scanning.new_files(library, folder, volume, base, base)

    assert [os.path.basename(path) for path, _size, _mtime in found] == ["new.jpg"]
    # One the library has, written again
    old = os.path.join(base, "old.jpg")
    with open(old, "ab") as file:
        file.write(b"more")
    found = scanning.new_files(library, folder, volume, base, base)
    assert {os.path.basename(path) for path, _size, _mtime in found} == {"new.jpg", "old.jpg"}


def test_taking_in_writes_the_row_and_gives_a_new_photo_the_preset(library, studio):
    folder, volume, base = studio
    path = os.path.join(base, "new.jpg")
    make_jpeg(path)
    info = os.stat(path)
    old = library.photo_state(volume, "Studio/old.jpg")[0]

    added = scanning.take_in(
        library, folder, volume, base, [(path, info.st_size, info.st_mtime_ns)], '{"preset": 1}'
    )

    ((photo, written),) = added
    assert written == path
    assert library.photo_state(volume, "Studio/new.jpg")[0] == photo
    assert library.edit(photo) == '{"preset": 1}'
    # Not the photo that was there
    assert library.edit(old) is None
    # Taken in again as it is: nothing to write
    assert (
        scanning.take_in(
            library, folder, volume, base, [(path, info.st_size, info.st_mtime_ns)], '{"preset": 1}'
        )
        == []
    )


def test_a_photo_with_an_edit_already_keeps_it(library, studio):
    folder, volume, base = studio
    old = library.photo_state(volume, "Studio/old.jpg")[0]
    library.save_edit(old, '{"mine": 1}')
    path = os.path.join(base, "old.jpg")
    with open(path, "ab") as file:
        file.write(b"more")
    info = os.stat(path)

    scanning.take_in(
        library, folder, volume, base, [(path, info.st_size, info.st_mtime_ns)], '{"preset": 1}'
    )

    assert library.edit(old) == '{"mine": 1}'
    assert library.photo(old).content_id is None


def test_a_scan_that_finds_a_photo_already_taken_in_leaves_it_as_it_is(library, studio):
    folder, volume, base = studio
    path = os.path.join(base, "new.jpg")
    make_jpeg(path)
    info = os.stat(path)
    ((photo, _path),) = scanning.take_in(
        library, folder, volume, base, [(path, info.st_size, info.st_mtime_ns)]
    )

    # A walk that started before it was taken in, and found it as new
    row = ("Studio/new.jpg", info.st_size, info.st_mtime_ns, dbm.local_time(info.st_mtime_ns))
    library.write_scan(folder.id, volume, new=[row])

    assert library.photo_state(volume, "Studio/new.jpg")[0] == photo


# A pass


def run_pass(library, studio, changes, stop, **kwargs):
    folder, volume, base = studio
    added, read = [], []
    hot_folder.run(
        library,
        folder,
        volume,
        base,
        changes,
        stop.is_set,
        added.append,
        lambda photos, failures: read.append((photos, failures)),
        **{"exiftool": FakeExifTool, "settle": 0.3, "poll": 0.02, "linger": 0.2, **kwargs},
    )
    return added, read


def test_a_file_written_in_pieces_is_read_once_it_is_complete(library, studio, monkeypatch):
    folder, _volume, base = studio
    path = os.path.join(base, "IMG_0001.jpg")
    data = jpeg_bytes()
    changes, stop = queue.SimpleQueue(), threading.Event()
    sizes = []

    def write():
        with open(path, "wb") as file:
            for start in range(0, len(data), 200):
                file.write(data[start : start + 200])
                file.flush()
                changes.put(base)
                time.sleep(0.1)

    writer = threading.Thread(target=write)
    writer.start()
    changes.put(base)
    real_take_in = scanning.take_in

    def take_in(library, folder, volume, base, files, edit_json=None):
        sizes.extend(size for _path, size, _mtime in files)
        return real_take_in(library, folder, volume, base, files, edit_json)

    monkeypatch.setattr(scanning, "take_in", take_in)
    try:
        added, read = run_pass(library, studio, changes, stop, linger=1.0)
    finally:
        writer.join()

    assert sizes == [len(data)]
    (((photo, written),),) = added
    assert written == path
    assert read == [([(photo, path)], [])]
    stored = library.photo(photo)
    assert stored.content_id and stored.model == "Canon EOS 90D"
    assert stored.size == len(data)
    assert "Studio/IMG_0001.jpg" in paths(library, folder)


def test_a_pass_ends_when_stopped_and_reads_nothing_after(library, studio):
    _folder, _volume, base = studio
    changes, stop = queue.SimpleQueue(), threading.Event()
    make_jpeg(os.path.join(base, "late.jpg"))
    changes.put(base)
    stop.set()

    added, read = run_pass(library, studio, changes, stop, linger=60)

    assert added == [] and read == []


def test_one_exiftool_is_kept_for_a_pass_and_closed_at_its_end(library, studio):
    _folder, _volume, base = studio
    made = []

    class Tool(FakeExifTool):
        def __init__(self):
            super().__init__()
            self.closed = False
            made.append(self)

        def close(self):
            self.closed = True

    changes, stop = queue.SimpleQueue(), threading.Event()
    make_jpeg(os.path.join(base, "one.jpg"))
    changes.put(base)

    def second(_photos, _failures):
        if not os.path.exists(os.path.join(base, "two.jpg")):
            make_jpeg(os.path.join(base, "two.jpg"), colour=(1, 2, 3))
            changes.put(base)

    folder, volume, base = studio
    added = []
    hot_folder.run(
        library, folder, volume, base, changes, stop.is_set, added.append, second,
        exiftool=Tool, settle=0.1, poll=0.02, linger=0.5,
    )  # fmt: skip

    assert len(added) == 2
    assert len(made) == 1 and made[0].closed


def test_without_exiftool_a_photo_is_taken_in_and_its_details_left_for_the_scan(library, studio):
    folder, volume, base = studio

    def missing():
        raise MetadataUnavailable("exiftool is not installed")

    changes, stop = queue.SimpleQueue(), threading.Event()
    make_jpeg(os.path.join(base, "new.jpg"))
    changes.put(base)

    added, read = run_pass(library, studio, changes, stop, exiftool=missing)

    (((photo, _path),),) = added
    assert read == [([(photo, os.path.join(base, "new.jpg"))], [])]
    assert library.photo(photo).metadata_state == 0
    assert library.pending_metadata(folder.id, 10) == [(photo, "Studio/new.jpg")]


def test_a_photo_that_cannot_be_read_is_named(library, studio):
    _folder, _volume, base = studio
    path = os.path.join(base, "broken.jpg")
    with open(path, "wb") as file:
        file.write(b"not a jpeg at all")
    changes, stop = queue.SimpleQueue(), threading.Event()
    changes.put(base)

    added, read = run_pass(
        library, studio, changes, stop, exiftool=lambda: FakeExifTool(broken=[path])
    )

    (((photo, _path),),) = added
    assert read == [([(photo, path)], [(path, "File format error")])]


def test_a_file_still_growing_does_not_hold_up_one_that_is_complete(tmp_path):
    clock = Clock()
    done, growing = tmp_path / "done.jpg", tmp_path / "growing.jpg"
    done.write_bytes(b"x")
    growing.write_bytes(b"x")
    settling = hot_folder.Settling(settle=0.5, clock=clock)
    for path in (done, growing):
        info = os.stat(path)
        settling.add(str(path), info.st_size, info.st_mtime_ns)

    clock.now = 0.3
    growing.write_bytes(b"xx")
    settling.check()
    clock.now = 0.6

    assert [path for path, _size, _mtime in settling.check()] == [str(done)]
