"""The shortcuts' overlay (? or F1) and the palette that finds an action by name
(Ctrl+K): both read the window's actions, so neither goes stale."""

import pytest
from conftest import TIMEOUT
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QWidget

from lumina.integration import style
from lumina.ui import shortcuts
from lumina.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot, service, drive):
    made = MainWindow(service)
    qtbot.addWidget(made)
    made.resize(1200, 800)
    made.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        made.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: made.model.rowCount() == 3, timeout=TIMEOUT)
    return made


def choose(window, *rows):
    window.grid.setCurrentIndex(window.model.index(rows[0]))
    for row in rows[1:]:
        window.grid.selectionModel().select(
            window.model.index(row), window.grid.selectionModel().SelectionFlag.Select
        )


def open_editor(qtbot, window):
    choose(window, 0)
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.edit_current()


def shortcut_actions(window):
    """Every action with a key on the window or on a widget it shows."""
    shown = [window, *(w for w in window.findChildren(QWidget) if w.isVisible())]
    return {
        action.text()
        for widget in shown
        for action in widget.actions()
        if action.text() and action.shortcuts()
    }


def overlay_names(window):
    return {entry.name for entry, _label in window._shortcut_overlay.rows}


# The overlay


def test_every_action_with_a_shortcut_is_in_the_overlay(qtbot, window):
    window.show_shortcuts()
    names = overlay_names(window)
    assert shortcut_actions(window) <= names
    assert {"Keyboard Shortcuts", "Find an Action", "Thumbs Up", "Stack"} <= names
    # The Develop view's keys are not the grid's
    assert "Crop" not in names
    window._shortcut_overlay.close_layer()

    open_editor(qtbot, window)
    window.show_shortcuts()
    names = overlay_names(window)
    assert shortcut_actions(window) <= names
    assert {"Crop", "Undo", "Zoom In", "Show Original While Held"} <= names
    assert window._shortcut_overlay.view_label.text() == "Develop"


def test_every_action_with_a_shortcut_has_a_group(window):
    actions = window.findChildren(QAction) + window.develop.findChildren(QAction)
    ungrouped = [
        action.text()
        for action in actions
        if action.shortcuts() and action.property("group") not in shortcuts.GROUPS[:-1]
    ]
    assert ungrouped == []


def test_the_overlay_shows_the_keys_in_groups(window):
    window.show_shortcuts()
    rows = {entry.name: entry for entry, _label in window._shortcut_overlay.rows}
    assert rows["Thumbs Up"].group == "Culling"
    assert rows["Thumbs Up"].keys == ["P"]
    assert rows["Merge to HDR…"].group == "Merges"
    assert rows["Keyboard Shortcuts"].keys == ["?", "F1"]


def test_question_mark_and_f1_open_it_and_any_key_closes_it(qtbot, window):
    assert QKeySequence("?") in window.shortcuts_action.shortcuts()
    assert QKeySequence(Qt.Key.Key_F1) in window.shortcuts_action.shortcuts()
    window.grid.setFocus()
    qtbot.keyClick(window.grid, Qt.Key.Key_F1)
    overlay = window._shortcut_overlay
    assert overlay is not None and overlay.isVisible()

    # A key under it is its own, not the window's
    similar = []
    window.similar_action.triggered.connect(lambda: similar.append(True))
    qtbot.keyClick(overlay, Qt.Key.Key_S)
    assert not overlay.isVisible()
    assert similar == []
    # And the focus is back where it was
    assert window.grid.hasFocus()


def test_the_overlay_dims_what_cannot_be_used_now(window):
    choose(window, 0)
    window.show_shortcuts()
    labels = {entry.name: label for entry, label in window._shortcut_overlay.rows}
    # One photo chosen: it can be flagged, not stacked
    assert labels["Thumbs Up"].isEnabled()
    assert not labels["Stack"].isEnabled()


def test_the_overlay_follows_the_density(window):
    window.show_shortcuts()
    layout = window._shortcut_overlay.card.layout()
    comfortable = layout.contentsMargins().left()
    style.set_density("compact", QApplication.instance())
    assert layout.contentsMargins().left() == style.gap(20) < comfortable


# The palette


def test_the_palette_finds_an_action_by_part_of_its_name_and_runs_it(qtbot, window):
    choose(window, 0)
    window.grid.setFocus()
    qtbot.keyClick(window.grid, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    palette = window._command_palette
    assert palette is not None and palette.isVisible()
    assert palette.field.hasFocus()

    qtbot.keyClicks(palette.field, "virtual co")
    assert palette.shown()[0].name == "Make Virtual Copy"
    assert palette.shown()[0].keys == ["Ctrl+'"]
    with qtbot.waitSignal(window.make_copy_action.triggered, timeout=TIMEOUT):
        qtbot.keyClick(palette.field, Qt.Key.Key_Return)
    assert not palette.isVisible()


def test_a_disabled_action_is_greyed_with_why_and_does_not_run(qtbot, window):
    choose(window, 0)
    window.show_palette()
    palette = window._command_palette
    qtbot.keyClicks(palette.field, "stack")
    entry = palette.shown()[0]
    assert entry.name == "Stack"
    assert not entry.enabled
    assert entry.why == "Choose two or more photos"

    ran = []
    window.stack_action.triggered.connect(lambda: ran.append(True))
    qtbot.keyClick(palette.field, Qt.Key.Key_Return)
    assert ran == []
    assert palette.isVisible()


def test_the_palette_moves_with_the_arrows_and_escape_closes_it(qtbot, window):
    window.show_palette()
    palette = window._command_palette
    qtbot.keyClicks(palette.field, "photo")
    assert len(palette.shown()) > 1
    qtbot.keyClick(palette.field, Qt.Key.Key_Down)
    assert palette.list.currentRow() == 1
    qtbot.keyClick(palette.field, Qt.Key.Key_Escape)
    assert not palette.isVisible()


def test_the_palette_offers_actions_without_keys_but_not_itself(window):
    window.show_palette()
    names = {entry.name for entry in window._command_palette.shown()}
    assert "Stack Bursts" in names
    assert "Find an Action" not in names
    assert "Keyboard Shortcuts" in names


def test_the_palette_says_when_nothing_matches(qtbot, window):
    window.show_palette()
    palette = window._command_palette
    qtbot.keyClicks(palette.field, "zzzz")
    assert palette.shown() == []
    assert palette.nothing.isVisible()


def test_matching_puts_names_that_start_with_it_first():
    found = [
        shortcuts.Entry("Make Virtual Copy", [], "Other"),
        shortcuts.Entry("Copy Settings…", [], "Other"),
        shortcuts.Entry("Previous Copy", [], "Other"),
        shortcuts.Entry("Next Photo", [], "Other"),
    ]
    names = [entry.name for entry in shortcuts.matching("copy", found)]
    assert names == ["Copy Settings…", "Make Virtual Copy", "Previous Copy"]
    assert [entry.name for entry in shortcuts.matching("cop vir", found)] == ["Make Virtual Copy"]
    assert len(shortcuts.matching("  ", found)) == 4
