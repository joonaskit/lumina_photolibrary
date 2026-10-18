import hashlib
import json
import os
from datetime import datetime, timedelta

import cv2
import numpy as np
import pytest
from fakes import FakeExifTool
from photos import make_jpeg
from rawfiles import bracket_mosaic, make_dng, surroundings, view, vignetted

from lumina.core import db as dbm
from lumina.core import library as scanning
from lumina.core import linear_tiff, panorama
from lumina.core.db import Library
from lumina.core.metadata import Metadata
from lumina.core.volumes import Mount, locate

DRIVE = "uuid:TEST-DRIVE"
START = datetime(2026, 10, 16, 15, 20, 0)
# A camera turned 35 degrees between frames of about 62 degrees across
YAWS = (-35, 0, 35)


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


@pytest.fixture(scope="module")
def around():
    return surroundings()


def mounted(drive):
    return [Mount(DRIVE, str(drive), "/", "ext4", "/dev/sdz1", "Photos drive")]


def shoot(folder, around, yaws=YAWS, stops=None, first=1, falloff=None):
    """Frames as a camera turning writes them: IMG_0001.dng and on, left to right. With
    `falloff` (a, b), through a lens that darkens the corners (rawfiles.vignetted)."""
    paths = []
    for i, yaw in enumerate(yaws):
        stop = stops[i] if stops else 0.0
        light = view(around, yaw)
        if falloff:
            light = vignetted(light, falloff)
        mosaic = bracket_mosaic(light, stop)
        paths.append(make_dng(folder / f"IMG_{first + i:04d}.dng", mosaic=mosaic))
    return paths


def scanned(library, drive, models=None):
    """The folder scanned, its photos 2 s apart, of one camera. Returns the ids in name
    order."""
    key, inside = locate(drive / "Photos", mounted(drive))
    folder = library.add_folder(key, inside, "Photos drive", str(drive))
    job = library.add_job(dbm.SCAN, folder)
    assert scanning.scan(library, job, folder, mounted(drive), exiftool=FakeExifTool) == dbm.DONE
    rows = sorted(library.photos(), key=lambda photo: photo.path)
    details = []
    for i, photo in enumerate(rows):
        data = Metadata(
            captured=START + timedelta(seconds=2 * i),
            make="Canon",
            model=(models or {}).get(i, "Canon EOS 90D"),
            lens="EF-S10-18mm",
            focal_length=10.0,
            file_type="DNG",
        )
        details.append((photo.id, photo.content_id, data))
    library.write_metadata(read=details)
    return [photo.id for photo in rows]


def stitched(library, drive, ids, tmp_path, **kwargs):
    options = {key: kwargs.pop(key) for key in ("projection", "crop") if key in kwargs}
    job = panorama.queue(library, ids, **options)
    state = panorama.run(library, job, mounted(drive), work_root=str(tmp_path / "work"), **kwargs)
    return state, library.job(job)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def light_of(path):
    return linear_tiff.read(path).astype(np.float32)


def found_in(image, around):
    """Where the result's green best matches the surroundings, and how well: (score,
    the surroundings there)."""
    green = image[..., 1]
    twice = np.concatenate([around, around], axis=1).astype(np.float32)
    scores = cv2.matchTemplate(twice, green, cv2.TM_CCOEFF_NORMED)
    _low, score, _where, (x, y) = cv2.minMaxLoc(scores)
    return score, twice[y : y + green.shape[0], x : x + green.shape[1]]


def test_overlapping_photos_are_stitched_into_the_scene_beside_the_first(
    library, drive, tmp_path, around
):
    frames = shoot(drive / "Photos", around)
    before = {path: digest(path) for path in frames}
    ids = scanned(library, drive)

    state, job = stitched(library, drive, ids, tmp_path)

    assert state == dbm.DONE, job.message
    assert job.message == "Stitched 3 photos into IMG_0001-pano.tif"
    image = light_of(drive / "Photos" / "IMG_0001-pano.tif")
    # As wide as the turn and a frame, on a cylinder: 70 + 62 degrees at 200 px a radian
    assert 440 < image.shape[1] < 470
    # Cropped to what the frames all cover: nothing left out
    assert np.count_nonzero(image.max(axis=2) == 0) == 0
    score, truth = found_in(image, around)
    assert score > 0.97
    # As bright as the scene
    assert abs(np.median(image[..., 1] / truth) - 1) < 0.02
    # The originals are as they were, and nothing else is left beside them
    assert {path: digest(path) for path in frames} == before
    assert sorted(os.listdir(drive / "Photos")) == [
        "IMG_0001-pano.tif",
        "IMG_0001.dng",
        "IMG_0002.dng",
        "IMG_0003.dng",
    ]
    assert not (tmp_path / "work" / str(job.id)).exists()


