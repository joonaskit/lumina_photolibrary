"""Thumbs up and down in the grid and the viewer: keys, buttons, dimming and hiding,
the flag filter, auto-advance and undo."""

import os

from conftest import TIMEOUT
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QStyleOptionViewItem

from lumina.core import db as dbm
from lumina.core.dbtypes import PICKED, REJECTED, UNFLAGGED
from lumina.qt_models.library_model import DIM, DIMMED_ROLE, HIDE, SHOW
from lumina.ui import settings
from lumina.ui.main_window import MainWindow


def open_window(qtbot, service, folder):
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.browser.add_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    assert window.model.rowCount() == 3
    return window


def names(window):
    return [window.model.name(row) for row in range(window.model.rowCount())]


def choose(window, *rows):
    selection = window.grid.selectionModel()
    selection.clearSelection()
    for row in rows:
        selection.select(window.model.index(row), QItemSelectionModel.SelectionFlag.Select)
    selection.setCurrentIndex(
        window.model.index(rows[-1]), QItemSelectionModel.SelectionFlag.NoUpdate
    )


def press(qtbot, widget, key, modifier=Qt.KeyboardModifier.NoModifier):
    widget.setFocus()
    qtbot.keyClick(widget, key, modifier)


def stored(service):
    """{name: flag} as the library has them."""
    service.settle()
    with dbm.Library(service.path) as library:
        return {os.path.basename(photo.path): photo.flag for photo in library.photos()}


def shown_flags(window):
    return {
        window.model.name(row): window.model.flag(row) for row in range(window.model.rowCount())
    }


