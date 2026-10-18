"""Photos a scan adds are inserted into the grid as rows, not by starting over: the
selection stays, and so does the place in the view, down to the pixel."""

import pytest
from conftest import TIMEOUT
from photos import make_jpeg
from PySide6.QtCore import QItemSelectionModel

from lumina.qt_models import library_model
from lumina.ui.main_window import MainWindow, _inset


def steps(old, new):
    return library_model._steps(old, new)


def test_rows_removed_and_added_with_the_others_in_order_are_steps():
    assert steps([1, 2, 3], [1, 2, 3]) == ([], [])
    assert steps([1, 2, 3, 4], [1, 4]) == ([(1, 2)], [])
    assert steps([1, 4], [1, 2, 3, 4, 5]) == ([], [(1, 2), (4, 4)])
    assert steps([1, 2, 3], [0, 1, 3, 4]) == ([(1, 1)], [(0, 0), (3, 3)])
    assert steps([], [1, 2]) == ([], [(0, 1)])
    assert steps([1, 2], []) == ([(0, 1)], [])


def test_a_reordering_is_not_steps():
    assert steps([1, 2, 3], [3, 2, 1]) is None
    assert steps([1, 2, 3], [2, 1, 3, 4]) is None


def test_too_many_insertions_are_left_to_starting_over():
    old = list(range(0, 400, 2))
    new = list(range(400))

    assert steps(old, new) is None
    assert steps(old[:20], new[:40]) is not None


def photos_window(qtbot, service, drive, count):
    folder = drive / "Photos"
    for n in range(count):
        # Dated by their names, which are the order: the file's time is when they were made
        make_jpeg(folder / f"p{n:04}.jpg", size=(60, 40), colour=(n % 250, 9, 9))
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.resize(1100, 700)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.browser.add_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    return window


def names(window):
    return [window.model.name(row) for row in range(window.model.rowCount())]


def refreshed(qtbot, window, service):
    """Let the grid read the rows again, and wait for it."""
    with qtbot.waitSignal(window.model.loading_changed, timeout=TIMEOUT):
        service.photos_changed.emit()
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)


def test_photos_added_are_inserted_as_rows_and_the_selection_stays(qtbot, service, drive):
    window = photos_window(qtbot, service, drive, 12)
    model = window.model
    resets, inserted = [], []
    model.modelReset.connect(lambda: resets.append(1))
    model.rowsInserted.connect(lambda _parent, first, last: inserted.append((first, last)))
    count = model.rowCount()
    chosen = [names(window)[3], names(window)[8]]
    selection = window.grid.selectionModel()
    for name in chosen:
        selection.select(
            model.index(names(window).index(name)), QItemSelectionModel.SelectionFlag.Select
        )

    # Dated in the middle of the others: the file time is the order for photos without one
    new = make_jpeg(drive / "Photos" / "new.jpg", size=(60, 40), colour=(1, 2, 3))
    import os

    middle = os.stat(drive / "Photos" / names(window)[5]).st_mtime
    os.utime(new, (middle, middle))
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.rescan()
    qtbot.waitUntil(lambda: model.rowCount() == count + 1, timeout=TIMEOUT)

    assert resets == [] and len(inserted) == 1
    assert "new.jpg" in names(window)
    # The same photos are still chosen, in their new places
    still = sorted(model.name(index.row()) for index in selection.selectedRows())
    assert still == sorted(chosen)


def test_the_place_in_the_view_is_kept_to_the_pixel_when_rows_come_in_above(
    qtbot, service, drive, tmp_path
):
    import os

    window = photos_window(qtbot, service, drive, 400)
    grid, model = window.grid, window.model
    count = model.rowCount()
    bar = grid.verticalScrollBar()
    # Between two rows, as a user leaves a scrolled grid
    # (a whole number of rows down, so that the top of the view is in a cell, not a gap)
    row_height = grid.gridSize().height()
    bar.setValue(bar.maximum() // 2 // row_height * row_height + 17)
    qtbot.wait(100)
    window.grid_page.save_place()
    top_id = window.grid_page.place[0]
    before = grid.visualRect(model.index(model.row_of(top_id))).top()
    assert window.grid_page.place[2] != 0 or before != 0

    # Photos dated before all the others
    oldest = min(os.stat(path).st_mtime for path in (drive / "Photos").iterdir())
    for number in range(7):
        path = make_jpeg(drive / "Photos" / f"early{number}.jpg", size=(60, 40))
        os.utime(path, (oldest - 1000 - number, oldest - 1000 - number))
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.rescan()
    qtbot.waitUntil(lambda: model.rowCount() == count + 7, timeout=TIMEOUT)
    qtbot.wait(200)

    # Inserted above, so the photos moved on by a few cells (and may be in another column)
    # but the one that was at the top is at the same height
    after = grid.visualRect(model.index(model.row_of(top_id))).top()
    assert after == before


@pytest.mark.parametrize("count", [5])
def test_a_filter_change_still_starts_over_and_keeps_nothing(qtbot, service, drive, count):
    window = photos_window(qtbot, service, drive, count)
    resets = []
    window.model.modelReset.connect(lambda: resets.append(1))

    window.filters.year.setCurrentIndex(0)
    window.model.set_filter(window.model.filter)
    qtbot.waitUntil(lambda: bool(resets), timeout=TIMEOUT)


def test_the_place_in_the_view_is_kept_when_rows_are_removed_far_down(qtbot, service, drive):
    # More photos than Qt lays out in one batch; a stack made far down folds rows away
    window = photos_window(qtbot, service, drive, 700)
    grid, model = window.grid, window.model
    bar = grid.verticalScrollBar()
    bar.setValue(bar.maximum() - grid.gridSize().height() * 3)
    qtbot.wait(100)
    count = model.rowCount()
    top = grid.indexAt(grid.viewport().rect().topLeft() + _inset()).row()
    assert top > 600
    selection = grid.selectionModel()
    selection.setCurrentIndex(
        model.index(top + 2), QItemSelectionModel.SelectionFlag.ClearAndSelect
    )
    selection.select(model.index(top + 9), QItemSelectionModel.SelectionFlag.Select)
    before = bar.value()

    window.stacking.stack_photos()
    qtbot.waitUntil(lambda: model.rowCount() == count - 1, timeout=TIMEOUT)
    qtbot.wait(100)

    assert bar.value() == before
