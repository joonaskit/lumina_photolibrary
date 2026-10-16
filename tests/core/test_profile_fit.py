import os
from pathlib import Path

import numpy as np
import pytest

from lumina.core import colour_checker as checker
from lumina.core import dcp, profile_fit
from lumina.core.edit import colour_profile as cp

SAMPLES = Path(os.environ.get("LUMINA_SAMPLES", Path(__file__).parent.parent / "samples"))
ADOBE_90D = np.array(
    [[1.1498, -0.3759, -0.1516], [-0.5073, 1.2954, 0.2349], [-0.0892, 0.1867, 0.6118]]
)
RGB_CAM = cp.libraw_rgb_cam(ADOBE_90D)
CAMERA = cp.CameraColour(
    tuple(map(tuple, RGB_CAM)), tuple(map(tuple, ADOBE_90D)), (0.55, 1.0, 0.65)
)


# Fitting a camera's look


def scene(count, seed):
    """Camera colours of a scene of ordinary colours: greys with some colour, not the
    saturated extremes a camera's JPEG would clip."""
    rng = np.random.default_rng(seed)
    grey = rng.random((count, 1)) ** 2.4 * 0.7
    return grey * (1 + 0.35 * rng.uniform(-1, 1, (count, 3)))


def a_camera_jpeg(camera_rgb, correction, tone):
    """What a camera that renders through `correction` and `tone` would make of
    `camera_rgb` (N x 3): its JPEG as linear sRGB."""
    prophoto = np.clip(camera_rgb @ (correction @ cp.SRGB_TO_PROPHOTO @ RGB_CAM).T, 0, 1)
    toned = cp.rgb_tone(prophoto, cp.tone_function(tone))
    return np.clip(toned @ cp.PROPHOTO_TO_SRGB.T, 0, 1)


def test_a_known_look_is_found_again():
    camera_rgb = scene(20000, 4)
    correction = np.array([[1.08, -0.05, -0.03], [-0.02, 1.04, -0.02], [0.0, -0.06, 1.06]])
    tone = np.array([[0, 0], [0.05, 0.11], [0.18, 0.45], [0.5, 0.85], [1, 1]])
    pairs = profile_fit.Pairs(camera_rgb, a_camera_jpeg(camera_rgb, correction, tone), CAMERA, 3)

    fitted = profile_fit.fit(pairs, "Canon EOS 90D")

    assert fitted.after < 1.0 < fitted.before
    curve = fitted.profile.tone_curve
    assert np.interp([0.05, 0.18, 0.5], curve[:, 0], curve[:, 1]) == pytest.approx(
        [0.11, 0.45, 0.85], abs=0.02
    )
    assert fitted.profile.name == "Canon EOS 90D Camera Look"
    assert fitted.profile.forward_matrices[0] @ np.ones(3) == pytest.approx(
        cp.xy_to_xyz(cp.D50_XY), abs=5e-4
    )


def test_the_tone_curve_rises_and_reaches_white():
    before = np.random.default_rng(5).random((5000, 3)) * 0.7
    after = np.sqrt(before) + np.random.default_rng(6).normal(0, 0.02, before.shape)

    curve = profile_fit.fit_tone(before, np.clip(after, 0, 1))

    assert (np.diff(curve[:, 1]) >= 0).all()
    assert curve[0].tolist() == [0.0, 0.0] and curve[-1].tolist() == [1.0, 1.0]


def test_the_colour_matrix_keeps_white():
    before = np.random.default_rng(7).random((3000, 3))
    true = np.array([[1.1, -0.1, 0.0], [0.05, 0.9, 0.05], [-0.02, -0.08, 1.1]])

    found = profile_fit.fit_matrix(before, before @ true.T)

    assert found == pytest.approx(true, abs=1e-6)
    assert found.sum(axis=1) == pytest.approx([1, 1, 1])


def test_the_look_table_follows_a_hue_turn():
    rng = np.random.default_rng(8)
    before = rng.random((40000, 3)) * 0.8
    h, s, v = cp.rgb_to_hsv(before)
    after = cp.hsv_to_rgb(h + 10 * 6 / 360, np.minimum(s * 1.2, 1), v)

    table = profile_fit.fit_look(before, after)

    middle = table.data[0, :, 3:6]
    assert np.median(middle[..., 0]) == pytest.approx(10.0, abs=1.0)
    assert np.median(middle[..., 1]) == pytest.approx(1.2, abs=0.05)
    # Grey is left alone
    assert (table.data[0, :, 0, 0] == 0).all() and (table.data[0, :, 0, 1] == 1).all()


def test_the_fitted_look_is_a_profile_that_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    camera_rgb = scene(8000, 9)
    tone = np.array([[0, 0], [0.18, 0.4], [1, 1]])
    pairs = profile_fit.Pairs(camera_rgb, a_camera_jpeg(camera_rgb, np.eye(3), tone), CAMERA, 2)
    fitted = profile_fit.fit(pairs, "Canon EOS 90D")
    path = dcp.write_new(tmp_path / "look.dcp", fitted.profile)
    ident, _ = dcp.store(path)
    cp.prepare.cache_clear()

    transform = cp.prepare(ident, CAMERA)
    grey = np.full((2, 2, 3), 0.18, dtype=np.float32)
    out = transform.apply(np.clip(grey @ transform.matrix.T.astype(np.float32), 0, 1))

    assert out[0, 0] == pytest.approx([0.4, 0.4, 0.4], abs=0.02)


