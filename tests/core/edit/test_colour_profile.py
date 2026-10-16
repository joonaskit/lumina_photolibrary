import numpy as np
import pytest
from rawfiles import make_dng

from lumina.core import dcp
from lumina.core.edit import colour_profile as cp
from lumina.core.edit import params as edit_params
from lumina.core.edit import pipeline, source
from lumina.core.edit.params import ColourProfile, Params

# A "camera" that sees sRGB: its colours are linear sRGB
SRGB_CAMERA_XYZ = cp.XYZ_D65_TO_SRGB
SRGB_CAMERA = cp.CameraColour(
    tuple(map(tuple, np.eye(3))), tuple(map(tuple, SRGB_CAMERA_XYZ)), (1.0, 1.0, 1.0)
)
SRGB_FORWARD = cp.D65_TO_D50 @ cp.SRGB_TO_XYZ_D65
FLAT = np.array([[0.0, 0.0], [1.0, 1.0]])


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cp.prepare.cache_clear()
    cp._profile_lut.cache_clear()
    dcp._stored.cache_clear()
    yield tmp_path
    cp.prepare.cache_clear()
    cp._profile_lut.cache_clear()


def stored(tmp_path, profile, name="p.dcp"):
    path = dcp.write_new(tmp_path / name, profile)
    ident, _ = dcp.store(path)
    return ident


def plain_profile(**changes):
    values = dict(
        name="Plain",
        camera="sRGB Camera",
        color_matrices=(SRGB_CAMERA_XYZ,),
        illuminants=(21,),
        forward_matrices=(SRGB_FORWARD,),
        tone_curve=FLAT,
    )
    values.update(changes)
    return dcp.Profile(**values)


def table(hues=6, sats=3, vals=1, shift=0.0, scale=1.0):
    data = np.zeros((vals, hues, sats, 3), dtype=np.float32)
    data[..., 0], data[..., 1], data[..., 2] = shift, scale, 1.0
    return dcp.Table((hues, sats, vals), data, 0)


def patch(rgb, size=4):
    return np.ones((size, size, 3), dtype=np.float32) * np.array(rgb, dtype=np.float32)


# The matrices


def test_librarys_matrix_keeps_white_white():
    rgb_cam = cp.libraw_rgb_cam(
        [[1.1498, -0.3759, -0.1516], [-0.5073, 1.2954, 0.2349], [-0.0892, 0.1867, 0.6118]]
    )

    assert rgb_cam.sum(axis=1) == pytest.approx([1, 1, 1])
    assert cp.libraw_rgb_cam(SRGB_CAMERA_XYZ) == pytest.approx(np.eye(3), abs=1e-6)


def test_the_two_lights_are_mixed_by_the_photos_white():
    profile = dcp.Profile(
        "Two", "Cam", (np.eye(3) * 2, cp.XYZ_D65_TO_SRGB), (17, 21), forward_matrices=()
    )
    d65_white = cp.XYZ_D65_TO_SRGB @ cp.xy_to_xyz(cp.D65_XY)

    xy, weight = cp.white_of(profile, d65_white)

    assert xy == pytest.approx(cp.D65_XY, abs=2e-3)
    # All the D65 calibration (the second): the white is D65
    assert weight == pytest.approx(0.0, abs=0.02)
    assert cp.temperature(cp.D65_XY) == pytest.approx(6504, rel=0.01)
    assert cp.temperature((0.4476, 0.4074)) == pytest.approx(2856, rel=0.01)


def test_white_goes_to_d50s_white():
    with_forward = plain_profile()
    without = plain_profile(forward_matrices=())

    for profile in (with_forward, without):
        matrix = cp.camera_to_xyz_d50(profile, (1.0, 1.0, 1.0))
        assert matrix @ np.ones(3) == pytest.approx(cp.xy_to_xyz(cp.D50_XY), abs=2e-3)


# The look


def test_rgb_tone_keeps_the_hue():
    rgb = np.array([[0.4, 0.2, 0.05], [0.1, 0.3, 0.6]])
    curve = cp.tone_function(cp.STANDARD_CURVE)

    toned = cp.rgb_tone(rgb, curve)

    h0, _s0, _v0 = cp.rgb_to_hsv(rgb)
    h1, _s1, v1 = cp.rgb_to_hsv(toned)
    assert h1 == pytest.approx(h0, abs=1e-9)
    assert v1 == pytest.approx(curve(rgb.max(axis=1)))


def test_hsv_comes_back():
    rgb = np.random.default_rng(1).random((200, 3))

    assert cp.hsv_to_rgb(*cp.rgb_to_hsv(rgb)) == pytest.approx(rgb, abs=1e-12)


def test_a_hue_shift_turns_the_colour():
    red = np.array([[0.6, 0.1, 0.1]])

    turned = cp.apply_table(red, table(shift=30.0))

    assert cp.rgb_to_hsv(turned)[0] == pytest.approx(cp.rgb_to_hsv(red)[0] + 0.5)


def test_a_saturation_scale_and_grey_stays_grey():
    colours = np.array([[0.6, 0.3, 0.3], [0.4, 0.4, 0.4]])

    out = cp.apply_table(colours, table(scale=0.5))

    assert cp.rgb_to_hsv(out)[1] == pytest.approx([0.25, 0.0])
    assert out[1] == pytest.approx([0.4, 0.4, 0.4])