def test_the_library_keeps_what_it_was_made_of(library, drive, tmp_path, around):
    shoot(drive / "Photos", around)
    ids = scanned(library, drive)

    # Chosen in another order: taken in the order shot
    _state, job = stitched(library, drive, ids[::-1], tmp_path, projection="spherical")

    derived = library.derived(job.derived_id)
    assert derived.kind == dbm.PANORAMA
    assert (derived.volume_key, derived.path) == (DRIVE, "Photos/IMG_0001-pano.tif")
    assert [s.path for s in derived.sources] == [
        "Photos/IMG_0001.dng",
        "Photos/IMG_0002.dng",
        "Photos/IMG_0003.dng",
    ]
    settings = json.loads(derived.settings)
    assert settings["projection"] == "spherical" and settings["crop"] is True
    assert library.derived_at(DRIVE, "Photos/IMG_0001-pano.tif").id == derived.id


def test_it_says_when_and_with_what_it_was_taken(library, drive, tmp_path, around, exiftool):
    shoot(drive / "Photos", around)
    ids = scanned(library, drive)

    stitched(library, drive, ids, tmp_path)

    data = exiftool.read(str(drive / "Photos" / "IMG_0001-pano.tif"))
    assert data.model == "Canon EOS 90D" and data.lens == "EF-S10-18mm"
    # The first photo's time
    assert data.captured == START


def test_frames_of_different_exposures_are_made_alike(library, drive, tmp_path, around):
    # Shot on automatic exposure: a stop apart
    shoot(drive / "Photos", around, stops=(-1, 0, 1))
    ids = scanned(library, drive)

    state, job = stitched(library, drive, ids, tmp_path)

    assert state == dbm.DONE, job.message
    image = light_of(drive / "Photos" / "IMG_0001-pano.tif")
    score, truth = found_in(image, around)
    assert score > 0.97
    # One brightness from left to right: no step where the frames meet
    columns = np.median(image[..., 1] / truth, axis=0)
    assert columns.max() / columns.min() < 1.1


def test_darker_corners_do_not_show_as_bands_across_the_seams(library, drive, tmp_path, around):
    # A lens a stop and a half darker in the corners: each frame is darker at its edges
    shoot(drive / "Photos", around, falloff=(-0.6, -0.4))
    ids = scanned(library, drive)

    state, job = stitched(library, drive, ids, tmp_path)

    assert state == dbm.DONE, job.message
    image = light_of(drive / "Photos" / "IMG_0001-pano.tif")
    score, truth = found_in(image, around)
    assert score > 0.97
    # As bright as the scene from the first column to the last (not darker at the ends,
    # and no step where the frames meet)
    columns = np.median(image[..., 1] / truth, axis=0)
    assert columns.max() / columns.min() < 1.1
    assert abs(np.median(columns) - 1) < 0.03


def planned(folder, around, falloff):
    """The plan made for frames shot through a lens with `falloff`."""
    paths = [str(path) for path in shoot(folder, around, falloff=falloff)]
    work = str(folder / "work")
    os.makedirs(work)
    for index, path in enumerate(paths):
        panorama.decode_frame(path, index, work, None)
    panorama.make_plan(work, len(paths), panorama.CYLINDRICAL, True, paths)
    return panorama.load_plan(work)


@pytest.mark.parametrize("falloff", [(-0.6, -0.4), (-0.3, 0.0)])
def test_the_falloff_found_is_the_lens(tmp_path, around, falloff):
    plan = planned(tmp_path, around, falloff)

    a, b = plan["vignette"]
    assert a == pytest.approx(falloff[0], abs=0.1) and b == pytest.approx(falloff[1], abs=0.1)
    # The gains are not made to do the lens's work: all the frames were a stop alike
    assert plan["gains"] == pytest.approx([1.0, 1.0, 1.0], abs=0.01)


def test_frames_of_a_lens_that_keeps_the_light_are_not_corrected(tmp_path, around):
    plan = planned(tmp_path, around, None)

    a, b = plan["vignette"]
    # A corner within 2 % of the middle
    assert abs(a + b) < 0.02


