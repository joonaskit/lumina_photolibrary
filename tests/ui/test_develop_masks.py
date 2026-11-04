"""Local adjustments in the Develop view: the list, the sliders, and the handles on the photo."""

import pytest
from conftest import TIMEOUT
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt

from lumina.core.edit.params import Local, Mask, Params
from lumina.ui import mask_frame
from lumina.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot, service, drive):
    made = MainWindow(service)
    qtbot.addWidget(made)
    made.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        made.browser.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: made.model.rowCount() == 3, timeout=TIMEOUT)
    return made


def open_editor(qtbot, window, name="a.jpg"):
    row = next(r for r in range(window.model.rowCount()) if window.model.name(r) == name)
    window.grid.setCurrentIndex(window.model.index(row))
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.nav.edit_current()


def panel_of(window):
    return window.develop.masks_panel


def place(view, share_x, share_y):
    target = view.image_rect()
    return QPointF(
        target.left() + share_x * target.width(), target.top() + share_y * target.height()
    )


# The list


def test_adding_a_mask_commits_it_and_chooses_it(qtbot, window):
    open_editor(qtbot, window)
    panel = panel_of(window)

    panel.add_buttons["linear"].click()

    (mask,) = window.session.params.masks
    assert mask.kind == "linear" and mask.local == Local()
    assert panel.selected == 0 and panel.list.count() == 1
    assert panel.list.item(0).text() == "Linear 1"
    assert window.develop.image.mask == mask

    panel.add_buttons["radial"].click()
    panel.add_buttons["linear"].click()
    assert [panel.list.item(i).text() for i in range(3)] == ["Linear 1", "Radial 1", "Linear 2"]
    assert panel.selected == 2

    window.session.undo()
    assert len(window.session.params.masks) == 2


def test_a_slider_changes_the_chosen_mask_and_one_step_undoes_it(qtbot, window):
    open_editor(qtbot, window)
    panel = panel_of(window)
    panel.add_buttons["radial"].click()
    slider = panel.sliders["exposure"]

    slider.slider.setSliderDown(True)
    slider.slider.setValue(-150)
    assert window.session.params.masks[0].local.exposure == -1.5  # previewed
    slider.slider.setSliderDown(False)

    assert window.session.params.masks[0].local.exposure == -1.5
    window.session.undo()
    assert window.session.params.masks[0].local.exposure == 0.0


def test_the_controls_follow_the_mask_chosen(qtbot, window):
    open_editor(qtbot, window)
    panel = panel_of(window)
    window.session.commit(
        Params(
            masks=(
                Mask("linear", local=Local(exposure=1.0)),
                Mask("radial", feather=20, invert=True, local=Local(saturation=-40)),
            )
        )
    )
    panel.select(0)
    assert panel.sliders["exposure"].value() == 1.0 and not panel.feather.isVisibleTo(panel)
    assert not panel.invert_box.isChecked()
    panel.select(1)
    assert panel.sliders["saturation"].value() == -40
    assert panel.feather.isVisibleTo(panel) and panel.feather.value() == 20
    assert panel.invert_box.isChecked()


def test_nothing_is_adjusted_without_a_mask_chosen(qtbot, window):
    open_editor(qtbot, window)
    panel = panel_of(window)
    assert panel.mask() is None
    assert not panel.sliders["exposure"].isEnabled() and not panel.delete_button.isEnabled()


def test_a_mask_is_switched_off_inverted_and_deleted(qtbot, window):
    open_editor(qtbot, window)
    panel = panel_of(window)
    panel.add_buttons["linear"].click()
    panel.add_buttons["radial"].click()

    panel.list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert not window.session.params.masks[0].enabled

    panel.select(1)
    panel.invert_box.click()
    assert window.session.params.masks[1].invert

    panel.delete_button.click()
    assert [m.kind for m in window.session.params.masks] == ["linear"]
    assert panel.selected == 0
    panel.delete_button.click()
    assert window.session.params.masks == () and panel.selected is None


def test_an_edit_keeps_at_most_the_most_masks(qtbot, window):
    from lumina.core.edit.params import MAX_MASKS

    open_editor(qtbot, window)
    panel = panel_of(window)
    for _ in range(MAX_MASKS + 2):
        panel.add_buttons["radial"].click()
    assert len(window.session.params.masks) == MAX_MASKS
    assert not panel.add_buttons["radial"].isEnabled()


def test_the_mask_that_is_chosen_follows_an_undo(qtbot, window):
    open_editor(qtbot, window)
    panel = panel_of(window)
    panel.add_buttons["linear"].click()
    window.session.undo()
    assert panel.selected is None and panel.list.count() == 0 and window.develop.image.mask is None


# On the photo


def test_dragging_a_handle_moves_the_mask_and_commits_once(qtbot, window):
    open_editor(qtbot, window)
    panel = panel_of(window)
    panel.add_buttons["linear"].click()
    view = window.develop.image
    start = mask_frame.points(view.mask, view.image_rect())["start"]
    end = place(view, 0.2, 0.2)
    before = len(window.session._undo)

    with qtbot.waitSignal(view.mask_committed) as done:
        qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=start.toPoint())
        qtbot.mouseMove(view, place(view, 0.3, 0.3).toPoint())
        qtbot.mouseMove(view, end.toPoint())
        qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end.toPoint())

    x0, y0, x1, y1 = window.session.params.masks[0].shape
    assert (x0, y0) == pytest.approx((0.2, 0.2), abs=0.02) and (x1, y1) == (0.5, 0.65)
    assert done.args[0] == window.session.params.masks[0].shape
    assert len(window.session._undo) == before + 1


