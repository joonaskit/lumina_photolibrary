import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from photos import make_jpeg, make_jpeg_with_exif
from rawfiles import make_dng

from lumina.core import metadata
from lumina.core.errors import PhotoError
from lumina.core.metadata import ExifTool, MetadataUnavailable, parse_date

# Parsing dates: no exiftool needed


def test_a_date_with_sub_seconds_and_offset():
    moment = parse_date("2024:06:01 18:30:05.25+03:00")

    assert moment == datetime(2024, 6, 1, 18, 30, 5, 250000, tzinfo=timezone(timedelta(hours=3)))


def test_the_offset_tag_is_used_when_the_date_has_none():
    assert parse_date("2024:06:01 18:30:05", "-05:00").utcoffset() == timedelta(hours=-5)
    assert parse_date("2024:06:01 18:30:05Z").tzinfo == timezone.utc


def test_a_date_without_an_offset_is_naive():
    assert parse_date("2024:06:01 18:30:05") == datetime(2024, 6, 1, 18, 30, 5)


@pytest.mark.parametrize("value", [None, "", "0000:00:00 00:00:00", "    :  :     :  :  ", 7])
def test_no_date_or_an_unset_clock_is_none(value):
    assert parse_date(value) is None


def test_a_missing_exiftool_is_reported(monkeypatch):
    monkeypatch.setattr(metadata.shutil, "which", lambda _name: None)

    with pytest.raises(MetadataUnavailable):
        ExifTool()


# Reading files: needs exiftool


def test_camera_metadata_is_read(exiftool, tmp_path):
    path = make_jpeg_with_exif(tmp_path / "a.jpg", orientation=6)

    data = exiftool.read(path)

    three = timezone(timedelta(hours=3))
    assert data.captured == datetime(2024, 6, 1, 18, 30, 5, 250000, tzinfo=three)
    assert (data.make, data.model) == ("Canon", "Canon EOS R6m2")
    assert data.lens == "RF24-105mm F4 L IS USM"
    assert data.focal_length == 50
    assert data.aperture == pytest.approx(2.8)
    assert data.exposure_time == pytest.approx(1 / 250)
    assert data.iso == 400
    assert data.orientation == 6
    # South and east: a negative latitude, a positive longitude
    assert data.latitude == pytest.approx(-(33 + 51 / 60 + 54 / 3600), abs=1e-6)
    assert data.longitude == pytest.approx(151 + 12 / 60 + 36 / 3600, abs=1e-6)


def test_a_photo_without_metadata_has_none(exiftool, tmp_path):
    data = exiftool.read(make_jpeg(tmp_path / "plain.jpg"))

    assert data.captured is None and data.make is None and data.latitude is None


def test_raw_files_are_read(exiftool, tmp_path):
    data = exiftool.read(make_dng(tmp_path / "a.dng", preview="jpeg", orientation=6))

    assert (data.make, data.model) == ("Lumina", "Test Camera")
    assert data.orientation == 6


def test_a_batch_reports_each_file_on_its_own(exiftool, tmp_path):
    good = make_jpeg_with_exif(tmp_path / "good.jpg")
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    missing = tmp_path / "missing.jpg"

    results = exiftool.read_many([good, empty, missing])

    assert results[str(good)].model == "Canon EOS R6m2"
    assert results[str(good)].file_type == "JPEG"
    for path in (empty, missing):
        assert isinstance(results[str(path)], PhotoError)
        assert results[str(path)].path == str(path)
    assert results[str(empty)].message == "File is empty"


def test_a_file_that_is_no_photo_says_what_it_is(exiftool, tmp_path):
    # Not an error to exiftool: telling a bad photo apart is the decoder's job
    path = tmp_path / "notes.jpg"
    path.write_text("not a photo")

    assert exiftool.read(path).file_type == "TXT"


def test_a_damaged_photo_has_a_warning(exiftool, tmp_path):
    path = tmp_path / "cut.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0")

    assert exiftool.read(path).warning == "JPEG format error"


def test_an_unreadable_file_is_an_error_naming_it(exiftool, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    path = make_jpeg_with_exif(tmp_path / "locked.jpg")
    path.chmod(0)
    try:
        with pytest.raises(PhotoError) as raised:
            exiftool.read(path)
    finally:
        path.chmod(0o644)

    assert raised.value.path == str(path)


@pytest.mark.parametrize(
    "name", ["-starts-with-dash.jpg", "with space.jpg", "trailing space.jpg ", "ääkköset 写真.jpg"]
)
def test_awkward_file_names(exiftool, tmp_path, name):
    path = make_jpeg_with_exif(tmp_path / name)

    assert exiftool.read(path).model == "Canon EOS R6m2"


def test_a_name_with_a_newline(exiftool, tmp_path):
    path = make_jpeg_with_exif(tmp_path / "two\nlines.jpg")

    assert exiftool.read(path).model == "Canon EOS R6m2"


def test_relative_paths_come_back_as_given(exiftool, tmp_path, monkeypatch):
    make_jpeg_with_exif(tmp_path / "a.jpg")
    monkeypatch.chdir(tmp_path)

    assert exiftool.read_many(["a.jpg"])["a.jpg"].model == "Canon EOS R6m2"


def test_many_batches_through_one_process(exiftool, tmp_path):
    paths = [make_jpeg_with_exif(tmp_path / f"{n}.jpg") for n in range(20)]

    for start in range(0, 20, 5):
        results = exiftool.read_many(paths[start : start + 5])
        assert all(result.iso == 400 for result in results.values())


def test_a_dead_process_is_started_again(exiftool, tmp_path):
    path = make_jpeg_with_exif(tmp_path / "a.jpg")
    exiftool.read(path)
    exiftool._process.kill()
    exiftool._process.wait()

    assert exiftool.read(path).iso == 400


def test_reading_leaves_the_photo_as_it_was(exiftool, tmp_path):
    path = make_jpeg_with_exif(tmp_path / "original.jpg")
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), os.stat(path).st_mtime_ns)

    exiftool.read(path)

    assert (hashlib.sha256(path.read_bytes()).hexdigest(), os.stat(path).st_mtime_ns) == before
    # exiftool keeps "<name>_original" when it writes: there must be none
    assert sorted(p.name for p in tmp_path.iterdir()) == ["original.jpg"]


def test_a_gentle_exiftool_runs_at_a_lower_priority_also_after_a_restart(exiftool, tmp_path):
    import os

    path = make_jpeg(tmp_path / "plain.jpg")
    exiftool.read(path)
    before = os.getpriority(os.PRIO_PROCESS, exiftool._process.pid)

    exiftool.set_gentle(True)
    assert os.getpriority(os.PRIO_PROCESS, exiftool._process.pid) > before
    assert exiftool.read(path) is not None

    # A process started later is gentle from the start
    exiftool._kill()
    exiftool.read(path)
    assert os.getpriority(os.PRIO_PROCESS, exiftool._process.pid) > before
