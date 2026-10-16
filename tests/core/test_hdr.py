import hashlib
import json
import os
from datetime import datetime, timedelta

import numpy as np
import pytest
from fakes import FakeExifTool
from photos import make_jpeg
from rawfiles import bracket_mosaic, make_dng, scene

from lumina.core import db as dbm
from lumina.core import hdr, linear_tiff
from lumina.core import library as scanning
from lumina.core.db import Library
from lumina.core.edit import source
from lumina.core.metadata import Metadata
from lumina.core.volumes import Mount, locate

DRIVE = "uuid:TEST-DRIVE"
START = datetime(2026, 10, 16, 14, 3, 5)


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeExifTool.reset()


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


@pytest.fixture
def drive(tmp_path):
    (tmp_path / "drive" / "Photos").mkdir(parents=True)
    return tmp_path / "drive"


def mounted(drive):
    return [Mount(DRIVE, str(drive), "/", "ext4", "/dev/sdz1", "Photos drive")]


def shoot(folder, stops=(-2, 0, 2), shifts=None, light=None, first=1, ghost=None):
    """A bracket as a camera writes it: IMG_0001.dng and on, darkest first. `ghost`
    (frame index) has something dark in the shadows that the others do not."""
    light = scene() if light is None else light
    paths = []
    for i, stop in enumerate(stops):
        seen = light.copy()
        if ghost == i:
            seen[60:90, 10:40] *= 0.2
        shift = shifts[i] if shifts else (0, 0)
        name = f"IMG_{first + i:04d}.dng"
        paths.append(make_dng(folder / name, mosaic=bracket_mosaic(seen, stop, shift)))
    return paths


def scanned(library, drive, stops=(-2, 0, 2), seconds=0.3, models=None):
    """The folder scanned, each photo with the exposure a camera bracketing at `stops`
    writes (1/60 s at f/8, ISO 100 in the middle), `seconds` apart. Returns the ids
    in name order."""
    key, inside = locate(drive / "Photos", mounted(drive))
    folder = library.add_folder(key, inside, "Photos drive", str(drive))
    job = library.add_job(scanning.SCAN, folder)
    assert scanning.scan(library, job, folder, mounted(drive), exiftool=FakeExifTool) == dbm.DONE
    rows = sorted(library.photos(), key=lambda photo: photo.path)
    details = []
    for i, photo in enumerate(rows):
        stop = stops[i % len(stops)]
        model = (models or {}).get(i, "Canon EOS 90D")
        data = Metadata(
            captured=START + timedelta(seconds=seconds * i),
            make="Canon",
            model=model,
            lens="EF-S10-18mm",
            focal_length=10.0,
            aperture=8.0,
            exposure_time=2.0**stop / 60,
            iso=100,
            file_type="DNG",
        )
        details.append((photo.id, photo.content_id, data))
    library.write_metadata(read=details)
    return [photo.id for photo in rows]


def merged(library, drive, ids, tmp_path, **kwargs):
    options = {key: kwargs.pop(key) for key in ("deghost", "align") if key in kwargs}
    job = hdr.queue(library, ids, **options)
    state = hdr.run(library, job, mounted(drive), work_root=str(tmp_path / "work"), **kwargs)
    return state, library.job(job)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def light_of(path):
    return linear_tiff.read(path).astype(np.float32)


