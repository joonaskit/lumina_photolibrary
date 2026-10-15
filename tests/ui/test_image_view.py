"""The viewer's zoom: fitted and 100 %, steps, moving, the mouse and the wheel."""

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QWheelEvent

from lumina.integration import style
from lumina.ui.image_view import ImageView


@pytest.fixture
def view(qtbot):
    made = ImageView(zoomable=True)
    qtbot.addWidget(made)
    inset = style.px(ImageView.inset)
    # A 200 x 150 area inside the inset
    made.resize(200 + 2 * inset, 150 + 2 * inset)
    made.show()
    return made


def image(width, height, colour="gray"):
    made = QImage(width, height, QImage.Format.Format_RGB32)
    made.fill(QColor(colour))
    return made


def ratio(view):
    return view.devicePixelRatioF()


def test_a_photo_starts_fitted(view):
    view.set_image(image(400, 300), full=(800, 600))

    assert not view.is_zoomed()
    assert view.zoom() == pytest.approx(0.25 * ratio(view))
    assert view.image_rect().size().toTuple() == pytest.approx((200, 150))
    assert not view.needs_full()


def test_z_style_toggle_shows_100_percent_and_asks_for_the_full_size(view, qtbot):
    view.set_image(image(400, 300), full=(800, 600))

    with qtbot.waitSignal(view.zoom_changed):
        view.toggle_zoom()

    assert view.is_zoomed() and view.zoom() == 1.0
    assert view.image_rect().width() == pytest.approx(800 / ratio(view))
    assert view.needs_full()
    view.set_image(image(800, 600), full=(800, 600))
    assert not view.needs_full()
    view.toggle_zoom()
    assert not view.is_zoomed() and view.center() == (0.5, 0.5)


def test_the_point_zoomed_at_stays_under_the_pointer(view):
    view.set_image(image(800, 600))
    rect = view.image_rect()
    # A quarter in from the left, a third down
    point = QPointF(rect.left() + rect.width() / 4, rect.top() + rect.height() / 3)

    view.set_zoom(0.5 * ratio(view), point)

    zoomed = view.image_rect()
    assert (point.x() - zoomed.left()) / zoomed.width() == pytest.approx(0.25, abs=0.01)
    assert (point.y() - zoomed.top()) / zoomed.height() == pytest.approx(1 / 3, abs=0.01)


def test_moving_stops_at_the_edges(view):
    view.set_image(image(800, 600))
    view.toggle_zoom()

    view.pan(10_000, 10_000)
    rect = view.image_rect()
    inset = style.px(ImageView.inset)
    assert rect.left() == pytest.approx(inset, abs=1) and rect.top() == pytest.approx(inset, abs=1)

    view.pan(-10_000, -10_000)
    rect = view.image_rect()
    assert rect.right() == pytest.approx(view.width() - inset, abs=1)
    assert rect.bottom() == pytest.approx(view.height() - inset, abs=1)


def test_a_photo_lower_than_the_view_stays_in_the_middle(view):
    # Wider than the view at 0.3, and less tall
    view.set_image(image(800, 300))
    view.set_zoom(0.3 * ratio(view))

    view.pan(50, 50)

    assert view.center()[1] == 0.5 and view.center()[0] < 0.5


def test_steps_in_and_out_end_fitted(view):
    view.set_image(image(1600, 1200))
    fitted = view.zoom()

    view.zoom_in()
    first = view.zoom()
    assert first > fitted and first in (0.25, 0.5, 1.0)
    view.zoom_in()
    assert view.zoom() > first
    view.zoom_out()
    view.zoom_out()
    assert not view.is_zoomed()


def test_the_zoom_and_place_stay_for_the_next_photo_of_the_same_shape(view):
    view.set_image(image(800, 600))
    view.toggle_zoom()
    view.pan(-60, -40)
    place = view.center()

    # The next photo of a burst: its thumbnail first, full size unknown
    view.set_image(image(80, 60))

    assert view.zoom() == 1.0 and view.center() == place
    assert view.full_size() == (800, 600)
    assert view.needs_full()
    # Another shape: the image's own size until told
    view.set_image(image(60, 80))
    assert view.full_size() == (60, 80)


def test_a_click_zooms_and_a_drag_moves(view, qtbot):
    view.set_image(image(800, 600))
    middle = view.rect().center()

    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=middle)
    assert view.is_zoomed()

    before = view.center()
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=middle)
    qtbot.mouseMove(view, middle + QPoint(20, 10))
    qtbot.mouseMove(view, middle + QPoint(40, 20))
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=middle + QPoint(40, 20))
    # A drag moves, and does not zoom out
    assert view.is_zoomed()
    assert view.center()[0] < before[0] and view.center()[1] < before[1]

    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=middle)
    assert not view.is_zoomed()


def wheel(view, dy, modifiers=Qt.KeyboardModifier.NoModifier):
    position = QPointF(view.rect().center())
    event = QWheelEvent(
        position,
        QPointF(view.mapToGlobal(position.toPoint())),
        QPoint(),
        QPoint(0, dy),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    view.wheelEvent(event)
    return event


def test_ctrl_and_the_wheel_zoom_and_the_wheel_moves(view):
    view.set_image(image(1600, 1200))

    assert not wheel(view, -120).isAccepted()
    wheel(view, 120, Qt.KeyboardModifier.ControlModifier)
    assert view.is_zoomed()
    before = view.center()
    wheel(view, -120)
    assert view.center()[1] > before[1]


def test_a_view_that_does_not_zoom_ignores_clicks(qtbot):
    plain = ImageView()
    qtbot.addWidget(plain)
    plain.resize(200, 150)
    plain.show()
    plain.set_image(image(800, 600))

    qtbot.mouseClick(plain, Qt.MouseButton.LeftButton, pos=plain.rect().center())
    plain.toggle_zoom()

    assert not plain.is_zoomed()


def test_100_percent_is_drawn_pixel_for_pixel(view):
    photo = image(800, 600)
    # A one-pixel checkerboard: any resampling turns it grey
    for y in range(600):
        for x in range(0, 800, 2):
            photo.setPixelColor(x + y % 2, y, QColor("white"))
            photo.setPixelColor(x + 1 - y % 2, y, QColor("black"))
    view.set_image(photo, full=(800, 600))
    view.set_zoom(1.0)

    shot = view.grab().toImage()
    middle = shot.rect().center()
    colours = {shot.pixelColor(middle.x() + dx, middle.y()).lightness() for dx in range(6)}

    assert colours <= {0, 255} and len(colours) == 2
