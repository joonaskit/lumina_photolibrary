import json

import numpy as np
import pytest
from conftest import TIMEOUT
from PySide6.QtCore import QPoint, Qt

from lumina.core import dcp
from lumina.core.edit import colour_profile
from lumina.qt_models import develop, workers
from lumina.qt_models.develop import DevelopSession, Params
from lumina.ui import develop_view, settings

CAMERA = "Testcam One"


def a_profile(name="Testcam Look", camera=CAMERA):
    return dcp.Profile(
        name=name,
        camera=camera,
        color_matrices=(colour_profile.XYZ_D65_TO_SRGB,),
        illuminants=(21,),
        forward_matrices=(colour_profile.D65_TO_D50 @ colour_profile.SRGB_TO_XYZ_D65,),
        tone_curve=np.array([[0.0, 0.0], [0.18, 0.4], [1.0, 1.0]]),
    )


@pytest.fixture(autouse=True)
def profiles(tmp_path, monkeypatch):
    """A folder of profiles of our own (and none of the system's); the work that would run
    in a worker process runs on a thread here, where the environment is this test's."""
    folder = tmp_path / "profiles"
    folder.mkdir()
    monkeypatch.setenv("LUMINA_PROFILES", str(folder))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(dcp, "default_folders", lambda: [folder, dcp.user_dir()], raising=True)
    dcp.forget_scans()
    colour_profile.prepare.cache_clear()
    submit = workers.submit

    def on_a_thread(fn, *args, on_done, on_error, owner, process=True):
        return submit(fn, *args, on_done=on_done, on_error=on_error, owner=owner, process=False)

    monkeypatch.setattr(develop.workers, "submit", on_a_thread)
    yield folder
    from lumina.core.edit import session

    session.close()
    dcp.forget_scans()
    colour_profile.prepare.cache_clear()


@pytest.fixture
def library(service, drive, qtbot):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    return service


def photo_named(library, name, model=CAMERA):
    row = next(p for p in library.db.photos() if p.path.endswith(name))
    with library.db.db:
        library.db.db.execute(
            "UPDATE photos SET make = ?, model = ? WHERE id = ?", ("Testcam", model, row.id)
        )
    return row.id, library.resolve(row.volume_key, row.path)


def opened(library, qtbot, photo):
    session = DevelopSession(library)
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.open(*photo)
    return session


def test_a_raw_file_starts_with_its_cameras_profile(library, qtbot, profiles):
    dcp.write_new(profiles / "look.dcp", a_profile())
    photo = photo_named(library, "c.dng")

    session = opened(library, qtbot, photo)

    assert session.is_raw()
    assert session.params.profile.name == "Testcam Look"
    assert dcp.stored(session.params.profile.id) is not None
    # Looking saves nothing
    assert library.edit_json(photo[0]) is None
    session.close()


def test_without_a_profile_a_raw_file_gets_luminas_look(library, qtbot):
    session = opened(library, qtbot, photo_named(library, "c.dng"))

    assert session.params.profile == develop.ColourProfile("standard", "Lumina Standard")
    assert session.profile_name() == "Lumina Standard"
    session.close()


def test_a_profile_from_a_colour_target_is_not_the_default(library, qtbot, profiles):
    dcp.write_new(profiles / "checker.dcp", a_profile(name="Testcam One ColorChecker 5000 K"))

    session = opened(library, qtbot, photo_named(library, "c.dng"))

    assert session.params.profile.id == "standard"
    session.close()


def test_a_jpeg_has_no_profile(library, qtbot):
    session = opened(library, qtbot, photo_named(library, "a.jpg"))

    assert not session.is_raw()
    assert session.params == Params()
    session.choose_profile("standard")
    assert session.params.profile.id == "libraw"
    session.close()


def test_an_edit_from_before_profiles_keeps_libraws_colours(library, qtbot, profiles):
    dcp.write_new(profiles / "look.dcp", a_profile())
    photo = photo_named(library, "c.dng")
    library.save_edit(photo[0], json.dumps({"version": 9, "exposure": 0.25}))

    session = opened(library, qtbot, photo)

    assert session.params.profile.id == "libraw"
    assert session.params.exposure == 0.25
    session.close()