def test_a_falloff_no_lens_makes_is_left_out():
    # Brighter towards the corners, or darkening more than 10 : 1, is not a lens
    assert panorama._plausible((-0.5, -0.2))
    assert panorama._plausible((-1.0, 0.4))
    assert not panorama._plausible((0.2, 0.0))
    assert not panorama._plausible((-1.0, 1.5))
    assert not panorama._plausible((-3.0, 0.0))


def previewed(folder, around, projection=panorama.CYLINDRICAL, crop=True, **options):
    """The picture of a preview of frames shot in `folder`: (width, height, rows)."""
    paths = [str(path) for path in shoot(folder, around, **options)]
    work = str(folder / "preview")
    os.makedirs(work)
    for index, path in enumerate(paths):
        panorama.preview_frame(path, index, work, paths[0])
    return panorama.preview_plan(work, len(paths), projection, crop, paths)


def test_a_preview_shows_the_panorama_before_it_is_made(library, drive, tmp_path, around):
    width, height, rows = previewed(drive / "Photos", around)

    assert len(rows) == width * height * 3
    picture = np.frombuffer(rows, np.uint8).reshape(height, width, 3)
    assert picture.min() > 0 and picture.max() > 100
    # What the stitch makes, smaller: of the same shape, and the same scene
    ids = scanned(library, drive)
    stitched(library, drive, ids, tmp_path)
    made = light_of(drive / "Photos" / "IMG_0001-pano.tif")
    assert width / height == pytest.approx(made.shape[1] / made.shape[0], rel=0.03)
    # (The picture is gamma-encoded, and of frames at half size: the scene's half size)
    linear = ((picture[..., 1] / 255.0) ** 2.2).astype(np.float32)
    half = cv2.resize(around, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    assert found_in(np.dstack([linear] * 3), half)[0] > 0.9


def test_a_preview_is_made_at_most_so_long(tmp_path, around, monkeypatch):
    monkeypatch.setattr(panorama, "PREVIEW_SIDE", 200)

    width, height, rows = previewed(tmp_path, around)

    assert max(width, height) == 200 and len(rows) == width * height * 3


def test_a_preview_takes_the_projection_and_the_crop(tmp_path, around):
    def previewed_in(name, **options):
        (tmp_path / name).mkdir()
        return previewed(tmp_path / name, around, **options)

    cylinder = previewed_in("a")
    whole = previewed_in("b", crop=False)
    plane = previewed_in("c", projection="perspective")

    # The frames' corners curve away on a cylinder: more is kept without the crop
    assert whole[1] > cylinder[1]
    assert plane[0] > 1.5 * cylinder[0]


def test_a_preview_names_the_photo_that_does_not_fit(tmp_path, around):
    frames = [str(path) for path in shoot(tmp_path, around, yaws=(-35, 0, 35, 180))]
    work = str(tmp_path / "preview")
    os.makedirs(work)
    for index, path in enumerate(frames):
        panorama.preview_frame(path, index, work, frames[0])

    with pytest.raises(panorama.PhotoError) as error:
        panorama.preview_plan(work, 4, panorama.CYLINDRICAL, True, frames)

    assert error.value.path == frames[3]
    assert error.value.message.startswith("does not overlap the other photos")


def test_a_preview_of_photos_of_other_sizes_says_so(tmp_path, around):
    one = make_dng(tmp_path / "IMG_0001.dng", mosaic=bracket_mosaic(view(around, 0)))
    other = make_dng(
        tmp_path / "IMG_0002.dng", mosaic=bracket_mosaic(view(around, 20, size=(200, 160)))
    )
    work = str(tmp_path / "preview")
    os.makedirs(work)
    panorama.preview_frame(str(one), 0, work, str(one))

    with pytest.raises(panorama.PhotoError, match="not of one size"):
        panorama.preview_frame(str(other), 1, work, str(one))


def test_without_the_crop_it_keeps_all_the_frames_cover(library, drive, tmp_path, around):
    shoot(drive / "Photos", around)
    ids = scanned(library, drive)

    stitched(library, drive, ids, tmp_path)
    state, job = stitched(library, drive, ids, tmp_path, crop=False)

    assert state == dbm.DONE, job.message
    cropped = light_of(drive / "Photos" / "IMG_0001-pano.tif")
    whole = light_of(drive / "Photos" / "IMG_0001-pano-2.tif")
    assert whole.shape[0] > cropped.shape[0] and whole.shape[1] >= cropped.shape[1]
    # The frames' corners curve away on a cylinder: what none covers is black
    assert np.count_nonzero(whole.max(axis=2) == 0) > 0
    assert whole[0, 0].max() == 0


def test_the_projections(library, drive, tmp_path, around):
    shoot(drive / "Photos", around)
    ids = scanned(library, drive)

    states = [
        stitched(library, drive, ids, tmp_path, projection=kind)[0] for kind in panorama.PROJECTIONS
    ]

    assert states == [dbm.DONE] * 3
    folder = drive / "Photos"
    cylinder = light_of(folder / "IMG_0001-pano.tif")
    sphere = light_of(folder / "IMG_0001-pano-2.tif")
    plane = light_of(folder / "IMG_0001-pano-3.tif")
    # Near the horizon a sphere is much as a cylinder; a plane stretches the sides
    assert abs(sphere.shape[1] - cylinder.shape[1]) < 10
    assert plane.shape[1] > 1.5 * cylinder.shape[1]
    assert found_in(sphere, around)[0] > 0.9


def test_a_result_too_large_is_made_smaller(library, drive, tmp_path, around, monkeypatch):
    shoot(drive / "Photos", around)
    ids = scanned(library, drive)
    monkeypatch.setattr(panorama, "MAX_PIXELS", 20_000)

    state, job = stitched(library, drive, ids, tmp_path, crop=False)

    assert state == dbm.DONE, job.message
    image = light_of(drive / "Photos" / "IMG_0001-pano.tif")
    assert image.shape[0] * image.shape[1] <= 20_000
    assert image.shape[1] > 200


def test_merged_photos_keep_their_light_past_white(library, drive, tmp_path, around):
    # HDR merges of each place (core/hdr.py) stitched: an HDR panorama
    for i, yaw in enumerate(YAWS):
        light = view(around, yaw) * 4.0
        width, height = light.shape[1], light.shape[0]
        layout = linear_tiff.layout(width, height, linear_tiff.Tags(model="Canon EOS 90D"))
        fd = os.open(drive / "Photos" / f"IMG_{i + 1:04d}-HDR.tif", os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            linear_tiff.start(fd, layout)
            linear_tiff.write_rows(fd, layout, 0, np.dstack([light] * 3))
        finally:
            os.close(fd)
    ids = scanned(library, drive)

    state, job = stitched(library, drive, ids, tmp_path)

    assert state == dbm.DONE, job.message
    image = light_of(drive / "Photos" / "IMG_0001-HDR-pano.tif")
    score, truth = found_in(image, around)
    assert score > 0.97
    assert abs(np.median(image[..., 1] / truth) - 4.0) < 0.1
    assert image.max() > 2.0


def test_a_file_that_is_there_is_never_written_over(library, drive, tmp_path, around):
    shoot(drive / "Photos", around)
    ids = scanned(library, drive)
    taken = drive / "Photos" / "IMG_0001-pano.tif"
    taken.write_bytes(b"someone else's")

    state, job = stitched(library, drive, ids, tmp_path)

    assert state == dbm.DONE
    assert taken.read_bytes() == b"someone else's"
    assert job.message == "Stitched 3 photos into IMG_0001-pano-2.tif"
    assert linear_tiff.is_linear(drive / "Photos" / "IMG_0001-pano-2.tif")


def test_a_cancel_leaves_nothing_behind(library, drive, tmp_path, around):
    frames = shoot(drive / "Photos", around)
    ids = scanned(library, drive)
    steps = []

    state, job = stitched(
        library,
        drive,
        ids,
        tmp_path,
        call=lambda fn, *args: steps.append(fn.__name__) or fn(*args),
        should_stop=lambda: "stitch_rows" in steps,
    )

    assert state == dbm.CANCELLED and job.state == dbm.CANCELLED
    assert "start_result" in steps
    assert sorted(os.listdir(drive / "Photos")) == [path.name for path in frames]
    assert not (tmp_path / "work" / str(job.id)).exists()


def test_after_a_quit_it_resumes_with_the_geometry_found(library, drive, tmp_path, around):
    shoot(drive / "Photos", around)
    ids = scanned(library, drive)
    job = panorama.queue(library, ids)
    steps = []

    def counting(fn, *args):
        steps.append(fn.__name__)
        return fn(*args)

    state = panorama.run(
        library,
        job,
        mounted(drive),
        call=counting,
        should_stop=lambda: "stitch_rows" in steps,
        work_root=str(tmp_path / "work"),
        keep_work=lambda: True,
    )
    assert state == dbm.CANCELLED
    assert steps.count("decode_frame") == 3 and steps.count("make_plan") == 1
    steps.clear()

    state = panorama.run(
        library, job, mounted(drive), call=counting, work_root=str(tmp_path / "work")
    )

    assert state == dbm.DONE
    # Nothing decoded again, nor planned: stitched again from the first row
    assert "decode_frame" not in steps and "make_plan" not in steps
    assert "stitch_rows" in steps
    assert (drive / "Photos" / "IMG_0001-pano.tif").exists()


def test_a_photo_that_does_not_overlap_fails_naming_it(library, drive, tmp_path, around):
    # The last one looks the other way
    frames = shoot(drive / "Photos", around, yaws=(-35, 0, 35, 180))
    ids = scanned(library, drive)

    state, job = stitched(library, drive, ids, tmp_path)

    assert state == dbm.FAILED_JOB
    [(path, message)] = library.failures(job.id)
    assert path == str(frames[3])
    assert message.startswith("does not overlap the other photos")
    assert job.message.startswith("Could not stitch: does not overlap")
    assert sorted(os.listdir(drive / "Photos")) == [path.name for path in frames]


def test_frames_of_another_size_fail_naming_the_file(library, drive, tmp_path, around):
    folder = drive / "Photos"
    shoot(folder, around, yaws=(-35, 0))
    make_dng(folder / "IMG_0003.dng", mosaic=bracket_mosaic(view(around, 35, size=(200, 160))))
    ids = scanned(library, drive)

    state, job = stitched(library, drive, ids, tmp_path)

    assert state == dbm.FAILED_JOB
    [(path, message)] = library.failures(job.id)
    assert path == str(folder / "IMG_0003.dng")
    assert "is 200 x 160, the other photos 240 x 160" in message


def test_a_missing_photo_fails_the_job_naming_it(library, drive, tmp_path, around):
    frames = shoot(drive / "Photos", around)
    ids = scanned(library, drive)
    job = panorama.queue(library, ids)
    frames[1].unlink()

    state = panorama.run(library, job, mounted(drive), work_root=str(tmp_path / "work"))

    assert state == dbm.FAILED_JOB
    assert library.failures(job) == [(str(frames[1]), "is missing: the photo is not where it was")]
    assert library.derived(library.job(job).derived_id).path is None


def test_a_drive_not_connected_leaves_the_job_waiting(library, drive, tmp_path, around):
    shoot(drive / "Photos", around)
    ids = scanned(library, drive)
    job = panorama.queue(library, ids)

    with pytest.raises(scanning.NotConnected):
        panorama.run(library, job, [], work_root=str(tmp_path / "work"))
    assert library.job(job).state == dbm.QUEUED


def test_photos_that_cannot_be_stitched_are_refused_naming_one(library, drive, around):
    shoot(drive / "Photos", around)
    make_jpeg(drive / "Photos" / "IMG_0004.jpg")
    ids = scanned(library, drive, models={2: "Canon EOS R6"})

    with pytest.raises(ValueError, match="IMG_0003.dng is from another camera"):
        panorama.queue(library, ids[:3])
    with pytest.raises(ValueError, match="IMG_0004.jpg is not a RAW file, the others are"):
        panorama.queue(library, [ids[0], ids[1], ids[3]])
    with pytest.raises(ValueError, match="at least 2"):
        panorama.queue(library, ids[:1])


def test_the_largest_rectangle_all_covered():
    covered = np.zeros((6, 8), bool)
    covered[1:5, 1:7] = True
    covered[1, 1] = False

    # Rows 1 to 4 of columns 2 to 6 (20), not rows 2 to 4 of columns 1 to 6 (18)
    assert panorama._largest_rectangle(covered) == (2, 1, 7, 5)
    assert panorama._largest_rectangle(np.zeros((3, 3), bool)) is None


def test_a_photo_of_nearly_the_same_view_fails_naming_both(library, drive, tmp_path, around):
    # Taken twice, and nothing else: the second one tells nothing of a turn. (Among
    # others that overlap both, a repeat is harmless and stitched)
    frames = shoot(drive / "Photos", around, yaws=(0, 0.2))
    ids = scanned(library, drive)

    state, job = stitched(library, drive, ids, tmp_path)

    assert state == dbm.FAILED_JOB
    [(path, message)] = library.failures(job.id)
    other = frames[1].name if path == str(frames[0]) else frames[0].name
    assert message.startswith(f"shows nearly the same view as {other}: choose one of them")
