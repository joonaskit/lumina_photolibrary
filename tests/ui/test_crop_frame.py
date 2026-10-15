"""The crop frame's geometry, held to an aspect ratio: pure functions of shares of the
photo."""

import random

import pytest

from lumina.qt_models.develop import Crop
from lumina.ui import crop_frame
from lumina.ui.crop_frame import dragged, dragged_to_ratio, reshaped

PARTS = ("tl", "tr", "bl", "br", "t", "b", "l", "r", "move")


def shape(crop, aspect):
    """The frame's width over its height in pixels, on a photo of `aspect`."""
    return (crop.right - crop.left) * aspect / (crop.bottom - crop.top)


def inside(crop):
    return (
        -1e-6 <= crop.left < crop.right <= 1 + 1e-6 and -1e-6 <= crop.top < crop.bottom <= 1 + 1e-6
    )


def test_the_ratios_the_frame_is_reshaped_to_are_exact_and_fit_the_photo():
    for aspect in (1.5, 0.75, 1.0, 16 / 9):
        for ratio in (1.0, 1.25, 0.8, 1.5, 2 / 3, 16 / 9, 9 / 16, 3.0):
            for crop in (Crop(), Crop(0.1, 0.2, 0.6, 0.7), Crop(0.6, 0.6, 1.0, 1.0)):
                out = reshaped(crop, ratio, aspect)

                assert inside(out)
                assert shape(out, aspect) == pytest.approx(ratio, rel=1e-3)


def test_reshaping_the_whole_photo_gives_the_largest_frame_of_the_ratio():
    # A 3:2 photo as a square: all of its height, two thirds of its width, in the middle
    assert reshaped(Crop(), 1.0, 1.5) == Crop(1 / 6, 0, 5 / 6, 1)
    # As 16:9: all of its width, less of its height
    wide = reshaped(Crop(), 16 / 9, 1.5)
    assert wide.left == 0 and wide.right == 1
    assert (wide.top + wide.bottom) / 2 == pytest.approx(0.5)


def test_reshaping_keeps_the_area_and_the_place_when_there_is_room():
    crop = Crop(0.3, 0.3, 0.7, 0.7)

    out = reshaped(crop, 2.0, 1.0)

    area = (out.right - out.left) * (out.bottom - out.top)
    assert area == pytest.approx(0.16, rel=1e-3)
    assert (out.left + out.right) / 2 == pytest.approx(0.5, abs=1e-6)
    assert (out.top + out.bottom) / 2 == pytest.approx(0.5, abs=1e-6)


def test_reshaping_near_an_edge_moves_the_frame_in():
    out = reshaped(Crop(0.7, 0.7, 1.0, 1.0), 1 / 4, 1.0)

    assert inside(out)
    assert shape(out, 1.0) == pytest.approx(0.25, rel=1e-3)


def test_turning_the_frame_gives_the_other_orientation_of_the_same_size():
    crop = reshaped(Crop(), 1.5, 1.5)
    assert crop == Crop()

    turned = reshaped(crop, 1 / 1.5, 1.5)

    assert shape(turned, 1.5) == pytest.approx(1 / 1.5, rel=1e-3)
    assert inside(turned)


@pytest.mark.parametrize("part", PARTS)
def test_dragging_to_a_ratio_keeps_the_ratio_and_stays_in_the_photo(part):
    generator = random.Random(7)
    for aspect, ratio in ((1.5, 1.0), (1.5, 16 / 9), (0.75, 1.25), (1.0, 2 / 3)):
        start = reshaped(Crop(0.1, 0.15, 0.8, 0.9), ratio, aspect)
        for _ in range(60):
            dx, dy = generator.uniform(-0.8, 0.8), generator.uniform(-0.8, 0.8)

            out = dragged_to_ratio(start, part, dx, dy, ratio, aspect)

            assert inside(out)
            assert shape(out, aspect) == pytest.approx(ratio, rel=1e-3)


@pytest.mark.parametrize(
    ("part", "kept"),
    [
        ("br", ("left", "top")),
        ("tl", ("right", "bottom")),
        ("tr", ("left", "bottom")),
        ("bl", ("right", "top")),
        ("r", ("left",)),
        ("l", ("right",)),
        ("b", ("top",)),
        ("t", ("bottom",)),
    ],
)
def test_dragging_a_handle_to_a_ratio_leaves_the_opposite_side_where_it_was(part, kept):
    start = Crop(0.25, 0.25, 0.65, 0.65)  # square on a square photo

    out = dragged_to_ratio(start, part, 0.05, -0.04, 1.0, 1.0)

    for side in kept:
        assert getattr(out, side) == pytest.approx(getattr(start, side), abs=1e-6)


def test_a_corner_follows_the_pointer_to_the_larger_of_the_two_sizes():
    start = Crop(0.2, 0.2, 0.4, 0.4)

    wider = dragged_to_ratio(start, "br", 0.3, 0.0, 1.0, 1.0)
    taller = dragged_to_ratio(start, "br", 0.0, 0.3, 1.0, 1.0)

    assert wider.right == pytest.approx(0.7) and wider.bottom == pytest.approx(0.7)
    assert taller.right == pytest.approx(0.7) and taller.bottom == pytest.approx(0.7)


def test_an_edge_widens_the_frame_about_its_middle():
    start = Crop(0.3, 0.3, 0.5, 0.5)

    out = dragged_to_ratio(start, "r", 0.2, 0.0, 1.0, 1.0)

    assert (out.left, out.right) == pytest.approx((0.3, 0.7))
    assert (out.top + out.bottom) / 2 == pytest.approx(0.4)
    assert out.bottom - out.top == pytest.approx(0.4)


def test_a_frame_cannot_be_dragged_out_of_the_photo_by_its_edge():
    out = dragged_to_ratio(Crop(0.0, 0.4, 0.2, 0.6), "r", 5.0, 0.0, 2.0, 1.0)

    assert inside(out)
    assert out.left == 0.0


def test_moving_the_frame_does_not_change_its_shape_or_the_free_drag():
    start = Crop(0.2, 0.2, 0.5, 0.4)

    assert dragged_to_ratio(start, "move", 0.1, 0.1, 1.5, 1.0) == dragged(start, "move", 0.1, 0.1)


def test_the_smallest_crop_is_a_share_of_the_photo():
    assert crop_frame.MIN_CROP == 0.02
