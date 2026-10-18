"""Copying the settings of one photo's edit to others: from the grid, the viewer and the
Develop view, and syncing a chosen set to the current photo's."""

import hashlib
import json

from conftest import TIMEOUT
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QMessageBox

from lumina.core.edit import params as edit_params
from lumina.core.edit.params import Band, Basic, Colour, Hsl, Params
from lumina.ui import settings
from lumina.ui.main_window import MainWindow
from lumina.ui.preset_dialog import CopyDialog

LOOK = Params(
    exposure=1.0,
    temperature=25,
    basic=Basic(contrast=30, shadows=20),
    colour=Colour(vibrance=40),
    hsl=Hsl(red=Band(hue=10)),
)


def open_window(qtbot, service, folder):
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.browser.add_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    assert window.model.rowCount() == 3
    return window


def photo_id(window, name):
    row = next(r for r in range(window.model.rowCount()) if window.model.name(r) == name)
    return window.model.photo_id(row)


def stored(window, name):
    """The photo's own edit as it is stored, or None."""
    text = window.service.edit_json(photo_id(window, name))
    return edit_params.from_json(text) if text else None


def edit_stored(qtbot, window, name, params):
    window.service.save_edit(photo_id(window, name), params.to_json())
    settled(qtbot, window)


def settled(qtbot, window):
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)


def select(window, current, *others):
    """Choose photos in the grid, `current` being the one the others follow."""
    model = window.grid.selectionModel()
    model.clearSelection()
    rows = {window.model.name(r): r for r in range(window.model.rowCount())}
    model.setCurrentIndex(
        window.model.index(rows[current]), QItemSelectionModel.SelectionFlag.NoUpdate
    )
    for name in (current, *others):
        model.select(window.model.index(rows[name]), QItemSelectionModel.SelectionFlag.Select)


def answer_dialog(monkeypatch, sections):
    """The Copy Settings and Sync Settings dialogs, filled in with `sections` and let
    through (None: cancelled). Collects what they asked."""
    asked = []

    def run(self):
        asked.append((self.windowTitle(), self.save_button.text(), self.sections()))
        if sections is None:
            return CopyDialog.DialogCode.Rejected
        self.set_sections(sections)
        self.save_button.click()
        return CopyDialog.DialogCode.Accepted

    monkeypatch.setattr(CopyDialog, "exec", run)
    return asked


def confirm(monkeypatch, answer=QMessageBox.StandardButton.Yes):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or answer)
    return asked


def digest(drive):
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((drive / "Photos").iterdir())
    }


def test_the_copy_dialog_starts_with_the_exposure_and_the_look(qtbot):
    dialog = CopyDialog()
    qtbot.addWidget(dialog)

    assert dialog.sections() == [
        "exposure",
        "white_balance",
        "basic",
        "presence",
        "sharpening",
        "noise",
        "colour",
        "hsl",
        "grading",
        "curves",
        "matte",
        "vignette",
        "grain",
    ]
    assert dialog.save_button.text() == "Copy" and dialog.name() == ""
    dialog.set_sections([])
    assert not dialog.save_button.isEnabled()


def test_copied_settings_go_over_the_edits_of_the_photos_chosen(qtbot, service, drive, monkeypatch):
    window = open_window(qtbot, service, drive / "Photos")
    originals = digest(drive)
    edit_stored(qtbot, window, "a.jpg", LOOK)
    # b has an edit of its own: its warmth is not in what is copied and must stay
    edit_stored(qtbot, window, "b.jpg", Params(tint=12, exposure=-1.0))
    asked = answer_dialog(monkeypatch, ["exposure", "basic", "hsl"])
    select(window, "a.jpg")
    assert window.copy_settings_action.isEnabled()
    assert not window.paste_settings_action.isEnabled()

    window.settings_copy.copy_settings()

    assert asked[0][0] == "Copy Settings"
    copied = window.service.copied
    assert copied.name == "a.jpg" and copied.sections == ("exposure", "basic", "hsl")
    select(window, "b.jpg", "c.dng")
    assert window.paste_settings_action.isEnabled()
    questions = confirm(monkeypatch)

    window.settings_copy.paste_settings()
    window.service.flush()

    assert "2 photos" in questions[0] and "a.jpg" in questions[0]
    expected = Params(exposure=1.0, basic=LOOK.basic, hsl=LOOK.hsl)
    assert stored(window, "c.dng") == expected
    assert stored(window, "b.jpg") == expected.with_changes(tint=12)
    # The source is as it was, and no photo's file was written to
    assert stored(window, "a.jpg") == LOOK
    assert digest(drive) == originals
    settled(qtbot, window)


