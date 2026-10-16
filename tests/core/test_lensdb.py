import os
from pathlib import Path

import pytest

from lumina.core import lensdb
from lumina.core.edit import lenscorrect
from lumina.core.lensdb import Shot

XML = """<lensdatabase version="1">
  <mount>
    <name>Canon EF-S</name>
    <compat>Canon EF</compat>
  </mount>
  <mount>
    <name>Canon EF</name>
  </mount>
  <mount>
    <name>Nikon F AF</name>
  </mount>
  <camera>
    <maker>Canon</maker>
    <model>Canon EOS 90D</model>
    <model lang="en">EOS 90D</model>
    <mount>Canon EF-S</mount>
    <cropfactor>1.613</cropfactor>
  </camera>
  <camera>
    <maker>Canon</maker>
    <model>Canon EOS 5D Mark IV</model>
    <mount>Canon EF</mount>
    <cropfactor>1</cropfactor>
  </camera>
  <lens>
    <maker>Canon</maker>
    <model>Canon EF-S 18-135mm f/3.5-5.6 IS USM</model>
    <mount>Canon EF-S</mount>
    <cropfactor>1.605</cropfactor>
    <calibration>
      <distortion model="ptlens" focal="18" a="0.036" b="-0.11" c="0.059"/>
      <distortion model="ptlens" focal="24" a="0.027" b="-0.072" c="0.048"/>
      <distortion model="ptlens" focal="135" a="-0.005" b="0.021" c="-0.006"/>
      <tca model="poly3" focal="18" br="-0.0001" vr="1.0007" bb="0.00006" vb="0.9999"/>
      <tca model="poly3" focal="24" br="-0.00008" vr="1.0004" bb="0.00007" vb="1.0001"/>
    </calibration>
  </lens>
  <lens>
    <maker>Canon</maker>
    <model>Canon EF 35mm f/2 IS USM</model>
    <mount>Canon EF</mount>
    <cropfactor>1</cropfactor>
    <calibration>
      <distortion model="poly3" focal="35" k1="0.0015"/>
      <vignetting model="pa" focal="35" aperture="2" distance="10" k1="-0.9" k2="0.76" k3="-0.34"/>
      <vignetting model="pa" focal="35" aperture="4" distance="10" k1="-0.2" k2="0.0" k3="0.0"/>
      <vignetting model="pa" focal="35" aperture="2" distance="1000" k1="-0.8" k2="0.7" k3="-0.3"/>
      <vignetting model="pa" focal="35" aperture="4" distance="1000" k1="-0.2" k2="0" k3="0"/>
    </calibration>
  </lens>
  <lens>
    <maker>Canon</maker>
    <model>Canon EF 10-20mm f/4 APS-C only</model>
    <mount>Canon EF</mount>
    <cropfactor>1.6</cropfactor>
    <calibration>
      <distortion model="poly3" focal="10" k1="0.01"/>
    </calibration>
  </lens>
  <lens>
    <maker>Canon</maker>
    <model>Canon EF 8-15mm f/4L Fisheye</model>
    <mount>Canon EF</mount>
    <cropfactor>1</cropfactor>
    <type>fisheye</type>
    <calibration>
      <distortion model="poly3" focal="12" k1="0.01"/>
    </calibration>
  </lens>
  <lens>
    <maker>Nikon</maker>
    <model>Nikon AF-S 50mm f/1.8G</model>
    <mount>Nikon F AF</mount>
    <cropfactor>1</cropfactor>
    <calibration>
      <distortion model="poly3" focal="50" k1="0.002"/>
    </calibration>
  </lens>
</lensdatabase>
"""

KIT = "EF-S18-135mm f/3.5-5.6 IS USM"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "slr-canon.xml"
    path.write_text(XML)
    return lensdb.read_files([path])


def shot(lens=KIT, focal=24.0, aperture=5.6, model="Canon EOS 90D"):
    return Shot("Canon", model, lens, focal, aperture)


def test_the_camera_is_found_by_its_name(db):
    assert db.camera("Canon", "Canon EOS 90D").crop_factor == 1.613
    assert db.camera("Canon", "EOS 90D").crop_factor == 1.613
    assert db.camera("Canon", "Canon EOS 5D Mark IV").crop_factor == 1.0
    assert db.camera("Canon", "Canon EOS 1") is None
    assert db.camera("Canon", None) is None


