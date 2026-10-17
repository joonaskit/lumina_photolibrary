"""Shooting with a camera on USB (#58), with a fake camera kept in a folder: each frame is
saved into the session's folder, taken into the library at once, given the preset, put in
the session's collection and shown."""

import json
import os
from datetime import datetime
from functools import partial

import pytest
from conftest import TIMEOUT
from fakes import FakeCamera
from photos import make_jpeg_with_exif
from PySide6.QtCore import QPoint, Qt

from lumina.core import db as dbm
from lumina.core.edit import params as _params
from lumina.qt_models import tether as camera_service
from lumina.qt_models.hot_folder import HotFolder
from lumina.qt_models.tether import CARD, COMPUTER, CameraService
from lumina.ui import settings
from lumina.ui.camera import CameraBar, CameraDialog, describe
from lumina.ui.main_window import _GRID, _LIVE, _VIEWER, MainWindow

TIMING = {"poll": 0.02, "retry": 0.05, "readings_every": 60.0, "live_fails_for": 0.3}


@pytest.fixture
def scanned(qtbot, service, drive):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        folder = service.add_folder(drive / "Photos")
    return service, folder


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "camera"
    path.mkdir()
    return path


@pytest.fixture
def camera(scanned, root):
    service, _folder = scanned
    hot = HotFolder(service, settle=0.1, linger=0.5, parent=service)
    made = CameraService(service, hot, partial(FakeCamera, root), in_process=False, parent=service)
    made._timing = TIMING
    yield made
    made.close()


def frame(tmp_path, name="shot.jpg"):
    return make_jpeg_with_exif(tmp_path / name).read_bytes()


def day():
    return f"{datetime.now():%Y}/{datetime.now():%Y-%m-%d}"


def connected(qtbot, camera):
    qtbot.waitUntil(lambda: camera.connected, timeout=TIMEOUT)


def test_a_frame_is_saved_into_the_session_and_taken_into_the_library(
    qtbot, scanned, camera, root, drive, tmp_path
):
    service, folder = scanned
    camera.start(folder, "Studio")
    connected(qtbot, camera)
    assert camera.where == str(drive / "Photos" / day() / "Studio")
    assert camera.info.model == "Fake EOS"
    assert FakeCamera.log(root, "target") == ["Memory card"]

    with qtbot.waitSignal(camera.hot.arrived, timeout=TIMEOUT) as arrived:
        FakeCamera.shoot(root, frame(tmp_path))

    photo = service.db.photo(arrived.args[0])
    assert photo["path"] == f"Photos/{day()}/Studio/20240601-183005-IMG_6000.CR3"
    assert camera.saved_count == 1
    # In the session's collection
    (collection,) = [c for c in service.collections() if c.name == "Studio"]
    qtbot.waitUntil(
        lambda: (
            [p.id for p in service.db.photos(dbm.Filter(collection_id=collection.id))]
            == [photo["id"]]
        ),
        timeout=TIMEOUT,
    )
    session = service.db.tether_session(camera.session.id)
    assert (session.folder, session.keep, session.collection_id) == (
        f"Photos/{day()}/Studio",
        CARD,
        collection.id,
    )


def test_each_photo_of_the_session_gets_its_preset(qtbot, scanned, camera, root, tmp_path):
    service, folder = scanned
    document = _params.preset(_params.Params(exposure=1.0), ["exposure"])
    service.save_preset("Product", json.dumps(document))
    camera.start(folder, "Studio", preset="Product")
    connected(qtbot, camera)

    with qtbot.waitSignal(camera.hot.arrived, timeout=TIMEOUT) as arrived:
        FakeCamera.shoot(root, frame(tmp_path))

    assert _params.from_json(service.edit_json(arrived.args[0])).exposure == 1.0


def test_computer_only_keeps_the_frame_in_the_cameras_memory_until_saved(
    qtbot, scanned, camera, root, tmp_path
):
    _service, folder = scanned
    camera.start(folder, "Studio", keep=COMPUTER)
    connected(qtbot, camera)
    assert FakeCamera.log(root, "target") == ["Internal RAM"]

    with qtbot.waitSignal(camera.saved, timeout=TIMEOUT):
        FakeCamera.shoot(root, frame(tmp_path))

    qtbot.waitUntil(lambda: FakeCamera.log(root, "released") == ["capt0000.cr3"], timeout=TIMEOUT)


