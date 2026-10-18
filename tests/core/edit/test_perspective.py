"""Perspective (keystone) correction: the homography, the fill, and turning an edit."""

import math

import numpy as np
import pytest

from lumina.core.edit import lenscorrect, params, perspective, pipeline
from lumina.core.edit.lenscorrect import Correction
from lumina.core.edit.params import Crop, LensProfile, Params, Perspective

WIDTH, HEIGHT = 120, 80


def ramp(width=WIDTH, height=HEIGHT):
    """Floats whose red and green are the pixel's own x and y: bilinear sampling of a
    ramp is exact, so a result says where each of its pixels came from."""
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    return np.stack([xs, ys, np.zeros_like(xs)], axis=-1)


def came_from(edit, width=WIDTH, height=HEIGHT):
    """The (x, y) in the photo each pixel of the geometry of `edit` is sampled at."""
    out = pipeline.geometry(ramp(width, height), edit)
    return out[..., 0], out[..., 1]


# Quarter of a pixel is far more than the 1/32 the interpolation is made in
TOLERANCE = 0.1


def reference(vertical, width, height, scale):
    """Where the result's pixels come from for a vertical keystone alone, worked out
    from the geometry of the view and nothing else: the photo is a plane `f` away, tilted
    by the angle; the middle of the photo stays where it is."""
    f = math.hypot(width, height)
    a = math.radians(perspective.MAX_ANGLE) * vertical / 100
    sin, cos = math.sin(a), math.cos(a)
    cx, cy = (width - 1) / 2, (height - 1) / 2
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    u, v = (xs - cx) / scale, (ys - cy) / scale
    # Turning the view moves the middle of the photo by this much; the result puts it back
    shift = -f * sin / cos
    v = v + shift
    depth = -sin * v + cos * f
    return f * u / depth + cx, f * (cos * v + sin * f) / depth + cy


@pytest.mark.parametrize("vertical", [-100, -37.5, 20, 100])
def test_a_vertical_keystone_is_the_homography_of_turning_the_view(vertical):
    edit = Params(perspective=Perspective(vertical=vertical))
    scale = perspective.fill_scale(WIDTH, HEIGHT, edit.perspective)

    x, y = came_from(edit)

    want_x, want_y = reference(vertical, WIDTH, HEIGHT, scale)
    assert np.abs(x - want_x).max() < TOLERANCE
    assert np.abs(y - want_y).max() < TOLERANCE


def test_a_horizontal_keystone_is_the_vertical_one_across_the_diagonal():
    # Widening the right is widening the bottom, with the photo's x and y swapped
    x, y = came_from(Params(perspective=Perspective(horizontal=60)), 80, 80)
    vx, vy = came_from(Params(perspective=Perspective(vertical=-60)), 80, 80)

    assert np.abs(x - vy.T).max() < TOLERANCE
    assert np.abs(y - vx.T).max() < TOLERANCE


def test_a_positive_vertical_widens_the_top_and_a_positive_horizontal_the_right():
    # The top row of the result shows a shorter stretch of the photo than the bottom one
    x, _ = came_from(Params(perspective=Perspective(vertical=100)))
    assert np.ptp(x[0]) < np.ptp(x[-1])
    _, y = came_from(Params(perspective=Perspective(horizontal=100)))
    assert np.ptp(y[:, -1]) < np.ptp(y[:, 0])


def test_no_keystone_is_the_plain_geometry():
    image = ramp()

    same = pipeline.geometry(image, Params(perspective=Perspective(), straighten=4.0))

    assert np.array_equal(same, pipeline.geometry(image, Params(straighten=4.0)))


# Filling the frame

CASES = [
    Perspective(100, 0),
    Perspective(-100, 0),
    Perspective(0, 100),
    Perspective(0, -75),
    Perspective(70, -90),
    Perspective(-100, -100),
]


@pytest.mark.parametrize("keystone", CASES)
@pytest.mark.parametrize("degrees", [0.0, 6.0, -25.0])
def test_the_result_is_sampled_from_inside_the_photo_and_zoomed_no_more_than_needed(
    keystone, degrees
):
    edit = Params(perspective=keystone, straighten=degrees)

    x, y = came_from(edit)

    assert x.min() >= -TOLERANCE and x.max() <= WIDTH - 1 + TOLERANCE
    assert y.min() >= -TOLERANCE and y.max() <= HEIGHT - 1 + TOLERANCE
    scale = perspective.fill_scale(WIDTH, HEIGHT, keystone, degrees)
    tighter = perspective.source_matrix(WIDTH, HEIGHT, keystone, degrees, scale * 0.995)
    assert scale >= 1.0
    # A little less would show past the photo, unless there was nothing to fill
    assert scale == 1.0 or not perspective._fits(tighter, WIDTH, HEIGHT)


def test_straightening_alone_needs_the_scale_the_pipeline_has_always_used():
    for degrees in (3.0, -17.0, 40.0):
        want = pipeline.straighten_scale(WIDTH, HEIGHT, degrees)
        # A corrected photo with nothing to correct but the bounds of its own frame
        got = perspective.fill_scale(WIDTH, HEIGHT, Perspective(1e-6, 0), degrees)
        # Up to the pixel centres: this one fills the frame between the first and last
        assert got == pytest.approx(want, rel=1e-2)


