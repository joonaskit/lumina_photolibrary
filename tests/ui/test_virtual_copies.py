"""Virtual copies in the UI: making one, showing which is which, editing and exporting one.
A copy never touches the original file or the photo's own edit."""

import hashlib
import json
import os

import pytest
from conftest import TIMEOUT, written

from lumina.core.edit.params import Params
from lumina.qt_models.develop import DevelopSession, ExportOptions, suggested_name
from lumina.ui.export_dialog import ExportDialog
from lumina.ui.main_window import _DEVELOP, _GRID, _VIEWER, MainWindow


@pytest.fixture
def library(service, drive, qtbot):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    return service


@pytest.fixture
def photo(library):
    row = next(p for p in library.db.photos() if p.path.endswith("a.jpg"))
    return row.id, library.resolve(row.volume_key, row.path)


def digest(path):
    with open(path, "rb") as file:
        return hashlib.sha256(file.read()).hexdigest()


def exposure(library, photo_id, copy=0):
    stored = library.edit_json(photo_id, copy)
    return None if stored is None else json.loads(stored)["exposure"]


def test_a_copy_starts_as_the_edit_it_was_made_from(library, photo):
    photo_id, path = photo
    library.save_edit(photo_id, Params(exposure=0.5).to_json())
    before = digest(path)

    number = written(library.make_copy, photo_id)
    other = written(library.make_copy, photo_id, number)

    assert (number, other) == (1, 2)
    assert library.copies(photo_id) == [1, 2]
    assert exposure(library, photo_id, 1) == 0.5
    assert exposure(library, photo_id) == 0.5
    assert digest(path) == before


def test_a_copy_of_an_unedited_photo_is_the_original_and_leaves_it_unedited(library, photo):
    photo_id, _path = photo

    number = written(library.make_copy, photo_id)

    assert exposure(library, photo_id, number) == 0
    assert library.edit_json(photo_id) is None


def test_the_grid_counts_a_photos_copies(library, photo):
    photo_id, _path = photo
    assert next(p for p in library.db.photos() if p.id == photo_id).copies == 0

    written(library.make_copy, photo_id)
    written(library.make_copy, photo_id)

    assert next(p for p in library.db.photos() if p.id == photo_id).copies == 2
    assert next(p for p in library.db.photos() if p.id != photo_id).copies == 0


def test_making_copies_of_several_photos_gives_each_one(library):
    ids = [row.id for row in library.db.photos()]

    library.make_copies(ids)

    assert all(library.copies(photo_id) == [1] for photo_id in ids)


def test_deleting_a_copy_leaves_the_photos_own_edit_and_the_other_copies(library, photo):
    photo_id, path = photo
    library.save_edit(photo_id, Params(exposure=0.5).to_json())
    first, second = written(library.make_copy, photo_id), written(library.make_copy, photo_id)
    before = digest(path)

    library.delete_copy(photo_id, first)
    library.flush()

    assert library.copies(photo_id) == [second]
    assert exposure(library, photo_id) == 0.5
    assert digest(path) == before
    with pytest.raises(ValueError):
        library.delete_copy(photo_id, 0)
    with pytest.raises(ValueError):
        library.save_edit(photo_id, None, second)


def test_editing_a_copy_saves_it_and_leaves_the_photos_own_edit(library, photo, qtbot):
    photo_id, path = photo
    library.save_edit(photo_id, Params(exposure=0.5).to_json())
    number = written(library.make_copy, photo_id)
    before = digest(path)
    session = DevelopSession(library)
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.open(photo_id, path, number)
    assert session.copy == number and session.params.exposure == 0.5

    session.commit(Params(exposure=2.0))

    assert exposure(library, photo_id, number) == 2.0
    assert exposure(library, photo_id) == 0.5
    session.undo()
    assert exposure(library, photo_id, number) == 0.5
    session.close()
    assert digest(path) == before


