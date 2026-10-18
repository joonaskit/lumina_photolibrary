"""Tethering a folder (#57): a new photo there is taken in as soon as it is complete and
shown, not after the watcher's quiet moment."""

import io
import json
import os
import threading
import time

import pytest
from conftest import TIMEOUT
from photos import make_jpeg

from lumina.core.edit import params as _params
from lumina.qt_models.hot_folder import HotFolder
from lumina.ui import settings
from lumina.ui.main_window import _DEVELOP, _GRID, _VIEWER, MainWindow
from lumina.ui.sidebar import FOLDER_ROLE
from lumina.ui.tether import TetherDialog, describe


@pytest.fixture
def scanned(qtbot, service, drive):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        folder = service.add_folder(drive / "Photos")
    return service, folder


@pytest.fixture
def tether(scanned):
    service, _folder = scanned
    # The watcher's quiet moment is the app's 8 s: a photo that comes sooner came by tethering
    return HotFolder(service, settle=0.2, linger=0.5, parent=service)


def wait_watched(qtbot, service, folder):
    watcher = service.watcher
    qtbot.waitUntil(lambda: folder in watcher.watched and not watcher._queue, timeout=TIMEOUT)


def ids(service):
    from lumina.core import db as dbm

    return {photo.id for photo in service.db.photos(dbm.Filter())}


def jpeg_bytes(colour):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 32), colour).save(out, "JPEG")
    return out.getvalue()


