import cv2
import numpy as np
import pytest

from lumina.core.edit import colour, denoise, sharpen
from lumina.core.edit.params import NoiseReduction, Params, Sharpening
from lumina.core.edit.pipeline import render, srgb_decode


def linear_of(encoded):
    return srgb_decode(np.clip(encoded, 0, 1)).astype(np.float32)


def shown(image, edit, **kwargs):
    """The render as encoded values in [0, 1]."""
    return render(image, edit, bits=16, **kwargs).astype(np.float64) / 65535


def soft_edge(low=0.3, high=0.6, width=300, height=80, blur=1.2):
    encoded = np.full((height, width, 3), low, dtype=np.float32)
    encoded[:, width // 2 :] = high
    return linear_of(cv2.GaussianBlur(encoded, (0, 0), blur))


def noisy(sigma=0.04, level=0.35, step=None, seed=0):
    """Flat grey with noise of its own in each channel (so, noise in brightness and in
    colour), and with `step` a jump to a brighter half."""
    rng = np.random.default_rng(seed)
    base = np.full((300, 600), level, dtype=np.float32)
    if step:
        base[:, 300:] += step
    encoded = base[..., None] + rng.normal(0, sigma, (300, 600, 3)).astype(np.float32)
    return linear_of(encoded)


def luma_std(out, window=(slice(50, 250), slice(50, 250))):
    return (out[window] @ colour.LUMA).std()


def chroma_std(out, window=(slice(50, 250), slice(50, 250))):
    patch = out[window]
    luma = patch @ colour.LUMA
    return float(np.hypot((patch[..., 0] - luma).std(), (patch[..., 2] - luma).std()))


# Sharpening


@pytest.mark.parametrize("bits", [8, 16])
def test_no_sharpening_and_no_noise_reduction_change_nothing(bits):
    image = np.random.default_rng(1).random((40, 60, 3), dtype=np.float32)
    quiet = Params(
        sharpening=Sharpening(amount=0, radius=2.5, detail=80, masking=60),
        noise=NoiseReduction(luminance=0, detail=10, colour=0),
    )

    assert np.array_equal(render(image, quiet, bits=bits), render(image, Params(), bits=bits))


def test_sharpening_makes_an_edge_steeper_and_keeps_the_plateaus():
    image = soft_edge()
    plain = shown(image, Params())[40, :, 0]

    out = shown(image, Params(sharpening=Sharpening(amount=100)))[40, :, 0]

    def steepest(row):
        return np.abs(np.diff(row)).max()

    assert steepest(out) > steepest(plain) * 1.15
    assert out[:100].mean() == pytest.approx(plain[:100].mean(), abs=0.002)
    assert out[-100:].mean() == pytest.approx(plain[-100:].mean(), abs=0.002)
    # Contrast right at the edge goes up, a little over and under it
    assert out.max() > plain.max() and out.min() < plain.min()


def test_more_amount_sharpens_more():
    image = soft_edge()

    overshoots = [
        shown(image, Params(sharpening=Sharpening(amount=a)))[40, :, 0].max()
        for a in (0, 50, 100, 150)
    ]

    assert overshoots == sorted(overshoots) and overshoots[-1] > overshoots[0] + 0.005


def test_a_larger_radius_reaches_further_from_the_edge():
    image = soft_edge(blur=2.5)

    def reach(radius):
        plain = shown(image, Params())[40, :, 0]
        out = shown(image, Params(sharpening=Sharpening(amount=150, radius=radius, detail=100)))
        return int(np.sum(np.abs(out[40, :, 0] - plain) > 0.004))

    assert reach(0.6) < reach(1.5) < reach(3.0)


def test_detail_holds_halos_down_at_a_strong_edge():
    encoded = np.full((80, 300, 3), 0.15, dtype=np.float32)
    encoded[:, 150:] = 0.85
    image = linear_of(cv2.GaussianBlur(encoded, (0, 0), 1.0))
    plain = shown(image, Params())[40, :, 0]

    def halo(detail):
        out = shown(image, Params(sharpening=Sharpening(amount=150, detail=detail)))[40, :, 0]
        return out.max() - plain.max()

    assert halo(0) < halo(50) < halo(100)


def test_masking_keeps_the_sharpening_off_flat_noise_and_on_edges():
    image = noisy(step=0.3)
    plain = shown(image, Params())

    def change(masking, window):
        out = shown(image, Params(sharpening=Sharpening(amount=100, masking=masking)))
        return np.abs(out - plain)[window].mean()

    flat, edge = (slice(50, 250), slice(50, 250)), (slice(None), slice(295, 305))
    assert change(0, flat) > 0.005
    assert change(60, flat) < change(0, flat) * 0.1
    assert change(60, edge) > 0.002
    assert change(100, edge) <= change(60, edge) + 1e-6


def test_the_colour_of_a_pixel_stays_while_sharpening_moves_its_brightness():
    image = soft_edge()
    image = image * np.array([1.0, 0.8, 0.5], dtype=np.float32)
    plain = shown(image, Params())

    out = shown(image, Params(sharpening=Sharpening(amount=120)))

    assert np.abs((out[..., 0] - out[..., 1]) - (plain[..., 0] - plain[..., 1])).max() < 0.01
    assert np.abs(out - plain).max() > 0.01


def test_a_small_render_is_sharpened_by_the_radius_it_would_have_at_its_size():
    # A quarter-size render of a photo whose radius is a pixel is a quarter of a pixel:
    # less than a blur can do, so nothing is done; at full size the same edge is sharpened
    edit = Params(sharpening=Sharpening(amount=150))
    small = soft_edge(width=100, height=20, blur=0.4)

    assert np.array_equal(
        render(small, edit, full=(400, 80)), render(small, Params(), full=(400, 80))
    )
    assert not np.array_equal(render(small, edit), render(small, Params()))


def test_sharpening_is_the_same_whatever_the_tiling():
    image = np.clip(np.random.default_rng(5).random((300, 200, 3), dtype=np.float32), 0.05, 0.95)
    edit = Params(sharpening=Sharpening(amount=90, radius=2.0, masking=30))

    one = render(image, edit, bits=16, tile_rows=0).astype(np.int32)
    tiled = render(image, edit, bits=16, tile_rows=37).astype(np.int32)

    assert np.abs(one - tiled).max() <= 2


def test_the_steepness_of_an_edge_is_the_same_at_any_size_of_render():
    # The same edge at full size and at half: the same steepness per pixel of the full size
    full = soft_edge(width=400, height=60, blur=2.0)[..., 0].astype(np.float32)
    half = cv2.resize(full, (200, 30), interpolation=cv2.INTER_AREA)

    at_full = sharpen._steepness(full, 1.0).max()
    at_half = sharpen._steepness(half, 0.5).max()

    assert at_half == pytest.approx(at_full, rel=0.25)


# Noise reduction


def test_luminance_noise_reduction_takes_out_brightness_noise_and_leaves_the_colour():
    image = noisy()
    plain = shown(image, Params())

    out = shown(image, Params(noise=NoiseReduction(luminance=100, detail=0)))

    assert luma_std(out) < luma_std(plain) * 0.4
    assert chroma_std(out) == pytest.approx(chroma_std(plain), rel=0.05)


def test_more_luminance_reduction_takes_out_more():
    image = noisy()

    spreads = [
        luma_std(shown(image, Params(noise=NoiseReduction(luminance=a, detail=0))))
        for a in (0, 25, 50, 100)
    ]

    assert spreads == sorted(spreads, reverse=True) and spreads[-1] < spreads[0] * 0.5


def test_detail_puts_some_of_it_back():
    image = noisy()

    flat, kept = (
        luma_std(shown(image, Params(noise=NoiseReduction(luminance=80, detail=d))))
        for d in (0, 100)
    )

    assert kept > flat * 1.5


def test_noise_reduction_keeps_an_edge_where_it_is():
    image = noisy(step=0.3)
    plain = shown(image, Params())

    out = shown(image, Params(noise=NoiseReduction(luminance=100, detail=0)))

    def jump(picture):
        return picture[100:200, 320:340, 0].mean() - picture[100:200, 260:280, 0].mean()

    assert jump(out) == pytest.approx(jump(plain), abs=0.03)
    # And not blurred across: just either side of it the picture is that side's level
    assert out[100:200, 296:299, 0].mean() == pytest.approx(
        plain[100:200, 260:280, 0].mean(), abs=0.03
    )
    assert out[100:200, 301:304, 0].mean() == pytest.approx(
        plain[100:200, 320:340, 0].mean(), abs=0.03
    )


def test_colour_noise_reduction_takes_out_colour_noise_and_leaves_the_brightness():
    image = noisy()
    plain = shown(image, Params())

    out = shown(image, Params(noise=NoiseReduction(colour=100)))

    assert chroma_std(out) < chroma_std(plain) * 0.5
    assert luma_std(out) == pytest.approx(luma_std(plain), rel=0.02)


def test_colour_noise_reduction_does_not_bleed_across_a_real_colour_edge():
    encoded = np.full((100, 300, 3), 0.4, dtype=np.float32)
    encoded[:, 150:, 0] += 0.25  # red on the right
    encoded[:, 150:, 2] -= 0.15
    image = linear_of(encoded)
    plain = shown(image, Params())

    out = shown(image, Params(noise=NoiseReduction(colour=100)))

    change = np.abs(out - plain)
    # Nothing moves away from the edge, and the pixels beside it only a little
    assert change[:, :140].max() < 0.01 and change[:, 160:].max() < 0.01
    assert change.max() < 0.06
    assert out[50, 154, 0] - out[50, 146, 0] > 0.18


def test_more_colour_reduction_takes_out_more():
    image = noisy()

    spreads = [
        chroma_std(shown(image, Params(noise=NoiseReduction(colour=a)))) for a in (0, 30, 60, 100)
    ]

    assert spreads == sorted(spreads, reverse=True) and spreads[-1] < spreads[0] * 0.5


def test_noise_reduction_keeps_the_brightness_of_a_plain_area():
    image = noisy()
    plain = shown(image, Params())

    out = shown(image, Params(noise=NoiseReduction(luminance=100, colour=100, detail=20)))

    assert (out[50:250, 50:250] @ colour.LUMA).mean() == pytest.approx(
        (plain[50:250, 50:250] @ colour.LUMA).mean(), abs=0.004
    )


def test_a_render_too_small_to_have_noise_is_left_alone():
    image = noisy()[::4, ::4]
    edit = Params(noise=NoiseReduction(luminance=100, colour=100))

    assert np.array_equal(
        render(image, edit, full=(2400, 1200)), render(image, Params(), full=(2400, 1200))
    )
    assert denoise.prepare(edit.noise, 1.0) is not None


def test_noise_reduction_is_the_same_whatever_the_tiling():
    image = noisy()[:160, :240]
    edit = Params(noise=NoiseReduction(luminance=60, colour=60, detail=40))

    one = render(image, edit, bits=16, tile_rows=0).astype(np.int32)
    tiled = render(image, edit, bits=16, tile_rows=29).astype(np.int32)

    assert np.abs(one - tiled).max() <= 2


def test_8_and_16_bits_agree_with_sharpening_and_noise_reduction():
    image = noisy()[:120, :160]
    edit = Params(
        sharpening=Sharpening(amount=80, masking=20), noise=NoiseReduction(luminance=50, colour=50)
    )

    eight = render(image, edit, bits=8).astype(np.float64)
    sixteen = render(image, edit, bits=16).astype(np.float64) / 257.0

    assert np.abs(eight - sixteen).max() <= 1.0


def test_all_the_detail_steps_together_stay_in_range_and_in_order():
    image = noisy(step=0.2)
    edit = Params(
        sharpening=Sharpening(amount=100, masking=30),
        noise=NoiseReduction(luminance=60, colour=60),
    )

    out = render(image, edit, bits=16)

    assert out.dtype == np.uint16
    # Noise reduction first, so the sharpening does not bring the noise back
    sharpened_only = shown(image, Params(sharpening=edit.sharpening))
    both = out.astype(np.float64) / 65535
    assert luma_std(both) < luma_std(sharpened_only)