def test_ending_the_session_takes_in_what_is_saved_and_lets_the_folder_go(
    qtbot, scanned, camera, root
):
    service, folder = scanned
    camera.start(folder, "Studio")
    connected(qtbot, camera)
    assert camera.hot.folder == folder

    with qtbot.waitSignal(
        camera.changed, timeout=TIMEOUT, check_params_cb=lambda: not camera.running
    ):
        camera.stop()
        assert camera.ending

    assert not camera.running and camera.hot.folder is None
    assert service.watcher.hot is None


def test_a_camera_another_program_has_is_told(qtbot, scanned, camera, root):
    _service, folder = scanned
    FakeCamera.make_busy(root)

    camera.start(folder, "Studio")

    qtbot.waitUntil(lambda: "Another program" in (camera.message or ""), timeout=TIMEOUT)
    assert not camera.connected
    FakeCamera.make_busy(root, False)
    connected(qtbot, camera)


def test_an_unplugged_camera_is_told_and_waited_for(qtbot, scanned, camera, root):
    _service, folder = scanned
    camera.start(folder, "Studio")
    connected(qtbot, camera)

    FakeCamera.unplug(root)
    qtbot.waitUntil(lambda: not camera.connected, timeout=TIMEOUT)
    assert "Connect the camera" in camera.message or "disconnected" in camera.message

    FakeCamera.unplug(root, False)
    connected(qtbot, camera)


def test_a_frame_not_saved_is_named_and_fetched_again(qtbot, scanned, camera, root, tmp_path):
    _service, folder = scanned
    camera.start(folder, "Studio")
    connected(qtbot, camera)
    FakeCamera.fail_fetching(root)

    with qtbot.waitSignal(camera.failed, timeout=TIMEOUT) as failed:
        FakeCamera.shoot(root, frame(tmp_path))
    assert failed.args[0] == "IMG_6000.CR3"
    assert len(camera.failures) == 1

    FakeCamera.fail_fetching(root, False)
    with qtbot.waitSignal(camera.saved, timeout=TIMEOUT):
        camera.fetch_again()
    qtbot.waitUntil(lambda: not camera.failures, timeout=TIMEOUT)


def test_a_setting_changed_is_read_back(qtbot, scanned, camera, root):
    _service, folder = scanned
    camera.start(folder, "Studio")
    connected(qtbot, camera)

    camera.set_setting("iso", "400")

    qtbot.waitUntil(lambda: camera.settings["iso"].value == "400", timeout=TIMEOUT)


def test_a_session_runs_in_a_process_of_its_own(qtbot, scanned, root, tmp_path):
    service, folder = scanned
    hot = HotFolder(service, settle=0.1, linger=0.5, parent=service)
    camera = CameraService(service, hot, partial(FakeCamera, root), parent=service)
    camera._timing = TIMING
    try:
        camera.start(folder, "Studio")
        qtbot.waitUntil(lambda: camera.connected, timeout=TIMEOUT)
        with qtbot.waitSignal(hot.arrived, timeout=TIMEOUT):
            FakeCamera.shoot(root, frame(tmp_path))
        assert camera._worker.pid != os.getpid()
    finally:
        camera.close()


# Live view (#59)


def live_on(qtbot, camera):
    with qtbot.waitSignal(camera.frame_ready, timeout=TIMEOUT):
        camera.set_live(True)
    return camera.frame


