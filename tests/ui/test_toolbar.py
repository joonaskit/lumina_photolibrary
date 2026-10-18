"""The window's rows: the toolbar (where you are, thumbs and stars in the middle, search
and the settings at the right), the filters under it, and how the grid shows the photos
in the status bar."""

from conftest import TIMEOUT
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QToolButton

from lumina.integration import style, theme
from lumina.qt_models.library_model import PICKED
from lumina.ui.main_window import MainWindow


def open_window(qtbot, service, folder):
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.browser.add_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    return window


def left(widget):
    return widget.mapTo(widget.window(), QPoint(0, 0)).x()


def centre(widget):
    return left(widget) + widget.width() / 2


def button_of(parent, action):
    return next(b for b in parent.findChildren(QToolButton) if b.defaultAction() is action)


def test_thumbs_and_stars_are_in_the_middle_of_the_toolbar(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")

    up = button_of(window.marking, window.pick_action)
    down = button_of(window.marking, window.reject_action)
    assert left(up) < left(window.rating_bar) < left(down)
    assert abs(centre(window.marking) - window.width() / 2) <= 2

    # Still in the middle above a photo, with the navigation at the left
    window.nav.show_photo(0)
    assert window.navigation.isVisible()
    assert abs(centre(window.marking) - window.width() / 2) <= 2


def test_settings_are_at_the_right_and_there_is_no_edit_button(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")

    settings = button_of(window, window.preferences_action)
    assert left(settings) > centre(window.marking)
    assert not [
        b for b in window.findChildren(QToolButton) if b.defaultAction() is window.edit_action
    ]


def test_the_edit_is_in_the_photos_context_menus(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")

    in_grid = window._photo_menu(in_grid=True).actions()
    assert window.edit_action in in_grid and window.sync_settings_action in in_grid
    in_viewer = window._photo_menu(in_grid=False).actions()
    assert window.edit_action in in_viewer and window.sync_settings_action not in in_viewer


def test_how_the_grid_shows_photos_is_in_the_status_bar_while_it_shows(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")

    assert window.grid_view_options.isVisible()
    assert window.grid_size.isVisible() and window.flag_options.isVisible()
    assert window.statusBar().isAncestorOf(window.grid_size)
    assert not window.filters.isAncestorOf(window.grid_size)

    window.nav.show_photo(0)
    assert not window.grid_view_options.isVisible()
    window.nav.show_grid()
    assert window.grid_view_options.isVisible()


def test_clear_shows_every_photo_again(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    filters = window.filters
    assert not filters.clear_button.isEnabled()

    window.grid.setCurrentIndex(window.model.index(0))
    window.culling.flag_photos(PICKED)
    filters.flag.setCurrentIndex(filters.flag.findData(PICKED))
    filters.year.setCurrentIndex(1)
    assert window.model.rowCount() == 1
    assert filters.clear_button.isEnabled()

    with qtbot.waitSignal(filters.flag_changed) as flag:
        filters.clear_button.click()
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert flag.args == [None]
    assert filters.values() == (None, None, None) and filters.flag_value() is None
    assert window.model.rowCount() == 3
    assert not filters.clear_button.isEnabled()


def test_every_control_in_the_toolbar_and_the_filters_is_as_tall(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    window.search_field.show()
    filters = window.filters
    settings = button_of(window, window.preferences_action).parentWidget()

    heights = {
        widget.objectName() or type(widget).__name__: widget.height()
        for widget in (
            window.marking,
            settings,
            window.search_field.edit,
            filters.year,
            filters.flag,
            filters.edit,
            filters.clear_button,
        )
    }
    assert set(heights.values()) == {style.row_height()}, heights
    # The buttons inside a card fit in it
    up = button_of(window.marking, window.pick_action)
    assert up.height() == window.rating_bar.height() == style.row_inner_height()


def corner_shows_behind(widget):
    """Whether `widget`'s top left corner shows what is behind it, not its own
    background: it is drawn round. From its parent: a grab of the widget alone fills its
    corners with its own colour."""
    parent = widget.parentWidget()
    image = parent.grab().toImage()
    place = widget.pos()
    corner = image.pixelColor(place.x() + 1, place.y() + 1)
    middle = image.pixelColor(place.x() + widget.width() // 2, place.y() + 2)
    return corner != middle


def test_the_rows_stay_rounded_with_the_shell_at_its_roundest(qtbot, qapp, service, drive):
    style.apply(qapp, theme.merge(theme.DEFAULTS, {"radius": {"large": 60}}))
    window = open_window(qtbot, service, drive / "Photos")
    window.search_field.show()

    assert corner_shows_behind(window.filters.clear_button)
    # The search field and the filters as round as the cards: 3 pixels in from the
    # corner is still what is behind them, where a dialog's smaller radius has a border
    image = window.grab().toImage()
    for widget in (window.search_field.edit, window.filters.year, window.filters.edit):
        place = widget.mapTo(window, QPoint(0, 0))
        behind = image.pixelColor(place.x() - 2, place.y() + widget.height() // 2)
        assert image.pixelColor(place.x() + 3, place.y() + 3) == behind, widget
