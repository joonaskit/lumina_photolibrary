"""The edit filter: only edited photos, only unedited ones, or all."""

from conftest import TIMEOUT
from PySide6.QtCore import QItemSelectionModel

from lumina.core.edit.params import Params
from lumina.qt_models.library_model import EDITED, UNEDITED
from lumina.ui.main_window import MainWindow


def open_window(qtbot, service, folder):
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.add_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    assert window.model.rowCount() == 3
    return window


def names(window):
    return [window.model.name(row) for row in range(window.model.rowCount())]


def edit_photo(qtbot, window, row, edit=True):
    """Store (or remove) the edit of the photo in `row` and wait for the grid to hear."""
    name = window.model.name(row)
    photo_id = window.model.photo_id(row)
    window.service.save_edit(photo_id, Params(exposure=0.5).to_json() if edit else None)

    def heard():
        shown = window.model.row_of(photo_id)
        return shown is not None and (window.model.edit(shown) is not None) == edit

    qtbot.waitUntil(heard, timeout=TIMEOUT)
    return name


def choose_edit(window, which):
    combo = window.filters.edit
    combo.setCurrentIndex(combo.findData(which))


def test_the_edit_filter_shows_only_edited_or_only_unedited_photos(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, third = names(window)
    edited = edit_photo(qtbot, window, 1)
    assert edited == second

    choose_edit(window, EDITED)
    assert names(window) == [second]
    choose_edit(window, UNEDITED)
    assert names(window) == [first, third]
    assert window.status.text() == "2 photos"
    choose_edit(window, None)
    assert names(window) == [first, second, third]


def test_a_photo_edited_under_unedited_stays_until_the_user_moves_on(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, third = names(window)
    choose_edit(window, UNEDITED)
    assert names(window) == [first, second, third]
    window.grid.selectionModel().setCurrentIndex(
        window.model.index(1), QItemSelectionModel.SelectionFlag.ClearAndSelect
    )

    edit_photo(qtbot, window, 1)

    assert names(window) == [first, second, third]
    window.model.settle(None)
    assert names(window) == [first, third]


def test_removing_the_edit_leaves_the_edited_view_the_same_way(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, _third = names(window)
    edit_photo(qtbot, window, 0)
    edit_photo(qtbot, window, 1)
    choose_edit(window, EDITED)
    assert names(window) == [first, second]

    edit_photo(qtbot, window, 0, edit=False)

    assert names(window) == [first, second]
    window.model.settle(None)
    assert names(window) == [second]


def test_the_edit_filter_works_with_the_flag_filter_and_the_hidden_count(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, third = names(window)
    edit_photo(qtbot, window, 0)
    edit_photo(qtbot, window, 1)
    window.grid.selectionModel().setCurrentIndex(
        window.model.index(0), QItemSelectionModel.SelectionFlag.ClearAndSelect
    )
    window.reject_action.trigger()
    choose_edit(window, EDITED)
    flags = window.filters.flag

    assert names(window) == [first, second]
    flags.setCurrentIndex(flags.findData(-1))
    assert names(window) == [first]
    # The unedited third photo is not hidden by the flags, only by the edit filter
    assert window.model.hidden_count() == 1
    assert third not in names(window)