def test_live_view_shows_the_newest_frames_and_none_after_it_is_stopped(
    qtbot, scanned, camera, root
):
    _service, folder = scanned
    camera.start(folder, "Studio")
    connected(qtbot, camera)
    first = live_on(qtbot, camera)
    assert camera.live.on and camera.live.view.zooms == (1, 5, 10)
    assert (first.image.width(), first.image.height()) == (96, 64)
    assert first.look.luminance and first.clipping is None
    # One after another, each one newer
    numbers = [first.number]
    camera.frame_ready.connect(lambda: numbers.append(camera.frame.number))
    qtbot.waitUntil(lambda: len(numbers) >= 5, timeout=TIMEOUT)
    assert numbers == sorted(set(numbers))

    camera.set_clipping(True)
    qtbot.waitUntil(lambda: camera.frame.clipping is not None, timeout=TIMEOUT)

    camera.set_live(False)
    assert not camera.live.on and camera.frame is None
    del numbers[:]
    qtbot.waitUntil(lambda: FakeCamera.log(root, "live") == ["on", "off"], timeout=TIMEOUT)
    qtbot.wait(300)
    assert numbers == [] and camera.frame is None


def test_live_view_stops_in_the_background_and_starts_again_in_front(qtbot, scanned, camera, root):
    _service, folder = scanned
    camera.start(folder, "Studio")
    connected(qtbot, camera)
    camera._pause.setInterval(50)
    live_on(qtbot, camera)

    camera.set_active(False)
    qtbot.waitUntil(lambda: camera.live.why == camera_service.PAUSED, timeout=TIMEOUT)
    assert not camera.live_wanted and camera.frame is None
    qtbot.waitUntil(lambda: FakeCamera.log(root, "live") == ["on", "off"], timeout=TIMEOUT)

    with qtbot.waitSignal(camera.frame_ready, timeout=TIMEOUT):
        camera.set_active(True)
    assert camera.live.on and camera.live.why is None

    # Back in front soon enough: it never stopped
    camera.set_active(False)
    camera.set_active(True)
    qtbot.wait(200)
    assert camera.live.on


def test_live_view_the_camera_stops_says_why(qtbot, scanned, camera, root):
    _service, folder = scanned
    camera.start(folder, "Studio")
    connected(qtbot, camera)
    FakeCamera.fail_previews(root)
    camera.set_live(True)

    qtbot.waitUntil(lambda: camera.live.why is not None, timeout=TIMEOUT)
    assert camera.live.why.startswith("The camera stopped sending live view")
    assert not camera.live_wanted and not camera.live.on


def test_the_crop_guide_is_the_session_presets_crop(qtbot, scanned, camera):
    service, folder = scanned
    edit = _params.Params(rotate=1, crop=_params.Crop(0.0, 0.0, 0.5, 1.0))
    service.save_preset("Square", json.dumps(_params.preset(edit, ["rotate", "crop"])))
    service.save_preset(
        "Bright", json.dumps(_params.preset(_params.Params(exposure=1), ["exposure"]))
    )
    camera.start(folder, "Studio", preset="Square")
    assert camera.crop_guide() == pytest.approx((0.0, 0.5, 1.0, 1.0))
    camera.stop()
    qtbot.waitUntil(lambda: not camera.running, timeout=TIMEOUT)

    camera.start(folder, "Other", preset="Bright")
    assert camera.crop_guide() is None


# The window


def open_window(qtbot, service, camera):
    window = MainWindow(service, tether=camera.hot, camera=camera)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    return window


def test_tether_camera_starts_a_session_and_the_new_photo_opens(
    qtbot, scanned, camera, root, tmp_path, monkeypatch
):
    service, folder = scanned
    window = open_window(qtbot, service, camera)
    assert window.sidebar.camera_button.isVisibleTo(window.sidebar)
    assert not window.capture_action.isEnabled()

    def accept(dialog):
        dialog.name_field.setText("Product shoot")
        return CameraDialog.DialogCode.Accepted

    # The fake camera needs no python-gphoto2, which the tests are run without
    monkeypatch.setattr(camera_service, "available", lambda: True)
    monkeypatch.setattr(CameraDialog, "exec", accept)
    window.camera_action.trigger()

    assert camera.running and camera.session.name == "Product shoot"
    assert settings.camera_choices()["name"] == "Product shoot"
    connected(qtbot, camera)
    assert window.camera_bar.isVisible() and window.capture_action.isEnabled()
    assert window.camera_bar.camera.text() == "Fake EOS"
    assert not window.tether_status.isVisible()

    with qtbot.waitSignal(camera.hot.arrived, timeout=TIMEOUT) as arrived:
        FakeCamera.shoot(root, frame(tmp_path))
    qtbot.waitUntil(lambda: window._viewed_id == arrived.args[0], timeout=TIMEOUT)
    assert window.pages.currentIndex() == _VIEWER

    # Space, or a foot pedal sending it, takes a photo
    window.capture_action.trigger()
    qtbot.waitUntil(lambda: camera.saved_count == 2, timeout=TIMEOUT)
    assert FakeCamera.log(root, "triggers") == ["focus"]

    window.camera_bar.end.click()
    qtbot.waitUntil(lambda: not window.camera_bar.isVisible(), timeout=TIMEOUT)
    assert not window.capture_action.isEnabled()


