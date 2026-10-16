import json

import pytest
from conftest import TIMEOUT

from lumina.core import lensdb
from lumina.qt_models import develop, workers
from lumina.qt_models.develop import DevelopSession, Params
from lumina.ui import develop_view
from lumina.ui.lens_dialog import LensDialog

XML = """<lensdatabase version="1">
  <mount><name>Test Mount</name></mount>
  <camera>
    <maker>Testcam</maker>
    <model>Testcam One</model>
    <mount>Test Mount</mount>
    <cropfactor>1.5</cropfactor>
  </camera>
  <lens>
    <maker>Testcam</maker>
    <model>Testcam 18-55mm f/3.5-5.6</model>
    <mount>Test Mount</mount>
    <cropfactor>1.5</cropfactor>
    <calibration>
      <distortion model="ptlens" focal="18" a="0.03" b="-0.1" c="0.05"/>
      <distortion model="ptlens" focal="55" a="0.0" b="0.01" c="0.0"/>
      <vignetting model="pa" focal="18" aperture="3.5" distance="1000" k1="-0.5" k2="0.1" k3="0"/>
    </calibration>
  </lens>
  <lens>
    <maker>Testcam</maker>
    <model>Testcam 50mm f/1.8</model>
    <mount>Test Mount</mount>
    <cropfactor>1.5</cropfactor>
    <calibration>
      <distortion model="poly3" focal="50" k1="0.002"/>
    </calibration>
  </lens>
</lensdatabase>
"""

LENS = "Testcam 18-55mm f/3.5-5.6"


@pytest.fixture(autouse=True)
def lens_data(tmp_path, monkeypatch):
    """A lens database of our own; the work that would run in a worker process runs on a
    thread here, where the environment and the database are this test's."""
    folder = tmp_path / "lensfun"
    folder.mkdir()
    (folder / "test.xml").write_text(XML)
    monkeypatch.setenv("LUMINA_LENSFUN", str(folder))
    lensdb._load.cache_clear()
    submit = workers.submit

    def on_a_thread(fn, *args, on_done, on_error, owner, process=True):
        return submit(fn, *args, on_done=on_done, on_error=on_error, owner=owner, process=False)

    monkeypatch.setattr(develop.workers, "submit", on_a_thread)
    yield folder
    from lumina.core.edit import session

    session.close()
    lensdb._load.cache_clear()


@pytest.fixture
def library(service, drive, qtbot):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    return service


def photo_named(library, name, lens=LENS, focal=18.0):
    row = next(p for p in library.db.photos() if p.path.endswith(name))
    with library.db.db:
        library.db.db.execute(
            "UPDATE photos SET make = ?, model = ?, lens = ?, focal_length = ?, aperture = ? "
            "WHERE id = ?",
            ("Testcam", "Testcam One", lens, focal, 3.5, row.id),
        )
    return row.id, library.resolve(row.volume_key, row.path)


def opened(library, qtbot, photo):
    session = DevelopSession(library)
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.open(*photo)
    return session


def test_a_raw_file_starts_with_its_lens_corrected(library, qtbot):
    photo = photo_named(library, "c.dng")

    session = opened(library, qtbot, photo)

    assert session.lens_state() == "known"
    assert session.params.lens.enabled
    assert session.params.lens.profile.name == LENS
    assert session.lens_name() == LENS
    # Nothing is saved until the edit is changed: looking at a photo does not edit it
    assert library.edit_json(photo[0]) is None
    session.close()


def test_a_change_to_a_new_edit_keeps_the_corrections(library, qtbot):
    photo = photo_named(library, "c.dng")
    session = opened(library, qtbot, photo)

    session.commit(session.params.with_changes(exposure=0.5))

    saved = json.loads(library.edit_json(photo[0]))
    assert saved["exposure"] == 0.5
    assert saved["lens"]["enabled"] is True
    assert saved["lens"]["profile"]["name"] == LENS
    session.close()


def test_a_reset_goes_back_to_the_corrected_original(library, qtbot):
    photo = photo_named(library, "c.dng")
    session = opened(library, qtbot, photo)
    session.commit(session.params.with_changes(exposure=0.5))

    session.reset()

    assert session.params.lens.enabled and session.params.exposure == 0
    assert library.edit_json(photo[0]) is None
    session.undo()
    assert session.params.exposure == 0.5
    session.close()


def test_turning_the_lens_off_is_an_edit(library, qtbot):
    photo = photo_named(library, "c.dng")
    session = opened(library, qtbot, photo)

    session.commit(Params())

    assert library.edit_json(photo[0]) is not None
    again = opened(library, qtbot, photo)
    assert not again.params.lens.enabled
    session.close()
    again.close()


