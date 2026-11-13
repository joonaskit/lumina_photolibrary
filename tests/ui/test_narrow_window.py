"""The window can be made narrow, as for half a screen (#143): the filters and the live
view's controls wrap onto more rows instead of setting a wide minimum."""

from conftest import HALF_A_SCREEN
from PySide6.QtWidgets import QFrame, QPushButton, QWidget

from lumina.integration import style
from lumina.ui.filters import FilterBar
from lumina.ui.flow_layout import FlowLayout
from lumina.ui.main_window import MainWindow

LONG_CAMERA = "Canon EOS R5 Mark II with a very long name indeed"
LONG_LENS = "RF 100-500mm F4.5-7.1 L IS USM with an extender and a long name"


def inside(widget, parent):
    """Whether `widget` is shown and lies wholly within `parent`."""
    if not widget.isVisible() or widget.width() <= 0:
        return False
    top_left = widget.mapTo(parent, widget.rect().topLeft())
    return parent.rect().contains(top_left) and parent.rect().contains(
        widget.mapTo(parent, widget.rect().bottomRight())
    )


def test_the_window_can_be_made_narrower_than_half_a_screen(qtbot, service):
    window = MainWindow(service)
    qtbot.addWidget(window)
    # Long names make the camera and lens choices as wide as they go
    window.filters.set_choices(["2024", "2025"], [LONG_CAMERA], [LONG_LENS])
    window.show()
    assert window.minimumSizeHint().width() < HALF_A_SCREEN


def test_the_window_stays_narrow_at_the_compact_density(qtbot, service):
    style.set_density("compact")
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    assert window.minimumSizeHint().width() < HALF_A_SCREEN


def test_at_a_narrow_width_every_filter_can_be_reached(qtbot):
    bar = FilterBar()
    qtbot.addWidget(bar)
    bar.set_choices(["2024", "2025"], [LONG_CAMERA], [LONG_LENS])
    bar.show()
    one_row = bar.layout().heightForWidth(bar.sizeHint().width())
    narrow = bar.minimumSizeHint().width() + 20
    bar.resize(narrow, bar.layout().heightForWidth(narrow))
    qtbot.waitUntil(lambda: bar.clear_button.geometry().bottom() > one_row)
    for widget in (*bar._combos(), bar.save_button):
        assert inside(widget, bar), widget
    # Clear and Save… stay at the right end
    assert bar.save_button.geometry().right() == bar.contentsRect().right()
    assert bar.layout().heightForWidth(narrow) > one_row


def test_wide_enough_the_filters_are_one_row(qtbot):
    bar = FilterBar()
    qtbot.addWidget(bar)
    bar.show()
    bar.resize(bar.sizeHint().width() + 200, bar.sizeHint().height())
    qtbot.waitUntil(lambda: bar.save_button.isVisible())
    tops = {widget.geometry().top() for widget in bar._combos()}
    assert len(tops) == 1
    assert bar.save_button.geometry().right() == bar.contentsRect().right()


def buttons_in_a_flow(qtbot, *kinds):
    """A FlowLayout of 100 px buttons, added as `kinds` says, with no spacing."""
    holder = QWidget()
    qtbot.addWidget(holder)
    layout = FlowLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    widgets = []
    for kind in kinds:
        if kind == "separator":
            widget = QFrame()
            widget.setFixedSize(2, 20)
            layout.add_separator(widget)
        else:
            widget = QPushButton(kind)
            widget.setFixedSize(100, 20)
            (layout.add_trailing if kind == "trailing" else layout.addWidget)(widget)
        widgets.append(widget)
    holder.show()
    return holder, layout, widgets


def test_a_flow_wraps_and_puts_trailing_widgets_at_the_right(qtbot):
    holder, layout, (a, b, c, d) = buttons_in_a_flow(qtbot, "a", "b", "c", "trailing")
    holder.resize(250, layout.heightForWidth(250))
    qtbot.waitUntil(lambda: c.geometry().top() == 20)
    assert (a.x(), a.y(), b.x(), b.y()) == (0, 0, 100, 0)
    assert (c.x(), c.y()) == (0, 20)
    assert (d.x(), d.y()) == (150, 20)
    assert layout.heightForWidth(250) == 40
    assert layout.heightForWidth(400) == 20
    assert layout.minimumSize().width() == 100


def test_a_separator_at_the_end_of_a_row_is_left_out(qtbot):
    holder, layout, (a, line, b) = buttons_in_a_flow(qtbot, "a", "separator", "b")
    holder.resize(150, layout.heightForWidth(150))
    qtbot.waitUntil(lambda: b.geometry().top() == 20)
    assert not holder.rect().intersects(line.geometry())
    holder.resize(300, layout.heightForWidth(300))
    qtbot.waitUntil(lambda: b.geometry().top() == 0)
    assert (line.x(), b.x()) == (100, 102)


def test_a_hidden_widget_takes_no_room(qtbot):
    holder, layout, (a, b, c) = buttons_in_a_flow(qtbot, "a", "b", "c")
    b.hide()
    holder.resize(200, layout.heightForWidth(200))
    qtbot.waitUntil(lambda: c.geometry().left() == 100)
    assert layout.heightForWidth(200) == 20
