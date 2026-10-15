import json

import pytest

from lumina.core.edit import params
from lumina.core.edit.params import Basic, Colour, Crop, Curves, Matte, NewerParams, Params


def edited():
    return Params(
        exposure=0.7,
        temperature=20.0,
        tint=-5.0,
        basic=Basic(contrast=20, highlights=-60, shadows=35, whites=-10, blacks=5),
        colour=Colour(vibrance=30, saturation=-15),
        curves=Curves(rgb=((0, 0), (0.5, 0.6), (1, 1)), blue=((0, 0.05), (1, 1))),
        matte=Matte(black=0.08, white=0.03, shadow_desaturation=0.4),
        rotate=1,
        crop=Crop(0.1, 0.05, 0.9, 0.95),
    )


def test_an_edit_survives_json():
    edit = edited()

    assert params.from_json(edit.to_json()) == edit


def test_the_document_says_its_version():
    assert json.loads(edited().to_json())["version"] == params.VERSION


def test_defaults_are_no_edit():
    assert Params().is_identity()
    assert params.from_json("{}").is_identity()
    assert params.from_json("not json").is_identity()
    assert params.from_dict([1, 2]).is_identity()


def test_values_out_of_range_are_clamped_and_wrong_kinds_fall_back():
    edit = params.from_dict(
        {
            "version": 1,
            "exposure": 12,
            "white_balance": {"temperature": "warm", "tint": -500},
            "matte": {"black": 0.9, "white": float("nan"), "shadow_desaturation": True},
            "rotate": 7,
            "curves": {"rgb": "S", "red": [[0, 0.1], [1, 0.9]]},
        }
    )

    assert edit.exposure == params.EXPOSURE[1]
    assert edit.temperature == 0.0 and edit.tint == params.WARMTH[0]
    assert edit.matte == Matte(black=params.MATTE_BLACK[1], white=0.0, shadow_desaturation=0.0)
    assert edit.rotate == 3
    assert edit.curves.rgb == ((0, 0), (1, 1))
    assert edit.curves.red == ((0, 0.1), (1, 0.9))


def test_a_crop_with_nothing_left_is_dropped():
    edit = params.from_dict({"crop": {"left": 0.6, "right": 0.4}})

    assert edit.crop == Crop()


def test_a_newer_edit_is_refused():
    with pytest.raises(NewerParams):
        params.from_dict({"version": params.VERSION + 1})


def test_older_versions_are_migrated_step_by_step(monkeypatch):
    # As it will be once there is a version 2 and 3: each step from the one before
    monkeypatch.setattr(params, "VERSION", 3)
    steps = []

    def to_2(data):
        steps.append(2)
        return {**data, "exposure": data["brightness"]}

    def to_3(data):
        steps.append(3)
        return {**data, "exposure": data["exposure"] / 2}

    monkeypatch.setattr(params, "_MIGRATIONS", {1: to_2, 2: to_3})

    migrated = params.migrate({"version": 1, "brightness": 2.0})

    assert steps == [2, 3]
    assert migrated["version"] == 3 and migrated["exposure"] == 1.0


def test_every_released_version_has_its_migration():
    assert set(params._MIGRATIONS) == set(range(1, params.VERSION))


def test_the_fingerprint_follows_the_look():
    assert edited().fingerprint() == params.from_json(edited().to_json()).fingerprint()
    assert edited().fingerprint() != edited().with_changes(exposure=0.8).fingerprint()


def test_a_preset_keeps_only_its_sections_and_applies_over_another():
    look = edited()
    saved = params.preset(look, ["curves", "matte"])

    assert set(saved) == {"version", "curves", "matte"}
    other = Params(exposure=-1.0, rotate=2)
    applied = params.apply_preset(other, json.loads(json.dumps(saved)))

    assert applied.curves == look.curves and applied.matte == look.matte
    assert applied.exposure == -1.0 and applied.rotate == 2


def test_a_newer_preset_is_refused():
    with pytest.raises(NewerParams):
        params.apply_preset(Params(), {"version": params.VERSION + 1, "exposure": 1})


def test_an_unknown_section_is_refused():
    with pytest.raises(ValueError):
        params.preset(Params(), ["sharpness"])


def test_a_version_1_edit_is_migrated_and_looks_as_it_did():
    stored = {"version": 1, "exposure": 0.5, "rotate": 1, "crop": {"left": 0.1}}

    edit = params.from_dict(stored)

    assert edit == Params(exposure=0.5, rotate=1, crop=Crop(left=0.1))
    assert edit.flip_horizontal is False and edit.straighten == 0.0
    assert json.loads(edit.to_json())["version"] == params.VERSION


def test_flip_and_straighten_survive_json_and_are_checked():
    edit = Params(flip_horizontal=True, straighten=-3.5)

    assert params.from_json(edit.to_json()) == edit
    odd = params.from_dict({"flip": {"horizontal": "yes"}, "straighten": 100})
    assert odd.flip_horizontal is False and odd.straighten == params.STRAIGHTEN[1]


def test_turning_four_times_is_the_same_edit():
    edit = Params(flip_horizontal=True, straighten=4, crop=Crop(0.1, 0.2, 0.6, 0.9))

    assert edit.turned(1).turned(1).turned(1).turned(1) == edit
    assert edit.turned(1).turned(-1) == edit
    assert edit.flipped(True).flipped(True) == edit


def test_a_version_2_edit_gets_the_basic_controls_at_zero():
    stored = {"version": 2, "exposure": 0.5, "straighten": 2.0}

    edit = params.from_dict(stored)

    assert edit == Params(exposure=0.5, straighten=2.0)
    assert edit.basic == Basic() and edit.basic.is_identity()


def test_the_basic_controls_are_checked():
    edit = params.from_dict(
        {"basic": {"contrast": 300, "highlights": "more", "shadows": -40, "blacks": None}}
    )

    assert edit.basic == Basic(contrast=params.BASIC[1], shadows=-40.0)


def test_a_preset_can_keep_the_basic_controls():
    look = edited()

    applied = params.apply_preset(Params(), params.preset(look, ["basic"]))

    assert applied == Params(basic=look.basic)


def test_a_version_3_edit_gets_vibrance_and_saturation_at_zero():
    stored = {"version": 3, "exposure": 0.5, "basic": {"contrast": 10}}

    edit = params.from_dict(stored)

    assert edit == Params(exposure=0.5, basic=Basic(contrast=10))
    assert edit.colour == Colour() and edit.colour.is_identity()


def test_the_colour_controls_are_checked():
    edit = params.from_dict({"colour": {"vibrance": 900, "saturation": "lots"}})

    assert edit.colour == Colour(vibrance=params.COLOUR[1])


def test_a_preset_can_keep_the_colour_controls():
    look = edited()

    applied = params.apply_preset(Params(), params.preset(look, ["colour"]))

    assert applied == Params(colour=look.colour)
