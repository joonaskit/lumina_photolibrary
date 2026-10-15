import colorsys

import numpy as np
import pytest

from lumina.core.edit import colour, hsl, params
from lumina.core.edit.params import Band, Hsl, Params
from lumina.core.edit.pipeline import render, srgb_decode


def patch(encoded, size=4):
    """A patch of one colour, given by its sRGB-encoded values, as linear floats."""
    linear = srgb_decode(np.array(encoded, dtype=np.float64)).astype(np.float32)
    return np.ones((size, size, 3), dtype=np.float32) * linear


def shown(image, edit):
    """The first pixel rendered, as 16-bit codes scaled to [0, 1]."""
    return render(image, edit, bits=16)[0, 0] / 65535.0


def hue_of(rgb):
    return colorsys.rgb_to_hsv(*rgb)[0] * 360.0


def luma_of(rgb):
    return float(np.asarray(rgb) @ colour.LUMA)


def only(band, **values):
    return Params(hsl=Hsl(**{band: Band(**values)}))


RED, YELLOW, GREEN, BLUE, MAGENTA = (
    [0.8, 0.2, 0.2],
    [0.8, 0.8, 0.2],
    [0.2, 0.7, 0.2],
    [0.2, 0.2, 0.8],  # hue 240, the centre of the blue band
    [0.8, 0.2, 0.7],
)


def test_the_bands_are_the_params_bands_in_order():
    assert len(hsl.CENTRES) == len(params.BANDS) == 8
    assert list(hsl.CENTRES) == sorted(hsl.CENTRES)


def test_the_hue_index_is_the_hue_to_half_a_degree():
    rng = np.random.default_rng(1)
    pixels = rng.random((60, 60, 3), dtype=np.float32)
    r, g, b = (pixels[..., c].copy() for c in range(3))
    high, low = np.maximum(np.maximum(r, g), b), np.minimum(np.minimum(r, g), b)

    found = (hsl.hue_index(r, g, b, high, low) / 2.0 - 60.0) % 360.0

    expected = np.array([hue_of(p) for p in pixels.reshape(-1, 3)]).reshape(60, 60)
    miss = np.abs(found - expected)
    assert np.minimum(miss, 360 - miss).max() <= 0.51


def test_a_band_has_all_of_its_control_at_its_centre_and_none_at_its_neighbours():
    bands = Hsl(blue=Band(saturation=100))
    _, saturation, _ = hsl.tables(bands)

    def at(degrees):
        # The tables start at hue -60
        return saturation[round((degrees - 360 * (degrees > 300) + 60) * 2)]

    assert at(240) == pytest.approx(1.0)
    assert at(180) == pytest.approx(0.0, abs=1e-6)
    assert at(270) == pytest.approx(0.0, abs=1e-6)
    assert 0.0 < at(210) < 1.0
    # Beyond the neighbours' centres, nothing
    assert at(120) == 0 and at(300) == 0 and at(0) == 0 and at(359.5) == 0


def test_a_table_that_is_all_zero_is_none():
    assert hsl.tables(Hsl(red=Band(hue=50)))[1] is None


@pytest.mark.parametrize("bits", [8, 16])
def test_zero_changes_nothing(bits):
    image = np.random.default_rng(1).random((16, 16, 3), dtype=np.float32)

    assert np.array_equal(
        render(image, Params(hsl=Hsl()), bits=bits), render(image, Params(), bits=bits)
    )


@pytest.mark.parametrize("colour_", [RED, YELLOW, GREEN, MAGENTA])
def test_a_band_that_does_not_reach_a_colour_leaves_it_alone(colour_):
    edit = only("blue", hue=100, saturation=100, luminance=-100)

    after, before = shown(patch(colour_), edit), shown(patch(colour_), Params())

    assert np.abs(after - before).max() < 2 / 65535


def test_a_band_changes_the_colours_it_reaches():
    edit = only("blue", saturation=-100)

    out = shown(patch(BLUE), edit)

    assert out.max() - out.min() < 2e-3


@pytest.mark.parametrize("band", params.BANDS)
def test_grey_stays_grey_whatever_the_bands_say(band):
    ramp = np.linspace(0, 1, 64, dtype=np.float32)
    image = np.repeat(ramp[None, :, None], 3, axis=2)
    edit = only(band, hue=100, saturation=100, luminance=100)

    out = render(image, edit, bits=16).astype(np.int32)

    assert np.abs(out - render(image, Params(), bits=16)).max() <= 1


def test_hue_turns_a_colour_the_way_the_hue_circle_goes_and_keeps_its_luma():
    before = shown(patch(RED), Params())

    plus = shown(patch(RED), only("red", hue=100))
    minus = shown(patch(RED), only("red", hue=-100))

    # Red towards orange and yellow (hue up), or towards magenta (down: 360 less)
    assert 10 < hue_of(plus) < 40
    assert 320 < hue_of(minus) < 350
    for out in (plus, minus):
        assert luma_of(out) == pytest.approx(luma_of(before), abs=0.01)


def test_hue_is_proportional_to_its_control():
    half = hue_of(shown(patch(RED), only("red", hue=50)))
    full = hue_of(shown(patch(RED), only("red", hue=100)))

    assert half == pytest.approx(full / 2, rel=0.2)


def test_saturation_scales_the_distance_from_grey_in_the_band():
    before = shown(patch(BLUE), Params())
    less = shown(patch(BLUE), only("blue", saturation=-50))
    more = shown(patch(BLUE), only("blue", saturation=50))

    def spread(rgb):
        return np.linalg.norm(rgb - luma_of(rgb))

    assert spread(less) == pytest.approx(spread(before) * 0.5, rel=0.05)
    assert spread(more) > spread(before) * 1.2
    for out in (less, more):
        assert luma_of(out) == pytest.approx(luma_of(before), abs=0.005)


def test_luminance_lightens_and_darkens_the_band():
    before = luma_of(shown(patch(GREEN), Params()))

    lighter = luma_of(shown(patch(GREEN), only("green", luminance=100)))
    darker = luma_of(shown(patch(GREEN), only("green", luminance=-100)))

    assert lighter > before + 0.1
    assert darker < before - 0.1


@pytest.mark.parametrize("band", params.BANDS)
def test_colours_stay_in_range_and_keep_their_luma_when_turned(band):
    rng = np.random.default_rng(5)
    image = rng.random((64, 64, 3), dtype=np.float32)
    plain = render(image, Params(), bits=16).astype(np.float64) / 65535
    edit = only(band, hue=100, saturation=100)

    out = render(image, edit, bits=16).astype(np.float64) / 65535

    assert abs(out @ colour.LUMA - plain @ colour.LUMA).max() < 3e-3


def test_8_and_16_bits_agree_with_the_bands():
    image = np.random.default_rng(3).random((32, 32, 3), dtype=np.float32)
    edit = Params(hsl=Hsl(red=Band(hue=40, luminance=20), blue=Band(saturation=-30)))

    eight = render(image, edit, bits=8).astype(np.float64)
    sixteen = render(image, edit, bits=16).astype(np.float64) / 257.0

    assert np.abs(eight - sixteen).max() <= 1.0


def test_tiles_match_one_pass_with_the_bands():
    image = np.random.default_rng(5).random((40, 24, 3), dtype=np.float32)
    edit = Params(exposure=0.3, hsl=Hsl(green=Band(hue=-60, saturation=40, luminance=-20)))

    assert np.array_equal(render(image, edit, tile_rows=7), render(image, edit, tile_rows=0))
