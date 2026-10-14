import numpy as np
import pytest

from lumina.core.edit import pipeline
from lumina.core.edit.params import Crop, Curves, Matte, Params
from lumina.core.edit.pipeline import render, srgb_decode, srgb_encode


def ramp(width=256, height=8):
    """Linear grey from black to white, left to right, as 16 bits."""
    row = np.linspace(0, 65535, width).astype(np.uint16)
    return np.repeat(np.repeat(row[None, :, None], height, axis=0), 3, axis=2)


def colour(rgb, size=8):
    """A patch of one linear colour, floats in [0, 1]."""
    return np.ones((size, size, 3), dtype=np.float32) * np.array(rgb, dtype=np.float32)


def test_encode_and_decode_undo_each_other():
    x = np.linspace(0, 1, 1001)

    assert np.allclose(srgb_decode(srgb_encode(x)), x, atol=1e-9)
    assert srgb_encode(np.array([0.0, 1.0])) == pytest.approx([0.0, 1.0], abs=1e-12)


@pytest.mark.parametrize("bits", [8, 16])
def test_no_edit_is_the_plain_srgb_encoding(bits):
    image = ramp()

    out = render(image, Params(), bits=bits).astype(np.float64)

    expected = srgb_encode(image[..., 0].astype(np.float64) / 65535) * ((1 << bits) - 1)
    assert np.abs(out[..., 0] - expected).max() <= (1.0 if bits == 8 else 1.5)


def test_one_stop_up_doubles_the_light():
    image = colour([0.1, 0.1, 0.1])

    out = render(image, Params(exposure=1.0), bits=16)

    assert srgb_decode(out[0, 0, 0] / 65535) == pytest.approx(0.2, abs=1e-4)


def test_exposure_clips_at_white():
    out = render(colour([0.6, 0.6, 0.6]), Params(exposure=2.0))

    assert (out == 255).all()


def test_warmer_is_redder_and_less_blue_with_the_same_luminance():
    grey = colour([0.18, 0.18, 0.18])

    warm = render(grey, Params(temperature=50), bits=16)[0, 0] / 65535
    cool = render(grey, Params(temperature=-50), bits=16)[0, 0] / 65535

    assert warm[0] > warm[1] > warm[2]
    assert cool[2] > cool[1] > cool[0]
    for result in (warm, cool):
        assert pipeline.LUMA @ srgb_decode(result) == pytest.approx(0.18, rel=0.01)


def test_positive_tint_is_magenta():
    out = render(colour([0.18, 0.18, 0.18]), Params(tint=50))[0, 0]

    assert out[1] < out[0] and out[1] < out[2]


def test_white_balance_at_zero_changes_nothing():
    assert pipeline.white_balance_gains(0, 0).tolist() == [1, 1, 1]


def test_the_curve_acts_on_encoded_values():
    # A point lifting encoded 0.5 to 0.6: a linear mid grey comes out at encoded 0.6
    curve = Params(curves=Curves(rgb=((0, 0), (0.5, 0.6), (1, 1))))
    mid = float(srgb_decode(np.array(0.5)))

    out = render(colour([mid] * 3), curve, bits=16)[0, 0, 0] / 65535

    assert out == pytest.approx(0.6, abs=1e-3)


def test_a_channel_curve_moves_only_its_channel():
    out = render(colour([0.2, 0.2, 0.2]), Params(curves=Curves(blue=((0, 0.2), (1, 1)))))[0, 0]

    assert out[2] > out[0] == out[1]


def test_matte_raises_black_and_lowers_white():
    matte = Params(matte=Matte(black=0.1, white=0.05))

    out = render(ramp(), matte, bits=16)[0, :, 0] / 65535

    assert out.min() == pytest.approx(0.1, abs=1e-3)
    assert out.max() == pytest.approx(0.95, abs=1e-3)


def test_shadow_desaturation_greys_the_shadows_and_leaves_highlights():
    shadow, highlight = colour([0.02, 0.005, 0.005]), colour([0.8, 0.4, 0.4])
    matte = Params(matte=Matte(shadow_desaturation=1.0))

    def spread(image, edit):
        rgb = render(image, edit, bits=16)[0, 0].astype(int)
        return rgb.max() - rgb.min()

    assert spread(shadow, matte) < spread(shadow, Params()) * 0.5
    assert spread(highlight, matte) == spread(highlight, Params())


def test_quarter_turns_and_crop():
    image = np.zeros((40, 60, 3), dtype=np.uint16)
    image[0, 0] = 65535  # top left marked

    turned = render(image, Params(rotate=1))
    assert turned.shape == (60, 40, 3)
    assert turned[0, -1, 0] == 255  # a quarter turn clockwise: top right

    cropped = render(image, Params(crop=Crop(0.5, 0.25, 1.0, 0.75)))
    assert cropped.shape == (20, 30, 3)
    assert pipeline.output_size(60, 40, Params(rotate=1, crop=Crop(0, 0, 0.5, 1))) == (20, 60)


@pytest.mark.parametrize("bits", [8, 16])
def test_tiles_give_the_same_image_as_one_pass(bits):
    rng = np.random.default_rng(1)
    image = rng.integers(0, 65535, size=(37, 23, 3), dtype=np.uint16)
    edit = Params(
        exposure=0.3,
        temperature=20,
        curves=Curves(rgb=((0, 0), (0.4, 0.5), (1, 1))),
        matte=Matte(black=0.05, shadow_desaturation=0.6),
        rotate=3,
    )

    whole = render(image, edit, bits=bits, tile_rows=None)
    tiled = render(image, edit, bits=bits, tile_rows=5)

    assert np.array_equal(whole, tiled)