def test_the_profiles_can_be_listed_and_chosen(library, qtbot, profiles):
    path = dcp.write_new(profiles / "look.dcp", a_profile())
    photo = photo_named(library, "c.dng")
    session = opened(library, qtbot, photo)

    with qtbot.waitSignal(session.profiles_ready, timeout=TIMEOUT) as ready:
        session.request_profiles()
    choices = ready.args[0]
    assert ("Lumina Standard", "standard") in choices
    assert ("Neutral (LibRaw)", "libraw") in choices
    assert ("Testcam Look", str(path)) in choices

    session.choose_profile("libraw", "Neutral (LibRaw)")
    assert json.loads(library.edit_json(photo[0]))["profile"]["id"] == "libraw"
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.choose_profile(str(path))
    assert session.params.profile.name == "Testcam Look"
    session.close()


def test_a_profile_from_any_file_can_be_used(library, qtbot, tmp_path):
    elsewhere = dcp.write_new(tmp_path / "elsewhere" / "x.dcp", a_profile(name="From a File"))
    photo = photo_named(library, "c.dng")
    session = opened(library, qtbot, photo)

    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.choose_profile(str(elsewhere))
    elsewhere.unlink()

    assert session.params.profile.name == "From a File"
    # The edit's copy is in the store: the file can go
    assert dcp.stored(session.params.profile.id) is not None
    session.close()


def test_a_file_that_is_not_a_profile_is_said(library, qtbot, tmp_path):
    bad = tmp_path / "bad.dcp"
    bad.write_bytes(b"not a profile")
    session = opened(library, qtbot, photo_named(library, "c.dng"))
    before = session.params

    with qtbot.waitSignal(session.failed, timeout=TIMEOUT) as failed:
        session.choose_profile(str(bad))

    assert "bad.dcp" in failed.args[0] or "camera profile" in failed.args[0]
    assert session.params == before
    session.close()


@pytest.fixture
def made_profiles(monkeypatch, profiles):
    """The makers of profiles, standing in for the fitting (tested in tests/core): they
    write a real profile, store it and say what they would."""
    calls = []
    from lumina.core.edit import session as worker

    def make(name, *args):
        calls.append(args)
        path = dcp.write_new(dcp.free_name(dcp.user_dir(), name), a_profile(name=name))
        ident, _ = dcp.store(path)
        return worker.Made(ident, name, str(path), 12.0, 3.0)

    monkeypatch.setattr(
        develop._session,
        "fit_camera_look",
        lambda paths, model: make(f"{model} Camera Look", paths),
    )
    monkeypatch.setattr(
        develop._session,
        "checker_profile",
        lambda path, look, corners, model, linear: make(
            f"{model} ColorChecker 5000 K", corners, linear
        ),
    )
    return calls


def test_the_cameras_look_is_made_from_its_raw_files(library, qtbot, made_profiles):
    photo = photo_named(library, "c.dng")
    session = opened(library, qtbot, photo)

    with qtbot.waitSignal(session.profile_made, timeout=TIMEOUT) as made:
        session.fit_camera_look()

    # The RAW files of that camera in the library, connected
    assert made_profiles[0][0] == [str(photo[1])]
    assert "Camera Look" in made.args[0] and "3.0 levels" in made.args[0]
    assert session.params.profile.name == f"{CAMERA} Camera Look"
    assert not session.is_making_profile()
    assert json.loads(library.edit_json(photo[0]))["profile"]["name"] == f"{CAMERA} Camera Look"
    session.close()


def test_a_camera_with_no_raw_files_connected_is_said(library, qtbot, made_profiles):
    session = opened(library, qtbot, photo_named(library, "c.dng"))
    with library.db.db:
        library.db.db.execute("UPDATE photos SET available = 0")

    with qtbot.waitSignal(session.failed, timeout=TIMEOUT) as failed:
        session.fit_camera_look()

    assert "No RAW files" in failed.args[0] and made_profiles == []
    session.close()


def test_photos_that_cannot_teach_a_look_are_said(library, qtbot):
    # The test's DNGs are nearly grey: too little to learn from
    session = opened(library, qtbot, photo_named(library, "c.dng"))

    with qtbot.waitSignal(session.failed, timeout=TIMEOUT) as failed:
        session.fit_camera_look()

    assert "learn" in failed.args[0]
    assert not session.is_making_profile()
    session.close()