def test_a_copy_reset_to_the_original_stays_a_copy(library, photo, qtbot):
    photo_id, path = photo
    number = written(library.make_copy, photo_id, params_json=Params(exposure=1.0).to_json())
    session = DevelopSession(library)
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.open(photo_id, path, number)

    session.reset()

    assert library.copies(photo_id) == [number]
    assert exposure(library, photo_id, number) == 0
    session.close()


def test_a_copy_is_exported_with_its_own_edit(library, photo, tmp_path, qtbot):
    photo_id, path = photo
    library.save_edit(photo_id, Params(exposure=0.5).to_json())
    number = written(library.make_copy, photo_id, params_json=Params(exposure=2.0).to_json())
    session = DevelopSession(library)
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.open(photo_id, path, number)
    own = DevelopSession(library)
    with qtbot.waitSignal(own.image_ready, timeout=TIMEOUT):
        own.open(photo_id, path)
    options = ExportOptions()
    out_copy, out_own = str(tmp_path / "copy.jpg"), str(tmp_path / "own.jpg")

    with qtbot.waitSignal(session.exported, timeout=TIMEOUT) as done:
        session.export(out_copy, options)
    assert done.args[1] is None
    with qtbot.waitSignal(own.exported, timeout=TIMEOUT):
        own.export(out_own, options)

    import numpy as np
    from PIL import Image

    def brightness(file):
        return np.asarray(Image.open(file).convert("L")).mean()

    assert brightness(out_copy) > brightness(out_own)
    session.close()
    own.close()


def test_the_export_name_says_which_copy_it_is(photo):
    _id, path = photo
    options = ExportOptions()

    assert suggested_name(path, options) == "a.jpg"
    assert suggested_name(path, options, 2) == "a-copy2.jpg"
    assert ExportDialog(path, copy=2).name.text() == "a-copy2.jpg"


def open_window(qtbot, service, drive):
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.browser.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    return window


def test_making_a_copy_in_the_viewer_shows_it_and_stepping_goes_through_the_edits(
    qtbot, service, drive
):
    window = open_window(qtbot, service, drive)
    window.nav.show_photo(0)
    photo_id = window.nav.viewed_id

    window.copies.make_virtual_copy()
    window.service.flush()

    assert window.pages.currentIndex() == _VIEWER
    assert window.nav.viewed_copy == 1
    assert "copy 1" in window.title.text()
    assert "Edit 2 of 2" in window.status.text()
    window.copies.step_copy(-1)
    assert window.nav.viewed_copy == 0
    assert "copy" not in window.title.text()
    window.copies.step_copy(-1)
    assert window.nav.viewed_copy == 0
    window.copies.step_copy(1)
    window.copies.step_copy(1)
    assert window.nav.viewed_copy == 1
    qtbot.waitUntil(
        lambda: window.model.copies(window.model.row_of(photo_id)) == 1, timeout=TIMEOUT
    )
    # Another photo starts at its own edit
    window.nav.step(1)
    assert window.nav.viewed_copy == 0


def test_a_copy_is_edited_in_the_develop_view_and_deleted_there(qtbot, service, drive, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = open_window(qtbot, service, drive)
    window.nav.show_photo(0)
    photo_id = window.nav.viewed_id
    window.nav.edit_current()
    assert window.pages.currentIndex() == _DEVELOP
    qtbot.waitUntil(lambda: window.session.size is not None, timeout=TIMEOUT)

    window.copies.make_virtual_copy()
    window.service.flush()

    assert window.session.copy == 1 and window.session.photo_id == photo_id
    qtbot.waitUntil(lambda: window.session.size is not None, timeout=TIMEOUT)
    window.session.commit(Params(exposure=1.0))
    assert exposure(service, photo_id, 1) == 1.0
    assert service.edit_json(photo_id) is None
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)

    window.copies.delete_virtual_copy()

    assert service.copies(photo_id) == []
    assert window.session.copy == 0
    assert not window.delete_copy_action.isEnabled()