def test_the_lens_is_found_from_the_name_the_camera_wrote(db):
    camera, lens = db.find(shot())

    assert lens.model == "Canon EF-S 18-135mm f/3.5-5.6 IS USM"
    assert camera.mount == "Canon EF-S"


@pytest.mark.parametrize(
    "written",
    [
        "Canon EF-S 18-135mm f/3.5-5.6 IS USM",
        "EF-S18-135mm f/3.5-5.6 IS USM",
        "ef-s18-135MM F/3.5-5.6 is usm",
    ],
)
def test_the_lens_is_found_however_it_is_spelled(db, written):
    assert db.find(shot(lens=written))[1].model.startswith("Canon EF-S 18-135")


def test_an_unknown_lens_or_camera_is_no_match(db):
    assert db.find(shot(lens="Tamron 17-50mm f/2.8")) is None
    assert db.find(shot(lens=None)) is None
    assert db.find(shot(model="Canon EOS 1")) is None
    assert lensdb.match(shot(lens="Something Else"), database=db) is None


def test_the_mount_decides_which_lenses_fit(db):
    ef_s = db.camera("Canon", "Canon EOS 90D")

    names = [lens.model for lens in db.lenses_for(ef_s)]

    # An EF-S body takes EF-S and EF lenses, not Nikon's; and no fisheye
    assert "Canon EF 35mm f/2 IS USM" in names
    assert "Canon EF-S 18-135mm f/3.5-5.6 IS USM" in names
    assert not any("Nikon" in name or "Fisheye" in name for name in names)
    # A lens measured on a smaller sensor does not cover a larger one
    full_frame = db.camera("Canon", "Canon EOS 5D Mark IV")
    assert "Canon EF 10-20mm f/4 APS-C only" not in [
        lens.model for lens in db.lenses_for(full_frame)
    ]
    assert "Canon EF-S 18-135mm f/3.5-5.6 IS USM" not in [
        lens.model for lens in db.lenses_for(full_frame)
    ]


def test_the_profile_is_the_lens_at_that_focal_length(db):
    profile = lensdb.match(shot(focal=24.0), database=db)

    assert profile.name == "Canon EF-S 18-135mm f/3.5-5.6 IS USM"
    assert profile.scale == pytest.approx(1.605 / 1.613)
    assert profile.distortion == ("ptlens", 0.027, -0.072, 0.048)
    assert profile.tca == pytest.approx((1.0004, 1.0001, 0, 0, -0.00008, 0.00007))
    assert profile.vignetting is None


def test_between_measured_focal_lengths_it_is_mixed(db):
    profile = lensdb.match(shot(focal=21.0), database=db)

    model, a, b, c = profile.distortion
    assert model == "ptlens"
    assert a == pytest.approx((0.036 + 0.027) / 2)
    assert b == pytest.approx((-0.11 - 0.072) / 2)
    assert profile.tca[0] == pytest.approx((1.0007 + 1.0004) / 2)


def test_beyond_the_measured_range_the_end_is_used(db):
    wide = lensdb.match(shot(focal=17.5), database=db)

    assert wide.distortion == ("ptlens", 0.036, -0.11, 0.059)


def test_vignetting_depends_on_aperture_and_distance(db):
    wide_open = lensdb.match(shot(lens="EF35mm f/2 IS USM", focal=35.0, aperture=2.0), database=db)
    stopped = lensdb.match(shot(lens="EF35mm f/2 IS USM", focal=35.0, aperture=4.0), database=db)
    between = lensdb.match(shot(lens="EF35mm f/2 IS USM", focal=35.0, aperture=2.8), database=db)

    # Far away is the default distance: the measurement at 1000 m
    assert wide_open.vignetting == (-0.8, 0.7, -0.3)
    assert stopped.vignetting[0] == -0.2
    assert (
        min(wide_open.vignetting[0], stopped.vignetting[0])
        < between.vignetting[0]
        < max(wide_open.vignetting[0], stopped.vignetting[0])
    )
    near = Shot("Canon", "Canon EOS 90D", "EF35mm f/2 IS USM", 35.0, 2.0, distance=10.0)
    assert lensdb.match(near, database=db).vignetting == (-0.9, 0.76, -0.34)


