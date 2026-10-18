import json

import numpy as np
import pytest

from lumina.core.edit import lenscorrect, pipeline
from lumina.core.edit.lenscorrect import Correction
from lumina.core.edit.params import Lens, LensProfile, Params

PTLENS = ("ptlens", 0.03, -0.08, 0.05)
BARREL = ("ptlens", -0.06, 0.0, 0.0)


def pattern(x, y):
    """A smooth picture of the scene as it should look, 0 to 1."""
    return 0.5 + 0.25 * np.sin(x / 9.0) + 0.25 * np.sin(y / 7.0 + x / 31.0)


def centre(width, height):
    return (width - 1) / 2, (height - 1) / 2


def distorted_view(profile, width, height):
    """The `pattern` as a lens with `profile` would have drawn it (16 bits): each pixel
    of the photo as shot looks at the point of the scene whose corrected radius Ru has
    Rd = Ru * k(Ru) at the pixel's radius."""
    cx, cy = centre(width, height)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    dx, dy = xs - cx, ys - cy
    norm = min(width, height) / 2 / profile.scale
    rd = np.hypot(dx, dy) / norm
    ru = rd.copy()
    for _ in range(40):
        # Newton on Ru * k(Ru) = Rd, by a small step
        h = 1e-5
        f = ru * lenscorrect._distortion(profile, ru) - rd
        slope = (
            (ru + h) * lenscorrect._distortion(profile, ru + h)
            - ru * lenscorrect._distortion(profile, ru)
        ) / h
        ru = ru - f / slope
    scene = np.where(rd > 0, ru / np.where(rd > 0, rd, 1), 1.0)
    return pattern(cx + dx * scene, cy + dy * scene), scene


def as_photo(values, peak=60000):
    return np.repeat((values * peak).astype(np.uint16)[..., None], 3, axis=2)


def test_a_distorted_scene_comes_back_as_it_was():
    width, height = 240, 160
    profile = LensProfile("test", distortion=PTLENS)
    shot, _ = distorted_view(profile, width, height)
    corr = Correction(profile)

    out = lenscorrect.resample(as_photo(shot), corr, (0, height, 0, width))

    zoom = lenscorrect.autoscale(corr, width, height)
    cx, cy = centre(width, height)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    expected = pattern(cx + (xs - cx) / zoom, cy + (ys - cy) / zoom) * 60000
    inside = (slice(8, -8), slice(8, -8))
    assert np.abs(out[..., 1][inside] - expected[inside]).mean() < 600


def test_distortion_amount_is_how_far_the_correction_goes():
    width, height = 120, 80
    profile = LensProfile("test", distortion=PTLENS)
    image = as_photo(pattern(*np.mgrid[0:height, 0:width][::-1]))
    box = (0, height, 0, width)

    none = lenscorrect.resample(image, Correction(profile, distortion=0.0), box)
    half = lenscorrect.resample(image, Correction(profile, distortion=0.5), box)
    full = lenscorrect.resample(image, Correction(profile), box)

    assert (none == image).all()
    assert 0 < np.abs(half.astype(int) - image).mean() < np.abs(full.astype(int) - image).mean()


@pytest.mark.parametrize("distortion", [PTLENS, BARREL, ("poly3", 0.05), ("poly5", -0.1, 0.05)])
def test_no_empty_border_shows(distortion):
    width, height = 200, 120
    corr = Correction(LensProfile("test", distortion=distortion))
    zoom = lenscorrect.autoscale(corr, width, height)
    t = np.linspace(0, 1, 50)
    edge_x = np.concatenate([t * (width - 1), t * (width - 1), 0 * t, 0 * t + width - 1])
    edge_y = np.concatenate([0 * t, 0 * t + height - 1, t * (height - 1), t * (height - 1)])

    sx, sy = lenscorrect._source(
        corr, width, height, zoom, edge_x.astype(np.float32), edge_y.astype(np.float32), 1
    )

    assert zoom >= 1.0
    assert sx.min() >= -0.6 and sy.min() >= -0.6
    assert sx.max() <= width - 0.4 and sy.max() <= height - 0.4


def test_poly3_and_poly5_are_lensfuns_formulas():
    ru = np.array([0.0, 0.5, 1.0, 1.4])

    poly3 = lenscorrect._distortion(LensProfile("t", distortion=("poly3", 0.04)), ru)
    poly5 = lenscorrect._distortion(LensProfile("t", distortion=("poly5", 0.04, -0.01)), ru)
    ptlens = lenscorrect._distortion(LensProfile("t", distortion=("ptlens", 0.01, 0.02, 0.03)), ru)

    assert poly3 == pytest.approx(1 - 0.04 + 0.04 * ru**2)
    assert poly5 == pytest.approx(1 + 0.04 * ru**2 - 0.01 * ru**4)
    assert ptlens == pytest.approx(0.01 * ru**3 + 0.02 * ru**2 + 0.03 * ru + (1 - 0.06))
    # poly5 leaves the middle of the picture alone; the others have their own constant
    assert poly5[0] == 1.0