def test_8_and_16_bits_agree():
    edit = Params(exposure=-0.5, curves=Curves(rgb=((0, 0.1), (0.5, 0.45), (1, 1))))

    eight = render(ramp(), edit).astype(np.float64)
    sixteen = render(ramp(), edit, bits=16).astype(np.float64) / 257

    assert np.abs(eight - sixteen).max() <= 1.0


def test_the_input_is_not_changed():
    image = ramp()
    before = image.copy()

    render(image, Params(exposure=1, matte=Matte(shadow_desaturation=1.0)), bits=16)

    assert np.array_equal(image, before)


# Mirroring and straightening


def marked(height=40, width=60):
    """Black, with the top left pixel white."""
    image = np.zeros((height, width, 3), dtype=np.uint16)
    image[0, 0] = 65535
    return image


def test_mirroring():
    assert render(marked(), Params(flip_horizontal=True))[0, -1, 0] == 255
    assert render(marked(), Params(flip_vertical=True))[-1, 0, 0] == 255
    assert render(marked(), Params(flip_horizontal=True, flip_vertical=True))[-1, -1, 0] == 255


def asymmetric(height=7, width=11):
    rng = np.random.default_rng(3)
    return rng.integers(0, 65535, size=(height, width, 3), dtype=np.uint16)


@pytest.mark.parametrize("rotate", range(4))
@pytest.mark.parametrize("flip_horizontal", [False, True])
@pytest.mark.parametrize("flip_vertical", [False, True])
def test_turning_an_edit_turns_what_it_shows(rotate, flip_horizontal, flip_vertical):
    image = asymmetric()
    edit = Params(
        rotate=rotate,
        flip_horizontal=flip_horizontal,
        flip_vertical=flip_vertical,
        crop=Crop(0.1, 0.2, 0.9, 0.8),
    )
    shown = render(image, edit)

    assert np.array_equal(render(image, edit.turned(1)), np.rot90(shown, k=-1))
    assert np.array_equal(render(image, edit.turned(-1)), np.rot90(shown, k=1))
    assert np.array_equal(render(image, edit.flipped(True)), shown[:, ::-1])
    assert np.array_equal(render(image, edit.flipped(False)), shown[::-1])


def test_straightening_by_nothing_changes_nothing():
    image = asymmetric()

    assert np.array_equal(render(image, Params(straighten=0.0)), render(image, Params()))


def test_straightening_keeps_the_size_and_leaves_no_empty_corners():
    grey = np.full((40, 60, 3), 30000, dtype=np.uint16)

    for degrees in (-45, -10, 3.5, 30):
        out = render(grey, Params(straighten=degrees), bits=16)
        assert out.shape == (40, 60, 3)
        # Filled: every pixel, corners too, came from inside the photo
        assert out.min() == out.max()


def test_a_positive_angle_turns_clockwise():
    # Dark left half, light right half: turned clockwise, the line between them leans
    # right at the top, so just right of the centre at the top is still dark
    image = np.zeros((100, 100, 3), dtype=np.uint16)
    image[:, 50:] = 65535

    clockwise = render(image, Params(straighten=10))
    counter = render(image, Params(straighten=-10))

    assert clockwise[2, 55, 0] < 64 and counter[2, 55, 0] > 192
    assert clockwise[97, 45, 0] > 192 and counter[97, 45, 0] < 64


def test_straightening_is_sampled_only_inside_the_crop_and_matches_the_whole():
    image = asymmetric(40, 60)
    whole = render(image, Params(straighten=7.0), bits=16)

    cropped = render(image, Params(straighten=7.0, crop=Crop(0.25, 0.5, 0.75, 1.0)), bits=16)

    assert np.array_equal(cropped, whole[20:40, 15:45])


def test_mirroring_a_straightened_edit_mirrors_what_it_shows():
    image = asymmetric(30, 50)
    edit = Params(straighten=6.0, crop=Crop(0.1, 0.1, 0.7, 0.9))

    shown = render(image, edit).astype(int)
    mirrored = render(image, edit.flipped(True)).astype(int)

    assert np.abs(mirrored - shown[:, ::-1]).max() <= 1


@pytest.mark.parametrize("bits", [8, 16])
def test_straightened_tiles_match_one_pass(bits, monkeypatch):
    image = asymmetric(37, 23)
    edit = Params(straighten=-12.0, flip_vertical=True, crop=Crop(0, 0.1, 0.9, 1))
    whole = render(image, edit, bits=bits, tile_rows=None)
    monkeypatch.setattr(pipeline, "_STRAIGHTEN_ROWS", 4)

    assert np.array_equal(render(image, edit, bits=bits, tile_rows=5), whole)


def test_output_size_follows_turns_and_crop_not_straightening():
    edit = Params(rotate=1, straighten=12, crop=Crop(0, 0, 0.5, 1))

    assert pipeline.output_size(60, 40, edit) == (20, 60)
    assert render(np.zeros((40, 60, 3), np.uint16), edit).shape[:2] == (60, 20)
