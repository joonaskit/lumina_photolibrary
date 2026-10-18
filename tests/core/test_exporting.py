"""Exporting many photos as a job: the files, and the rules that keep the originals and
other files safe."""

import errno
import hashlib
import json
import os

import pytest
from fakes import FakeExifTool
from photos import make_jpeg
from PIL import Image
from rawfiles import make_dng

from lumina.core import db as dbm
from lumina.core import exporting
from lumina.core import library as scanning
from lumina.core.db import Library
from lumina.core.edit import export as exporter
from lumina.core.edit.params import Params
from lumina.core.volumes import Mount, locate

DRIVE = "uuid:EXPORT-DRIVE"


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeExifTool.reset()


@pytest.fixture
def drive(tmp_path):
    path = tmp_path / "drive"
    (path / "Photos").mkdir(parents=True)
    make_jpeg(path / "Photos" / "a.jpg", colour=(200, 100, 50), size=(64, 48))
    make_jpeg(path / "Photos" / "b.jpg", colour=(10, 200, 10), size=(64, 48))
    make_dng(path / "Photos" / "a.dng", preview="jpeg")
    return path


def mounted(path):
    return [Mount(DRIVE, str(path), "/", "exfat", "/dev/sdz1", "Photos drive")]


@pytest.fixture
def library(tmp_path, drive):
    with Library(tmp_path / "library.sqlite3") as opened:
        key, inside = locate(drive / "Photos", mounted(drive))
        folder = opened.add_folder(key, inside, "Photos drive", str(drive))
        job = opened.add_job(scanning.SCAN, folder)
        assert scanning.scan(opened, job, folder, mounted(drive), exiftool=FakeExifTool) == "done"
        yield opened


@pytest.fixture
def out(tmp_path):
    return tmp_path / "export"


def photo(library, name):
    return next(p.id for p in library.photos() if p.path.endswith("/" + name))


def snapshot(root):
    found = {}
    for directory, _folders, files in os.walk(root):
        for name in files:
            path = os.path.join(directory, name)
            with open(path, "rb") as file:
                found[path] = (hashlib.sha256(file.read()).hexdigest(), os.stat(path).st_mtime_ns)
    return found


def queued(library, drive, out, names=("a.jpg", "b.jpg"), replace=False, options=None):
    entries = [(photo(library, name), 0) for name in names]
    return exporting.queue(
        library,
        entries,
        str(out),
        options or exporter.ExportOptions(),
        replace,
        mounts=mounted(drive),
    )


def files(folder):
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_a_selection_is_exported_with_its_edits_and_the_originals_are_left_alone(
    library, drive, out
):
    library.save_edit(photo(library, "a.jpg"), Params(exposure=1.5).to_json())
    before = snapshot(drive)
    job = queued(library, drive, out)

    state = exporting.run(library, job, mounted(drive))

    assert state == dbm.DONE
    assert files(out) == ["a.jpg", "b.jpg"]
    assert library.job(job).message == "Exported 2 files"
    assert [i.state for i in library.export_items(job)] == [exporting.EXPORTED] * 2
    plain = Image.open(drive / "Photos" / "a.jpg").convert("RGB").getpixel((10, 10))
    brighter = Image.open(out / "a.jpg").convert("RGB").getpixel((10, 10))
    assert sum(brighter) > sum(plain)
    assert snapshot(drive) == before


def test_photos_with_one_name_get_numbers_and_a_virtual_copy_its_own(library, drive, out):
    copy = library.new_copy(photo(library, "a.jpg"), Params(exposure=1.0).to_json())
    entries = [(photo(library, "a.jpg"), 0), (photo(library, "a.dng"), 0)]
    entries.append((photo(library, "a.jpg"), copy))
    job = exporting.queue(
        library, entries, str(out), exporter.ExportOptions(), mounts=mounted(drive)
    )

    exporting.run(library, job, mounted(drive))

    assert files(out) == ["a-2.jpg", "a-copy1.jpg", "a.jpg"]


def test_an_export_into_the_photos_own_folder_never_writes_the_original(library, drive):
    before = snapshot(drive)
    job = queued(library, drive, drive / "Photos", names=("a.jpg",))

    exporting.run(library, job, mounted(drive))

    assert (drive / "Photos" / "a-edit.jpg").exists()
    assert {k: v for k, v in snapshot(drive).items() if not k.endswith("a-edit.jpg")} == before


def test_a_file_that_exists_is_kept_and_named_and_the_rest_are_exported(library, drive, out):
    out.mkdir()
    (out / "a.jpg").write_bytes(b"precious")
    job = queued(library, drive, out)

    state = exporting.run(library, job, mounted(drive))

    assert state == dbm.DONE
    assert (out / "a.jpg").read_bytes() == b"precious"
    assert (out / "b.jpg").exists()
    [(path, message)] = library.failures(job)
    assert path == str(out / "a.jpg") and "already exists" in message
    assert library.job(job).message == "Exported 1 file, 1 could not be exported"


def test_replace_replaces_what_the_user_chose_to_and_nothing_else(library, drive, out):
    out.mkdir()
    (out / "a.jpg").write_bytes(b"old")
    (out / "other.txt").write_bytes(b"mine")
    job = queued(library, drive, out, replace=True)

    exporting.run(library, job, mounted(drive))

    assert (out / "a.jpg").read_bytes() != b"old"
    assert (out / "other.txt").read_bytes() == b"mine"
    assert library.failures(job) == []


