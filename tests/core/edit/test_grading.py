import numpy as np
import pytest

from lumina.core.edit import colour, grading
from lumina.core.edit.params import Grading, Params, Wheel
from lumina.core.edit.pipeline import render, srgb_decode


def grey(encoded, size=4):
    """A patch of grey, given by its sRGB-encoded value, as linear floats."""
    return np.ones((size, size, 3), dtype=np.float32) * np.float32(srgb_decode(encoded))


def shown(image, edit):
    return render(image, edit, bits=16)[0, 0] / 65535.0


def graded(**wheels):
    return Params(grading=Grading(**wheels))


BLUE_SHADOWS = Wheel(hue=240, saturation=100)
ORANGE_HIGHLIGHTS = Wheel(hue=40, saturation=100)


@pytest.mark.parametrize("bits", [8, 16])
def test_zero_changes_nothing(bits):
    image = np.random.default_rng(1).random((16, 16, 3), dtype=np.float32)

    assert np.array_equal(render(image, graded(), bits=bits), render(image, Params(), bits=bits))


def test_a_wheel_with_no_strength_changes_nothing_whatever_its_hue():
    image = np.random.default_rng(1).random((16, 16, 3), dtype=np.float32)

    out = render(image, graded(shadows=Wheel(hue=123), highlights=Wheel(hue=300)))

    assert np.array_equal(out, render(image, Params()))


def test_the_weights_of_the_ranges_add_up_to_one_and_follow_the_luma():
    luma = np.linspace(0, 1, 101, dtype=np.float32)

    for balance in (-100, 0, 100):
        for blending in (0, 50, 100):
            edges = grading.prepare(Grading(balance=balance, blending=blending))[2]
            shadows, midtones, highlights = grading.weights(luma, edges)
            assert np.allclose(shadows + midtones + highlights, 1.0, atol=1e-5)
            if balance == 0:
                assert shadows[0] > 0.9 and highlights[-1] > 0.9
            assert np.all(np.diff(shadows) <= 1e-6) and np.all(np.diff(highlights) >= -1e-6)


def test_the_shadows_wheel_tints_the_shadows_and_not_the_highlights():
    edit = graded(shadows=BLUE_SHADOWS)

    dark = shown(grey(0.2), edit)
    bright = shown(grey(0.9), edit)

    assert dark[2] > dark[0] + 0.05
    assert np.abs(bright - bright.mean()).max() < 0.002


def test_the_highlights_wheel_tints_the_highlights_and_not_the_shadows():
    edit = graded(highlights=ORANGE_HIGHLIGHTS)

    bright = shown(grey(0.8), edit)
    dark = shown(grey(0.1), edit)

    assert bright[0] > bright[2] + 0.05
    assert np.abs(dark - dark.mean()).max() < 0.002


def test_the_midtones_wheel_tints_the_middle():
    edit = graded(midtones=Wheel(hue=120, saturation=100))

    middle = shown(grey(0.5), edit)
    ends = [shown(grey(v), edit) for v in (0.02, 0.98)]

    assert middle[1] > middle[0] + 0.05
    assert all(np.abs(e - e.mean()).max() < 0.03 for e in ends)


@pytest.mark.parametrize("level", [0.1, 0.3, 0.5, 0.7, 0.9])
def test_a_tint_keeps_the_luma(level):
    edit = graded(
        shadows=BLUE_SHADOWS, midtones=Wheel(hue=100, saturation=80), highlights=ORANGE_HIGHLIGHTS
    )

    out = shown(grey(level), edit)

    assert out @ colour.LUMA == pytest.approx(level, abs=0.003)


def test_a_tint_is_cut_to_what_the_pixel_can_take_so_black_and_white_stay():
    edit = graded(shadows=BLUE_SHADOWS, highlights=ORANGE_HIGHLIGHTS)

    for level in (0.0, 1.0):
        assert np.abs(shown(grey(level), edit) - level).max() < 0.002


def test_a_tint_never_clips_a_channel_of_a_colourful_pixel():
    rng = np.random.default_rng(7)
    image = rng.random((64, 64, 3), dtype=np.float32)
    plain = render(image, Params(), bits=16).astype(np.float64) / 65535
    edit = graded(
        shadows=BLUE_SHADOWS, midtones=Wheel(hue=0, saturation=100), highlights=ORANGE_HIGHLIGHTS
    )

    out = render(image, edit, bits=16).astype(np.float64) / 65535

    assert abs(out @ colour.LUMA - plain @ colour.LUMA).max() < 3e-3


def test_a_wheels_luminance_lightens_or_darkens_its_range_only():
    lighter = graded(shadows=Wheel(luminance=100))
    darker = graded(shadows=Wheel(luminance=-100))

    assert shown(grey(0.2), lighter).mean() > 0.2 + 0.1
    assert shown(grey(0.2), darker).mean() < 0.2 - 0.05
    assert shown(grey(0.95), lighter).mean() == pytest.approx(0.95, abs=0.01)


def test_balance_gives_more_of_the_photo_to_the_highlights_when_positive():
    edit = lambda balance: Params(  # noqa: E731
        grading=Grading(highlights=ORANGE_HIGHLIGHTS, balance=balance)
    )

    def warmth(balance):
        out = shown(grey(0.5), edit(balance))
        return out[0] - out[2]

    assert warmth(100) > warmth(0) + 0.02
    assert warmth(-100) <= warmth(0)


def test_blending_widens_the_transition_between_ranges():
    def reach(blending):
        edit = Params(grading=Grading(shadows=BLUE_SHADOWS, blending=blending))
        out = shown(grey(0.4), edit)
        return out[2] - out[0]

    assert reach(100) > reach(0)


def test_8_and_16_bits_and_tiles_agree_with_grading():
    image = np.random.default_rng(3).random((40, 24, 3), dtype=np.float32)
    edit = graded(shadows=BLUE_SHADOWS, highlights=Wheel(hue=40, saturation=60, luminance=20))

    eight = render(image, edit, bits=8).astype(np.float64)
    sixteen = render(image, edit, bits=16).astype(np.float64) / 257.0

    assert np.abs(eight - sixteen).max() <= 1.0
    assert np.array_equal(render(image, edit, tile_rows=7), render(image, edit, tile_rows=0))