def test_photos_without_a_jpeg_inside_cannot_be_learnt_from(tmp_path):
    from rawfiles import make_dng

    path = make_dng(tmp_path / "a.dng", preview=None)

    with pytest.raises(ValueError):
        profile_fit.gather([path])


@pytest.mark.skipif(not list(SAMPLES.glob("*.CR3")), reason="no real CR3 files in tests/samples")
def test_the_fitted_look_of_real_photos_is_near_their_jpegs():
    paths = sorted(SAMPLES.glob("*.CR3"))
    learn, check = paths[:-1], paths[-1:]

    fitted = profile_fit.fit(profile_fit.gather(learn), "Canon EOS 90D")
    held = profile_fit.gather(check)

    matrix = cp.XYZ_D50_TO_PROPHOTO @ fitted.profile.forward_matrices[0]
    rendered = profile_fit._render(
        held, matrix, fitted.profile.look_table, fitted.profile.tone_curve
    )
    expected = cp._encode(np.clip(held.target, 0, 1))
    plain = cp._encode(np.clip(held.camera @ np.array(held.colour.rgb_cam).T, 0, 1))
    assert np.abs(rendered - expected).mean() * 255 < 0.5 * np.abs(plain - expected).mean() * 255


# The colour checker


def test_ciede2000_agrees_with_sharmas_test_data():
    pairs = [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
        ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ]

    for first, second, expected in pairs:
        assert checker.delta_e_2000(np.array(first), np.array(second)) == pytest.approx(
            expected, abs=1e-4
        )


def test_lab_and_xyz_undo_each_other():
    assert checker.xyz_to_lab(checker.lab_to_xyz(checker.REFERENCE_LAB)) == pytest.approx(
        checker.REFERENCE_LAB, abs=1e-9
    )


def test_the_patches_are_found_from_the_four_corners():
    # A target turned and seen at a slant: its four corner patches
    corners = [(100, 50), (600, 80), (580, 420), (90, 390)]

    centres = checker.patch_centres(corners)

    assert centres.shape == (24, 2)
    assert centres[0] == pytest.approx(corners[0])
    assert centres[5] == pytest.approx(corners[1])
    assert centres[23] == pytest.approx(corners[2])
    assert centres[18] == pytest.approx(corners[3])


def draw_target(camera_colours, size=(700, 480)):
    """A photo of a target: patches of `camera_colours` (24 x 3) on black."""
    image = np.zeros((size[1], size[0], 3), dtype=np.float32)
    corners = [(80, 60), (620, 60), (620, 420), (80, 420)]
    for (x, y), colour in zip(checker.patch_centres(corners), camera_colours, strict=True):
        x, y = int(x), int(y)
        image[y - 35 : y + 35, x - 35 : x + 35] = colour
    return image, corners


def test_a_target_photographed_gives_back_its_colours():
    camera_colours = np.random.default_rng(10).random((24, 3))
    image, corners = draw_target(camera_colours)

    assert checker.sample(image, corners) == pytest.approx(camera_colours, abs=1e-6)


def test_a_patch_off_the_photo_is_said():
    image = np.zeros((100, 100, 3))

    with pytest.raises(ValueError, match="off the photo"):
        checker.sample(image, [(10, 10), (80, 10), (300, 300), (10, 80)])


def test_a_camera_with_a_known_matrix_is_profiled_exactly():
    # The camera's white-balanced colours of each patch, under a light that tints them
    forward = np.array([[0.62, 0.20, 0.144], [0.28, 0.70, 0.02], [0.02, -0.08, 0.885]])
    forward *= (cp.xy_to_xyz(cp.D50_XY) / (forward @ np.ones(3)))[:, None]
    camera = checker.REFERENCE_XYZ @ np.linalg.inv(forward).T
    tinted = camera * np.array([0.8, 1.0, 1.3]) * 0.6

    fitted = checker.fit(tinted, CAMERA, "Canon EOS 90D")

    # Near: the target's greys are not quite grey, which the white balance is taken from
    assert fitted.profile.forward_matrices[0] == pytest.approx(forward, abs=0.01)
    assert fitted.mean_error < 0.5
    assert fitted.profile.tone_curve is None
    assert "ColorChecker" in fitted.profile.name


def test_a_noisy_target_gives_a_close_profile_and_says_how_close():
    forward = cp.D65_TO_D50 @ cp.SRGB_TO_XYZ_D65
    camera = checker.REFERENCE_XYZ @ np.linalg.inv(forward).T
    noisy = camera * (1 + np.random.default_rng(11).normal(0, 0.01, camera.shape))

    fitted = checker.fit(np.clip(noisy, 1e-4, None), CAMERA, "Cam", linear=True)

    assert fitted.mean_error < 1.5 and fitted.max_error < 4
    assert fitted.profile.tone_curve.tolist() == [[0, 0], [1, 1]]
    assert len(fitted.errors) == 24


def test_photos_of_one_colour_cannot_teach_a_look():
    grey = np.full((5000, 3), 0.2) * np.random.default_rng(12).random((5000, 1))
    pairs = profile_fit.Pairs(grey, grey, CAMERA, 1)

    with pytest.raises(ValueError, match="too few different colours"):
        profile_fit.fit(pairs, "Cam")


def test_patches_clicked_in_the_wrong_order_are_refused():
    camera = checker.REFERENCE_XYZ @ np.linalg.inv(cp.D65_TO_D50 @ cp.SRGB_TO_XYZ_D65).T
    # As if black had been clicked first: the patches read backwards
    backwards = camera[::-1]

    with pytest.raises(ValueError, match="greys should get darker"):
        checker.fit(backwards, CAMERA, "Cam")
