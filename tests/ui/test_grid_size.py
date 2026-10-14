from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from lumina.integration import style
from lumina.ui import settings
from lumina.ui.main_window import GRID_ICONS, MainWindow


def open_window(qtbot, service):
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    return window


def icon_side(window):
    return window.grid.iconSize().width()


def test_medium_is_the_default(qtbot, service):
    window = open_window(qtbot, service)

    assert window.grid_size.value() == "medium"
    assert icon_side(window) == style.px(GRID_ICONS["medium"])


def test_a_size_chosen_is_shown_and_remembered(qtbot, service):
    window = open_window(qtbot, service)

    window.grid_size.buttons["large"].click()

    assert icon_side(window) == style.px(GRID_ICONS["large"])
    assert window.grid.gridSize().width() > icon_side(window)
    assert settings.grid_size() == "large"
    again = open_window(qtbot, service)
    assert again.grid_size.value() == "large"
    assert icon_side(again) == style.px(GRID_ICONS["large"])


def test_the_keys_step_through_the_sizes_and_stop_at_the_ends(qtbot, service):
    window = open_window(qtbot, service)
    window.grid.setFocus()

    window.smaller_action.trigger()
    window.smaller_action.trigger()
    assert window.grid_size.value() == "small"
    window.larger_action.trigger()
    window.larger_action.trigger()
    window.larger_action.trigger()
    assert window.grid_size.value() == "large"


def wheel(window, up, modifiers):
    viewport = window.grid.viewport()
    event = QWheelEvent(
        QPointF(10, 10),
        QPointF(viewport.mapToGlobal(QPoint(10, 10))),
        QPoint(),
        QPoint(0, 120 if up else -120),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(viewport, event)


def test_ctrl_and_the_wheel_zoom_and_the_wheel_alone_scrolls(qtbot, service):
    window = open_window(qtbot, service)

    wheel(window, True, Qt.KeyboardModifier.ControlModifier)
    assert window.grid_size.value() == "large"
    wheel(window, False, Qt.KeyboardModifier.ControlModifier)
    wheel(window, False, Qt.KeyboardModifier.ControlModifier)
    assert window.grid_size.value() == "small"
    wheel(window, True, Qt.KeyboardModifier.NoModifier)
    assert window.grid_size.value() == "small"


def test_the_buttons_beside_the_filters_are_as_tall_as_the_combos(qtbot, service):
    from lumina.ui.main_window import MainWindow

    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    height = window.filters.year.sizeHint().height()

    for button in (*window.grid_size.buttons.values(), window.flag_options):
        assert button.size().height() == height
        assert button.size().width() == height