def test_the_table_lookup_is_the_look_it_was_made_from():
    """Within half a level on average, and a little more at the edge of sRGB, where
    the sRGB curve's steep start makes a small difference of a channel at zero look large."""
    look = cp._standard_look
    lut = cp.build_lut(look)
    srgb = np.random.default_rng(2).random((100, 100, 3)) ** 2.2
    rgb = np.clip(srgb @ cp.SRGB_TO_PROPHOTO.T, 0, 1).astype(np.float32)

    looked = cp.lookup(lut, rgb)

    direct = look(rgb.reshape(-1, 3).astype(np.float64), 1.0).reshape(rgb.shape)
    levels = np.abs(cp._encode(np.clip(looked, 0, 1)) - cp._encode(np.clip(direct, 0, 1))) * 255
    assert levels.mean() < 0.2 and np.percentile(levels, 99) < 1.5
    assert np.abs(looked - direct).max() < 0.02


# In the pipeline


def test_an_edit_from_before_profiles_renders_as_before(tmp_path):
    """LibRaw's colours through the camera's matrix are LibRaw's sRGB decode."""
    path = make_dng(tmp_path / "a.dng")
    old = source.raw_linear(path)
    new, camera = source.raw_camera(path)
    edit = edit_params.from_dict({"version": 9, "exposure": 0.5})

    assert edit.profile == ColourProfile("libraw", "")
    before = pipeline.render(old, edit, bits=16).astype(int)
    after = pipeline.render(new, edit, bits=16, camera=camera).astype(int)
    assert np.abs(before - after).max() <= 16
    assert (
        np.abs(
            pipeline.render(old, edit).astype(int) - pipeline.render(new, edit, camera=camera)
        ).max()
        <= 1
    )


def test_a_photo_in_srgb_has_no_use_for_a_profile():
    image = patch([0.2, 0.3, 0.4])

    plain = pipeline.render(image, Params())

    assert (pipeline.render(image, Params(profile=ColourProfile("standard"))) == plain).all()


def test_a_profile_that_changes_nothing_changes_nothing(store):
    ident = stored(store, plain_profile())
    image = patch([0.05, 0.2, 0.5])

    neutral = pipeline.render(image, Params(), bits=16, camera=SRGB_CAMERA).astype(int)
    profiled = pipeline.render(
        image, Params(profile=ColourProfile(ident)), bits=16, camera=SRGB_CAMERA
    ).astype(int)

    assert np.abs(neutral - profiled).max() < 400


def test_the_profiles_tone_curve_is_used(store):
    bright = np.array([[0.0, 0.0], [0.18, 0.5], [1.0, 1.0]])
    ident = stored(store, plain_profile(tone_curve=bright))
    image = patch([0.18, 0.18, 0.18])

    out = pipeline.render(image, Params(profile=ColourProfile(ident)), bits=16, camera=SRGB_CAMERA)

    assert pipeline.srgb_decode(out[0, 0, 0] / 65535) == pytest.approx(0.5, abs=0.01)


def test_a_profile_without_a_tone_curve_gets_luminas(store):
    ident = stored(store, plain_profile(tone_curve=None))
    image = patch([0.18, 0.18, 0.18])

    out = pipeline.render(image, Params(profile=ColourProfile(ident)), bits=16, camera=SRGB_CAMERA)

    expected = dict(cp.STANDARD_CURVE)[0.18]
    assert pipeline.srgb_decode(out[0, 0, 0] / 65535) == pytest.approx(expected, abs=0.01)


def test_exposure_and_white_balance_act_through_a_profile(store):
    ident = stored(store, plain_profile())
    image = patch([0.1, 0.1, 0.1])
    base = Params(profile=ColourProfile(ident))

    plain = pipeline.render(image, base, bits=16, camera=SRGB_CAMERA).astype(float)
    brighter = pipeline.render(image, base.with_changes(exposure=1.0), bits=16, camera=SRGB_CAMERA)
    warmer = pipeline.render(image, base.with_changes(temperature=60), bits=16, camera=SRGB_CAMERA)

    assert pipeline.srgb_decode(brighter[0, 0, 0] / 65535) == pytest.approx(0.2, abs=0.005)
    assert warmer[0, 0, 0] > plain[0, 0, 0] and warmer[0, 0, 2] < plain[0, 0, 2]


def test_the_standard_look_is_brighter_and_more_colourful_than_libraw():
    image = patch([0.12, 0.08, 0.05])

    neutral = pipeline.render(image, Params(), camera=SRGB_CAMERA).astype(float)
    standard = pipeline.render(image, Params(profile=ColourProfile("standard")), camera=SRGB_CAMERA)

    assert standard.mean() > neutral.mean()
    _h0, s0, _v0 = cp.rgb_to_hsv(neutral[0, 0][None] / 255)
    _h1, s1, _v1 = cp.rgb_to_hsv(standard[0, 0][None].astype(float) / 255)
    assert s1 > s0


def test_a_profile_gone_from_the_store_falls_back_to_the_standard_look(store):
    transform = cp.prepare("0123456789abcdef", SRGB_CAMERA)

    assert transform.name == "Lumina Standard"
    assert transform.lut is not None


def test_the_profile_survives_json_and_bad_ids_are_libraws():
    edit = Params(profile=ColourProfile("0123456789abcdef", "Canon EOS 90D"))

    assert edit_params.from_json(edit.to_json()) == edit
    for bad in ("../../x", 42, None, "ABCDEF0123456789"):
        data = edit.to_dict()
        data["profile"]["id"] = bad
        assert edit_params.from_dict(data).profile.id == "libraw"
    assert "profile" not in edit_params.SECTIONS
