import numpy as np

from lumina.core.edit import auto, pipeline
from lumina.core.edit.params import Params

W, H = 200, 120


def ramp(low, high, size=(H, W)):
    # A grey ramp from left to right, in linear light, as float32 RGB
    row = np.linspace(low, high, size[1], dtype=np.float32)
    return np.repeat(np.repeat(row[None, :, None], size[0], axis=0), 3, axis=2)


def encoded_luma(edit, linear):
    image = np.clip(linear * 65535, 0, 65535).astype(np.uint16)
    out = pipeline.render(image, edit, bits=16)
    return out.astype(np.float32).mean(axis=2) / 65535


def test_analysis_is_the_photo_in_linear_light():
    image = np.full((H, W, 3), int(0.5 * 65535), dtype=np.uint16)

    linear = auto.analysis(image, Params())

    assert np.allclose(linear, 0.5, atol=0.01)


def test_analysis_keeps_the_light_past_white_and_ignores_the_edit_besides_its_geometry():
    image = np.full((H, W, 3), 65535, dtype=np.uint16)
    edit = Params(exposure=1.5)

    linear = auto.analysis(image, edit)

    assert np.allclose(linear, 1.0, atol=0.01)
    assert auto.analysis(image, Params(crop=Params().crop)).shape == (H, W, 3)


def test_a_dark_photo_is_brightened_and_its_shadows_lifted():
    linear = ramp(0.002, 0.12)

    exposure, controls = auto.auto_tone(linear)

    assert exposure > 0.5
    after = encoded_luma(Params(exposure=exposure, basic=controls), linear)
    before = encoded_luma(Params(), linear)
    assert np.median(after) > np.median(before) + 0.08


def test_a_photo_with_light_past_white_is_brought_back():
    # Mostly a dim scene, a tenth of it a sky three times white
    linear = ramp(0.1, 0.3)
    linear[:, -W // 10 :] = 3.0

    exposure, controls = auto.auto_tone(linear)

    assert controls.highlights < -30
    assert controls.whites == 0


def test_a_flat_photo_gets_contrast_and_a_milky_one_its_blacks_down():
    flat = ramp(0.3, 0.5)
    milky = ramp(0.12, 0.9)

    assert auto.auto_tone(flat)[1].contrast > 20
    assert auto.auto_tone(milky)[1].blacks < -20


def test_a_well_exposed_photo_is_left_nearly_alone():
    # Even in how it looks, from black to white
    linear = pipeline.srgb_decode(ramp(0.0, 1.0)).astype(np.float32)

    exposure, controls = auto.auto_tone(linear)

    assert abs(exposure) < 0.5
    for name in ("contrast", "highlights", "shadows", "whites", "blacks"):
        assert abs(getattr(controls, name)) <= 40, name


def test_a_black_photo_gets_no_tone_edit():
    assert auto.auto_tone(np.zeros((H, W, 3), np.float32)) == (0.0, Params().basic)


def test_auto_tone_stays_in_the_controls_ranges():
    for linear in (ramp(0.0, 0.001), ramp(0.5, 4.0), ramp(0.4, 0.41)):
        exposure, controls = auto.auto_tone(linear)
        assert abs(exposure) <= auto.EXPOSURE_LIMIT
        for name in ("contrast", "highlights", "shadows", "whites", "blacks"):
            assert -100 <= getattr(controls, name) <= 100


def seen(linear, temperature, tint):
    # The mean colour of the photo with the white balance set, as chroma between channels
    gains = pipeline.white_balance_gains(temperature, tint)
    mean = (linear.reshape(-1, 3) * gains).mean(axis=0)
    return np.abs(np.log2(mean / mean[1])).max()


def cast(colour, shape=(H, W)):
    return ramp(0.1, 0.8, shape) * np.array(colour, np.float32)


def test_a_blue_cast_is_warmed_and_a_green_one_gets_magenta():
    blue = cast((0.8, 1.0, 1.4))
    green = cast((1.0, 1.3, 1.0))

    temperature, _ = auto.auto_white_balance(blue)
    _, tint = auto.auto_white_balance(green)

    assert temperature > 10
    assert tint > 10


def test_auto_white_balance_makes_the_photo_nearly_grey():
    for colour in ((0.8, 1.0, 1.4), (1.3, 1.0, 0.8), (1.0, 1.2, 1.0), (1.1, 1.0, 1.1)):
        linear = cast(colour)

        temperature, tint = auto.auto_white_balance(linear)

        assert seen(linear, temperature, tint) < 0.1, colour


def test_a_neutral_photo_stays_as_shot():
    temperature, tint = auto.auto_white_balance(cast((1.0, 1.0, 1.0)))

    assert abs(temperature) <= 2
    assert abs(tint) <= 2


def test_a_big_patch_of_colour_does_not_drag_the_balance_off():
    # A bluish grey ramp, a third of the photo a red wall
    linear = cast((0.8, 1.0, 1.4))
    linear[:, : W // 3] = (0.7, 0.12, 0.1)
    grey = linear[:, W // 2 :]

    temperature, tint = auto.auto_white_balance(linear)

    assert temperature > 10
    assert seen(grey, temperature, tint) < 0.25


def test_the_picker_makes_the_patch_grey():
    linear = cast((1.0, 1.0, 1.0)) * 0 + np.array([0.7, 0.3, 0.2], np.float32)
    patch = np.array([0.35, 0.4, 0.55], np.float32)
    linear[40:80, 100:140] = patch

    temperature, tint = auto.white_balance_at(linear, 120 / (W - 1), 60 / (H - 1))

    gains = pipeline.white_balance_gains(temperature, tint)
    neutral = patch * gains
    assert np.abs(np.log2(neutral / neutral[1])).max() < 0.05


def test_the_picker_takes_a_point_at_the_edge_and_nothing_in_the_dark():
    linear = np.zeros((H, W, 3), np.float32)
    linear[:, W // 2 :] = (0.3, 0.4, 0.5)

    assert auto.white_balance_at(linear, 1.0, 1.0) is not None
    assert auto.white_balance_at(linear, 0.0, 0.0) is None


def test_the_found_white_balance_is_within_the_sliders():
    extreme = np.full((H, W, 3), (0.05, 0.5, 2.0), np.float32)

    temperature, tint = auto.auto_white_balance(extreme)

    assert -100 <= temperature <= 100
    assert -100 <= tint <= 100