def test_the_handles_are_gone_while_the_crop_is_set(qtbot, window):
    open_editor(qtbot, window)
    panel_of(window).add_buttons["radial"].click()
    assert window.develop.image.mask is not None
    window.develop.set_cropping(True)
    assert window.develop.image.mask is None
    window.develop.set_cropping(False)
    assert window.develop.image.mask is not None


def test_o_toggles_the_tint(qtbot, window):
    open_editor(qtbot, window)
    panel_of(window).add_buttons["radial"].click()
    view = window.develop.image
    assert view.mask_overlay
    window.develop.image.setFocus()
    qtbot.keyClick(view, Qt.Key.Key_O)
    assert not view.mask_overlay and not panel_of(window).overlay_box.isChecked()
    qtbot.keyClick(view, Qt.Key.Key_O)
    assert view.mask_overlay


# The handles' geometry

RECT = QRectF(10, 20, 400, 200)


def test_the_handles_of_a_linear_mask_are_its_two_points():
    mask = Mask("linear", (0.25, 0.5, 0.75, 0.5))
    handles = mask_frame.points(mask, RECT)
    assert handles["start"] == QPointF(110, 120) and handles["end"] == QPointF(310, 120)


def test_the_handles_of_a_radial_mask_turn_with_it():
    mask = Mask("radial", (0.5, 0.5, 0.25, 0.1, 90.0))
    handles = mask_frame.points(mask, RECT)
    assert handles["centre"] == QPointF(210, 120)
    # The first radius points down on the screen, the second to the left; a share of the
    # longer side (400) each
    assert handles["rx"].x() == pytest.approx(210) and handles["rx"].y() == pytest.approx(220)
    assert handles["ry"].x() == pytest.approx(170) and handles["ry"].y() == pytest.approx(120)


def test_a_click_takes_a_handle_then_the_body_then_nothing():
    mask = Mask("radial", (0.5, 0.5, 0.25, 0.25, 0.0))
    assert mask_frame.hit(mask, QPointF(212, 121), RECT) == "centre"
    assert mask_frame.hit(mask, QPointF(210 + 50, 120), RECT) == "move"
    assert mask_frame.hit(mask, QPointF(210 + 100, 120), RECT) == "rx"
    assert mask_frame.hit(mask, QPointF(20, 30), RECT) is None
    line = Mask("linear", (0.5, 0.25, 0.5, 0.75))
    assert mask_frame.hit(line, QPointF(210, 120), RECT) == "move"
    assert mask_frame.hit(line, QPointF(210, 30), RECT) is None
    assert mask_frame.hit(line, QPointF(210, 70), RECT) == "start"


def test_dragging_the_parts_of_a_mask():
    line = Mask("linear", (0.5, 0.25, 0.5, 0.75))
    moved = mask_frame.dragged(line, "move", QPointF(0, 0), QPointF(40, 20), RECT)
    assert moved.shape == pytest.approx((0.6, 0.35, 0.6, 0.85))
    # Not past the edge of the room the handles have: the line keeps its length
    far = mask_frame.dragged(line, "move", QPointF(0, 0), QPointF(0, 5000), RECT)
    assert far.shape[3] - far.shape[1] == pytest.approx(0.5)
    end = mask_frame.dragged(line, "end", QPointF(0, 0), QPointF(110, 70), RECT)
    assert end.shape == pytest.approx((0.5, 0.25, 0.25, 0.25))

    ellipse = Mask("radial", (0.5, 0.5, 0.25, 0.25, 0.0))
    # The first radius's handle sets the radius and the angle together
    grown = mask_frame.dragged(ellipse, "rx", QPointF(0, 0), QPointF(210, 120 + 200), RECT)
    assert grown.shape[2] == pytest.approx(0.5) and grown.shape[4] == pytest.approx(90.0)
    taller = mask_frame.dragged(ellipse, "ry", QPointF(0, 0), QPointF(210, 120 + 80), RECT)
    assert taller.shape[3] == pytest.approx(0.2) and taller.shape[2] == 0.25
    centred = mask_frame.dragged(ellipse, "centre", QPointF(210, 120), QPointF(210 + 40, 120), RECT)
    assert centred.shape[0] == pytest.approx(0.6)


def test_clicking_the_chosen_mask_again_chooses_none(qtbot, window):
    open_editor(qtbot, window)
    panel = panel_of(window)
    panel.add_buttons["linear"].click()
    panel.add_buttons["radial"].click()
    view = window.develop.image
    assert panel.selected == 1 and view.mask is not None
    row = panel.list.visualItemRect(panel.list.item(1))
    name = row.center() + QPoint(40, 0)  # on the name, not the check box

    qtbot.mouseClick(panel.list.viewport(), Qt.MouseButton.LeftButton, pos=name)

    assert panel.selected is None and view.mask is None
    assert panel.list.currentRow() == -1 and not panel.sliders["exposure"].isEnabled()
    assert window.session.params.masks[1].enabled  # nothing else changed

    # Clicking a row chooses it; the check box switches it, and does not unchoose it
    qtbot.mouseClick(panel.list.viewport(), Qt.MouseButton.LeftButton, pos=name)
    assert panel.selected == 1 and view.mask is not None
    box = row.topLeft() + QPoint(14, row.height() // 2)
    qtbot.mouseClick(panel.list.viewport(), Qt.MouseButton.LeftButton, pos=box)
    assert panel.selected == 1 and not window.session.params.masks[1].enabled