def test_pasting_to_many_photos_asks_first_and_no_leaves_them_alone(
    qtbot, service, drive, monkeypatch
):
    window = open_window(qtbot, service, drive / "Photos")
    edit_stored(qtbot, window, "a.jpg", LOOK)
    answer_dialog(monkeypatch, ["exposure"])
    select(window, "a.jpg")
    window.settings_copy.copy_settings()
    select(window, "b.jpg", "c.dng")
    confirm(monkeypatch, QMessageBox.StandardButton.No)

    window.settings_copy.paste_settings()
    window.service.flush()

    assert stored(window, "b.jpg") is None and stored(window, "c.dng") is None
    settled(qtbot, window)


def test_pasting_to_one_photo_does_not_ask(qtbot, service, drive, monkeypatch):
    window = open_window(qtbot, service, drive / "Photos")
    edit_stored(qtbot, window, "a.jpg", LOOK)
    answer_dialog(monkeypatch, ["exposure"])
    select(window, "a.jpg")
    window.settings_copy.copy_settings()
    select(window, "b.jpg")
    questions = confirm(monkeypatch)

    window.settings_copy.paste_settings()
    window.service.flush()

    assert not questions
    assert stored(window, "b.jpg") == Params(exposure=1.0)
    settled(qtbot, window)


def test_a_cancelled_copy_keeps_nothing(qtbot, service, drive, monkeypatch):
    window = open_window(qtbot, service, drive / "Photos")
    answer_dialog(monkeypatch, None)
    select(window, "a.jpg")

    window.settings_copy.copy_settings()

    assert window.service.copied is None
    settled(qtbot, window)


def test_copying_from_a_photo_without_an_edit_pastes_the_original_over_the_sections(
    qtbot, service, drive, monkeypatch
):
    window = open_window(qtbot, service, drive / "Photos")
    edit_stored(qtbot, window, "b.jpg", Params(exposure=2.0, tint=9))
    answer_dialog(monkeypatch, ["exposure"])
    select(window, "a.jpg")
    window.settings_copy.copy_settings()
    select(window, "b.jpg")

    window.settings_copy.paste_settings()
    window.service.flush()

    # Exposure back to none; the tint was not in what was copied
    assert stored(window, "b.jpg") == Params(tint=9)
    answer_dialog(monkeypatch, ["exposure", "white_balance"])
    select(window, "a.jpg")
    window.settings_copy.copy_settings()
    select(window, "b.jpg")
    window.settings_copy.paste_settings()
    window.service.flush()
    # Nothing left of the edit: it is gone, not kept empty
    assert stored(window, "b.jpg") is None
    settled(qtbot, window)


def test_an_edit_from_a_newer_lumina_is_left_alone_and_named(qtbot, service, drive, monkeypatch):
    window = open_window(qtbot, service, drive / "Photos")
    edit_stored(qtbot, window, "a.jpg", LOOK)
    newer = json.dumps({"version": edit_params.VERSION + 1, "exposure": 3.0})
    window.service.save_edit(photo_id(window, "b.jpg"), newer)
    answer_dialog(monkeypatch, ["exposure"])
    select(window, "a.jpg")
    window.settings_copy.copy_settings()
    select(window, "b.jpg", "c.dng")
    confirm(monkeypatch)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warned.append(a[2]))

    window.settings_copy.paste_settings()
    window.service.flush()

    assert window.service.edit_json(photo_id(window, "b.jpg")) == newer
    assert stored(window, "c.dng") == Params(exposure=1.0)
    assert "b.jpg" in warned[0] and "c.dng" not in warned[0]
    settled(qtbot, window)


