import numpy as np
import pytest

from lumina.core.edit import colour
from lumina.core.edit.params import Colour, Matte, Params
from lumina.core.edit.pipeline import render


def patch(rgb, size=4):
    """A patch of one linear colour, floats in [0, 1]."""
    return np.ones((size, size, 3), dtype=np.float32) * np.array(rgb, dtype=np.float32)


def chroma(out):
    # How far from grey, on the encoded values
    out = out.astype(np.float64)
    return float((out.max(axis=-1) - out.min(axis=-1)).mean())


def hue_and_luma(rgb):
    rgb = np.asarray(rgb, dtype=np.float64)
    return rgb - rgb @ colour.LUMA, rgb @ colour.LUMA


MUTED = [0.30, 0.22, 0.15]  # a dull brown-orange
STRONG = [0.05, 0.30, 0.01]  # a strong green
SKIN = [0.45, 0.28, 0.20]
BLUE_MUTED = [0.15, 0.20, 0.32]


@pytest.mark.parametrize("bits", [8, 16])
def test_zero_changes_nothing(bits):
    image = np.random.default_rng(1).random((16, 16, 3), dtype=np.float32)

    same = render(image, Params(colour=Colour()), bits=bits)

    assert np.array_equal(same, render(image, Params(), bits=bits))


@pytest.mark.parametrize("control", ["vibrance", "saturation"])
@pytest.mark.parametrize("value", [-100, -40, 40, 100])
def test_grey_stays_grey(control, value):
    ramp = np.linspace(0, 1, 64, dtype=np.float32)
    image = np.repeat(ramp[None, :, None], 3, axis=2)

    out = render(image, Params(colour=Colour(**{control: value})), bits=16).astype(np.int32)

    assert np.abs(out[..., 0] - out[..., 1]).max() <= 1
    assert np.abs(out[..., 0] - out[..., 2]).max() <= 1
    assert np.abs(out - render(image, Params(), bits=16)).max() <= 1


def test_saturation_down_to_minus_100_is_monochrome_with_the_luma_kept():
    before = render(patch(MUTED), Params(), bits=16)[0, 0] / 65535.0
    out = render(patch(MUTED), Params(colour=Colour(saturation=-100)), bits=16)[0, 0] / 65535.0

    assert out.max() - out.min() < 1e-3
    assert out[0] == pytest.approx(before @ colour.LUMA, abs=2e-3)


def test_saturation_scales_the_colour_and_keeps_luma_and_hue():
    before = render(patch(MUTED), Params(), bits=16)[0, 0] / 65535.0
    more = render(patch(MUTED), Params(colour=Colour(saturation=50)), bits=16)[0, 0] / 65535.0
    less = render(patch(MUTED), Params(colour=Colour(saturation=-50)), bits=16)[0, 0] / 65535.0

    d0, l0 = hue_and_luma(before)
    for after, factor in ((more, 1.5), (less, 0.5)):
        d, luma = hue_and_luma(after)
        assert luma == pytest.approx(l0, abs=1e-3)
        assert d == pytest.approx(d0 * factor, abs=2e-3)


def test_vibrance_lifts_muted_colours_more_than_strong_ones():
    def gain(rgb):
        base = chroma(render(patch(rgb), Params()))
        return chroma(render(patch(rgb), Params(colour=Colour(vibrance=60)))) / base

    assert gain(BLUE_MUTED) > 1.3
    assert gain(BLUE_MUTED) > gain(STRONG) + 0.2


def test_vibrance_leaves_skin_tones_more_alone_than_other_hues():
    # The same chroma and luma-ish, a skin hue against a blue one
    def gain(rgb):
        base = chroma(render(patch(rgb), Params()))
        return chroma(render(patch(rgb), Params(colour=Colour(vibrance=60)))) / base

    assert 1.0 < gain(SKIN) < gain(BLUE_MUTED)
    # Saturation does not know about skin
    skin = render(patch(SKIN), Params(colour=Colour(saturation=40)))
    assert chroma(skin) / chroma(render(patch(SKIN), Params())) == pytest.approx(1.4, abs=0.05)


def test_negative_vibrance_pulls_muted_colours_to_grey_before_strong_ones():
    def keep(rgb):
        base = chroma(render(patch(rgb), Params()))
        return chroma(render(patch(rgb), Params(colour=Colour(vibrance=-60)))) / base

    assert keep(BLUE_MUTED) < keep(STRONG)


@pytest.mark.parametrize("control", ["vibrance", "saturation"])
def test_a_boost_never_clips_a_channel_it_did_not_have_at_the_edge(control):
    rng = np.random.default_rng(7)
    image = rng.random((64, 64, 3), dtype=np.float32)
    plain = render(image, Params(), bits=16).astype(np.int32)

    out = render(image, Params(colour=Colour(**{control: 100})), bits=16).astype(np.int32)

    # The luma stays and no channel is out of range, so none was clipped to get there
    luma = np.array(colour.LUMA, dtype=np.float64)
    assert np.abs(out @ luma - plain @ luma).max() <= 3.0
    # And the hue stays: the colour only moves along its line through the grey
    before, after = plain - (plain @ luma)[..., None], out - (out @ luma)[..., None]
    cross = np.cross(before, after)
    scale = np.linalg.norm(before, axis=-1) * np.linalg.norm(after, axis=-1) + 1e-9
    assert (np.linalg.norm(cross, axis=-1) / scale).max() < 0.01


def test_a_colour_at_the_edge_of_the_range_is_not_pushed_over_it():
    out = render(patch([1.0, 0.0, 0.0]), Params(colour=Colour(saturation=100)))

    assert out[0, 0].tolist() == [255, 0, 0]


def test_the_ramp_of_a_hue_keeps_rising_in_luma_with_saturation():
    ramp = np.linspace(0, 1, 256, dtype=np.float32)
    image = ramp[None, :, None] * np.array([1.0, 0.6, 0.3], dtype=np.float32)

    out = render(image, Params(colour=Colour(vibrance=100, saturation=100)), bits=16)

    luma = out.astype(np.float64) @ colour.LUMA
    assert (np.diff(luma[0]) >= -2.0).all()


def test_8_and_16_bits_agree_with_colour():
    image = np.random.default_rng(3).random((32, 32, 3), dtype=np.float32)
    edit = Params(colour=Colour(vibrance=45, saturation=-20))

    eight = render(image, edit, bits=8).astype(np.float64)
    sixteen = render(image, edit, bits=16).astype(np.float64) / 257.0

    assert np.abs(eight - sixteen).max() <= 1.0


def test_tiles_match_one_pass_with_colour():
    image = np.random.default_rng(5).random((40, 24, 3), dtype=np.float32)
    edit = Params(exposure=0.3, colour=Colour(vibrance=60, saturation=30))

    assert np.array_equal(render(image, edit, tile_rows=7), render(image, edit, tile_rows=0))


def test_colour_works_with_the_matte_desaturation_and_the_tone_step():
    image = np.random.default_rng(9).random((16, 16, 3), dtype=np.float32)
    edit = Params(
        colour=Colour(saturation=30),
        matte=Matte(shadow_desaturation=0.5),
    )

    out = render(image, edit)

    assert out.shape == (16, 16, 3) and out.dtype == np.uint8
