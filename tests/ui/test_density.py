"""Comfortable and compact: how much room the interface takes."""

import pytest
import shiboken6
from conftest import TIMEOUT
from PySide6.QtWidgets import QWidget

from lumina.integration import style
from lumina.ui import settings
from lumina.ui.main_window import MainWindow
from lumina.ui.settings_dialog import SettingsDialog


def open_window(qtbot, service, folder=None):
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    if folder is not None:
        with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
            window.add_folder(folder)
        qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    return window


def test_comfortable_is_the_default_and_sizes_are_as_they_were():
    assert style.density() == "comfortable" == settings.density()
    assert style.row_height() == style.px(36)
    assert style.group_padding() == style.px(4)
    assert style.row_inner_height() == style.px(36) - 2 * style.px(4) - 2
    for size in (0, 1, 2, 3, 4, 5, 6, 8, 10, 14, 16, 20):
        assert style.gap(size) == style.px(size)


def test_compact_takes_less_room_and_the_stylesheet_differs(qapp):
    comfortable = qapp.styleSheet()
    assert style.set_density("compact", qapp)

    assert qapp.styleSheet() != comfortable
    assert style.row_height() < style.px(36)
    assert style.group_padding() < style.px(4)
    assert 0 < style.gap(8) < style.px(8)
    # A zero stays zero, and nothing shrinks to nothing
    assert style.gap(0) == 0 and style.gap(1) == 1
    # The icons fit in a button of the compact row
    assert style.row_inner_height() >= style.px(18) + 2 * style.gap(3)
    # Setting it again is no change
    assert not style.set_density("compact", qapp)
    assert style.set_density("nonsense", qapp) and style.density() == "comfortable"
    assert qapp.styleSheet() == comfortable


def test_a_stored_value_is_read_and_a_bad_one_is_not(qtbot):
    settings.set_density("compact")
    assert settings.density() == "compact"
    settings.set_density("huge")
    assert settings.density() == "comfortable"

    settings.set_density("compact")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.density.currentData() == "compact"


def test_a_changed_value_is_kept_and_applied_at_once(qtbot, qapp, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    dialog = SettingsDialog(parent=window)
    qtbot.addWidget(dialog)
    before = {
        "sheet": qapp.styleSheet(),
        "toolbar": window.marking.height(),
        "filter": window.filters.year.height(),
        "search": window.search_field.edit.height(),
        "side": window.grid_size.buttons["small"].height(),
        "cell": window.grid.gridSize(),
        "icon": window.grid.iconSize(),
        "sidebar": window.sidebar.add_button.height(),
        "panel": window.develop.layout().itemAt(1).widget().widget().layout().spacing(),
    }
    assert before["toolbar"] == before["filter"] == before["search"] == style.px(36)

    dialog.density.setCurrentIndex(dialog.density.findData("compact"))

    assert settings.density() == "compact"
    assert qapp.styleSheet() != before["sheet"]
    for name, widget in (
        ("toolbar", window.marking),
        ("filter", window.filters.year),
        ("search", window.search_field.edit),
        ("side", window.grid_size.buttons["small"]),
        ("sidebar", window.sidebar.add_button),
    ):
        assert widget.height() < before[name], name
    assert window.marking.height() == window.filters.year.height() == style.row_height()
    # Closer cells, the same thumbnails
    assert window.grid.gridSize().height() < before["cell"].height()
    assert window.grid.gridSize().width() < before["cell"].width()
    assert window.grid.iconSize() == before["icon"]
    panel = window.develop.layout().itemAt(1).widget().widget().layout()
    assert panel.spacing() < before["panel"]

    # And back
    dialog.density.setCurrentIndex(dialog.density.findData("comfortable"))
    assert window.marking.height() == before["toolbar"]
    assert window.grid.gridSize() == before["cell"]
    assert qapp.styleSheet() == before["sheet"]


def test_a_new_window_is_made_in_the_density_in_use(qtbot, qapp, service):
    style.set_density("compact", qapp)
    window = open_window(qtbot, service)

    assert window.marking.height() == style.row_height() < style.px(36)


def test_a_widget_that_is_gone_is_not_asked_to_fit(qtbot, qapp):
    widget = QWidget()
    fitted = []
    style.follow_density(widget, lambda: fitted.append(1))
    assert len(fitted) == 1
    style.set_density("compact", qapp)
    assert len(fitted) == 2

    shiboken6.delete(widget)
    style.set_density("comfortable", qapp)
    assert len(fitted) == 2


@pytest.mark.parametrize("name", ["comfortable", "compact"])
def test_every_row_control_is_as_tall_as_the_row_in_either_density(qtbot, qapp, service, name):
    style.set_density(name, qapp)
    window = open_window(qtbot, service)
    window.search_field.show()

    for widget in (window.marking, window.search_field.edit, window.filters.clear_button):
        assert widget.height() == style.row_height(), widget
