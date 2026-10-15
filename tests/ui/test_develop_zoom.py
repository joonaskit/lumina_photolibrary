"""The Develop view at 100 %: the photo's own pixels, kept while a newer part is made."""

import numpy as np
import pytest
from conftest import TIMEOUT
from PIL import Image
from PySide6.QtCore import QPointF, Qt

from lumina.core.edit.session import Rendered
from lumina.qt_models import develop
from lumina.qt_models.develop import Params
from lumina.ui.main_window import MainWindow

WIDTH, HEIGHT = 3000, 2000
# The JPEG's own rounding, and the 8-bit tables the render goes through
TOLERANCE = 5


def gradient():
    """Red with the column, green with the row: every pixel says where it is."""
    x = np.linspace(0, 255, WIDTH).astype(np.uint8)
    y = np.linspace(0, 255, HEIGHT).astype(np.uint8)
    pixels = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    pixels[..., 0] = x[None, :]
    pixels[..., 1] = y[:, None]
    pixels[..., 2] = 90
    return pixels


@pytest.fixture(scope="module")
def pixels():
    return gradient()


@pytest.fixture
def window(qtbot, service, tmp_path, pixels):
    folder = tmp_path / "burst"
    folder.mkdir()
    for number in range(2):
        Image.fromarray(pixels).save(folder / f"{number}.jpg", quality=100, subsampling=0)
    made = MainWindow(service)
    qtbot.addWidget(made)
    made.resize(1000, 700)
    made.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        made.add_folder(folder)
    qtbot.waitUntil(lambda: made.model.rowCount() == 2, timeout=TIMEOUT)
    with qtbot.waitSignal(made.session.image_ready, timeout=TIMEOUT):
        made.show_develop(0)
    return made


def rgb(image):
    """A QImage as an array of rows of (red, green, blue)."""
    image = image.convertToFormat(image.Format.Format_RGB888)
    rows = np.frombuffer(image.constBits(), np.uint8, image.sizeInBytes())
    return rows.reshape(image.height(), image.bytesPerLine())[:, : image.width() * 3].reshape(
        image.height(), image.width(), 3
    )


def zoom_in(qtbot, window):
    with qtbot.waitSignal(window.session.region_ready, timeout=TIMEOUT) as part:
        qtbot.keyClick(window.develop.image, Qt.Key.Key_Z)
    return part.args


def test_the_develop_view_shows_the_full_size_the_edit_has(window):
    # Not the screen-sized image's size: zoom is in pixels of the photo
    assert window.develop.image.full_size() == (WIDTH, HEIGHT)
    assert window.session.full_size() == (WIDTH, HEIGHT)


def test_z_zooms_to_100_percent_and_the_photos_own_pixels_come(qtbot, window, pixels):
    window.develop.image.setFocus()
    assert not window.develop.image.has_detail()

    image, corner, whole = zoom_in(qtbot, window)

    assert window.develop.image.is_zoomed() and window.develop.image.zoom() == 1.0
    assert whole == (WIDTH, HEIGHT)
    left, top = corner
    # The view's size at least, a few pixels more, at the middle of the photo
    area = window.develop.image._area()
    assert area.width() <= image.width() <= area.width() + 3
    assert abs(left + image.width() / 2 - WIDTH / 2) <= 1
    assert abs(top + image.height() / 2 - HEIGHT / 2) <= 1
    # Every pixel of it is the file's, not a scaled one
    expected = pixels[top : top + image.height(), left : left + image.width()]
    assert np.abs(rgb(image).astype(int) - expected).max() <= TOLERANCE
    assert window.develop.image.has_detail()
    assert "100 %" in window.status.text() and "rendering" not in window.status.text()