def test_syncing_copies_the_current_photos_sections_to_the_others_chosen(
    qtbot, service, drive, monkeypatch
):
    window = open_window(qtbot, service, drive / "Photos")
    originals = digest(drive)
    edit_stored(qtbot, window, "b.jpg", LOOK)
    select(window, "b.jpg")
    assert not window.sync_settings_action.isEnabled()
    select(window, "b.jpg", "a.jpg", "c.dng")
    assert window.sync_settings_action.isEnabled()
    asked = answer_dialog(monkeypatch, ["white_balance", "colour"])

    window.settings_copy.sync_settings()
    window.service.flush()

    assert asked[0][0] == "Sync Settings" and asked[0][1] == "Sync"
    expected = Params(temperature=25, colour=LOOK.colour)
    assert stored(window, "a.jpg") == expected and stored(window, "c.dng") == expected
    assert stored(window, "b.jpg") == LOOK
    # What was copied for pasting is not touched by a sync
    assert window.service.copied is None
    assert digest(drive) == originals
    settled(qtbot, window)


def test_the_menu_actions_follow_what_there_is_to_copy_and_paste(
    qtbot, service, drive, monkeypatch
):
    window = open_window(qtbot, service, drive / "Photos")
    window.grid.selectionModel().clearSelection()
    window.grid.setCurrentIndex(window.model.index(0))
    window.grid.selectionModel().clear()
    assert not window.paste_settings_action.isEnabled()
    answer_dialog(monkeypatch, ["exposure"])
    select(window, "a.jpg")
    window.settings_copy.copy_settings()

    assert window.paste_settings_action.isEnabled()
    assert not window.sync_settings_action.isEnabled()
    settled(qtbot, window)


def test_settings_copied_in_the_develop_view_go_to_another_photo_and_can_be_undone(
    qtbot, service, drive, monkeypatch
):
    window = open_window(qtbot, service, drive / "Photos")
    edit_stored(qtbot, window, "a.jpg", LOOK)
    select(window, "a.jpg")
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.nav.edit_current()
    # An edit not yet kept anywhere else: the copy is of the edit as it is now
    window.session.commit(LOOK.with_changes(grain=edit_params.Grain(amount=40)))
    answer_dialog(monkeypatch, ["exposure", "grain"])
    assert not window.develop.paste_settings_button.isEnabled()

    window.develop.copy_settings_button.click()

    assert window.develop.paste_settings_button.isEnabled()
    window.nav.go_back()
    select(window, "b.jpg")
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.nav.edit_current()
    window.session.commit(Params(tint=5))

    window.develop.paste_settings_button.click()

    assert window.session.params == Params(tint=5, exposure=1.0, grain=edit_params.Grain(amount=40))
    window.undo_action.trigger()
    assert window.session.params == Params(tint=5)
    settled(qtbot, window)


def test_pasting_in_the_viewer_edits_the_photo_shown(qtbot, service, drive, monkeypatch):
    window = open_window(qtbot, service, drive / "Photos")
    edit_stored(qtbot, window, "a.jpg", LOOK)
    answer_dialog(monkeypatch, ["exposure", "hsl"])
    select(window, "a.jpg")
    window.settings_copy.copy_settings()
    window.nav.show_photo(next(r for r in range(3) if window.model.name(r) == "b.jpg"))
    assert window.paste_settings_action.isEnabled()

    window.settings_copy.paste_settings()
    window.service.flush()

    assert stored(window, "b.jpg") == Params(exposure=1.0, hsl=LOOK.hsl)
    settled(qtbot, window)


def test_the_choice_of_sections_is_remembered_for_the_next_copy(qtbot, service, drive, monkeypatch):
    window = open_window(qtbot, service, drive / "Photos")
    answer_dialog(monkeypatch, ["crop", "rotate"])
    select(window, "a.jpg")

    window.settings_copy.copy_settings()

    assert settings.copy_sections() == ["rotate", "crop"]
    settled(qtbot, window)
    fresh = CopyDialog()
    qtbot.addWidget(fresh)
    assert fresh.sections() == ["rotate", "crop"]


def test_settings_can_be_pasted_on_a_photo_on_a_drive_that_is_not_connected(
    qtbot, service, drive, mounts, monkeypatch
):
    window = open_window(qtbot, service, drive / "Photos")
    edit_stored(qtbot, window, "a.jpg", LOOK)
    answer_dialog(monkeypatch, ["exposure"])
    select(window, "a.jpg")
    window.settings_copy.copy_settings()
    mounts.plugged = False
    qtbot.waitUntil(lambda: window.model.path(0) is None, timeout=TIMEOUT)
    select(window, "b.jpg")

    window.settings_copy.paste_settings()
    window.service.flush()

    # Edits live in the library, not on the drive
    assert stored(window, "b.jpg") == Params(exposure=1.0)
    settled(qtbot, window)
