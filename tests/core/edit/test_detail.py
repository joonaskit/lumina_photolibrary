import numpy as np
import pytest

from lumina.core.edit import dehaze, detail
from lumina.core.edit.params import Params, Presence
from lumina.core.edit.pipeline import render, srgb_decode


def stripes(period, width=1200, height=240, amplitude=0.12, level=0.5):
    """Vertical stripes: a sine of `period` pixels across, in encoded values."""
    encoded = level + amplitude * np.sin(2 * np.pi * np.arange(width) / period)
    row = srgb_decode(encoded).astype(np.float32)
    return np.repeat(np.repeat(row[None, :, None], height, axis=0), 3, axis=2)


def swing(out):
    """Half the range of the first channel along the middle row, in encoded values."""
    row = out[out.shape[0] // 2, :, 0].astype(np.float64) / 65535
    return (row.max() - row.min()) / 2


def gain(image, **controls):
    """How much the edit multiplies the swing of `image` by."""
    plain = swing(render(image, Params(), bits=16))
    return swing(render(image, Params(presence=Presence(**controls)), bits=16)) / plain


# Clarity and texture


@pytest.mark.parametrize("bits", [8, 16])
def test_no_presence_changes_nothing(bits):
    image = np.random.default_rng(1).random((40, 60, 3), dtype=np.float32)

    assert np.array_equal(
        render(image, Params(presence=Presence()), bits=bits), render(image, Params(), bits=bits)
    )


def test_clarity_adds_local_contrast_and_texture_fine_detail():
    fine, medium, coarse = stripes(4), stripes(40), stripes(300)

    # Texture is for the finest detail and leaves broad tones alone
    assert gain(fine, texture=100) > 1.3
    assert gain(medium, texture=100) < 1.1
    assert gain(coarse, texture=100) == pytest.approx(1.0, abs=0.01)
    # Clarity reaches further: medium detail too, and not the broadest tones
    assert gain(medium, clarity=100) > 1.3
    assert gain(medium, clarity=100) > gain(medium, texture=100) + 0.2
    assert gain(coarse, clarity=100) < 1.1


def test_negative_amounts_soften_by_the_same_means():
    fine, medium = stripes(4), stripes(40)

    assert gain(fine, texture=-100) < 0.8
    assert gain(medium, clarity=-100) < 0.7
    # Softer, not gone: the broad tones stay
    assert gain(medium, clarity=-100) > 0.1


def test_more_of_a_control_changes_more():
    image = stripes(40)

    gains = [gain(image, clarity=amount) for amount in (0, 25, 50, 100)]

    assert gains == sorted(gains) and gains[0] == pytest.approx(1.0)


def test_brightness_stays_where_it_was():
    image = stripes(40)
    plain = render(image, Params(), bits=16)[..., 0].astype(np.float64)

    out = render(image, Params(presence=Presence(clarity=100, texture=100)), bits=16)

    assert out[..., 0].astype(np.float64).mean() == pytest.approx(plain.mean(), rel=0.01)


def test_the_colour_of_a_pixel_stays_while_its_brightness_moves():
    rng = np.random.default_rng(3)
    base = rng.random((40, 60, 1), dtype=np.float32) * 0.3 + 0.35
    image = np.concatenate([base * 0.9, base, base * 0.7], axis=2)
    plain = render(image, Params(), bits=16).astype(np.float64)

    out = render(image, Params(presence=Presence(clarity=80, texture=80)), bits=16).astype(
        np.float64
    )

    # The differences between channels are those of the plain render: one shift for all
    assert np.abs((out[..., 0] - out[..., 1]) - (plain[..., 0] - plain[..., 1])).max() < 600
    assert np.abs(out - plain).max() > 1000


def test_a_hard_edge_does_not_grow_a_halo_of_its_own():
    image = np.full((200, 600, 3), srgb_decode(0.25), dtype=np.float32)
    image[:, 300:] = srgb_decode(0.75)
    plain = render(image, Params(), bits=16)[..., 0].astype(np.float64) / 65535

    out = render(image, Params(presence=Presence(clarity=100)), bits=16)[..., 0] / 65535.0

    row = out[100]
    # Across the step the encoded value overshoots a little on each side, not far
    assert row[:300].min() > plain[100, 0] - 0.12
    assert row[300:].max() < plain[100, -1] + 0.12


def test_the_shadows_and_highlights_are_left_more_alone_by_clarity_than_the_midtones():
    def change(level):
        image = stripes(30, level=level, amplitude=0.04)
        return gain(image, clarity=100)

    assert change(0.5) > change(0.06) and change(0.5) > change(0.94)


@pytest.mark.parametrize("rows", [37, 100, 211])
def test_tiles_match_one_pass(rows):
    image = np.clip(np.random.default_rng(5).random((300, 220, 3), dtype=np.float32), 0.05, 0.95)
    edit = Params(presence=Presence(clarity=70, texture=-40, dehaze=35))

    one = render(image, edit, bits=16, tile_rows=0).astype(np.int32)
    tiled = render(image, edit, bits=16, tile_rows=rows).astype(np.int32)

    assert np.abs(one - tiled).max() <= 2


def test_8_and_16_bits_agree_with_presence():
    image = np.random.default_rng(3).random((60, 80, 3), dtype=np.float32)
    edit = Params(presence=Presence(clarity=50, texture=50, dehaze=-30))

    eight = render(image, edit, bits=8).astype(np.float64)
    sixteen = render(image, edit, bits=16).astype(np.float64) / 257.0

    assert np.abs(eight - sixteen).max() <= 1.0


def test_the_sizes_are_shares_of_the_photo_so_a_small_render_is_like_a_large_one():
    # The same photo at two sizes: stripes four times as wide on the larger
    small = gain(stripes(30, width=600), clarity=100)
    large = gain(stripes(120, width=2400), clarity=100)

    assert small == pytest.approx(large, rel=0.08)


def test_the_halo_is_what_the_blurs_reach_and_a_multiple_of_the_copy_they_are_made_on():
    steps = detail.prepare(Presence(clarity=50, texture=50), (4000, 6000))

    halo, align = detail.layout(steps)

    assert align >= 8 and halo % align == 0
    assert halo >= 4 * 0.015 * 6000
    assert detail.layout(detail.prepare(Presence(), (4000, 6000))) == (1, 1)


# Dehaze


def hazy(size=(300, 400), air=0.85, depth=True):
    """A scene of strong dark and light patches, and the haze over it: more towards the
    right, as if farther away. In encoded values; also returns the clear scene."""
    rng = np.random.default_rng(11)
    scene = rng.random((size[0] // 20, size[1] // 20, 3)).astype(np.float32)
    scene = np.kron(scene, np.ones((20, 20, 1), dtype=np.float32)) * 0.9
    transmission = np.linspace(0.8, 0.35, size[1], dtype=np.float32)[None, :, None]
    if not depth:
        transmission = np.full_like(transmission, 0.5)
    seen = scene * transmission + air * (1 - transmission)
    return scene, seen


def linear_of(encoded):
    return srgb_decode(encoded).astype(np.float32)


def test_dehaze_brings_back_contrast_and_dark_tones():
    _scene, seen = hazy()
    image = linear_of(seen)
    plain = render(image, Params(), bits=16).astype(np.float64) / 65535

    out = render(image, Params(presence=Presence(dehaze=100)), bits=16).astype(np.float64) / 65535

    assert out.std() > plain.std() * 1.3
    assert np.percentile(out, 2) < np.percentile(plain, 2) - 0.1


def test_dehaze_is_closer_to_the_clear_scene_than_the_hazy_photo_is():
    scene, seen = hazy()
    out = (
        render(linear_of(seen), Params(presence=Presence(dehaze=100)), bits=16).astype(np.float64)
        / 65535
    )

    error_before = np.abs(seen - scene).mean()
    error_after = np.abs(out - scene).mean()

    assert error_after < error_before * 0.8


def test_dehaze_does_more_where_the_haze_is_thicker():
    _scene, seen = hazy()
    plain = render(linear_of(seen), Params(), bits=16).astype(np.float64) / 65535
    out = (
        render(linear_of(seen), Params(presence=Presence(dehaze=100)), bits=16).astype(np.float64)
        / 65535
    )

    def contrast(image, columns):
        return image[:, columns].std()

    near, far = slice(0, 100), slice(300, 400)
    # The contrast it adds, in relation to what there was: more far away
    assert contrast(out, far) / contrast(plain, far) > contrast(out, near) / contrast(plain, near)


def test_more_dehaze_clears_more():
    _scene, seen = hazy()
    image = linear_of(seen)

    spreads = [
        render(image, Params(presence=Presence(dehaze=a)), bits=16).astype(np.float64).std()
        for a in (0, 30, 60, 100)
    ]

    assert spreads == sorted(spreads) and spreads[-1] > spreads[0] * 1.2


def test_negative_dehaze_puts_haze_in():
    image = linear_of(np.random.default_rng(2).random((120, 160, 3)).astype(np.float32))
    plain = render(image, Params(), bits=16).astype(np.float64) / 65535

    out = render(image, Params(presence=Presence(dehaze=-100)), bits=16).astype(np.float64) / 65535

    assert out.std() < plain.std() * 0.7
    # Towards the colour of the air: the darkest tones come up
    assert out.min() > plain.min() + 0.1


def test_dehaze_of_a_clear_photo_changes_it_little_and_keeps_it_in_range():
    image = linear_of(np.random.default_rng(4).random((120, 160, 3)).astype(np.float32))
    plain = render(image, Params(), bits=16).astype(np.float64) / 65535

    out = render(image, Params(presence=Presence(dehaze=40)), bits=16).astype(np.float64) / 65535

    assert 0.0 <= out.min() and out.max() <= 1.0
    assert np.abs(out - plain).mean() < 0.12


def test_dehaze_is_the_same_whatever_the_tiling():
    _scene, seen = hazy((260, 340))
    image = linear_of(seen)
    edit = Params(presence=Presence(dehaze=70))

    one = render(image, edit, bits=16, tile_rows=0).astype(np.int32)
    tiled = render(image, edit, bits=16, tile_rows=41).astype(np.int32)

    assert np.abs(one - tiled).max() <= 2


def test_dehaze_reads_the_air_from_the_haziest_part():
    _scene, seen = hazy(air=0.9)
    stage = (np.clip(seen, 0, 1) * 65535).astype(np.uint16)

    analysis = dehaze.analyse(stage, 50)

    assert analysis.light == pytest.approx([0.9, 0.9, 0.9], abs=0.12)
    assert analysis.transmission.min() >= dehaze._FLOOR
    assert analysis.transmission.max() <= 1.0