def test_the_status_says_it_is_rendering_until_the_part_is_there(qtbot, window):
    window.develop.image.setFocus()
    qtbot.keyClick(window.develop.image, Qt.Key.Key_Z)

    assert "100 %, rendering…" in window.status.text()
    qtbot.waitUntil(lambda: window.develop.image.has_detail(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: "rendering" not in window.status.text(), timeout=TIMEOUT)


def test_panning_asks_for_the_part_that_is_now_in_view(qtbot, window, pixels):
    window.develop.image.setFocus()
    zoom_in(qtbot, window)

    with qtbot.waitSignal(window.session.region_ready, timeout=TIMEOUT) as part:
        window.develop.image.pan(-500, -300)

    image, (left, top), _whole = part.args
    assert left > 0 and top > 0
    expected = pixels[top : top + image.height(), left : left + image.width()]
    assert np.abs(rgb(image).astype(int) - expected).max() <= TOLERANCE


def test_the_old_part_stays_while_a_newer_one_is_made(qtbot, window):
    window.develop.image.setFocus()
    zoom_in(qtbot, window)

    window.develop.image.pan(-500, 0)

    assert window.develop.image.has_detail()


def test_a_moving_control_shows_the_proxy_scaled_up_not_the_old_part(qtbot, window):
    window.develop.image.setFocus()
    zoom_in(qtbot, window)
    parts = []
    window.session.region_ready.connect(lambda *args: parts.append(args))

    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT) as shown:
        window.session.preview(Params(exposure=1.0))

    assert shown.args[2] == develop.PROXY
    assert not window.develop.image.has_detail()
    # Panning while it moves asks for nothing: the part would be of a look on the way
    window.develop.image.pan(-100, 0)
    qtbot.wait(300)
    assert not parts and not window.develop.image.has_detail()

    # Let go: the screen-sized render, then the part of the new look
    with qtbot.waitSignal(window.session.region_ready, timeout=TIMEOUT) as part:
        window.session.commit()
    brightness = rgb(part.args[0]).mean()
    assert brightness > 0
    assert window.develop.image.has_detail()


def test_a_part_of_an_older_look_is_dropped(qtbot, window):
    session = window.session
    parts = []
    session.region_ready.connect(lambda *args: parts.append(args))
    rendered = Rendered(4, 4, bytes(48), None, develop.REGION, corner=(0, 0), whole=(8, 8))
    session._busy = True
    old_look = Params(exposure=-1.0).to_json()

    session._on_rendered(session._token, (old_look, develop.REGION, None), rendered)
    assert parts == []

    session._busy = True
    session._on_rendered(session._token, (session._look(), develop.REGION, None), rendered)
    assert len(parts) == 1


def test_a_part_never_takes_the_place_of_a_render_that_changes_the_look():
    assert not develop._replaces(develop.REGION, develop.SCREEN)
    assert not develop._replaces(develop.REGION, develop.PROXY)
    assert develop._replaces(develop.REGION, develop.REGION)
    assert develop._replaces(develop.SCREEN, develop.REGION)
    assert develop._replaces(develop.PROXY, develop.REGION)
    assert not develop._replaces(develop.PROXY, develop.SCREEN)
    assert develop._replaces(develop.SCREEN, develop.PROXY)


def test_the_crop_shows_the_whole_photo_and_the_zoom_comes_back(qtbot, window):
    image = window.develop.image
    image.setFocus()
    zoom_in(qtbot, window)
    image.pan(-300, -200)
    state = image.zoom_state()

    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.develop.set_cropping(True)
    assert not image.is_zoomed()
    # Zoom is off while it is on: a click or the key does nothing
    qtbot.keyClick(image, Qt.Key.Key_Z)
    assert not image.is_zoomed()

    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.develop.set_cropping(False)
    assert image.zoom_state() == state
    qtbot.waitUntil(image.has_detail, timeout=TIMEOUT)


def test_escape_goes_back_to_fitted_then_out_of_the_develop_view(qtbot, window):
    window.develop.image.setFocus()
    zoom_in(qtbot, window)

    qtbot.keyClick(window.develop.image, Qt.Key.Key_Escape)
    assert not window.develop.image.is_zoomed()
    assert window.pages.currentIndex() == 3

    qtbot.keyClick(window.develop.image, Qt.Key.Key_Escape)
    assert window.pages.currentIndex() == 1


def test_stepping_keeps_the_zoom_and_coming_in_again_starts_fitted(qtbot, window):
    window.develop.image.setFocus()
    zoom_in(qtbot, window)
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        qtbot.keyClick(window.develop.image, Qt.Key.Key_Right)
    assert window.develop.image.is_zoomed()
    qtbot.waitUntil(window.develop.image.has_detail, timeout=TIMEOUT)

    window.go_back()  # fitted
    window.go_back()  # out
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.show_develop(0)
    assert not window.develop.image.is_zoomed()


def test_the_point_zoomed_at_stays_under_the_pointer(qtbot, window):
    image = window.develop.image
    rect = image.image_rect()
    point = QPointF(rect.left() + rect.width() / 4, rect.top() + rect.height() / 3)

    image.set_zoom(1.0, point)

    zoomed = image.image_rect()
    assert (point.x() - zoomed.left()) / zoomed.width() == pytest.approx(0.25, abs=0.01)
    assert (point.y() - zoomed.top()) / zoomed.height() == pytest.approx(1 / 3, abs=0.01)
