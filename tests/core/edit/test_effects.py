import numpy as np
import pytest

from lumina.core.edit import effects
from lumina.core.edit.params import Crop, Grain, Params, Vignette
from lumina.core.edit.pipeline import render, srgb_decode


def grey(encoded, size=(240, 360)):
    """A uniform photo of one sRGB-encoded grey, as linear floats."""
    return np.full((*size, 3), srgb_decode(encoded), dtype=np.float32)


def difference(image, edit):
    """What `edit` changes in the first channel of `image`, as encoded values in [0, 1]."""
    out = render(image, edit, bits=16)[..., 0].astype(np.float64)
    return out / 65535.0 - render(image, Params(), bits=16)[..., 0] / 65535.0


def autocorrelation_length(noise):
    """How far along a row, in pixels, grain stays correlated: to where it is half."""
    row = noise[noise.shape[0] // 2] - noise[noise.shape[0] // 2].mean()
    correlation = np.correlate(row, row, "full")[len(row) - 1 :]
    return int(np.argmax(correlation / correlation[0] < 0.5))


# Vignette


@pytest.mark.parametrize("bits", [8, 16])
def test_no_vignette_and_no_grain_change_nothing(bits):
    image = np.random.default_rng(1).random((24, 32, 3), dtype=np.float32)
    # With no amount, the rest is of no matter
    quiet = Params(
        vignette=Vignette(amount=0, midpoint=10, feather=90), grain=Grain(amount=0, size=90)
    )

    assert np.array_equal(render(image, quiet, bits=bits), render(image, Params(), bits=bits))


def test_a_dark_vignette_leaves_the_centre_and_darkens_the_corners():
    out = render(grey(0.5), Params(vignette=Vignette(amount=-100)), bits=16)[..., 0] / 65535.0

    assert out[120, 180] == pytest.approx(0.5, abs=0.002)
    assert out[0, 0] < 0.02
    assert out[0, 0] <= out[0, 60] <= out[60, 120] <= out[120, 180] + 1e-6


def test_a_light_vignette_lightens_the_corners():
    out = render(grey(0.5), Params(vignette=Vignette(amount=100)), bits=16)[..., 0] / 65535.0

    assert out[120, 180] == pytest.approx(0.5, abs=0.002)
    assert out[0, 0] > 0.98


def test_a_stronger_vignette_changes_more():
    def corner(amount):
        return difference(grey(0.5), Params(vignette=Vignette(amount=amount)))[0, 0]

    assert corner(-100) < corner(-50) < corner(-10) < 0
    assert corner(10) < corner(50) < corner(100)


def test_the_vignette_is_the_same_in_the_four_corners_and_follows_the_frame():
    out = difference(grey(0.5), Params(vignette=Vignette(amount=-100)))

    corners = [out[0, 0], out[0, -1], out[-1, 0], out[-1, -1]]
    assert max(corners) - min(corners) < 0.003
    # The middle of a long edge and of a short one are as far from the centre, as a
    # share of the frame
    assert out[120, 0] == pytest.approx(out[0, 180], abs=0.01)


def test_a_higher_midpoint_keeps_the_vignette_further_out():
    def at_edge(midpoint):
        edit = Params(vignette=Vignette(amount=-100, midpoint=midpoint))
        return difference(grey(0.5), edit)[120, 0]

    assert at_edge(10) < at_edge(50) < at_edge(90)


def test_a_wider_feather_makes_the_vignette_come_in_more_gradually():
    def steepest(feather):
        edit = Params(vignette=Vignette(amount=-100, midpoint=60, feather=feather))
        along = difference(grey(0.5), edit)[120]
        return np.abs(np.diff(along)).max()

    assert steepest(0) > steepest(50) > steepest(100)


def test_the_vignette_follows_the_crop_not_the_original():
    image = grey(0.5, size=(400, 600))
    crop = Crop(left=0.25, top=0.25, right=0.75, bottom=0.75)

    out = render(image, Params(crop=crop, vignette=Vignette(amount=-100)), bits=16)[..., 0]

    # The corners of the crop, which are in the middle of the original, are dark
    assert out[0, 0] / 65535.0 < 0.02 and out[-1, -1] / 65535.0 < 0.02
    h, w = out.shape
    assert out[h // 2, w // 2] / 65535.0 == pytest.approx(0.5, abs=0.002)


def test_a_small_render_has_the_vignette_of_a_large_one():
    edit = Params(vignette=Vignette(amount=-80, midpoint=40, feather=30))
    small = difference(grey(0.5, size=(100, 150)), edit)
    large = difference(grey(0.5, size=(400, 600)), edit)

    # The same places, a quarter of the size: read at the same shares of the frame
    for share_y, share_x in ((0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (0.25, 0.25), (0.5, 0.5)):
        a = small[min(int(share_y * 100), 99), min(int(share_x * 150), 149)]
        b = large[min(int(share_y * 400), 399), min(int(share_x * 600), 599)]
        assert a == pytest.approx(b, abs=0.03)


# Grain


def test_grain_is_the_same_every_time_and_every_tiling():
    image = grey(0.5)
    edit = Params(grain=Grain(amount=70, size=40), vignette=Vignette(amount=-40))

    once = render(image, edit)

    assert np.array_equal(once, render(image, edit))
    assert np.array_equal(once, render(image, edit, tile_rows=37))
    assert np.array_equal(once, render(image, edit, tile_rows=0))


def test_8_and_16_bits_agree_with_grain_and_vignette():
    image = np.random.default_rng(3).random((40, 60, 3), dtype=np.float32)
    edit = Params(grain=Grain(amount=50, size=30), vignette=Vignette(amount=-60))

    eight = render(image, edit, bits=8).astype(np.float64)
    sixteen = render(image, edit, bits=16).astype(np.float64) / 257.0

    assert np.abs(eight - sixteen).max() <= 1.0


@pytest.mark.parametrize("size", [0, 25, 100])
def test_grain_has_the_strength_asked_for_at_any_size(size):
    noise = difference(grey(0.5), Params(grain=Grain(amount=50, size=size)))

    assert noise.std() == pytest.approx(0.04, rel=0.1)
    assert abs(noise.mean()) < 0.003


def test_grain_is_the_same_on_all_channels():
    image = grey(0.5)
    out = render(image, Params(grain=Grain(amount=60)), bits=16).astype(np.int32)

    assert np.abs(out[..., 0] - out[..., 1]).max() <= 1
    assert np.abs(out[..., 0] - out[..., 2]).max() <= 1


def test_more_grain_is_stronger():
    def spread(amount):
        return difference(grey(0.5), Params(grain=Grain(amount=amount))).std()

    assert spread(20) < spread(50) < spread(100)


def test_a_larger_size_is_coarser_grain():
    def length(size):
        return autocorrelation_length(
            difference(grey(0.5), Params(grain=Grain(size=size, amount=80)))
        )

    assert length(0) <= length(40) < length(100)


def test_grain_is_the_same_size_in_a_small_render_and_a_large_one():
    # Coarse enough that neither is held to one pixel, the least a grain can be
    edit = Params(grain=Grain(amount=60, size=100))
    small = difference(grey(0.5, size=(400, 600)), edit)
    large = difference(grey(0.5, size=(1600, 2400)), edit)

    assert small.std() == pytest.approx(large.std(), rel=0.1)
    # The same share of the photo: four times as many pixels along it
    ratio = autocorrelation_length(large) / autocorrelation_length(small)
    assert 3.0 < ratio < 5.5


def test_grain_is_weaker_in_the_shadows_and_gone_from_pure_black_and_white():
    edit = Params(grain=Grain(amount=100))

    middle = difference(grey(0.5), edit).std()
    dark = difference(grey(0.04), edit).std()

    assert dark < middle * 0.7
    for level in (0.0, 1.0):
        out = render(grey(level), edit, bits=16)
        assert (out == render(grey(level), Params(), bits=16)).all()


def test_the_noise_of_a_lattice_is_even_and_not_a_pattern():
    ix = np.arange(300)[None, :]
    iy = np.arange(300)[:, None]

    noise = effects._lattice(ix, iy)

    assert noise.min() >= -1 and noise.max() <= 1
    assert abs(noise.mean()) < 0.01
    assert noise.std() == pytest.approx(1 / np.sqrt(3), rel=0.02)
    # Neighbours do not follow each other
    assert abs(np.corrcoef(noise[:, :-1].ravel(), noise[:, 1:].ravel())[0, 1]) < 0.02
    assert abs(np.corrcoef(noise[:-1].ravel(), noise[1:].ravel())[0, 1]) < 0.02