def test_stop_tethering_in_the_sidebar_ends_the_camera_session(qtbot, scanned, camera):
    service, folder = scanned
    window = open_window(qtbot, service, camera)
    camera.start(folder, "Studio")
    connected(qtbot, camera)

    window.stop_tethering()

    qtbot.waitUntil(lambda: not camera.running, timeout=TIMEOUT)
    assert camera.hot.folder is None


def test_the_bar_shows_the_settings_and_why_it_cannot_shoot(qtbot, scanned, camera, root, request):
    _service, folder = scanned
    bar = CameraBar()
    qtbot.addWidget(bar)
    follow = camera.changed.connect(lambda: bar.show_service(camera))
    request.addfinalizer(lambda: camera.changed.disconnect(follow))
    FakeCamera.unplug(root)
    camera.start(folder, "Studio")
    bar.show()
    qtbot.waitUntil(lambda: "Connect the camera" in bar.state.text(), timeout=TIMEOUT)
    assert not bar.capture.isEnabled()

    FakeCamera.unplug(root, False)
    connected(qtbot, camera)
    _label, iso = bar.boxes["iso"]
    assert iso.isVisibleTo(bar) and iso.currentText() == "100"
    assert [iso.itemText(i) for i in range(iso.count())] == ["Auto", "100", "200", "400"]
    assert not bar.boxes["aperture"][1].isVisibleTo(bar)
    assert bar.state.text() == "battery 56%, 0 photos this session"

    with qtbot.waitSignal(bar.setting_chosen, timeout=1000) as chosen:
        iso.setCurrentIndex(3)
        iso.activated.emit(3)
    assert chosen.args == ["iso", "400"]


def test_the_dialog_says_where_the_photos_go(qtbot, scanned):
    service, folder = scanned
    folders = service.folders()
    dialog = CameraDialog(folders, ["Product"], settings.camera_choices())
    qtbot.addWidget(dialog)

    dialog.name_field.setText("Studio")
    assert dialog.example.text() == os.path.join(folders[0].path, day(), "Studio", "")
    assert not dialog.warning.isVisibleTo(dialog)
    dialog.computer.setChecked(True)
    assert dialog.warning.isVisibleTo(dialog)
    dialog.preset.setCurrentIndex(1)

    assert dialog.chosen() == {
        "folder": folder,
        "layout": "year-day",
        "name": "Studio",
        "keep": COMPUTER,
        "preset": "Product",
    }


def test_the_state_in_words():
    class Stood:
        message = None
        settings = {}
        saved_count = 3
        fetching = {7: "IMG_0007.CR3"}
        failures = {5: None}

    assert describe(Stood) == "3 photos this session, fetching IMG_0007.CR3, 1 not saved"
    Stood.message = "Connect the camera by USB and turn it on"
    assert describe(Stood) == "Connect the camera by USB and turn it on"