def test_a_bracket_is_merged_into_light_past_white_beside_the_first_photo(library, drive, tmp_path):
    frames = shoot(drive / "Photos")
    before = {path: digest(path) for path in frames}
    ids = scanned(library, drive)

    state, job = merged(library, drive, ids, tmp_path)

    assert state == dbm.DONE, job.message
    assert job.message == "Merged 3 photos into IMG_0001-HDR.tif"
    result = drive / "Photos" / "IMG_0001-HDR.tif"
    image = light_of(result)
    middle = source.raw_linear(frames[1]).astype(np.float32) / 65535
    # As bright as the middle exposure where it is well exposed
    well = (middle.max(axis=2) > 0.05) & (middle.max(axis=2) < 0.7)
    ratio = image[..., 1][well] / middle[..., 1][well]
    assert abs(np.median(ratio) - 1) < 0.01
    # Light the middle exposure clipped is there, up to what the darkest frame caught
    assert image.max() > 3.5
    clipped = middle.max(axis=2) >= 0.999
    assert np.median(image[..., 1][clipped]) > 1.5
    # The originals are as they were, and nothing else is left beside them
    assert {path: digest(path) for path in frames} == before
    assert sorted(os.listdir(drive / "Photos")) == [
        "IMG_0001-HDR.tif",
        "IMG_0001.dng",
        "IMG_0002.dng",
        "IMG_0003.dng",
    ]
    assert not (tmp_path / "work" / str(job.id)).exists()


def test_the_library_keeps_what_it_was_made_of(library, drive, tmp_path):
    shoot(drive / "Photos")
    ids = scanned(library, drive)

    _state, job = merged(library, drive, ids, tmp_path, deghost="low")

    derived = library.derived(job.derived_id)
    assert derived.kind == hdr.HDR
    assert (derived.volume_key, derived.path) == (DRIVE, "Photos/IMG_0001-HDR.tif")
    assert [s.path for s in derived.sources] == [
        "Photos/IMG_0001.dng",
        "Photos/IMG_0002.dng",
        "Photos/IMG_0003.dng",
    ]
    settings = json.loads(derived.settings)
    assert settings["reference"] == 1 and settings["deghost"] == "low"
    assert library.derived_at(DRIVE, "Photos/IMG_0001-HDR.tif").id == derived.id


def test_it_says_when_and_with_what_it_was_taken(library, drive, tmp_path, exiftool):
    shoot(drive / "Photos")
    ids = scanned(library, drive)

    merged(library, drive, ids, tmp_path)

    data = exiftool.read(str(drive / "Photos" / "IMG_0001-HDR.tif"))
    assert data.model == "Canon EOS 90D" and data.lens == "EF-S10-18mm"
    # The middle exposure's time
    assert data.captured == START + timedelta(seconds=0.3)


def test_frames_moved_between_exposures_are_aligned(library, drive, tmp_path):
    shoot(drive / "Photos", shifts=[(-4, 1), (0, 0), (3, -2)])
    ids = scanned(library, drive)

    state, job = merged(library, drive, ids, tmp_path)
    _state, unaligned = merged(library, drive, ids, tmp_path, align=False)

    assert state == dbm.DONE, job.message
    image = light_of(drive / "Photos" / "IMG_0001-HDR.tif")
    # Cut to what all three cover: 4 + 3 columns and 1 + 2 rows fewer
    assert image.shape[:2] == (128 - 3, 192 - 7)
    middle = source.raw_linear(drive / "Photos" / "IMG_0002.dng").astype(np.float32) / 65535
    well = (middle.max(axis=2) > 0.05) & (middle.max(axis=2) < 0.7)

    def error(image, box):
        rows, columns = box
        ratio = image[..., 1] / middle[rows, columns][..., 1]
        return np.median(np.abs(ratio[well[rows, columns]] - 1))

    # Well exposed in the middle frame: the same light, not a blur of three
    aligned = error(image, (slice(2, -1), slice(4, -3)))
    blurred = error(light_of(drive / "Photos" / "IMG_0001-HDR-2.tif"), (slice(None), slice(None)))
    assert aligned < 0.01
    assert blurred > 5 * aligned
    assert unaligned.state == dbm.DONE