def test_p_and_x_flag_the_chosen_photo_and_again_clears_it(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first = window.model.name(0)
    choose(window, 0)

    press(qtbot, window.grid, Qt.Key.Key_P)
    assert window.model.flag(0) == PICKED
    assert stored(service)[first] == PICKED
    assert window.pick_action.isChecked() and not window.reject_action.isChecked()

    press(qtbot, window.grid, Qt.Key.Key_X)
    assert window.model.flag(0) == REJECTED
    assert window.reject_action.isChecked() and not window.pick_action.isChecked()

    press(qtbot, window.grid, Qt.Key.Key_X)
    assert window.model.flag(0) == UNFLAGGED
    assert stored(service)[first] == UNFLAGGED

    press(qtbot, window.grid, Qt.Key.Key_P)
    press(qtbot, window.grid, Qt.Key.Key_U)
    assert window.model.flag(0) == UNFLAGGED


def test_the_toolbar_buttons_flag_every_chosen_photo(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0, 2)

    window.reject_action.trigger()

    assert [window.model.flag(row) for row in range(3)] == [REJECTED, UNFLAGGED, REJECTED]
    assert window.reject_action.isChecked()
    # Mixed: neither button shows as set
    choose(window, 0, 1)
    assert not window.reject_action.isChecked() and not window.pick_action.isChecked()
    # Some have it, some not: all get it
    window.reject_action.trigger()
    assert [window.model.flag(row) for row in range(3)] == [REJECTED] * 3


def test_thumbs_down_photos_are_dimmed_shown_or_hidden(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    rejected = window.model.name(1)
    choose(window, 1)
    window.reject_action.trigger()

    def dimmed(row):
        return window.model.data(window.model.index(row), DIMMED_ROLE)

    assert window.flag_options.rejected() == DIM
    assert [dimmed(row) for row in range(3)] == [False, True, False]
    window.flag_options.rejected_actions[SHOW].trigger()
    assert not dimmed(1)
    assert settings.rejected_photos() == SHOW

    window.flag_options.rejected_actions[HIDE].trigger()
    assert rejected not in names(window)
    assert window.status.text() == "2 photos  ·  1 hidden by flags"
    assert settings.rejected_photos() == HIDE


def test_a_photo_flagged_out_of_the_view_stays_until_the_user_moves_on(qtbot, service, drive):
    settings.set_rejected_photos(HIDE)
    window = open_window(qtbot, service, drive / "Photos")
    middle = window.model.name(1)
    choose(window, 1)

    press(qtbot, window.grid, Qt.Key.Key_X)
    # Still there, greyed out, and still chosen
    assert middle in names(window)
    assert window.model.data(window.model.index(1), DIMMED_ROLE)

    press(qtbot, window.grid, Qt.Key.Key_Right)
    qtbot.waitUntil(lambda: middle not in names(window), timeout=TIMEOUT)
    assert window.model.rowCount() == 2
    # The photo moved to is still the current one
    assert window.grid.currentIndex().row() == 1


def test_the_flag_filter_shows_only_one_flag(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, third = names(window)
    choose(window, 0)
    window.pick_action.trigger()
    choose(window, 1)
    window.reject_action.trigger()
    combo = window.filters.flag

    combo.setCurrentIndex(combo.findData(PICKED))
    assert names(window) == [first]
    combo.setCurrentIndex(combo.findData(REJECTED))
    assert names(window) == [second]
    # Thumbs-down photos asked for are not greyed out
    assert not window.model.data(window.model.index(0), DIMMED_ROLE)
    combo.setCurrentIndex(combo.findData(UNFLAGGED))
    assert names(window) == [third]
    assert window.status.text() == "1 photo  ·  2 hidden by flags"

    # Going through the photos not flagged yet: one flagged stays until moved past
    choose(window, 0)
    window.pick_action.trigger()
    assert names(window) == [third]
    window.model.settle(None)
    assert names(window) == []

    combo.setCurrentIndex(combo.findData(None))
    assert names(window) == [first, second, third]


def test_in_the_viewer_flagging_steps_on_only_when_asked(qtbot, service, drive):
    settings.set_rejected_photos(HIDE)
    window = open_window(qtbot, service, drive / "Photos")
    first, second, third = names(window)
    window.nav.show_photo(0)

    press(qtbot, window.viewer, Qt.Key.Key_X)
    assert window.title.text() == first
    assert "Thumbs down" in window.status.text()

    settings.set_advance_after_flag(True)
    press(qtbot, window.viewer, Qt.Key.Key_P)
    # The first photo's thumbs down became a thumbs up, and the viewer moved on
    assert window.title.text() == second
    press(qtbot, window.viewer, Qt.Key.Key_X)
    assert window.title.text() == third
    assert window.status.text().startswith("2 of 2")

    # Back: the thumbs-down photo is hidden and skipped
    press(qtbot, window.viewer, Qt.Key.Key_Left)
    assert window.title.text() == first
    assert names(window) == [first, third]


def test_flag_changes_are_undone_and_redone(qtbot, service, drive):
    settings.set_rejected_photos(HIDE)
    window = open_window(qtbot, service, drive / "Photos")
    first = window.model.name(0)
    choose(window, 0)
    press(qtbot, window.grid, Qt.Key.Key_X)
    window.model.settle(None)
    assert first not in names(window)

    press(qtbot, window.grid, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

    assert first in names(window)
    assert stored(service)[first] == UNFLAGGED
    assert window.model.can_redo_flags()
    press(qtbot, window.grid, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert not window.model.can_undo_flags()

    window.redo_flag_action.trigger()
    assert stored(service)[first] == REJECTED


def test_flags_are_there_when_the_library_is_read_again(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first = window.model.name(0)
    choose(window, 0)
    window.pick_action.trigger()

    window.model.refresh()
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert shown_flags(window)[first] == PICKED
    assert stored(service)[first] == PICKED


def test_flags_keys_do_nothing_while_typing_a_search(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)

    press(qtbot, window.search_field.edit, Qt.Key.Key_P)

    assert window.model.flag(0) == UNFLAGGED


def test_a_flagged_cell_has_a_badge_and_a_dimmed_one_is_paler(qtbot, service, drive):
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
    window.pick_action.trigger()
    window.grid.clearSelection()
    assert cell(0) != plain
    choose(window, 0)
    window.reject_action.trigger()
    window.grid.clearSelection()
    dimmed = cell(0)

    def alpha_sum(image):
        return sum(
            image.pixelColor(x, y).alpha()
            for x in range(0, image.width(), 4)
            for y in range(0, image.height(), 4)
        )

    assert alpha_sum(dimmed) < alpha_sum(plain)


def test_the_develop_view_flags_the_photo_being_edited(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, _third = names(window)
    settings.set_advance_after_flag(True)
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.nav.show_develop(0)
    assert window.marking.isVisible()

    press(qtbot, window.develop, Qt.Key.Key_P)
    assert window.model.flag(0) == PICKED and window.pick_action.isChecked()
    assert "Thumbs up" in window.status.text()
    press(qtbot, window.develop, Qt.Key.Key_X)
    assert window.model.flag(0) == REJECTED and window.reject_action.isChecked()
    press(qtbot, window.develop, Qt.Key.Key_X)
    assert window.model.flag(0) == UNFLAGGED
    press(qtbot, window.develop, Qt.Key.Key_P)
    press(qtbot, window.develop, Qt.Key.Key_U)
    assert window.model.flag(0) == UNFLAGGED
    window.reject_action.trigger()
    assert window.model.flag(0) == REJECTED

    # Still on the same photo, however the option is set: stepping decodes the next one
    assert window.title.text() == first and second not in window.title.text()
    assert stored(service)[first] == REJECTED


def test_the_develop_views_own_keys_still_work(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.nav.show_develop(0)

    press(qtbot, window.develop, Qt.Key.Key_C)

    assert window.develop.is_cropping()
    assert window.model.flag(0) == UNFLAGGED