def test_the_bar_does_not_make_the_window_wider(qtbot, service):
    """A window can't be narrower than its widest row: a bar wider than the screen kept
    it from being maximized (the 90D's choices, and a long reason, made it 1869 px)."""
    from lumina.core.tether import CameraInfo, Setting

    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    before = window.minimumSizeHint().width()

    class Stood:
        running, connected, ending, can_focus = True, True, False, True
        live = camera_service.LiveState(None)
        info = CameraInfo("Canon EOS 90D", "x", "usb:003,006")
        message = None
        saved_count, fetching, failures = 12, {}, {}
        settings = {
            name: Setting(name, name, value, choices)
            for name, value, choices in (
                ("iso", "Auto", ("Auto", "100", "25600")),
                ("shutterspeed", "1/250", ("bulb", "30", "1/8000", "1/250")),
                ("aperture", "5.6", ("5", "5.6", "32")),
                ("whitebalance", "Color Temperature", ("Auto", "Color Temperature")),
                ("imageformat", "cRAW + cS2", ("L", "RAW + L", "cRAW + cS2")),
                ("drivemode", "Continuous high speed", ("Single", "Continuous high speed")),
            )
        }

    window.camera_bar.show_service(Stood)
    assert window.minimumSizeHint().width() <= before
    Stood.message = "KDE has the camera open (a Dolphin window or the device notifier " * 3
    Stood.connected = False
    window.camera_bar.show_service(Stood)
    assert window.minimumSizeHint().width() <= before


def test_live_view_in_the_window(qtbot, scanned, camera, root, tmp_path):
    service, folder = scanned
    window = open_window(qtbot, service, camera)
    camera.start(folder, "Studio")
    connected(qtbot, camera)
    with qtbot.waitSignal(camera.hot.arrived, timeout=TIMEOUT):
        FakeCamera.shoot(root, frame(tmp_path), "IMG_0001.JPG")
    qtbot.waitUntil(lambda: window.pages.currentIndex() == _VIEWER, timeout=TIMEOUT)
    window.show_grid()

    with qtbot.waitSignal(camera.frame_ready, timeout=TIMEOUT):
        window.camera_bar.live.click()
    panel = window.live_view
    assert window.pages.currentIndex() == _LIVE and window.camera_bar.live.isChecked()
    qtbot.waitUntil(lambda: panel.canvas.frame is not None, timeout=TIMEOUT)
    assert panel.histogram.isVisible() and panel.start.isChecked()
    assert [b.isVisible() for b in panel.zooms.values()] == [True, True, True]

    # Focus, without a picture
    assert window.camera_bar.autofocus.isEnabled()
    window.camera_bar.autofocus.click()
    qtbot.waitUntil(lambda: FakeCamera.log(root, "focus") == ["at the point"], timeout=TIMEOUT)

    # A click focuses there, with Focus on Click
    panel.focus.setChecked(True)
    target = panel.canvas.image_rect()
    spot = QPoint(round(target.left() + target.width() / 4), round(target.center().y()))
    qtbot.mouseClick(panel.canvas, Qt.MouseButton.LeftButton, pos=spot)
    qtbot.waitUntil(lambda: len(FakeCamera.log(root, "focus")) == 2, timeout=TIMEOUT)
    x, y = map(float, FakeCamera.log(root, "point")[0].split(","))
    assert x == pytest.approx(0.25, abs=0.02) and y == pytest.approx(0.5, abs=0.02)
    panel.zooms[5].click()
    qtbot.waitUntil(lambda: camera.live.zoom == 5, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: panel.canvas.zoom == 5, timeout=TIMEOUT)

    # The last shot over the frame
    with qtbot.waitSignal(window.last_shot.ready, timeout=TIMEOUT):
        panel.last.setChecked(True)
    assert panel.canvas.last_shot is not None and panel.canvas.opacity == 0.4

    # A shot taken in live view stays in it
    with qtbot.waitSignal(camera.hot.arrived, timeout=TIMEOUT):
        window.capture_action.trigger()
    qtbot.wait(200)
    assert window.pages.currentIndex() == _LIVE

    # Escape leaves it, and live view stops
    window.back_action.trigger()
    assert window.pages.currentIndex() == _GRID and not window.camera_bar.live.isChecked()
    assert not camera.live_wanted
    qtbot.waitUntil(lambda: FakeCamera.log(root, "live")[-1] == "off", timeout=TIMEOUT)

    # Ending the session in live view leaves it
    with qtbot.waitSignal(camera.frame_ready, timeout=TIMEOUT):
        window.camera_bar.live.click()
    window.camera_bar.end.click()
    qtbot.waitUntil(lambda: window.pages.currentIndex() == _GRID, timeout=TIMEOUT)
