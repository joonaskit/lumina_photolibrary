"""Local adjustments: the weights of a mask, and the render with masks."""

import json

import cv2
import numpy as np
import pytest

from lumina.core.edit import masks, params, pipeline
from lumina.core.edit.params import Crop, Local, Mask, Params, Presence
from lumina.core.edit.source import make_proxy

SIZE = (100, 200)  # height, width


def same(part, whole):
    """Equal but for the last bit of a float sum that is added in another order."""
    assert part.shape == whole.shape
    assert np.abs(part.astype(np.int32) - whole).max() <= 1


def photo(width=411, height=317, seed=3):
    """Linear uint16: soft shapes and noise, an odd size on purpose."""
    rng = np.random.default_rng(seed)
    base = cv2.GaussianBlur(rng.random((height, width, 3)).astype(np.float32), (0, 0), 9)
    base = (base - base.min()) / (base.max() - base.min())
    base[:, width // 2 :] *= 0.5
    base += rng.normal(0, 0.03, base.shape).astype(np.float32)
    return (np.clip(base, 0, 1) ** 2.2 * 65535).astype(np.uint16)


def weight(mask, row=0, column=0, rows=100, columns=200, size=SIZE):
    return masks.weights(mask, row, column, rows, columns, size)


def test_a_linear_mask_is_full_before_its_line_and_gone_after():
    mask = Mask("linear", (0.5, 0.2, 0.5, 0.8))
    w = weight(mask)
    assert w.shape == (100, 200) and w.dtype == np.float32
    assert np.allclose(w[:19], 1.0) and np.allclose(w[81:], 0.0)
    assert 0.0 < w[50, 0] < 1.0
    # Falling all the way, and the same along the line
    assert np.all(np.diff(w[:, 0]) <= 1e-6) and np.allclose(w, w[:, :1])


def test_a_linear_mask_follows_its_direction():
    w = weight(Mask("linear", (0.2, 0.5, 0.8, 0.5)))
    assert np.allclose(w[:, :39], 1.0) and np.allclose(w[:, 161:], 0.0)
    assert np.allclose(w, w[:1])


def test_a_linear_mask_with_its_ends_together_does_not_fail():
    w = weight(Mask("linear", (0.5, 0.5, 0.5, 0.5)))
    assert np.isfinite(w).all() and w.min() >= 0 and w.max() <= 1


def test_a_radial_mask_is_full_inside_and_gone_outside():
    mask = Mask("radial", (0.5, 0.5, 0.2, 0.2, 0.0), feather=10)
    w = weight(mask)
    assert w[50, 100] == pytest.approx(1.0)
    assert w[0, 0] == 0.0 and w[99, 199] == 0.0
    # Round, not an ellipse of the photo's shape: a radius is of the longer side (200)
    assert w[50, 100 + 30] == pytest.approx(1.0) and w[50, 100 + 50] == pytest.approx(0.0)
    assert w[50 + 30, 100] == pytest.approx(1.0) and w[50 + 50 - 1, 100] < 0.1


def test_a_radial_mask_turns_clockwise():
    flat = Mask("radial", (0.5, 0.5, 0.3, 0.05, 0.0), feather=5)
    upright = Mask("radial", (0.5, 0.5, 0.3, 0.05, 90.0), feather=5)
    assert weight(flat)[50, 100 + 40] > 0.9 and weight(flat)[50 + 30, 100] == 0.0
    assert weight(upright)[50, 100 + 40] == 0.0
    assert weight(upright)[50 + 30, 100] > 0.0 or weight(upright)[50 + 20, 100] > 0.9


def test_feather_softens_a_radial_edge():
    hard = weight(Mask("radial", (0.5, 0.5, 0.2, 0.2, 0.0), feather=1))[50]
    soft = weight(Mask("radial", (0.5, 0.5, 0.2, 0.2, 0.0), feather=80))[50]
    assert np.count_nonzero((hard > 0.01) & (hard < 0.99)) < np.count_nonzero(
        (soft > 0.01) & (soft < 0.99)
    )


def test_inverting_a_mask_gives_one_minus_its_weight():
    for kind in ("linear", "radial"):
        mask = Mask(kind)
        assert np.allclose(weight(replace_invert(mask)), 1.0 - weight(mask), atol=1e-6)


def replace_invert(mask):
    return Mask(mask.kind, mask.shape, mask.feather, True, mask.enabled, mask.name, mask.local)


@pytest.mark.parametrize("kind", ["linear", "radial"])
def test_the_weights_of_a_part_are_those_of_the_whole(kind):
    mask = Mask(kind)
    whole = weight(mask)
    part = weight(mask, row=23, column=61, rows=40, columns=77)
    assert np.array_equal(part, whole[23:63, 61:138])


# The render


def render(image, edit, **kwargs):
    return pipeline.render(image, edit, **kwargs)


def test_a_mask_that_changes_nothing_leaves_the_render_as_it_was():
    image = photo()
    plain = render(image, Params())
    assert np.array_equal(render(image, Params(masks=(Mask("radial"),))), plain)
    off = Mask("radial", local=Local(exposure=-2), enabled=False)
    assert np.array_equal(render(image, Params(masks=(off,))), plain)


def test_a_mask_changes_the_photo_only_where_it_applies():
    image = photo()
    mask = Mask("linear", (0.5, 0.3, 0.5, 0.6), local=Local(exposure=-2))
    plain = render(image, Params())
    edited = render(image, Params(masks=(mask,)))
    height = plain.shape[0]
    # Beyond the line the weight is 0; a render with any step after tone rounds its 8 bits
    # by the interpolated table, so it may differ from one without by the last bit
    below = slice(int(height * 0.65), None)
    assert np.abs(edited[below].astype(np.int32) - plain[below]).max() <= 1
    assert edited[: int(height * 0.25)].mean() < plain[: int(height * 0.25)].mean() * 0.6


def test_each_local_control_does_something():
    image = photo()
    plain = render(image, Params())
    for name, value in {
        "exposure": 1.0,
        "temperature": 60,
        "tint": 60,
        "contrast": 60,
        "highlights": -60,
        "shadows": 60,
        "whites": 60,
        "blacks": 60,
        "vibrance": 60,
        "saturation": -60,
    }.items():
        mask = Mask("linear", (0.5, 2.0, 0.5, 3.0), local=Local(**{name: value}))
        assert not np.array_equal(render(image, Params(masks=(mask,))), plain), name


def test_a_local_exposure_is_the_global_one_where_the_mask_is_full():
    image = photo()
    everywhere = Mask("linear", (0.5, 2.0, 0.5, 3.0), local=Local(exposure=1.0))
    local = render(image, Params(masks=(everywhere,)), bits=16)
    globally = render(image, Params(exposure=1.0), bits=16)
    assert np.abs(local.astype(np.int32) - globally).max() <= 40  # 16-bit steps of the tables


def test_masks_are_made_in_order_one_on_the_other():
    image = photo()
    a = Mask("linear", (0.5, 2.0, 0.5, 3.0), local=Local(exposure=-1.0))
    b = Mask("linear", (0.5, 2.0, 0.5, 3.0), local=Local(exposure=-1.0))
    twice = render(image, Params(masks=(a, b)), bits=16)
    once = render(
        image,
        Params(masks=(Mask("linear", (0.5, 2.0, 0.5, 3.0), local=Local(exposure=-2.0)),)),
        bits=16,
    )
    assert np.abs(twice.astype(np.int32) - once).max() <= 60


def test_masks_follow_the_crop_and_the_render_size():
    image = photo()
    mask = Mask("linear", (0.5, 0.0, 0.5, 1.0), local=Local(exposure=-2))
    edit = Params(masks=(mask,), crop=Crop(0.25, 0.25, 0.75, 0.75))
    out = render(image, edit)
    # The line runs over the cropped photo: from full at its top to gone at its bottom
    assert out[:5].mean() < out[-5:].mean()


@pytest.mark.parametrize("region", [(0, 0, 120, 90), (100, 80, 260, 200), (301, 217, 411, 317)])
def test_a_region_of_a_masked_render_is_the_same_pixels(region):
    image = photo()
    edit = Params(
        masks=(
            Mask("radial", (0.4, 0.5, 0.3, 0.2, 30.0), local=Local(exposure=1, saturation=-50)),
            Mask("linear", local=Local(contrast=40, vibrance=30)),
        ),
        presence=Presence(clarity=40),
    )
    whole = render(image, edit)
    left, top, right, bottom = region
    same(render(image, edit, region=region), whole[top:bottom, left:right])


def test_the_proxy_looks_like_the_full_render():
    image = photo(width=800, height=600)
    edit = Params(masks=(Mask("radial", local=Local(exposure=-1.5)),))
    proxy = make_proxy(image, 200 * 150)
    small = render(proxy, edit, full=(800, 600))
    full = render(image, edit)
    scaled = cv2.resize(full, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_AREA)
    assert np.abs(small.astype(np.int32) - scaled).mean() < 4


# Parameters


def test_masks_round_trip_through_json():
    edit = Params(
        masks=(
            Mask("linear", (0.1, 0.2, 0.3, 0.9), invert=True, name="Sky", local=Local(exposure=-1)),
            Mask("radial", (0.5, 0.4, 0.2, 0.1, 45.0), feather=20, enabled=False),
        )
    )
    assert params.from_json(edit.to_json()) == edit
    assert json.loads(edit.to_json())["masks"][0]["name"] == "Sky"


def test_an_edit_without_masks_has_none():
    assert Params().masks == () and Params().is_identity()
    assert json.loads(Params().to_json())["masks"] == []


def test_an_edit_before_masks_is_migrated_to_none():
    old = {"version": 11, "exposure": 0.5}
    assert params.from_dict(old) == Params(exposure=0.5)


def test_damaged_masks_are_dropped_or_clamped_not_fatal():
    data = {
        "version": params.VERSION,
        "masks": [
            "nonsense",
            {"kind": "brush", "shape": [0, 0]},
            {"kind": "linear", "shape": [0, 0, 1]},
            {"kind": "linear", "shape": [0, 0, 1, "x"]},
            {"kind": "linear", "shape": [-5, 0, 9, 1], "local": {"exposure": 99, "tint": "x"}},
        ],
    }
    (only,) = params.from_dict(data).masks
    assert only.shape == (-1.0, 0.0, 2.0, 1.0)
    assert only.local.exposure == 5.0 and only.local.tint == 0.0
    assert params.from_dict({"version": params.VERSION, "masks": 7}).masks == ()


def test_an_edit_keeps_at_most_the_most_masks():
    many = [{"kind": "radial", "shape": [0.5, 0.5, 0.1, 0.1, 0]}] * (params.MAX_MASKS + 5)
    assert (
        len(params.from_dict({"version": params.VERSION, "masks": many}).masks) == params.MAX_MASKS
    )


def test_turning_and_mirroring_the_photo_moves_its_masks_with_it():
    line = Mask("linear", (0.5, 0.2, 0.5, 0.8))
    ellipse = Mask("radial", (0.25, 0.5, 0.2, 0.1, 0.0))
    edit = Params(masks=(line, ellipse))
    assert edit.turned(4) == edit
    assert edit.flipped(True).flipped(True) == edit
    assert edit.flipped(False).flipped(False) == edit
    # The weights of the turned photo are those of the photo turned
    tall = (200, 100)  # a quarter turn swaps width and height
    for mask in (line, ellipse):
        original = masks.weights(mask, 0, 0, 100, 200, SIZE)
        turned = masks.weights(mask.turned(), 0, 0, 200, 100, tall)
        assert np.allclose(turned, np.rot90(original, k=-1), atol=1e-5)
        mirrored = masks.weights(mask.flipped(True), 0, 0, 100, 200, SIZE)
        assert np.allclose(mirrored, original[:, ::-1], atol=1e-5)
        mirrored = masks.weights(mask.flipped(False), 0, 0, 100, 200, SIZE)
        assert np.allclose(mirrored, original[::-1], atol=1e-5)


def test_presets_and_pasting_leave_masks_alone():
    mine = Params(exposure=1.0, masks=(Mask("radial", local=Local(exposure=1)),))
    document = params.preset(Params(exposure=-1.0, masks=(Mask("linear"),)), ["exposure"])
    applied = params.apply_preset(mine, document)
    assert applied.exposure == -1.0 and applied.masks == mine.masks
    assert "masks" not in document
