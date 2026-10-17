"""Stacks of photos in the grid: one cell while collapsed, the filters look at the top,
the viewer skips what is hidden."""

import os

from conftest import TIMEOUT
from PySide6.QtWidgets import QStyleOptionViewItem
from test_flags import choose, names, open_window

from lumina.qt_models.library_model import PICKED, STACK_ROLE
from lumina.ui import badges
from lumina.ui.main_window import _VIEWER


def wait_for_rows(qtbot, window, count):
    qtbot.waitUntil(lambda: window.model.rowCount() == count, timeout=TIMEOUT)


def stack_all(qtbot, window, rows=(0, 1, 2)):
    choose(window, *rows)
    window.stack_photos()


def test_a_collapsed_stack_is_one_cell_with_its_top_and_its_size(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first = names(window)[0]
    choose(window, 0, 1)

    window.stack_photos()
    wait_for_rows(qtbot, window, 2)

    assert names(window)[0] == first
    assert window.model.data(window.model.index(0), STACK_ROLE) == (1, 2, True)
    assert window.model.data(window.model.index(1), STACK_ROLE) is None
    assert window.status.text().startswith("2 photos")


def test_expanding_shows_the_members_after_the_top_and_collapsing_hides_them(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)

    window.toggle_stacks()
    wait_for_rows(qtbot, window, 3)

    assert names(window) == before
    assert [window.model.data(window.model.index(r), STACK_ROLE)[0] for r in range(3)] == [1, 2, 3]
    window.toggle_stacks()
    wait_for_rows(qtbot, window, 1)


def test_a_new_top_is_the_cell_a_collapsed_stack_shows(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)
    window.toggle_stacks()
    wait_for_rows(qtbot, window, 3)
    choose(window, 2)
    assert window.stack_top_action.isEnabled()

    window.set_stack_top()
    qtbot.waitUntil(lambda: names(window)[0] == before[2], timeout=TIMEOUT)
    choose(window, 0)
    window.toggle_stacks()

    wait_for_rows(qtbot, window, 1)
    assert names(window) == [before[2]]


def test_unstacking_gives_every_photo_its_own_cell_again(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)

    window.unstack_photos()

    wait_for_rows(qtbot, window, 3)
    assert names(window) == before
    assert window.model.data(window.model.index(0), STACK_ROLE) is None


def test_a_stack_is_shown_only_while_its_top_passes_the_filters(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window, (0, 1))
    wait_for_rows(qtbot, window, 2)
    # The second photo, not the top, is picked
    choose(window, 0)
    window.toggle_stacks()
    wait_for_rows(qtbot, window, 3)
    choose(window, 1)
    window.flag_photos(PICKED)
    qtbot.waitUntil(lambda: window.model.flag(1) == PICKED, timeout=TIMEOUT)

    window.set_flag_filter(PICKED)

    # Picked photos only: the stack's top is not one, so the stack is not shown
    assert names(window) == []
    window.set_flag_filter(None)
    assert names(window) == before


def test_an_expanded_stack_shows_only_the_members_that_pass(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window, (0, 1, 2))
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)
    window.toggle_stacks()
    wait_for_rows(qtbot, window, 3)
    choose(window, 0)
    window.flag_photos(PICKED)
    choose(window, 2)
    window.flag_photos(PICKED)
    qtbot.waitUntil(
        lambda: window.model.flag(0) == PICKED and window.model.flag(2) == PICKED,
        timeout=TIMEOUT,
    )

    window.set_flag_filter(PICKED)

    assert names(window) == [before[0], before[2]]


def test_stepping_in_the_viewer_skips_the_photos_a_collapsed_stack_hides(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window, (0, 1))
    wait_for_rows(qtbot, window, 2)

    window.show_photo(0)
    assert window.pages.currentIndex() == _VIEWER
    window.step(1)

    assert os.path.basename(window._viewed_path()) == before[2]
    assert "2 of 2" in window.status.text()


def test_a_stack_made_in_the_grid_survives_a_new_window(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    window.close()

    again = open_window_again(qtbot, service)

    assert again.model.rowCount() == 1


def open_window_again(qtbot, service):
    from lumina.ui.main_window import MainWindow

    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(
        lambda: not window.model.is_loading() and window.model.rowCount(), timeout=TIMEOUT
    )
    return window


def test_stacking_needs_two_photos_chosen(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)
    assert not window.stack_action.isEnabled()
    choose(window, 0, 1)
    assert window.stack_action.isEnabled()
    assert not window.unstack_action.isEnabled()


def test_a_photo_is_taken_out_of_an_expanded_stack(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)
    window.toggle_stacks()
    wait_for_rows(qtbot, window, 3)
    photo = window.model.photo_id(1)
    choose(window, 1)
    assert window.leave_stack_action.isEnabled()

    window.leave_stack()

    def left():
        row = window.model.row_of(photo)
        return window.model.data(window.model.index(row), STACK_ROLE) is None

    qtbot.waitUntil(left, timeout=TIMEOUT)
    # The two left are still a stack, shown together after the top
    others = [r for r in range(3) if window.model.data(window.model.index(r), STACK_ROLE)]
    assert [window.model.name(r) for r in others] == [before[0], before[2]]
    assert window.model.rowCount() == 3


def test_a_click_on_the_badge_opens_and_closes_a_stack(qtbot, service, drive):
    from PySide6.QtCore import QPoint, Qt

    window = open_window(qtbot, service, drive / "Photos")
    window.resize(900, 600)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    qtbot.waitUntil(
        lambda: window.grid.visualRect(window.model.index(0)).isValid(), timeout=TIMEOUT
    )
    index = window.model.index(0)

    def badge_point():
        option = QStyleOptionViewItem()
        window.grid.initViewItemOption(option)
        option.rect = window.grid.visualRect(index)
        option.widget = window.grid
        photo = window._delegate.photo_rect(option, index)
        card = badges.pill_rect(photo, badges.stack_text(index.data(STACK_ROLE)))
        return card.center().toPoint()

    qtbot.mouseClick(window.grid.viewport(), Qt.MouseButton.LeftButton, pos=badge_point())
    wait_for_rows(qtbot, window, 3)
    index = window.model.index(0)
    qtbot.mouseClick(window.grid.viewport(), Qt.MouseButton.LeftButton, pos=badge_point())
    wait_for_rows(qtbot, window, 1)
    # Elsewhere on the cell chooses it and changes nothing
    far = window.grid.visualRect(index).bottomRight() - QPoint(2, 2)
    qtbot.mouseClick(window.grid.viewport(), Qt.MouseButton.LeftButton, pos=far)
    assert window.model.rowCount() == 1
