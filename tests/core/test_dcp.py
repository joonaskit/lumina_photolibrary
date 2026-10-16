import json
from pathlib import Path

import numpy as np
import pytest

from lumina.core import dcp

ADOBE_90D = np.array(
    [[1.1498, -0.3759, -0.1516], [-0.5073, 1.2954, 0.2349], [-0.0892, 0.1867, 0.6118]]
)
A_MATRIX = np.array(
    [[1.4683, -0.7448, -0.0905], [-0.4609, 1.3134, 0.1588], [-0.0324, 0.1195, 0.6559]]
)
FORWARD_A = np.array([[0.5439, 0.2524, 0.1680], [0.2750, 0.6756, 0.0495], [0.0067, 0.0062, 0.8122]])
FORWARD_D65 = np.array(
    [[0.6543, 0.1193, 0.1907], [0.3165, 0.6258, 0.0577], [0.0001, 0.0075, 0.8175]]
)


def table(hues=6, sats=3, vals=1, shift=0.0, scale=1.0):
    data = np.zeros((vals, hues, sats, 3), dtype=np.float32)
    data[..., 0] = shift
    data[..., 1] = scale
    data[..., 2] = 1.0
    return dcp.Table((hues, sats, vals), data, 0)


def two_light(camera="Canon EOS 90D", name="Test Look"):
    return dcp.Profile(
        name=name,
        camera=camera,
        color_matrices=(A_MATRIX, ADOBE_90D),
        illuminants=(17, 21),
        forward_matrices=(FORWARD_A, FORWARD_D65),
        hue_sat_maps=(table(shift=2.0), table(shift=-2.0)),
        look_table=table(hues=8, sats=4, vals=3, scale=1.1),
        tone_curve=np.array([[0.0, 0.0], [0.18, 0.3], [1.0, 1.0]]),
        exposure_offset=0.25,
        copyright="Test",
    )


def test_a_profile_survives_writing_and_reading(tmp_path):
    written = dcp.write_new(tmp_path / "a.dcp", two_light())

    again = dcp.read(written)

    assert again.name == "Test Look" and again.camera == "Canon EOS 90D"
    assert again.illuminants == (17, 21)
    for a, b in zip(again.color_matrices, (A_MATRIX, ADOBE_90D), strict=True):
        assert np.allclose(a, b, atol=1e-4)
    assert np.allclose(again.forward_matrices[1], FORWARD_D65, atol=1e-4)
    assert again.hue_sat_maps[0].dims == (6, 3, 1)
    assert np.array_equal(again.hue_sat_maps[1].data, table(shift=-2.0).data)
    assert again.look_table.dims == (8, 4, 3)
    assert np.allclose(again.tone_curve, [[0, 0], [0.18, 0.3], [1, 1]], atol=1e-6)
    assert again.exposure_offset == pytest.approx(0.25)
    assert again.copyright == "Test"


def test_one_light_and_nothing_but_a_matrix(tmp_path):
    plain = dcp.Profile("Plain", "Cam", (ADOBE_90D,), (21,))

    again = dcp.read(dcp.write_new(tmp_path / "plain.dcp", plain))

    assert len(again.color_matrices) == 1 and again.forward_matrices == ()
    assert again.hue_sat_maps == () and again.look_table is None and again.tone_curve is None


def test_writing_never_replaces_a_file(tmp_path):
    target = tmp_path / "a.dcp"
    target.write_bytes(b"mine")

    with pytest.raises(FileExistsError):
        dcp.write_new(target, two_light())

    assert target.read_bytes() == b"mine"
    assert not any(p.name.endswith(".part") for p in tmp_path.iterdir())


def test_a_free_name_is_found(tmp_path):
    (tmp_path / "Canon EOS 90D Look.dcp").write_bytes(b"")

    assert dcp.free_name(tmp_path, "Canon EOS 90D Look").name == "Canon EOS 90D Look 2.dcp"
    assert dcp.free_name(tmp_path, "a/b:c").name == "abc.dcp"


