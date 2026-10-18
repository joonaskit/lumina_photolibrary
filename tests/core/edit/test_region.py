"""Rendering a part of the photo gives the pixels the whole render has there."""

import cv2
import numpy as np
import pytest

from lumina.core.edit import pipeline
from lumina.core.edit.params import (
    Basic,
    Colour,
    Crop,
    Grain,
    Hsl,
    NoiseReduction,
    Params,
    Perspective,
    Presence,
    Sharpening,
    Vignette,
)
from lumina.core.edit.source import make_proxy


def same(part, whole, bits=8):
    """Equal but for the last bit of a float sum that is added in another order."""
    assert part.shape == whole.shape
    assert np.abs(part.astype(np.int32) - whole).max() <= (1 if bits == 8 else 2)


def photo(width=411, height=317, seed=3):
    """Linear uint16: soft shapes, edges and noise, an odd size on purpose."""
    rng = np.random.default_rng(seed)
    base = cv2.GaussianBlur(rng.random((height, width, 3)).astype(np.float32), (0, 0), 9)
    base = (base - base.min()) / (base.max() - base.min())
    base[:, width // 2 :] *= 0.5
    base += rng.normal(0, 0.03, base.shape).astype(np.float32)
    return (np.clip(base, 0, 1) ** 2.2 * 65535).astype(np.uint16)


EVERYTHING = Params(
    basic=Basic(contrast=20, shadows=30),
    colour=Colour(vibrance=30, saturation=10),
    presence=Presence(texture=40, clarity=50),
    sharpening=Sharpening(amount=60, radius=1.5, masking=20),
    noise=NoiseReduction(luminance=40, colour=50),
    vignette=Vignette(amount=-40),
    grain=Grain(amount=30),
)

REGIONS = [
    (0, 0, 120, 90),  # a corner of the photo
    (100, 80, 260, 200),  # inside it, odd offsets
    (301, 217, 411, 317),  # the opposite corner
    (13, 0, 14, 317),  # one column
]


@pytest.mark.parametrize("region", REGIONS)
def test_a_region_is_the_same_pixels_as_in_the_whole_render(region):
    image = photo()
    whole = pipeline.render(image, EVERYTHING, bits=16)
    left, top, right, bottom = region

    part = pipeline.render(image, EVERYTHING, bits=16, region=region)

    assert part.shape == (bottom - top, right - left, 3)
    same(part, whole[top:bottom, left:right], 16)


@pytest.mark.parametrize(
    "edit",
    [
        EVERYTHING.with_changes(straighten=3.5),
        EVERYTHING.with_changes(perspective=Perspective(40, -25), straighten=-2.0),
        EVERYTHING.with_changes(rotate=1, crop=Crop(0.1, 0.2, 0.9, 0.8)),
        EVERYTHING.with_changes(flip_horizontal=True, crop=Crop(0.05, 0.1, 0.7, 0.95)),
    ],
)
def test_a_region_follows_the_turn_the_mirroring_the_straightening_and_the_crop(edit):
    image = photo()
    whole = pipeline.render(image, edit)
    height, width = whole.shape[:2]
    region = (width // 5, height // 4, width * 4 // 5 + 1, height * 3 // 4 + 1)

    part = pipeline.render(image, edit, region=region)

    left, top, right, bottom = region
    same(part, whole[top:bottom, left:right])


def test_a_region_of_a_smaller_copy_has_the_pixels_of_that_copy_s_render():
    # What the zoom below 100 % renders: a copy made smaller, told the full size
    image = photo(1200, 800)
    small = np.ascontiguousarray(image[::2, ::2])
    full = (1200, 800)
    whole = pipeline.render(small, EVERYTHING, bits=16, full=full)

    part = pipeline.render(small, EVERYTHING, bits=16, full=full, region=(150, 90, 400, 300))

    same(part, whole[90:300, 150:400], 16)


def test_a_region_with_dehaze_uses_the_measure_of_the_whole_photo():
    image = photo()
    edit = EVERYTHING.with_changes(presence=Presence(texture=40, dehaze=60))
    haze = pipeline.haze_analysis(make_proxy(image, pixels=100_000), edit)
    whole = pipeline.render(image, edit, bits=16, haze=haze)

    part = pipeline.render(image, edit, bits=16, haze=haze, region=(100, 80, 260, 200))

    assert haze is not None
    same(part, whole[80:200, 100:260], 16)


def test_the_measure_from_a_smaller_copy_is_near_the_whole_photo_s_own():
    image = photo()
    edit = Params(presence=Presence(dehaze=60))
    whole = pipeline.render(image, edit, bits=16)
    haze = pipeline.haze_analysis(make_proxy(image, pixels=100_000), edit)

    part = pipeline.render(image, edit, bits=16, haze=haze, region=(0, 0, 411, 317))

    # A fraction of a percent of the range: the haze varies slowly
    assert np.abs(part.astype(float) - whole).mean() / 65535 < 0.005


def test_there_is_no_measure_without_dehaze():
    assert pipeline.haze_analysis(photo(), Params(presence=Presence(clarity=30))) is None


def test_hsl_and_a_region_of_nothing_much():
    image = photo()
    edit = Params(hsl=Hsl())
    same(pipeline.render(image, edit, region=(5, 5, 6, 6)), pipeline.render(image, edit)[5:6, 5:6])