def write_slowly(path, data, pieces=4, pause=0.1):
    """A capture program writing a frame as it downloads it."""

    def write():
        step = -(-len(data) // pieces)
        with open(path, "wb") as file:
            for start in range(0, len(data), step):
                file.write(data[start : start + step])
                file.flush()
                time.sleep(pause)

    thread = threading.Thread(target=write)
    thread.start()
    return thread


def test_a_new_photo_is_taken_in_at_once_with_watching_off(qtbot, scanned, tether, drive):
    service, folder = scanned
    assert not service.watcher.enabled
    tether.start(folder)
    wait_watched(qtbot, service, folder)
    before = ids(service)
    path = drive / "Photos" / "IMG_0001.jpg"

    began = time.monotonic()
    with qtbot.waitSignal(tether.arrived, timeout=TIMEOUT) as arrived:
        writer = write_slowly(path, jpeg_bytes((10, 20, 30)))
    writer.join()

    assert time.monotonic() - began < 5
    (photo,) = ids(service) - before
    assert arrived.args == [photo]
    assert tether.count == 1
    # Read once, complete
    stored = service.db.photo(photo)
    assert stored.size == os.path.getsize(path)
    qtbot.waitUntil(lambda: service.db.photo(photo).model == "Canon EOS 90D", timeout=TIMEOUT)
    assert service.db.photo(photo).content_id


def test_each_new_photo_is_given_the_preset(qtbot, scanned, tether, drive):
    service, folder = scanned
    document = _params.preset(_params.Params(exposure=1.0), ["exposure"])
    service.save_preset("Studio", json.dumps(document))
    old = ids(service)
    tether.start(folder, "Studio")
    wait_watched(qtbot, service, folder)

    with qtbot.waitSignal(tether.arrived, timeout=TIMEOUT) as arrived:
        make_jpeg(drive / "Photos" / "IMG_0002.jpg", colour=(1, 2, 3))

    edit = _params.from_json(service.edit_json(arrived.args[0]))
    assert edit.exposure == 1.0
    assert all(service.edit_json(photo) is None for photo in old)


def test_a_preset_there_is_not_is_refused(scanned, tether):
    _service, folder = scanned

    with pytest.raises(KeyError):
        tether.start(folder, "Gone")
    assert tether.folder is None


def test_stopping_goes_back_to_the_normal_watcher(qtbot, scanned, tether, drive):
    service, folder = scanned
    tether.start(folder)
    wait_watched(qtbot, service, folder)
    assert service.watcher.hot == folder

    tether.stop()

    assert service.watcher.hot is None and tether.folder is None
    # Watching is off: the folder is let go of, and a photo written now waits for a scan
    assert folder not in service.watcher.watched
    arrived = []
    tether.arrived.connect(arrived.append)
    make_jpeg(drive / "Photos" / "IMG_0003.jpg", colour=(4, 5, 6))
    qtbot.wait(800)
    assert arrived == []


def test_with_watching_on_the_folder_stays_watched_after_tethering(qtbot, scanned, tether, drive):
    service, folder = scanned
    service.watcher.set_enabled(True)
    tether.start(folder)
    wait_watched(qtbot, service, folder)

    tether.stop()

    assert folder in service.watcher.watched


def test_a_photo_in_a_folder_made_while_tethered_is_taken_in(qtbot, scanned, tether, drive):
    service, folder = scanned
    tether.start(folder)
    wait_watched(qtbot, service, folder)
    day = drive / "Photos" / "2026-10-17"

    day.mkdir()
    qtbot.waitUntil(lambda: str(day) in service.watcher._owner, timeout=TIMEOUT)
    with qtbot.waitSignal(tether.arrived, timeout=TIMEOUT):
        make_jpeg(day / "IMG_0004.jpg", colour=(7, 8, 9))


def test_removing_the_folder_stops_tethering_and_says_so(qtbot, scanned, tether):
    service, folder = scanned
    tether.start(folder)

    with qtbot.waitSignal(tether.stopped, timeout=TIMEOUT) as stopped:
        service.remove_folder(folder)

    assert tether.folder is None
    assert stopped.args == ["The tethered folder was removed from the library"]


def test_unplugging_the_drive_stops_tethering(qtbot, scanned, tether, mounts):
    service, folder = scanned
    tether.start(folder)

    with qtbot.waitSignal(tether.stopped, timeout=TIMEOUT) as stopped:
        mounts.plugged = False

    assert stopped.args == ["Tethering stopped: Photos is not connected"]


# The window


def open_window(qtbot, service, tether):
    window = MainWindow(service, tether=tether)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    return window


def shoot(qtbot, tether, path, colour):
    with qtbot.waitSignal(tether.arrived, timeout=TIMEOUT) as arrived:
        make_jpeg(path, colour=colour)
    return arrived.args[0]


def test_tethering_from_the_sidebar_asks_for_the_preset_and_shows_it(
    qtbot, scanned, tether, monkeypatch
):
    service, folder = scanned
    service.save_preset(
        "Studio", json.dumps(_params.preset(_params.Params(exposure=1.0), ["exposure"]))
    )
    window = open_window(qtbot, service, tether)
    monkeypatch.setattr(TetherDialog, "exec", lambda dialog: dialog.preset.setCurrentIndex(1) or 1)

    window.sidebar.tether_requested.emit(folder)

    assert tether.folder == folder and tether.preset == "Studio"
    assert settings.tether_preset() == "Studio"
    assert window.tether_status.isVisible()
    assert (
        window.tether_status.label.text() == "Tethered to Photos: no new photos yet, preset Studio"
    )
    assert window.sidebar.current_folder() == folder

    window.tether_status.stop.click()

    assert tether.folder is None and not window.tether_status.isVisible()


def test_stop_tethering_in_the_sidebars_menu_stops_it(qtbot, scanned, tether):
    service, folder = scanned
    window = open_window(qtbot, service, tether)
    tether.start(folder)
    item = next(
        window.sidebar.list.item(n)
        for n in range(window.sidebar.list.count())
        if window.sidebar.list.item(n).data(FOLDER_ROLE) == folder
    )

    menu = window.sidebar.menu_for(item)
    (action,) = [action for action in menu.actions() if action.text() == "Stop Tethering"]
    action.trigger()

    assert tether.folder is None and service.watcher.hot is None
    assert not window.tether_status.isVisible()


def test_the_newest_photo_opens_in_the_viewer(qtbot, scanned, tether, drive):
    service, folder = scanned
    window = open_window(qtbot, service, tether)
    tether.start(folder)
    wait_watched(qtbot, service, folder)
    assert window.pages.currentIndex() == _GRID

    photo = shoot(qtbot, tether, drive / "Photos" / "IMG_0010.jpg", (20, 20, 20))

    qtbot.waitUntil(lambda: window.nav.viewed_id == photo, timeout=TIMEOUT)
    assert window.pages.currentIndex() == _VIEWER
    assert window.tether_status.label.text() == "Tethered to Photos: 1 new photo"

    # And the next, while the last one is looked at
    second = shoot(qtbot, tether, drive / "Photos" / "IMG_0011.jpg", (40, 40, 40))
    qtbot.waitUntil(lambda: window.nav.viewed_id == second, timeout=TIMEOUT)


def test_an_older_photo_looked_at_is_not_taken_away(qtbot, scanned, tether, drive):
    service, folder = scanned
    window = open_window(qtbot, service, tether)
    tether.start(folder)
    wait_watched(qtbot, service, folder)
    photo = shoot(qtbot, tether, drive / "Photos" / "IMG_0020.jpg", (20, 20, 20))
    qtbot.waitUntil(lambda: window.nav.viewed_id == photo, timeout=TIMEOUT)

    # Back to an older one
    older = min(ids(service))
    window.nav.show_photo(window.model.row_of(older))
    second = shoot(qtbot, tether, drive / "Photos" / "IMG_0021.jpg", (40, 40, 40))
    qtbot.waitUntil(lambda: window.model.row_of(second) is not None, timeout=TIMEOUT)
    qtbot.wait(100)

    assert window.nav.viewed_id == older
    # Onto the newest again: followed from there
    window.nav.show_photo(window.model.row_of(second))
    third = shoot(qtbot, tether, drive / "Photos" / "IMG_0022.jpg", (60, 60, 60))
    qtbot.waitUntil(lambda: window.nav.viewed_id == third, timeout=TIMEOUT)


def test_the_develop_view_stays_open_showing_the_new_photo(qtbot, scanned, tether, drive):
    service, folder = scanned
    window = open_window(qtbot, service, tether)
    tether.start(folder)
    wait_watched(qtbot, service, folder)
    window.nav.show_develop(window.model.rowCount() - 1)
    assert window.pages.currentIndex() == _DEVELOP

    photo = shoot(qtbot, tether, drive / "Photos" / "IMG_0030.jpg", (20, 20, 20))

    qtbot.waitUntil(lambda: window.nav.viewed_id == photo, timeout=TIMEOUT)
    assert window.pages.currentIndex() == _DEVELOP
    window.session.close()


def test_the_status_says_how_many_and_the_preset():
    assert describe("Studio", 0, None) == "Tethered to Studio: no new photos yet"
    assert describe("Studio", 1, None) == "Tethered to Studio: 1 new photo"
    assert describe("Studio", 12, "Product") == "Tethered to Studio: 12 new photos, preset Product"
