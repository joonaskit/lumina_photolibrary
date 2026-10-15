"""The viewer at 100 %: Z, the full size fetched, stepping zoomed, Escape."""

import pytest
from conftest import TIMEOUT
from photos import make_jpeg
from PySide6.QtCore import Qt

from lumina.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot, service, tmp_path):
    # Larger than the viewer, so that 100 % is larger than fitted
    folder = tmp_path / "burst"
    folder.mkdir()
    for number, colour in enumerate(((200, 40, 40), (40, 200, 40), (40, 40, 200))):
        make_jpeg(folder / f"{number}.jpg", size=(3000, 2000), colour=colour)
    made = MainWindow(service)
    qtbot.addWidget(made)
    made.resize(900, 600)
    made.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        made.add_folder(folder)
    qtbot.waitUntil(lambda: made.model.rowCount() == 3, timeout=TIMEOUT)
    return made


def press(qtbot, window, key):
    window.viewer.setFocus()
    qtbot.keyClick(window.viewer, key)


def shown_width(window):
    pixmap = window.viewer.pixmap()
    return pixmap.width() if pixmap is not None else 0


def test_z_shows_100_percent_from_the_full_size(qtbot, window):
    window.show_photo(0)
    qtbot.waitUntil(lambda: shown_width(window) > 320, timeout=TIMEOUT)
    assert window.viewer.full_size() == (3000, 2000)
    assert shown_width(window) < 3000

    press(qtbot, window, Qt.Key.Key_Z)

    assert window.viewer.zoom() == 1.0
    qtbot.waitUntil(lambda: shown_width(window) == 3000, timeout=TIMEOUT)
    assert "100 %" in window.status.text()
    assert "loading" not in window.status.text()


def test_stepping_zoomed_keeps_the_zoom_and_the_place(qtbot, window):
    window.show_photo(0)
    qtbot.waitUntil(lambda: shown_width(window) > 320, timeout=TIMEOUT)
    press(qtbot, window, Qt.Key.Key_Z)
    window.viewer.pan(-200, -100)
    place = window.viewer.center()

    press(qtbot, window, Qt.Key.Key_Right)

    assert window.title.text() == "1.jpg"
    assert window.viewer.is_zoomed() and window.viewer.center() == place
    qtbot.waitUntil(lambda: shown_width(window) == 3000, timeout=TIMEOUT)
    colour = window.viewer.pixmap().toImage().pixelColor(1500, 1000)
    assert colour.green() > 150 and colour.red() < 100
    # Said once the full size is there
    assert "100 %" in window.status.text() and "loading" not in window.status.text()


def test_escape_goes_back_to_fitted_then_to_the_grid(qtbot, window):
    window.show_photo(0)
    qtbot.waitUntil(lambda: shown_width(window) > 320, timeout=TIMEOUT)
    press(qtbot, window, Qt.Key.Key_Z)

    press(qtbot, window, Qt.Key.Key_Escape)
    assert not window.viewer.is_zoomed()
    assert window.pages.currentIndex() == 2

    press(qtbot, window, Qt.Key.Key_Escape)
    assert window.pages.currentIndex() == 1
    # Opening a photo again starts fitted
    window.show_photo(1)
    assert not window.viewer.is_zoomed()


def test_ctrl_plus_and_minus_zoom_in_steps(qtbot, window):
    window.show_photo(0)
    qtbot.waitUntil(lambda: shown_width(window) > 320, timeout=TIMEOUT)
    window.viewer.setFocus()

    qtbot.keyClick(window.viewer, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier)
    assert window.viewer.is_zoomed()
    qtbot.keyClick(window.viewer, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier)
    assert not window.viewer.is_zoomed()