def test_the_profile_scale_widens_the_radius():
    """A lens measured on a larger sensor is seen through less of its image circle."""
    width, height = 200, 120
    x = np.array([[199.0]], dtype=np.float32)
    y = np.array([[119.0]], dtype=np.float32)
    plain = LensProfile("t", scale=1.0, distortion=PTLENS)
    smaller = LensProfile("t", scale=0.8, distortion=PTLENS)

    far = lenscorrect._source(Correction(plain), width, height, 1.0, x, y, 1)
    near = lenscorrect._source(Correction(smaller), width, height, 1.0, x, y, 1)

    # A corner is further out for the smaller scale, so it is moved more
    assert abs(near[0][0, 0] - 199.0) != abs(far[0][0, 0] - 199.0)


def test_a_flat_field_comes_back_flat():
    width, height = 300, 200
    vignetting = (-0.9, 0.76, -0.34)
    corr = Correction(LensProfile("test", vignetting=vignetting))
    cx, cy = centre(width, height)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    r = np.hypot(xs - cx, ys - cy) / (np.hypot(width, height) / 2)
    darkness = 1 + vignetting[0] * r**2 + vignetting[1] * r**4 + vignetting[2] * r**6
    shot = as_photo(darkness, peak=30000)

    out = lenscorrect.resample(shot, corr, (0, height, 0, width))

    peak = lenscorrect.headroom(corr, width, height)
    flat = out[..., 0].astype(np.float64) * peak
    assert peak > 1.5
    assert np.abs(flat / 30000 - 1).max() < 0.01


