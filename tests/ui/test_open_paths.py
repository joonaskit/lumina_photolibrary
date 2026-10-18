"""Opening photos and folders from outside: the command line, the file manager."""

from conftest import TIMEOUT
from photos import make_jpeg
from PySide6.QtWidgets import QMessageBox
from test_main_window import add_and_wait, open_window


def show_viewed(qtbot, window, name):
    qtbot.waitUntil(lambda: window.pages.currentIndex() == 2, timeout=TIMEOUT)
    assert window.title.text() == name


def test_a_photo_of_the_library_opens_in_the_viewer(qtbot, service, drive):
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")
    window.nav.show_grid()

    window.opening.open_photo(str(drive / "Photos" / "b.jpg"))

    show_viewed(qtbot, window, "b.jpg")
    assert not window.opening.quick_views


def test_a_photo_of_a_folder_inside_the_library_opens_in_the_library(qtbot, service, drive):
    (drive / "Photos" / "sub").mkdir()
    make_jpeg(drive / "Photos" / "sub" / "d.jpg")
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")

    window.opening.open_photo(str(drive / "Photos" / "sub" / "d.jpg"))

    show_viewed(qtbot, window, "d.jpg")


def test_a_photo_outside_the_library_is_shown_apart_and_nothing_is_added(qtbot, service, drive):
    other = drive / "Other"
    other.mkdir()
    make_jpeg(other / "x.jpg", size=(120, 80))
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")

    window.opening.open_photo(str(other / "x.jpg"))

    assert len(window.opening.quick_views) == 1
    view = window.opening.quick_views[0]
    qtbot.waitUntil(lambda: view.view.pixmap() is not None, timeout=TIMEOUT)
    assert [f.name for f in service.folders()] == ["Photos"]
    assert window.pages.currentIndex() == 1


def test_not_now_keeps_the_photo_and_asks_no_more(qtbot, service, drive):
    other = drive / "Other"
    other.mkdir()
    make_jpeg(other / "x.jpg")
    window = open_window(qtbot, service)
    window.opening.open_photo(str(other / "x.jpg"))
    view = window.opening.quick_views[0]

    view.dismiss_button.click()

    assert view.banner.isHidden()
    assert view.isVisible()
    assert service.folders() == []


def test_saying_yes_adds_the_folder_and_opens_the_photo(qtbot, service, drive):
    other = drive / "Other"
    other.mkdir()
    make_jpeg(other / "x.jpg")
    make_jpeg(other / "y.jpg")
    window = open_window(qtbot, service)

    window.opening.open_photo(str(other / "y.jpg"))
    view = window.opening.quick_views[0]
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        view.add_button.click()

    show_viewed(qtbot, window, "y.jpg")
    assert [f.name for f in service.folders()] == ["Other"]
    qtbot.waitUntil(lambda: not window.opening.quick_views, timeout=TIMEOUT)


def test_a_folder_is_added_and_shown(qtbot, service, drive):
    window = open_window(qtbot, service)

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.opening.open_paths([str(drive / "Photos")])

    assert [f.name for f in service.folders()] == ["Photos"]


def test_something_that_is_not_a_photo_is_told(qtbot, service, drive, monkeypatch):
    (drive / "Photos" / "notes.txt").write_text("hello")
    told = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: told.append(args[2]))
    window = open_window(qtbot, service)

    window.opening.open_paths(
        [str(drive / "Photos" / "notes.txt"), str(drive / "Photos" / "gone.jpg")]
    )

    assert len(told) == 2
    assert not window.opening.quick_views
    assert service.folders() == []