@pytest.mark.parametrize(
    "data",
    [b"", b"II*\x00\x08\x00\x00\x00", b"IIRC\x08\x00\x00\x00", b"IIRC\x08\x00\x00\x00\x01\x00"],
)
def test_what_is_not_a_profile_is_refused(tmp_path, data):
    path = tmp_path / "bad.dcp"
    path.write_bytes(data)

    with pytest.raises(dcp.ProfileError) as error:
        dcp.read(path)

    assert error.value.path.endswith("bad.dcp")
    assert dcp.peek(path) is None or dcp.peek(path)[0] == ""


def test_a_profile_without_a_colour_matrix_is_refused(tmp_path):
    data = dcp.serialize(dcp.Profile("x", "y", (ADOBE_90D,), (21,)))
    # The colour matrix's tag made another
    tag = (dcp.COLOR_MATRIX_1).to_bytes(2, "little")
    broken = data.replace(tag, (1).to_bytes(2, "little"), 1)

    with pytest.raises(ValueError):
        dcp.parse(broken)


def test_profiles_are_found_for_their_camera(tmp_path):
    dcp.write_new(tmp_path / "90d.dcp", two_light())
    dcp.write_new(tmp_path / "other.dcp", two_light(camera="Nikon D850", name="Nikon"))
    (tmp_path / "notes.txt").write_text("not a profile")
    dcp.forget_scans()

    found = dcp.profiles_for("Canon", "Canon EOS 90D", [tmp_path])

    assert [f.name for f in found] == ["Test Look"]
    assert dcp.profiles_for("Canon", "Canon EOS R5", [tmp_path]) == []


def test_another_name_of_the_same_camera_finds_its_profile(tmp_path):
    dcp.write_new(tmp_path / "250d.dcp", two_light(camera="Canon EOS 250D", name="250D"))
    (tmp_path / "camera_model_aliases.json").write_text(
        json.dumps({"Canon EOS 250D": ["Canon EOS Kiss X10", "Canon EOS Rebel SL3"]})
    )
    dcp.forget_scans()

    assert [f.name for f in dcp.profiles_for("Canon", "Canon EOS Rebel SL3", [tmp_path])] == [
        "250D"
    ]


def test_the_store_keeps_a_copy_by_content(tmp_path):
    original = dcp.write_new(tmp_path / "mine.dcp", two_light())
    store = tmp_path / "store"

    ident, profile = dcp.store(original, store)
    again, _ = dcp.store(original, store)
    original.unlink()

    assert ident == again and len(ident) == 16
    assert profile.name == "Test Look"
    kept = dcp.stored(ident, store)
    assert kept is not None and kept.name == "Test Look"
    assert len(list(store.iterdir())) == 1


def test_a_profile_missing_from_the_store_is_none(tmp_path):
    assert dcp.stored("0123456789abcdef", tmp_path) is None
    assert dcp.stored("../../etc/passwd", tmp_path) is None


def test_storing_what_is_not_a_profile_names_it(tmp_path):
    bad = tmp_path / "bad.dcp"
    bad.write_bytes(b"nothing")

    with pytest.raises(dcp.ProfileError) as error:
        dcp.store(bad, tmp_path / "store")

    assert error.value.path == str(bad)
    assert not (tmp_path / "store").exists() or not any((tmp_path / "store").iterdir())


# RawTherapee's, where it is installed

_RAWTHERAPEE = Path("/usr/share/rawtherapee/dcpprofiles")


@pytest.mark.skipif(not _RAWTHERAPEE.is_dir(), reason="RawTherapee's profiles are not installed")
def test_every_rawtherapee_profile_reads_and_survives_writing():
    paths = sorted(_RAWTHERAPEE.glob("*.dcp"))

    for path in paths:
        profile = dcp.read(path)
        again = dcp.parse(dcp.serialize(profile))
        assert np.allclose(again.color_matrices[0], profile.color_matrices[0], atol=1e-4)
        if profile.look_table is not None:
            assert np.array_equal(again.look_table.data, profile.look_table.data)
    assert paths