def test_vignetting_keeps_the_light_it_brings_past_white():
    width, height = 100, 60
    corr = Correction(LensProfile("test", vignetting=(-0.9, 0.76, -0.34)))
    shot = np.full((height, width, 3), 60000, dtype=np.uint16)

    out = lenscorrect.resample(shot, corr, (0, height, 0, width))

    # A corner is lifted by the headroom, but the stored value stays below white
    assert out.max() < 65535
    assert out.max() == out[0, 0, 0] or out[0, 0, 0] >= out[height // 2, width // 2, 0]


def test_vignetting_amount():
    width, height = 100, 60
    profile = LensProfile("test", vignetting=(-0.9, 0.76, -0.34))
    shot = np.full((height, width, 3), 20000, dtype=np.uint16)

    def corner(amount):
        corr = Correction(profile, vignetting=amount)
        out = lenscorrect.resample(shot, corr, (0, height, 0, width))
        return out[0, 0, 0] * lenscorrect.headroom(corr, width, height)

    assert corner(0.0) == pytest.approx(20000, rel=1e-3)
    assert 20000 < corner(0.5) < corner(1.0)


def test_chromatic_aberration_moves_red_and_blue_against_green():
    width, height = 400, 100
    # Red a little larger than green, blue a little smaller
    corr = Correction(LensProfile("test", tca=(1.004, 0.996, 0, 0, 0, 0)))
    shot = np.zeros((height, width, 3), dtype=np.uint16)
    shot[:, 330:, :] = 40000  # a vertical edge 130 px right of the middle

    out = lenscorrect.resample(shot, corr, (0, height, 0, width)).astype(np.int32)

    def edge(channel):
        row = out[50, :, channel]
        return float(np.interp(20000, row[320:345], np.arange(320, 345)))

    # Red is sampled further out: its edge appears nearer the middle
    assert edge(0) < edge(1) < edge(2)
    assert edge(1) - edge(0) == pytest.approx(0.004 * 130, abs=0.25)


def test_straightening_with_a_do_nothing_lens_is_the_plain_straightening():
    width, height = 90, 60
    ys, xs = np.mgrid[0:height, 0:width]
    photo = as_photo(pattern(xs.astype(np.float64), ys.astype(np.float64)))
    params = Params(straighten=7.0)
    nothing = Correction(LensProfile("test", distortion=("poly5", 0.0, 0.0)))
    scale = pipeline.straighten_scale(width, height, 7.0)

    ours = lenscorrect.resample(photo, nothing, (0, height, 0, width), (7.0, scale))

    plain = pipeline.geometry(photo, params)
    assert np.abs(ours.astype(int) - plain.astype(int)).max() <= 2


def test_float_photos_are_corrected_too():
    width, height = 80, 50
    corr = Correction(LensProfile("test", vignetting=(-0.5, 0.0, 0.0)))
    shot = np.full((height, width, 3), 0.5, dtype=np.float32)

    out = lenscorrect.resample(shot, corr, (0, height, 0, width))

    assert out.dtype == np.float32
    assert out[height // 2, width // 2, 0] == pytest.approx(
        0.5 / lenscorrect.headroom(corr, width, height), rel=0.05
    )


def test_the_profile_survives_json():
    profile = LensProfile(
        "Canon EF-S 18-135mm", 0.995, PTLENS, (1.0004, 1.0001, 0, 0, -8e-5, 6e-5), (-0.2, 0.0, 0.01)
    )

    again = LensProfile.from_dict(json.loads(json.dumps(profile.to_dict())))

    assert again == profile


@pytest.mark.parametrize(
    "damaged",
    [
        None,
        "lens",
        {},
        {"name": "x", "scale": "big"},
        {"name": "x", "scale": 0.0},
        {"name": "x", "scale": float("nan")},
    ],
)
def test_a_damaged_profile_is_no_profile(damaged):
    assert LensProfile.from_dict(damaged) is None


def test_a_damaged_part_of_a_profile_is_left_out():
    profile = LensProfile.from_dict(
        {
            "name": "x",
            "scale": 1.0,
            "distortion": {"model": "ptlens", "terms": [1, 2]},
            "tca": [1, 2, 3],
            "vignetting": [0.1, "a", 0.3],
        }
    )

    assert profile == LensProfile("x")


# In the edit and the pipeline


def lens_on(**profile):
    return Params(lens=Lens(enabled=True, profile=LensProfile("test", **profile)))


def test_a_lens_that_is_off_or_unknown_changes_nothing():
    photo = as_photo(pattern(*np.mgrid[0:60, 0:90][::-1].astype(np.float64)))

    plain = pipeline.render(photo, Params())

    off = Params(lens=Lens(enabled=False, profile=LensProfile("x", distortion=PTLENS)))
    unknown = Params(lens=Lens(enabled=True, profile=None))
    nothing_asked = Params(
        lens=Lens(True, 0.0, 0.0, 0.0, LensProfile("x", distortion=PTLENS, vignetting=(-1, 0, 0)))
    )
    for edit in (off, unknown, nothing_asked):
        assert (pipeline.render(photo, edit) == plain).all()


def test_the_lens_corrects_in_the_render():
    width, height = 240, 160
    profile = LensProfile("test", distortion=PTLENS)
    shot, _ = distorted_view(profile, width, height)
    photo = as_photo(shot)

    out = pipeline.render(photo, lens_on(distortion=PTLENS), bits=16)

    assert out.shape == (height, width, 3)
    assert not (out == pipeline.render(photo, Params(), bits=16)).all()


def test_vignetting_does_not_change_the_exposure():
    """What the vignetting correction divides by to keep its light is put back."""
    width, height = 120, 80
    vignetting = (-0.4, 0.1, 0.0)
    photo = np.full((height, width, 3), 20000, dtype=np.uint16)

    out = pipeline.render(photo, lens_on(vignetting=vignetting), bits=16)

    middle = out[height // 2, width // 2, 0]
    plain = pipeline.render(photo, Params(), bits=16)[height // 2, width // 2, 0]
    assert abs(int(middle) - int(plain)) < 600
    # and the corners are lighter than the middle was
    assert out[0, 0, 0] > middle


def test_turned_photos_are_corrected_the_same():
    width, height = 120, 80
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    photo = as_photo(pattern(xs, ys))
    turned = np.rot90(photo, k=-1, axes=(0, 1))
    edit = lens_on(distortion=PTLENS, vignetting=(-0.3, 0.0, 0.0))

    straight = pipeline.render(photo, edit, bits=16)
    sideways = pipeline.render(turned, edit.with_changes(rotate=3), bits=16)

    # Turned back by the edit, the same picture: the corrections are the same in a turned frame
    assert np.abs(sideways.astype(int) - straight.astype(int)).max() < 300


def test_a_part_is_what_the_whole_has_there():
    width, height = 160, 100
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    photo = as_photo(pattern(xs, ys))
    edit = lens_on(distortion=PTLENS, vignetting=(-0.3, 0.0, 0.0)).with_changes(straighten=3.0)

    whole = pipeline.render(photo, edit)
    part = pipeline.render(photo, edit, region=(40, 20, 120, 80))

    assert np.abs(part.astype(int) - whole[20:80, 40:120].astype(int)).max() <= 1


def test_the_proxy_of_a_photo_is_corrected_like_the_photo():
    width, height = 400, 260
    profile = LensProfile("test", distortion=PTLENS, vignetting=(-0.4, 0.0, 0.0))
    shot, _ = distorted_view(profile, width, height)
    photo = as_photo(shot)
    small = photo[::2, ::2]
    edit = Params(lens=Lens(enabled=True, profile=profile))

    full = pipeline.render(photo, edit)
    proxy = pipeline.render(small, edit, full=(width, height))

    # The same picture at half the size
    reduced = full[::2, ::2].astype(int)
    assert np.abs(proxy.astype(int) - reduced).mean() < 6