def test_the_result_is_the_size_of_the_photo_whatever_the_keystone():
    edit = Params(perspective=Perspective(80, 30), straighten=2.0)

    assert pipeline.geometry(ramp(), edit).shape == (HEIGHT, WIDTH, 3)
    assert pipeline.output_size(WIDTH, HEIGHT, edit) == (WIDTH, HEIGHT)


def test_the_crop_takes_only_its_own_pixels_of_the_corrected_photo():
    keystone = Perspective(50, -40)
    whole = came_from(Params(perspective=keystone, straighten=3.0))
    crop = Crop(0.25, 0.5, 0.75, 1.0)

    part = came_from(Params(perspective=keystone, straighten=3.0, crop=crop))

    for got, full in zip(part, whole, strict=True):
        assert got.shape == (HEIGHT // 2, WIDTH // 2)
        assert np.abs(got - full[HEIGHT // 2 :, WIDTH // 4 : WIDTH * 3 // 4]).max() < TOLERANCE


def test_integer_photos_keep_their_type_and_their_values():
    grey = np.full((HEIGHT, WIDTH, 3), 30000, dtype=np.uint16)

    out = pipeline.geometry(grey, Params(perspective=Perspective(90, 90), straighten=5.0))

    assert out.dtype == np.uint16
    assert np.array_equal(out, np.full_like(out, 30000))


# Turning and mirroring an edit


def edits():
    base = Params(
        perspective=Perspective(45, -30),
        straighten=4.0,
        crop=Crop(0.1, 0.2, 0.9, 0.8),
    )
    for rotate in range(4):
        for flip_horizontal in (False, True):
            for flip_vertical in (False, True):
                yield base.with_changes(
                    rotate=rotate,
                    flip_horizontal=flip_horizontal,
                    flip_vertical=flip_vertical,
                )


def same_sampling(a, b):
    """Two geometries of the ramp that say they came from the same places."""
    assert a.shape == b.shape
    assert np.abs(a - b).max() < TOLERANCE


@pytest.mark.parametrize("edit", list(edits()))
def test_turning_a_corrected_edit_turns_what_it_shows(edit):
    image = ramp()
    shown = pipeline.geometry(image, edit)

    same_sampling(pipeline.geometry(image, edit.turned(1)), np.rot90(shown, k=-1))
    same_sampling(pipeline.geometry(image, edit.turned(-1)), np.rot90(shown, k=1))


@pytest.mark.parametrize("edit", list(edits()))
def test_mirroring_a_corrected_edit_mirrors_what_it_shows(edit):
    image = ramp()
    shown = pipeline.geometry(image, edit)

    same_sampling(pipeline.geometry(image, edit.flipped(True)), shown[:, ::-1])
    same_sampling(pipeline.geometry(image, edit.flipped(False)), shown[::-1])


def test_four_quarter_turns_and_two_mirrorings_are_the_edit_again():
    edit = Params(perspective=Perspective(45, -30), straighten=4.0)

    assert edit.turned(4) == edit
    assert edit.flipped(True).flipped(True) == edit
    assert edit.flipped(False).flipped(False) == edit


# With a lens

LOW_PASS = Correction(LensProfile("test", distortion=("poly5", 0.0, 0.0)))


def test_a_lens_that_does_nothing_leaves_the_keystone_as_it_is():
    ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH].astype(np.float64)
    photo = (20000 + 300 * np.sin(xs / 7) + 250 * np.cos(ys / 5))[..., None].repeat(3, axis=2)
    photo = photo.astype(np.uint16)
    keystone = Perspective(60, -40)
    edit = Params(perspective=keystone, straighten=3.0)
    scale = perspective.fill_scale(WIDTH, HEIGHT, keystone, 3.0)
    matrix = perspective.source_matrix(WIDTH, HEIGHT, keystone, 3.0, scale)

    ours = lenscorrect.resample(photo, LOW_PASS, (0, HEIGHT, 0, WIDTH), matrix)

    plain = pipeline.geometry(photo, edit)
    # Both bilinear; this one in sixteenths of a pixel, the other off a coarser map
    assert np.abs(ours.astype(int) - plain.astype(int)).max() <= 12


# The document


def test_the_keystone_survives_json_and_is_clamped():
    edit = Params(perspective=Perspective(12.5, -40))

    assert params.from_json(edit.to_json()) == edit
    clamped = params.from_dict({"version": params.VERSION, "perspective": {"vertical": 500}})
    assert clamped.perspective == Perspective(100, 0)
    damaged = params.from_dict({"version": params.VERSION, "perspective": "up"})
    assert damaged.perspective == Perspective()


def test_a_version_10_edit_gets_no_perspective():
    stored = {"version": 10, "exposure": 0.5, "straighten": 2.0}

    edit = params.from_dict(stored)

    assert edit.perspective == Perspective() and edit.straighten == 2.0
    assert edit == Params(exposure=0.5, straighten=2.0)


def test_a_preset_can_carry_the_keystone():
    edit = Params(perspective=Perspective(30, 10), exposure=1.0)

    document = params.preset(edit, ["perspective"])
    applied = params.apply_preset(Params(straighten=5.0), document)

    assert set(document) == {"version", "perspective"}
    assert applied.perspective == Perspective(30, 10) and applied.straighten == 5.0