def test_deghosting_leaves_out_what_moved(library, drive, tmp_path):
    shoot(drive / "Photos", ghost=2)
    ids = scanned(library, drive)

    _state, plain = merged(library, drive, ids, tmp_path)
    _state, deghosted = merged(library, drive, ids, tmp_path, deghost="medium")

    truth = source.raw_linear(drive / "Photos" / "IMG_0002.dng").astype(np.float32) / 65535
    region = (slice(65, 85), slice(15, 35))
    ghosted = light_of(drive / "Photos" / "IMG_0001-HDR.tif")[region][..., 1].mean()
    clean = light_of(drive / "Photos" / "IMG_0001-HDR-2.tif")[region][..., 1].mean()
    expected = truth[region][..., 1].mean()
    # The brightest frame has the most say in the shadows: its ghost shows without
    assert ghosted < 0.6 * expected
    assert abs(clean / expected - 1) < 0.1
    assert plain.state == deghosted.state == dbm.DONE


def test_a_file_that_is_there_is_never_written_over(library, drive, tmp_path):
    shoot(drive / "Photos")
    ids = scanned(library, drive)
    taken = drive / "Photos" / "IMG_0001-HDR.tif"
    taken.write_bytes(b"someone else's")

    state, job = merged(library, drive, ids, tmp_path)

    assert state == dbm.DONE
    assert taken.read_bytes() == b"someone else's"
    assert job.message == "Merged 3 photos into IMG_0001-HDR-2.tif"
    assert linear_tiff.is_linear(drive / "Photos" / "IMG_0001-HDR-2.tif")


def test_a_cancel_leaves_nothing_behind(library, drive, tmp_path):
    frames = shoot(drive / "Photos")
    ids = scanned(library, drive)
    steps = []

    def stop_when_merging():
        return len(steps) > 4

    state, job = merged(
        library,
        drive,
        ids,
        tmp_path,
        call=lambda fn, *args: steps.append(fn.__name__) or fn(*args),
        should_stop=stop_when_merging,
    )

    assert state == dbm.CANCELLED and job.state == dbm.CANCELLED
    assert "start_result" in steps
    assert sorted(os.listdir(drive / "Photos")) == [path.name for path in frames]
    assert not (tmp_path / "work" / str(job.id)).exists()


def test_after_a_quit_it_resumes_from_the_frames_decoded(library, drive, tmp_path):
    shoot(drive / "Photos")
    ids = scanned(library, drive)
    job = hdr.queue(library, ids)
    decoded = []

    def counting(fn, *args):
        if fn is hdr.decode_frame:
            decoded.append(args[1])
        return fn(*args)

    state = hdr.run(
        library,
        job,
        mounted(drive),
        call=counting,
        should_stop=lambda: len(decoded) == 2,
        work_root=str(tmp_path / "work"),
        keep_work=lambda: True,
    )
    assert state == dbm.CANCELLED
    assert (tmp_path / "work" / str(job)).exists()

    state = hdr.run(library, job, mounted(drive), call=counting, work_root=str(tmp_path / "work"))

    assert state == dbm.DONE
    # The reference and one more were decoded before; only the last one after
    assert decoded == [1, 0, 2]
    assert (drive / "Photos" / "IMG_0001-HDR.tif").exists()


def test_a_missing_photo_fails_the_job_naming_it(library, drive, tmp_path):
    frames = shoot(drive / "Photos")
    ids = scanned(library, drive)
    job = hdr.queue(library, ids)
    frames[2].unlink()

    state = hdr.run(library, job, mounted(drive), work_root=str(tmp_path / "work"))

    assert state == dbm.FAILED_JOB
    assert library.failures(job) == [(str(frames[2]), "is missing: the photo is not where it was")]
    assert not (drive / "Photos" / "IMG_0001-HDR.tif").exists()
    assert library.derived(library.job(job).derived_id).path is None


def test_frames_of_another_size_fail_naming_the_file(library, drive, tmp_path):
    light = scene()
    folder = drive / "Photos"
    make_dng(folder / "IMG_0001.dng", mosaic=bracket_mosaic(light, -2))
    make_dng(folder / "IMG_0002.dng", mosaic=bracket_mosaic(light, 0))
    make_dng(folder / "IMG_0003.dng", mosaic=bracket_mosaic(light[:, :160], 2))
    ids = scanned(library, drive)

    state, job = merged(library, drive, ids, tmp_path)

    assert state == dbm.FAILED_JOB
    [(path, message)] = library.failures(job.id)
    assert path == str(folder / "IMG_0003.dng")
    assert "is 160 x 128, the other photos 192 x 128" in message
    assert sorted(os.listdir(folder)) == ["IMG_0001.dng", "IMG_0002.dng", "IMG_0003.dng"]