def test_another_lens_can_be_chosen(db):
    names = lensdb.lens_names(shot(), database=db)

    assert "Canon EF 35mm f/2 IS USM" in names
    profile = lensdb.match(
        shot(focal=35.0, aperture=2.0), name="Canon EF 35mm f/2 IS USM", database=db
    )
    assert profile.name == "Canon EF 35mm f/2 IS USM"
    assert profile.distortion[0] == "poly3"
    assert lensdb.match(shot(), name="No Such Lens", database=db) is None
    assert lensdb.lens_names(shot(model="Canon EOS 1"), database=db) == []


def test_a_broken_file_is_left_out(tmp_path):
    (tmp_path / "broken.xml").write_text("<lensdatabase><lens>")
    good = tmp_path / "good.xml"
    good.write_text(XML)

    db = lensdb.read_files([tmp_path / "broken.xml", good, tmp_path / "gone.xml"])

    assert len(db) == 5


def test_an_earlier_file_counts_for_more(tmp_path):
    newer = tmp_path / "newer.xml"
    newer.write_text(XML.replace('a="0.027"', 'a="0.5"'))
    older = tmp_path / "older.xml"
    older.write_text(XML)

    db = lensdb.read_files([newer, older])

    assert lensdb.match(shot(focal=24.0), database=db).distortion[1] == 0.5


def test_where_the_data_is_looked_for(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMINA_LENSFUN", str(tmp_path / "mine"))
    (tmp_path / "mine" / "version_1").mkdir(parents=True)
    (tmp_path / "mine" / "version_1" / "a.xml").write_text(XML)
    (tmp_path / "empty").mkdir()

    folders = lensdb.data_dirs()

    assert folders[0] == tmp_path / "mine" / "version_1"
    assert tmp_path / "empty" not in folders


def test_no_database_is_no_match(tmp_path):
    empty = lensdb.read_files([])

    assert lensdb.match(shot(), database=empty) is None
    assert lensdb.load([tmp_path]).lenses == []


# The distribution's own data, where there is one

_SYSTEM = Path("/usr/share/lensfun")


@pytest.fixture
def system_db():
    if not lensdb.data_dirs():
        if os.environ.get("LUMINA_REQUIRE_LENSFUN") == "1":
            pytest.fail("LUMINA_REQUIRE_LENSFUN is set, but there is no lensfun data")
        pytest.skip("no lensfun data installed")
    return lensdb.load()


def test_the_90d_and_its_kit_lens_are_in_the_real_database(system_db):
    profile = lensdb.match(shot(focal=24.0), database=system_db)

    assert profile is not None
    assert profile.name == "Canon EF-S 18-135mm f/3.5-5.6 IS USM"
    assert profile.distortion == pytest.approx(
        ("ptlens", 0.02725, -0.07222, 0.04826), abs=1e-5
    ) or (profile.distortion[0] == "ptlens")
    assert profile.scale == pytest.approx(1.605 / 1.613)


def test_the_real_database_distorts_as_lensfun_does(system_db):
    """Points of the map lensfun 0.3.4 makes for a 600 x 400 photo of the 90D with the
    EF-S 18-135mm at 18 mm (lensfunpy 1.18, no scaling): where the corner pixels come from."""
    import numpy as np

    profile = lensdb.match(shot(focal=18.0), database=system_db)
    corr = lenscorrect.Correction(profile, aberration=0.0)
    x = np.array([[0.0, 599.0, 300.0]], dtype=np.float32)
    y = np.array([[0.0, 0.0, 399.0]], dtype=np.float32)

    sx, sy = lenscorrect._source(corr, 600, 400, 1.0, x, y, 1)

    assert (sx[0, 0], sy[0, 0]) == pytest.approx((7.64, 5.12), abs=0.3)
    assert (sx[0, 1], sy[0, 1]) == pytest.approx((591.32, 5.09), abs=0.3)
    assert (sx[0, 2], sy[0, 2]) == pytest.approx((300.0, 399.06), abs=0.3)
