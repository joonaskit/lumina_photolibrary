"""Find Duplicates: groups of photos that look alike across the library, to stack or to
thumbs-down all but the best."""

from conftest import TIMEOUT
from test_flags import names, open_window
from test_stacks import give_vectors, unit, wait_for_rows

from lumina.qt_models.library_model import REJECTED, STACK_ROLE


def open_dialog(qtbot, window, groups):
    window.find_duplicates()
    dialog = window.similar_dialog
    qtbot.waitUntil(lambda: dialog.summary.text() != "Looking…", timeout=TIMEOUT)
    assert dialog.tree.topLevelItemCount() == groups
    return dialog


def choose_group(dialog, number):
    dialog.tree.clearSelection()
    dialog.tree.topLevelItem(number).setSelected(True)


def test_photos_that_look_alike_are_listed_as_a_group_with_the_best_first(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    give_vectors(service, window, [unit(1), unit(1), unit(0, 1)])

    dialog = open_dialog(qtbot, window, 1)

    group = dialog.tree.topLevelItem(0)
    assert group.childCount() == 2
    assert group.text(0).startswith("2 photos")
    assert "(best)" in group.child(0).text(0)
    assert "2 photos" not in dialog.summary.text()
    assert "1 group" in dialog.summary.text()


def test_nothing_is_listed_when_no_photos_look_alike(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")

    dialog = open_dialog(qtbot, window, 0)

    assert dialog.summary.text() == "No photos that look alike at this setting"
    assert not dialog.stack_button.isEnabled()


def test_a_group_chosen_is_stacked_with_the_best_on_top(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first = names(window)[0]
    give_vectors(service, window, [unit(1), unit(1), unit(0, 1)])
    dialog = open_dialog(qtbot, window, 1)
    choose_group(dialog, 0)

    dialog.stack_button.click()

    wait_for_rows(qtbot, window, 2)
    assert names(window)[0] == first
    assert window.model.data(window.model.index(0), STACK_ROLE)[1] == 2


def test_the_others_of_a_group_get_a_thumbs_down_and_none_is_deleted(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    give_vectors(service, window, [unit(1), unit(1), unit(0, 1)])
    dialog = open_dialog(qtbot, window, 1)
    choose_group(dialog, 0)

    dialog.reject_button.click()

    qtbot.waitUntil(lambda: window.model.flag(1) == REJECTED, timeout=TIMEOUT)
    assert window.model.flag(0) == 0
    assert window.model.flag(2) == 0
    assert window.model.rowCount() == 3


def test_a_lower_setting_finds_photos_that_only_resemble_each_other(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    give_vectors(service, window, [unit(1), unit(0.9, 0.436), unit(0, 1)])
    dialog = open_dialog(qtbot, window, 0)

    dialog.slider.setValue(85)

    qtbot.waitUntil(lambda: dialog.tree.topLevelItemCount() == 1, timeout=TIMEOUT)