def test_a_drive_not_connected_leaves_the_job_waiting(library, drive, tmp_path):
    shoot(drive / "Photos")
    ids = scanned(library, drive)
    job = hdr.queue(library, ids)

    with pytest.raises(scanning.NotConnected):
        hdr.run(library, job, [], work_root=str(tmp_path / "work"))
    assert library.job(job).state == dbm.QUEUED


def test_photos_of_another_camera_are_refused_naming_one(library, drive):
    shoot(drive / "Photos")
    ids = scanned(library, drive, models={2: "Canon EOS R6"})

    with pytest.raises(ValueError, match="IMG_0003.dng is from another camera"):
        hdr.queue(library, ids)


def test_only_raw_files_are_merged(library, drive):
    shoot(drive / "Photos", stops=(-2, 0))
    make_jpeg(drive / "Photos" / "IMG_0003.jpg")
    ids = scanned(library, drive)

    with pytest.raises(ValueError, match="IMG_0003.jpg is not a RAW file"):
        hdr.queue(library, ids)
    with pytest.raises(ValueError, match="at least 2"):
        hdr.queue(library, ids[:1])


def test_the_frames_are_put_in_order_of_exposure(library, drive, tmp_path):
    # Shot middle, dark, bright, as many cameras bracket
    shoot(drive / "Photos", stops=(0, -2, 2))
    ids = scanned(library, drive, stops=(0, -2, 2))

    state, job = merged(library, drive, ids, tmp_path)

    derived = library.derived(job.derived_id)
    assert [s.path for s in derived.sources] == [
        "Photos/IMG_0002.dng",
        "Photos/IMG_0001.dng",
        "Photos/IMG_0003.dng",
    ]
    # Named after the first photo taken, and as bright as the middle exposure
    assert state == dbm.DONE and job.message.endswith("IMG_0001-HDR.tif")


def row(i, seconds, stop, model="Canon EOS 90D", lens="EF-S10-18mm"):
    return {
        "id": i,
        "taken": (START + timedelta(seconds=seconds)).isoformat(sep=" "),
        "model": model,
        "lens": lens,
        "focal_length": 10.0,
        "aperture": 8.0,
        "exposure_time": 2.0**stop / 60,
        "iso": 100,
    }


def test_brackets_are_runs_of_quick_frames_at_different_exposures():
    rows = [
        # A bracket
        row(1, 0.0, 0),
        row(2, 0.3, -2),
        row(3, 0.6, 2),
        # Three seconds later, another
        row(4, 3.6, -1),
        row(5, 3.9, 1),
        # A burst at one exposure is not one
        row(6, 10.0, 0),
        row(7, 10.2, 0),
        row(8, 10.4, 0),
        # Nor a third of a stop either way
        row(9, 20.0, 0),
        row(10, 20.2, 0.5),
    ]

    assert hdr.brackets(rows) == [[1, 2, 3], [4, 5]]
    assert hdr.bracket_around(rows, 5) == [4, 5]
    assert hdr.bracket_around(rows, 7) is None


def test_a_bracket_ends_where_the_camera_or_lens_changes_or_a_frame_repeats():
    rows = [
        row(1, 0.0, 0),
        row(2, 0.3, -2),
        row(3, 0.6, 2, lens="EF 50mm"),
        row(4, 0.9, 0, lens="EF 50mm"),
        # The next bracket starts at once: its middle frame is the one before's again
        row(5, 1.2, 0, lens="EF 50mm"),
        row(6, 1.5, 2, lens="EF 50mm"),
    ]

    assert hdr.brackets(rows) == [[1, 2], [3, 4], [5, 6]]