def test_an_edit_made_before_lens_corrections_stays_as_it_was(library, qtbot):
    photo = photo_named(library, "c.dng")
    library.save_edit(photo[0], json.dumps({"version": 8, "exposure": 0.25}))

    session = opened(library, qtbot, photo)

    assert session.params.exposure == 0.25 and not session.params.lens.enabled
    # the lens is known, so it can be turned on
    assert session.lens_state() == "known" and session.detected_lens() is not None
    session.close()


def test_a_jpeg_is_not_corrected_unless_asked(library, qtbot):
    photo = photo_named(library, "a.jpg")

    session = opened(library, qtbot, photo)

    assert session.lens_state() == "known"
    assert session.params == Params()
    session.close()


def test_an_unknown_lens_is_left_alone(library, qtbot):
    photo = photo_named(library, "c.dng", lens="Some Other Lens 85mm")

    session = opened(library, qtbot, photo)

    assert session.lens_state() == "unknown"
    assert session.params.lens == develop.Lens()
    # Nothing to do with the lens: a RAW file starts with a camera look all the same
    assert session.params.profile.id == "standard"
    session.close()


def test_without_a_lens_database_it_says_so(library, qtbot, monkeypatch):
    monkeypatch.setattr(lensdb, "data_dirs", lambda: [])
    photo = photo_named(library, "c.dng")

    session = opened(library, qtbot, photo)

    assert session.lens_state() == "no data"
    assert session.params.lens == develop.Lens()
    # Nothing to do with the lens: a RAW file starts with a camera look all the same
    assert session.params.profile.id == "standard"
    session.close()


def test_another_lens_can_be_chosen(library, qtbot):
    photo = photo_named(library, "c.dng")
    session = opened(library, qtbot, photo)

    with qtbot.waitSignal(session.lenses_ready, timeout=TIMEOUT) as names:
        session.request_lenses()
    assert LENS in names.args[0] and "Testcam 50mm f/1.8" in names.args[0]
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.choose_lens("Testcam 50mm f/1.8")

    assert session.params.lens.profile.name == "Testcam 50mm f/1.8"
    assert session.params.lens.profile.distortion[0] == "poly3"
    assert (
        json.loads(library.edit_json(photo[0]))["lens"]["profile"]["name"] == "Testcam 50mm f/1.8"
    )
    session.close()


def test_the_render_follows_the_lens(library, qtbot):
    photo = photo_named(library, "c.dng")
    session = opened(library, qtbot, photo)
    corrected = []
    session.image_ready.connect(lambda image, *_: corrected.append(image))

    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.commit(session.params.with_changes(exposure=0.1))
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.commit(Params(exposure=0.1))

    assert corrected[0] != corrected[1]
    session.close()


# The panel


@pytest.fixture
def view(library, qtbot):
    photo = photo_named(library, "c.dng")
    session = DevelopSession(library)
    made = develop_view.DevelopView(session)
    qtbot.addWidget(made)
    made.show()
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.open(*photo)
    return made


def test_the_panel_shows_the_lens_and_its_amounts(view):
    assert view.lens_on.isChecked() and view.lens_on.isEnabled()
    assert view.lens_name.text() == LENS
    assert all(slider.isEnabled() for slider in view.lens.values())
    assert view.lens["distortion"].value() == 100


def test_the_switch_turns_the_corrections_off_and_on(view, qtbot):
    with qtbot.waitSignal(view.session.image_ready, timeout=TIMEOUT):
        view.lens_on.click()
    assert not view.session.params.lens.enabled
    assert not any(slider.isEnabled() for slider in view.lens.values())
    # The profile stays, so the amounts are not lost
    assert view.session.params.lens.profile is not None

    with qtbot.waitSignal(view.session.image_ready, timeout=TIMEOUT):
        view.lens_on.click()
    assert view.session.params.lens.enabled


def test_an_amount_slider_commits_the_amount(view, qtbot):
    with qtbot.waitSignal(view.session.image_ready, timeout=TIMEOUT):
        view.lens["vignetting"].committed.emit(40.0)

    assert view.session.params.lens.vignetting == 40.0


def test_the_panel_says_when_no_lens_is_known(library, qtbot):
    photo = photo_named(library, "c.dng", lens="Some Other Lens 85mm")
    session = DevelopSession(library)
    made = develop_view.DevelopView(session)
    qtbot.addWidget(made)
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.open(*photo)

    assert not made.lens_on.isEnabled()
    assert made.lens_name.text() == "No profile for this lens"


def test_the_chooser_filters_and_picks(qtbot):
    dialog = LensDialog(["Canon EF 24-70mm f/2.8L", "Canon EF 50mm f/1.8", "Sigma 24mm"], None)
    qtbot.addWidget(dialog)

    dialog.filter.setText("24 canon")
    dialog.list.setCurrentRow(0)

    assert [dialog.list.item(r).isHidden() for r in range(3)] == [False, True, True]
    assert dialog.chosen() == "Canon EF 24-70mm f/2.8L"
