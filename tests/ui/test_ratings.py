"""Stars in the grid, the viewer and the Develop view: keys, the filter, undo with the
flags, the badge, auto-advance."""

import os

from conftest import TIMEOUT
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QMenu, QStyleOptionViewItem
from test_flags import choose, names, open_window, press

from lumina.core import db as dbm
from lumina.core.dbtypes import PICKED, REJECTED, UNFLAGGED
from lumina.qt_models.library_model import NOT_RATED
from lumina.ui import settings

KEYS = {
    1: Qt.Key.Key_1,
    2: Qt.Key.Key_2,
    3: Qt.Key.Key_3,
    4: Qt.Key.Key_4,
    5: Qt.Key.Key_5,
    0: Qt.Key.Key_0,
}


def stored(service):
    """{name: rating} as the library has them."""
    service.settle()
    with dbm.Library(service.path) as library:
        return {os.path.basename(photo.path): photo.rating for photo in library.photos()}


def shown(window):
    return {
        window.model.name(row): window.model.rating(row) for row in range(window.model.rowCount())
    }


def test_the_number_keys_rate_the_chosen_photo_and_the_same_key_clears_it(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first = window.model.name(0)
    choose(window, 0)

    press(qtbot, window.grid, KEYS[3])
    assert window.model.rating(0) == 3 and stored(service)[first] == 3
    assert window.rate_actions[3].isChecked() and not window.rate_actions[2].isChecked()

    press(qtbot, window.grid, KEYS[5])
    assert window.model.rating(0) == 5

    press(qtbot, window.grid, KEYS[5])
    assert window.model.rating(0) == 0 and stored(service)[first] == 0

    press(qtbot, window.grid, KEYS[2])
    press(qtbot, window.grid, KEYS[0])
    assert window.model.rating(0) == 0


def test_a_rating_is_apart_from_the_flag(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)

    press(qtbot, window.grid, Qt.Key.Key_P)
    press(qtbot, window.grid, KEYS[4])
    press(qtbot, window.grid, Qt.Key.Key_U)

    assert window.model.flag(0) == UNFLAGGED and window.model.rating(0) == 4
    press(qtbot, window.grid, KEYS[0])
    window.pick_action.trigger()
    assert window.model.flag(0) == PICKED and window.model.rating(0) == 0


def test_every_chosen_photo_gets_the_stars(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0, 2)
    window.rate_actions[2].trigger()
    assert [window.model.rating(row) for row in range(3)] == [2, 0, 2]

    # Some have it, some not: all get it
    choose(window, 0, 1)
    window.rate_actions[2].trigger()
    assert [window.model.rating(row) for row in range(3)] == [2, 2, 2]
    # All have it: cleared
    window.rate_actions[2].trigger()
    assert [window.model.rating(row) for row in range(3)] == [0, 0, 2]


def test_the_rating_filter_shows_those_with_that_many_stars_or_more(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, third = names(window)
    for row, stars in enumerate((5, 2, 0)):
        choose(window, row)
        window.rate_actions[stars].trigger()
    window.model.settle(None)

    window.filters.rating.setCurrentIndex(window.filters.rating.findData(2))
    assert names(window) == [first, second]
    window.filters.rating.setCurrentIndex(window.filters.rating.findData(5))
    assert names(window) == [first]
    window.filters.rating.setCurrentIndex(window.filters.rating.findData(NOT_RATED))
    assert names(window) == [third]
    window.filters.rating.setCurrentIndex(0)
    assert names(window) == [first, second, third]


def test_a_photo_rated_out_of_the_view_stays_until_the_user_moves_on(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    window.filters.rating.setCurrentIndex(window.filters.rating.findData(NOT_RATED))
    assert window.model.rowCount() == 3
    middle = window.model.name(1)
    choose(window, 1)

    press(qtbot, window.grid, KEYS[4])

    assert middle in names(window)
    press(qtbot, window.grid, Qt.Key.Key_Right)
    qtbot.waitUntil(lambda: middle not in names(window), timeout=TIMEOUT)
    assert window.model.rowCount() == 2


def test_the_filter_s_count_is_not_the_flags(qtbot, service, drive):
    settings.set_rejected_photos("hide")
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)
    press(qtbot, window.grid, KEYS[1])
    window.model.settle(None)

    window.filters.rating.setCurrentIndex(window.filters.rating.findData(1))

    # Narrowed by the rating, which no flag hid
    assert window.model.rowCount() == 1
    assert window.model.hidden_count() == 0


def test_flag_and_rating_changes_are_undone_in_one_order(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first = window.model.name(0)
    choose(window, 0)
    press(qtbot, window.grid, Qt.Key.Key_X)
    press(qtbot, window.grid, KEYS[4])

    press(qtbot, window.grid, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert window.model.rating(0) == 0 and window.model.flag(0) == REJECTED
    assert "Rating" in window.statusBar().currentMessage()

    press(qtbot, window.grid, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert window.model.flag(0) == UNFLAGGED

    window.redo_flag_action.trigger()
    window.redo_flag_action.trigger()
    assert window.model.flag(0) == REJECTED and window.model.rating(0) == 4
    assert stored(service)[first] == 4


def test_ratings_are_there_when_the_library_is_read_again(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first = window.model.name(0)
    choose(window, 0)
    window.rate_actions[5].trigger()

    window.model.refresh()
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert shown(window)[first] == 5


def test_the_number_keys_do_nothing_while_typing_a_search(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)

    press(qtbot, window.search_field.edit, KEYS[3])

    assert window.model.rating(0) == 0


def test_the_viewer_rates_the_photo_shown_and_says_so(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, _third = names(window)
    window.nav.show_photo(0)

    press(qtbot, window.viewer, KEYS[3])
    assert window.title.text() == first
    assert "★★★" in window.status.text()

    # Clearing does not step on, and nothing steps on unless asked
    settings.set_advance_after_flag(True)
    press(qtbot, window.viewer, KEYS[3])
    assert window.title.text() == first and "★" not in window.status.text()
    press(qtbot, window.viewer, KEYS[2])
    assert window.title.text() == second
    assert shown(window)[first] == 2


def test_the_develop_view_rates_the_photo_being_edited_and_never_steps_on(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, _third = names(window)
    settings.set_advance_after_flag(True)
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.nav.show_develop(0)

    press(qtbot, window.develop, KEYS[4])
    press(qtbot, window.develop, Qt.Key.Key_P)

    assert window.title.text() == first
    assert window.model.rating(0) == 4 and window.model.flag(0) == PICKED
    assert "★★★★" in window.status.text() and "Thumbs up" in window.status.text()
    assert window.marking.isVisible()
    assert second not in window.title.text()


def test_in_the_develop_view_undo_is_the_edits_not_the_flags(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.nav.show_develop(0)
    press(qtbot, window.develop, Qt.Key.Key_X)
    assert not window.undo_flag_action.isEnabled()

    press(qtbot, window.develop, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

    assert window.model.flag(0) == REJECTED
    # Back in the grid the change can be undone
    window.nav.go_back()
    assert window.undo_flag_action.isEnabled()
    window.undo_flag_action.trigger()
    assert window.model.flag(0) == UNFLAGGED


def test_the_context_menu_has_the_stars(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    window.grid.setFocus()
    choose(window, 0)
    seen = []

    def look():
        menu = next(w for w in window.findChildren(QMenu) if w.isVisible() and w.title() == "")
        rating = next(a for a in menu.actions() if a.text() == "Rating").menu()
        seen.extend(a.text() for a in rating.actions() if a.text())
        menu.close()

    QTimer.singleShot(200, look)
    window._grid_menu(window.grid.visualRect(window.model.index(0)).center())

    assert seen == ["1 Star", "2 Stars", "3 Stars", "4 Stars", "5 Stars", "No Stars"]


def test_a_rated_cell_has_stars(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    for row in range(3):
        window.model.data(window.model.index(row), Qt.ItemDataRole.DecorationRole)
    qtbot.waitUntil(
        lambda: all(window.model.thumbnail_pixmap(row) is not None for row in range(3)),
        timeout=TIMEOUT,
    )
    window.grid.clearSelection()

    def cell(row):
        rect = window.grid.visualRect(window.model.index(row))
        image = QImage(rect.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        painter.translate(-rect.topLeft())
        option = QStyleOptionViewItem()
        window.grid.initViewItemOption(option)
        option.rect = rect
        window.grid.itemDelegate().paint(painter, option, window.model.index(row))
        painter.end()
        return image

    plain = cell(0)
    choose(window, 0)
    window.rate_actions[1].trigger()
    window.grid.clearSelection()
    one = cell(0)
    choose(window, 0)
    window.rate_actions[5].trigger()
    window.grid.clearSelection()
    five = cell(0)

    assert one != plain and five != one
    # Unrated cells are as they were
    choose(window, 0)
    window.rate_actions[5].trigger()
    window.grid.clearSelection()
    assert cell(0) == plain


def star_point(window, number):
    bar = window.rating_bar
    return QPoint(
        round(2 * bar.devicePixelRatioF() + (number - 0.5) * bar._cell()), bar.height() // 2
    )


def test_the_toolbar_stars_rate_and_the_same_star_again_clears(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)
    assert window.marking.isVisible() and window.rating_bar.isEnabled()

    qtbot.mouseClick(window.rating_bar, Qt.MouseButton.LeftButton, pos=star_point(window, 4))
    assert window.model.rating(0) == 4 and window.rating_bar.rating() == 4

    qtbot.mouseClick(window.rating_bar, Qt.MouseButton.LeftButton, pos=star_point(window, 2))
    assert window.model.rating(0) == 2 and window.rating_bar.rating() == 2

    qtbot.mouseClick(window.rating_bar, Qt.MouseButton.LeftButton, pos=star_point(window, 2))
    assert window.model.rating(0) == 0 and window.rating_bar.rating() == 0


def test_the_toolbar_stars_follow_the_photo_chosen_and_are_off_without_one(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)
    window.rate_actions[3].trigger()

    choose(window, 1)
    assert window.rating_bar.rating() == 0
    choose(window, 0)
    assert window.rating_bar.rating() == 3

    # Photos that differ show none, and a click gives them all the stars
    choose(window, 0, 1)
    assert window.rating_bar.rating() == 0
    qtbot.mouseClick(window.rating_bar, Qt.MouseButton.LeftButton, pos=star_point(window, 5))
    assert [window.model.rating(row) for row in range(2)] == [5, 5]

    window.grid.clearSelection()
    window.grid.setCurrentIndex(window.model.index(-1))
    qtbot.waitUntil(lambda: not window.rating_bar.isEnabled(), timeout=TIMEOUT)


def test_the_toolbar_stars_are_there_in_the_viewer_and_the_develop_view(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    window.nav.show_photo(0)
    qtbot.mouseClick(window.rating_bar, Qt.MouseButton.LeftButton, pos=star_point(window, 3))
    assert window.model.rating(0) == 3

    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.nav.show_develop(0)
    assert window.marking.isVisible() and window.rating_bar.rating() == 3
    qtbot.mouseClick(window.rating_bar, Qt.MouseButton.LeftButton, pos=star_point(window, 5))
    assert window.model.rating(0) == 5
    assert window.title.text() == window.model.name(0)


def test_hovering_a_star_shows_what_a_click_gives(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)
    before = window.rating_bar.grab().toImage()

    qtbot.mouseMove(window.rating_bar, star_point(window, 3))

    assert window.rating_bar.grab().toImage() != before
    assert window.rating_bar.rating() == 0