def test_the_grid_makes_a_copy_of_each_photo_chosen(qtbot, service, drive):
    window = open_window(qtbot, service, drive)
    window.grid.selectAll()
    assert window.pages.currentIndex() == _GRID

    window.copies.make_virtual_copy()
    window.service.flush()

    qtbot.waitUntil(
        lambda: all(window.model.copies(row) == 1 for row in range(window.model.rowCount())),
        timeout=TIMEOUT,
    )
    assert os.path.exists(window.model.path(0))


def open_grid(qtbot, library, photo_id):
    window = MainWindow(library)
    qtbot.addWidget(window)
    window.resize(900, 600)
    qtbot.waitUntil(lambda: window.model.row_of(photo_id) is not None, timeout=TIMEOUT)
    return window


def test_opening_the_copies_makes_a_cell_of_each_and_closing_hides_them(qtbot, library, photo):
    photo_id, _path = photo
    written(library.make_copy, photo_id)
    written(library.make_copy, photo_id)
    window = open_grid(qtbot, library, photo_id)
    before = window.model.rowCount()

    window.model.toggle_copies(photo_id)

    row = window.model.row_of(photo_id)
    assert window.model.rowCount() == before + 2
    assert [window.model.copy(row + n) for n in range(3)] == [0, 1, 2]
    assert window.model.photo_id(row + 2) == photo_id
    assert window.model.row_of(photo_id, 2) == row + 2
    window.model.toggle_copies(photo_id)
    assert window.model.rowCount() == before


def test_a_click_on_the_badge_opens_the_copies(qtbot, library, photo):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QStyleOptionViewItem

    from lumina.ui import badges

    photo_id, _path = photo
    written(library.make_copy, photo_id)
    window = open_grid(qtbot, library, photo_id)
    window.show()
    qtbot.waitUntil(
        lambda: window.grid.visualRect(window.model.index(0)).isValid(), timeout=TIMEOUT
    )
    index = window.model.index(window.model.row_of(photo_id))

    def badge():
        option = QStyleOptionViewItem()
        window.grid.initViewItemOption(option)
        option.rect = window.grid.visualRect(index)
        option.widget = window.grid
        return badges.diamond_rect(window._delegate.photo_rect(option, index), "1")

    # Once the thumbnail is made, there is a photo to put the badge on
    qtbot.waitUntil(lambda: badge() is not None, timeout=TIMEOUT)
    card = badge()
    before = window.model.rowCount()

    qtbot.mouseClick(window.grid.viewport(), Qt.MouseButton.LeftButton, pos=card.center().toPoint())

    assert window.model.rowCount() == before + 1


def test_a_copy_chosen_in_the_grid_is_deleted_and_the_photo_is_not(
    qtbot, library, photo, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    photo_id, path = photo
    written(library.make_copy, photo_id)
    other = written(library.make_copy, photo_id)
    before = digest(path)
    window = open_grid(qtbot, library, photo_id)
    window.model.toggle_copies(photo_id)
    row = window.model.row_of(photo_id, other)
    window.grid.setCurrentIndex(window.model.index(window.model.row_of(photo_id)))

    # The photo's own cell has no copy to delete
    window.copies.update_actions()
    assert not window.delete_copy_action.isEnabled()

    window.grid.setCurrentIndex(window.model.index(row))
    window.copies.update_actions()
    assert window.delete_copy_action.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.copies.delete_virtual_copy()

    assert library.copies(photo_id) == [1]
    qtbot.waitUntil(
        lambda: window.model.row_of(photo_id, other) == window.model.row_of(photo_id),
        timeout=TIMEOUT,
    )
    assert digest(path) == before


def test_a_copy_cell_opens_that_copy(qtbot, library, photo):
    photo_id, _path = photo
    written(library.make_copy, photo_id)
    window = open_grid(qtbot, library, photo_id)
    window.model.toggle_copies(photo_id)
    row = window.model.row_of(photo_id, 1)

    window.grid.activated.emit(window.model.index(row))

    assert window.pages.currentIndex() == _VIEWER
    assert window.nav.viewed_copy == 1