def test_a_target_off_the_photo_is_said(library, qtbot):
    session = opened(library, qtbot, photo_named(library, "c.dng"))

    with qtbot.waitSignal(session.failed, timeout=TIMEOUT) as failed:
        session.make_checker_profile([(0.0, 0.0), (1.5, 0.0), (1.5, 1.5), (0.0, 1.5)])

    assert "off the photo" in failed.args[0]
    assert not session.is_making_profile()
    session.close()


def test_a_photo_that_is_not_of_a_target_is_said(library, qtbot):
    session = opened(library, qtbot, photo_named(library, "c.dng"))

    with qtbot.waitSignal(session.failed, timeout=TIMEOUT) as failed:
        session.make_checker_profile([(0.15, 0.2), (0.85, 0.2), (0.85, 0.8), (0.15, 0.8)])

    assert "ColorChecker" in failed.args[0]
    session.close()


def test_a_target_makes_a_profile_the_edit_uses(library, qtbot, made_profiles):
    session = opened(library, qtbot, photo_named(library, "c.dng"))
    corners = [(0.15, 0.2), (0.85, 0.2), (0.85, 0.8), (0.15, 0.8)]

    with qtbot.waitSignal(session.profile_made, timeout=TIMEOUT) as made:
        session.make_checker_profile(corners, linear=True)

    assert made_profiles[0] == (corners, True)
    assert "ΔE00" in made.args[0]
    assert "ColorChecker" in session.params.profile.name
    session.close()


# The panel


@pytest.fixture
def view(library, qtbot, profiles):
    dcp.write_new(profiles / "look.dcp", a_profile())
    session = DevelopSession(library)
    made = develop_view.DevelopView(session)
    qtbot.addWidget(made)
    made.resize(1200, 800)
    made.show()
    with qtbot.waitSignal(session.profiles_ready, timeout=TIMEOUT):
        session.open(*photo_named(library, "c.dng"))
    qtbot.waitUntil(lambda: made.image.pixmap() is not None, timeout=TIMEOUT)
    return made


def test_the_panel_shows_and_changes_the_profile(view, qtbot):
    choice = view.profile_choice
    assert choice.currentText() == "Testcam Look"
    assert choice.isEnabled() and view.profile_fit.isEnabled()

    index = choice.findData("standard")
    choice.setCurrentIndex(index)
    choice.activated.emit(index)

    assert view.session.params.profile.id == "standard"
    assert choice.currentText() == "Lumina Standard"


def test_the_panel_says_a_jpeg_has_no_profile(library, qtbot):
    session = DevelopSession(library)
    made = develop_view.DevelopView(session)
    qtbot.addWidget(made)
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.open(*photo_named(library, "a.jpg"))

    assert not made.profile_choice.isEnabled() and not made.checker_button.isEnabled()
    assert "Only RAW files" in made.profile_note.text()


def test_four_clicks_on_the_target_then_make_profile(view, qtbot, made_profiles):
    view.checker_button.click()
    assert view.is_picking() and view.checker_make.isVisible()
    assert "dark skin" in view.profile_note.text()
    rect = view.image.image_rect()
    for x, y in ((0.15, 0.2), (0.85, 0.2), (0.85, 0.8), (0.15, 0.8)):
        point = QPoint(int(rect.left() + x * rect.width()), int(rect.top() + y * rect.height()))
        qtbot.mouseClick(view.image, Qt.MouseButton.LeftButton, pos=point)

    assert len(view.image.points) == 4 and view.checker_make.isEnabled()
    with qtbot.waitSignal(view.session.profile_made, timeout=TIMEOUT):
        view.checker_make.click()

    assert not view.is_picking() and not view.image.picking
    assert "ColorChecker" in view.session.params.profile.name


def test_picking_is_cancelled(view):
    view.checker_button.click()

    view.cancel_picking()

    assert not view.is_picking() and view.image.points == []
    assert not view.checker_make.isVisible()


def test_profile_folders_are_kept(qtbot, tmp_path):
    from lumina.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    dialog.add_profile_folder_path(str(tmp_path / "mine"))
    dialog.add_profile_folder_path(str(tmp_path / "mine"))

    assert settings.profile_folders() == [str(tmp_path / "mine")]
    dialog.profile_folders.setCurrentRow(0)
    dialog.remove_chosen_profile_folder()
    assert settings.profile_folders() == []