def test_a_file_that_appears_while_the_batch_runs_is_never_written_over(
    library, drive, out, monkeypatch
):
    real = exporter.pipeline.render

    def taken_meanwhile(*args, **kwargs):
        # Made by someone else after the check at the start and before the write
        (out / "b.jpg").write_bytes(b"theirs")
        return real(*args, **kwargs)

    job = queued(library, drive, out, names=("b.jpg",))
    monkeypatch.setattr(exporter.pipeline, "render", taken_meanwhile)

    state = exporting.run(library, job, mounted(drive))

    assert state == dbm.DONE
    assert (out / "b.jpg").read_bytes() == b"theirs"
    assert files(out) == ["b.jpg"]
    [(path, _message)] = library.failures(job)
    assert path == str(out / "b.jpg")


def test_a_cancel_stops_before_the_next_file_and_the_job_resumes_without_repeating(
    library, drive, out
):
    job = queued(library, drive, out)
    calls = []

    def first_only(source, params, destination, options, replace, should_stop):
        calls.append(destination)
        return exporting.render(source, params, destination, options, replace, should_stop)

    def stop():
        # After the first file is done
        return any(i.state == exporting.EXPORTED for i in library.export_items(job))

    assert exporting.run(library, job, mounted(drive), stop, render=first_only) == dbm.CANCELLED
    assert library.job(job).state == dbm.CANCELLED
    assert files(out) == ["a.jpg"]
    assert [p for p in out.iterdir() if p.name.startswith(".")] == []

    library.update_job(job, state=dbm.QUEUED)
    calls.clear()
    assert exporting.run(library, job, mounted(drive), render=first_only) == dbm.DONE
    assert [os.path.basename(c) for c in calls] == ["b.jpg"]
    assert files(out) == ["a.jpg", "b.jpg"]


def test_a_drive_that_is_not_connected_leaves_the_job_waiting(library, drive, out):
    job = queued(library, drive, out)

    with pytest.raises(scanning.NotConnected):
        exporting.run(library, job, [])

    assert files(out) == []
    assert [i.state for i in library.export_items(job)] == [exporting.TO_DO] * 2


def test_a_cancel_inside_a_file_leaves_no_part_file_and_nothing_marked_done(library, drive, out):
    job = queued(library, drive, out, names=("a.jpg",))

    state = exporting.run(library, job, mounted(drive), lambda: bool(out.exists()) and True)

    # The folder exists once the job has begun: stopped before the first file is written
    assert state == dbm.CANCELLED
    assert files(out) == []
    assert [i.state for i in library.export_items(job)] == [exporting.TO_DO]


def test_a_photo_gone_in_the_middle_waits_and_is_exported_when_it_is_back(library, drive, out):
    job = queued(library, drive, out)
    (drive / "Photos" / "b.jpg").rename(drive / "Photos" / "b.moved")

    with pytest.raises(scanning.NotConnected):
        exporting.run(library, job, mounted(drive))

    states = {os.path.basename(i.path): i.state for i in library.export_items(job)}
    assert states == {"a.jpg": exporting.EXPORTED, "b.jpg": exporting.TO_DO}
    assert library.failures(job) == []

    (drive / "Photos" / "b.moved").rename(drive / "Photos" / "b.jpg")
    assert exporting.run(library, job, mounted(drive)) == dbm.DONE
    assert files(out) == ["a.jpg", "b.jpg"]


def test_a_photo_that_cannot_be_exported_is_named_and_the_rest_go_on(library, drive, out):
    (drive / "Photos" / "a.jpg").write_bytes(b"not a photo")
    job = queued(library, drive, out)

    state = exporting.run(library, job, mounted(drive))

    assert state == dbm.DONE
    assert files(out) == ["b.jpg"]
    [(path, _message)] = library.failures(job)
    assert path.endswith("a.jpg")
    assert library.job(job).message == "Exported 1 file, 1 could not be exported"


def test_a_full_disk_is_a_failure_naming_the_file_not_a_done_job(library, drive, out, monkeypatch):
    job = queued(library, drive, out)

    def full(source, params, destination, options, replace, should_stop):
        if destination.endswith("a.jpg"):
            raise OSError(errno.ENOSPC, "No space left on device", destination)
        return exporting.render(source, params, destination, options, replace, should_stop)

    state = exporting.run(library, job, mounted(drive), render=full)

    assert state == dbm.DONE
    [(path, message)] = library.failures(job)
    assert path == str(out / "a.jpg") and "No space left" in message
    assert files(out) == ["b.jpg"]


def test_a_folder_that_cannot_be_made_fails_the_job_naming_it(library, drive, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file")
    job = queued(library, drive, blocker / "inside")

    state = exporting.run(library, job, mounted(drive))

    assert state == dbm.FAILED_JOB
    [(path, _message)] = library.failures(job)
    assert path == str(blocker / "inside")


def test_the_options_are_kept_with_the_job(library, drive, out):
    options = exporter.ExportOptions("png", 16, 90, 32)
    job = queued(library, drive, out, names=("b.jpg",), options=options)

    stored, replace, folder = library.export_job(job)

    assert exporter.ExportOptions(**json.loads(stored)) == options
    assert (replace, folder) == (False, str(out))
    exporting.run(library, job, mounted(drive))
    assert files(out) == ["b.png"]
    assert max(Image.open(out / "b.png").size) == 32
