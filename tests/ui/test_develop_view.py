import os

import pytest
from conftest import TIMEOUT
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtWidgets import QMessageBox

from lumina.qt_models.develop import (
    Band,
    Basic,
    Colour,
    Crop,
    Grading,
    Grain,
    Hsl,
    NoiseReduction,
    Params,
    Presence,
    Sharpening,
    Vignette,
    Wheel,
)
from lumina.ui import develop_view, settings
from lumina.ui.controls import ValueSlider
from lumina.ui.curve_editor import CurveEditor
from lumina.ui.export_dialog import ExportDialog
from lumina.ui.main_window import MainWindow
from lumina.ui.preset_dialog import PresetDialog
from lumina.ui.ratio_dialog import RatioDialog


@pytest.fixture
def window(qtbot, service, drive):
    made = MainWindow(service)
    qtbot.addWidget(made)
    made.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        made.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: made.model.rowCount() == 3, timeout=TIMEOUT)
    return made


def row_of(window, name):
    return next(r for r in range(window.model.rowCount()) if window.model.name(r) == name)


def open_editor(qtbot, window, name="a.jpg"):
    window.grid.setCurrentIndex(window.model.index(row_of(window, name)))
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.edit_current()


# The slider


def test_a_slider_previews_while_dragged_and_commits_when_let_go(qtbot):
    slider = ValueSlider("Exposure", -5, 5)
    qtbot.addWidget(slider)
    moved, committed = [], []
    slider.moved.connect(moved.append)
    slider.committed.connect(committed.append)

    slider.slider.setSliderDown(True)
    slider.slider.setValue(150)
    slider.slider.setValue(200)
    assert moved == [1.5, 2.0] and committed == []
    slider.slider.setSliderDown(False)

    assert committed == [2.0]
    assert slider.value_label.text() == "+2.00"


def test_a_key_commits_once_it_settles(qtbot):
    slider = ValueSlider("Tint", -100, 100, step=1, decimals=0)
    qtbot.addWidget(slider)

    with qtbot.waitSignal(slider.committed, timeout=TIMEOUT) as done:
        slider.slider.triggerAction(slider.slider.SliderAction.SliderSingleStepAdd)

    assert done.args == [1.0]


def test_a_double_click_resets(qtbot):
    slider = ValueSlider("Exposure", -5, 5)
    qtbot.addWidget(slider)
    slider.set_value(1.25)

    with qtbot.waitSignal(slider.committed) as done:
        qtbot.mouseDClick(slider.slider, Qt.MouseButton.LeftButton)

    assert done.args == [0.0] and slider.value() == 0.0


# The curve


@pytest.fixture
def curve(qtbot):
    editor = CurveEditor()
    qtbot.addWidget(editor)
    editor.resize(216, 216)
    editor.show()
    return editor


def at(editor, x, y):
    point = editor._to_screen(x, y)
    return QPoint(round(point.x()), round(point.y()))


def test_a_click_adds_a_point_on_the_curve_and_a_drag_moves_it(qtbot, curve):
    committed = []
    curve.committed.connect(committed.append)

    qtbot.mousePress(curve, Qt.MouseButton.LeftButton, pos=at(curve, 0.5, 0.5))
    qtbot.mouseMove(curve, at(curve, 0.5, 0.7))
    qtbot.mouseRelease(curve, Qt.MouseButton.LeftButton, pos=at(curve, 0.5, 0.7))

    assert len(committed) == 1
    (x0, y0), (x, y), (x1, y1) = committed[0]
    assert (x0, y0, x1, y1) == (0, 0, 1, 1)
    assert x == pytest.approx(0.5, abs=0.02) and y == pytest.approx(0.7, abs=0.02)


def test_end_points_move_only_up_and_down(qtbot, curve):
    committed = []
    curve.committed.connect(committed.append)

    qtbot.mousePress(curve, Qt.MouseButton.LeftButton, pos=at(curve, 0, 0))
    qtbot.mouseMove(curve, at(curve, 0.3, 0.2))
    qtbot.mouseRelease(curve, Qt.MouseButton.LeftButton, pos=at(curve, 0.3, 0.2))

    first = committed[-1][0]
    assert first[0] == 0 and first[1] == pytest.approx(0.2, abs=0.02)


def test_a_point_is_removed_by_a_double_click_or_dragging_it_away(qtbot, curve):
    curve.set_curve("rgb", ((0, 0), (0.3, 0.4), (0.7, 0.6), (1, 1)))
    committed = []
    curve.committed.connect(committed.append)

    qtbot.mouseDClick(curve, Qt.MouseButton.LeftButton, pos=at(curve, 0.3, 0.4))
    assert committed[-1] == ((0, 0), (0.7, 0.6), (1, 1))

    qtbot.mousePress(curve, Qt.MouseButton.LeftButton, pos=at(curve, 0.7, 0.6))
    qtbot.mouseMove(curve, QPoint(at(curve, 0.7, 0.6).x(), curve.height() + 60))
    qtbot.mouseRelease(curve, Qt.MouseButton.LeftButton)
    assert committed[-1] == ((0, 0), (1, 1))


def test_a_point_cannot_pass_its_neighbour(qtbot, curve):
    curve.set_curve("rgb", ((0, 0), (0.3, 0.3), (0.6, 0.6), (1, 1)))
    committed = []
    curve.committed.connect(committed.append)

    qtbot.mousePress(curve, Qt.MouseButton.LeftButton, pos=at(curve, 0.3, 0.3))
    qtbot.mouseMove(curve, at(curve, 0.9, 0.3))
    qtbot.mouseRelease(curve, Qt.MouseButton.LeftButton, pos=at(curve, 0.9, 0.3))

    xs = [p[0] for p in committed[-1]]
    assert xs == sorted(xs) and xs[1] < 0.6


# The crop


def test_dragging_the_crop():
    whole = Crop()

    assert develop_view.dragged(whole, "tl", 0.1, 0.2) == Crop(0.1, 0.2, 1, 1)
    assert develop_view.dragged(whole, "r", -0.25, 0.5) == Crop(0, 0, 0.75, 1)
    # Moving stays inside the photo
    assert develop_view.dragged(Crop(0.2, 0.2, 0.6, 0.6), "move", 0.9, -0.9) == Crop(
        0.6, 0.0, 1.0, 0.4
    )
    # An edge never passes the other one
    shrunk = develop_view.dragged(Crop(0.2, 0.2, 0.6, 0.6), "l", 0.9, 0)
    assert shrunk.left < shrunk.right


def test_rotating_keeps_the_crop_on_the_same_part_of_the_photo(qtbot, window):
    open_editor(qtbot, window)
    window.session.commit(Params(crop=Crop(0.1, 0.2, 0.5, 0.6)))

    window.develop.rotate_right.click()
    assert window.session.params.rotate == 1
    assert window.session.params.crop == Crop(0.4, 0.1, 0.8, 0.5)

    window.develop.rotate_left.click()
    assert window.session.params.crop == Crop(0.1, 0.2, 0.5, 0.6)


# The window


def test_edit_opens_the_develop_view_and_back_leaves_it(qtbot, window):
    open_editor(qtbot, window)

    assert window.pages.currentIndex() == 3
    assert window.editing.isVisible()
    assert window.develop.image.pixmap() is not None

    window.go_back()
    assert window.pages.currentIndex() == 1
    assert not window.session.is_open()


def test_an_edit_made_shows_in_the_grid(qtbot, window):
    open_editor(qtbot, window)
    window.develop.exposure.slider.setValue(100)
    with qtbot.waitSignal(window.develop.exposure.committed, timeout=TIMEOUT):
        pass
    window.go_back()

    row = row_of(window, "a.jpg")
    qtbot.waitUntil(lambda: window.model.edit(row) is not None, timeout=TIMEOUT)
    window.model.data(window.model.index(row), Qt.ItemDataRole.DecorationRole)
    qtbot.waitUntil(lambda: window.model.thumbnail_pixmap(row) is not None, timeout=TIMEOUT)


def test_undo_and_redo_from_the_toolbar(qtbot, window):
    open_editor(qtbot, window)
    assert not window.undo_action.isEnabled()

    window.session.commit(Params(exposure=1.0))
    assert window.undo_action.isEnabled()
    window.undo_action.trigger()

    assert window.session.params == Params()
    assert window.redo_action.isEnabled()
    assert window.develop.exposure.value() == 0.0


def test_escape_leaves_the_crop_before_the_view(qtbot, window):
    open_editor(qtbot, window)
    window.develop.set_cropping(True)

    window.go_back()
    assert window.pages.currentIndex() == 3 and not window.develop.crop_button.isChecked()
    window.go_back()
    assert window.pages.currentIndex() == 1


def test_the_arrows_step_to_the_next_photo_to_edit(qtbot, window):
    open_editor(qtbot, window, "a.jpg")
    before = window.session.photo_id

    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.step(1)

    assert window.session.photo_id != before


# Export


def test_the_dialog_never_suggests_the_original(qtbot, drive):
    original = drive / "Photos" / "a.jpg"
    dialog = ExportDialog(str(original))
    qtbot.addWidget(dialog)

    dialog.folder.setText(str(drive / "Photos"))

    assert dialog.name.text() == "a-edit.jpg"
    dialog.format.setCurrentIndex(dialog.format.findData("png"))
    assert dialog.name.text() == "a.png"


def test_the_depth_follows_the_format(qtbot, drive):
    dialog = ExportDialog(str(drive / "Photos" / "a.jpg"))
    qtbot.addWidget(dialog)

    dialog.format.setCurrentIndex(dialog.format.findData("tiff"))
    dialog.bits.setCurrentIndex(dialog.bits.findData(16))
    assert dialog.options().bits == 16
    dialog.format.setCurrentIndex(dialog.format.findData("jpeg"))
    assert dialog.options().bits == 8 and not dialog.bits.isEnabled()


def test_export_asks_before_replacing(qtbot, window, tmp_path, monkeypatch):
    open_editor(qtbot, window)
    destination = tmp_path / "out.jpg"
    destination.write_bytes(b"keep me")
    monkeypatch.setattr(ExportDialog, "exec", lambda self: ExportDialog.DialogCode.Accepted)
    monkeypatch.setattr(ExportDialog, "destination", lambda self: str(destination))
    asked = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Cancel,
    )

    window.export_photo()

    assert asked and destination.read_bytes() == b"keep me"

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(window.session.exported, timeout=TIMEOUT) as done:
        window.export_photo()
    assert done.args[1] is None
    assert os.path.getsize(destination) > 100


def test_a_photo_on_an_unplugged_drive_cannot_be_edited(qtbot, window, mounts, monkeypatch):
    mounts.plugged = False
    qtbot.waitUntil(lambda: window.model.path(0) is None, timeout=TIMEOUT)
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: told.append(a[2]))
    window.grid.setCurrentIndex(window.model.index(0))

    window.edit_current()

    assert "not connected" in told[0]
    assert window.pages.currentIndex() == 1


def test_the_crop_frame_follows_the_mouse(qtbot, window):
    open_editor(qtbot, window)
    window.develop.set_cropping(True)
    view = window.develop.image
    qtbot.waitUntil(lambda: view.image_rect() is not None, timeout=TIMEOUT)
    target = view.image_rect()
    corner = target.topLeft()
    end = QPointF(corner.x() + target.width() * 0.25, corner.y() + target.height() * 0.25)

    with qtbot.waitSignal(view.crop_committed) as done:
        qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=corner.toPoint())
        qtbot.mouseMove(view, end.toPoint())
        qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end.toPoint())

    crop = done.args[0]
    assert crop.left == pytest.approx(0.25, abs=0.02) and crop.top == pytest.approx(0.25, abs=0.02)
    assert window.session.params.crop == crop


# Flip, straighten, and the crop's keys


def press(qtbot, window, key):
    window.develop.image.setFocus()
    qtbot.keyClick(window.develop.image, key)


def test_c_opens_the_crop_and_accepts_it(qtbot, window):
    open_editor(qtbot, window)

    press(qtbot, window, Qt.Key.Key_C)
    assert window.develop.is_cropping()
    press(qtbot, window, Qt.Key.Key_C)
    assert not window.develop.is_cropping()
    assert window.pages.currentIndex() == 3


def test_enter_accepts_the_crop_with_its_changes(qtbot, window):
    open_editor(qtbot, window)
    press(qtbot, window, Qt.Key.Key_C)
    changed = window.session.params.with_changes(crop=Crop(0.1, 0.1, 0.8, 0.9), straighten=3.0)
    window.session.commit(changed)

    press(qtbot, window, Qt.Key.Key_Return)

    assert not window.develop.is_cropping()
    assert window.session.params == changed
    assert window.pages.currentIndex() == 3


def test_escape_cancels_the_crop_and_the_straightening(qtbot, window):
    open_editor(qtbot, window)
    window.session.commit(Params(exposure=0.5, crop=Crop(0.2, 0.2, 0.8, 0.8)))
    press(qtbot, window, Qt.Key.Key_C)
    window.session.commit(window.session.params.with_changes(crop=Crop(), straighten=-4.0))

    press(qtbot, window, Qt.Key.Key_Escape)

    assert not window.develop.is_cropping()
    assert window.pages.currentIndex() == 3
    assert window.session.params == Params(exposure=0.5, crop=Crop(0.2, 0.2, 0.8, 0.8))
    # The cancel is a step of its own: undo brings back what was cancelled
    window.session.undo()
    assert window.session.params.straighten == -4.0


def test_enter_does_nothing_outside_the_crop(qtbot, window):
    open_editor(qtbot, window)

    press(qtbot, window, Qt.Key.Key_Return)

    assert not window.develop.is_cropping() and window.pages.currentIndex() == 3


def test_flip_buttons_mirror_the_photo_and_its_crop(qtbot, window):
    open_editor(qtbot, window)
    window.session.commit(Params(straighten=2.0, crop=Crop(0.1, 0.2, 0.5, 0.6)))

    window.develop.flip_horizontal.click()
    assert window.session.params.flip_horizontal
    assert window.session.params.crop == Crop(0.5, 0.2, 0.9, 0.6)
    assert window.session.params.straighten == -2.0

    window.develop.flip_vertical.click()
    assert window.session.params.flip_vertical
    assert window.session.params.crop == Crop(0.5, 0.4, 0.9, 0.8)


def test_the_straighten_slider_commits_degrees(qtbot, window):
    open_editor(qtbot, window)
    slider = window.develop.straighten

    slider.slider.setSliderDown(True)
    slider.slider.setValue(-35)
    slider.slider.setSliderDown(False)

    assert window.session.params.straighten == -3.5
    assert slider.value_label.text() == "-3.5°"


def test_reset_crop_also_straightens(qtbot, window):
    open_editor(qtbot, window)
    window.session.commit(Params(straighten=5.0, crop=Crop(0.1, 0.1, 0.9, 0.9)))

    window.develop.reset_crop.click()

    assert window.session.params == Params()


def test_the_basic_sliders_commit_their_control_and_show_the_edit(qtbot, window):
    open_editor(qtbot, window)
    window.session.commit(Params(exposure=0.5, basic=Basic(contrast=10)))
    slider = window.develop.basic["highlights"]

    slider.slider.setSliderDown(True)
    slider.slider.setValue(-40)
    slider.slider.setSliderDown(False)

    assert window.session.params == Params(exposure=0.5, basic=Basic(contrast=10, highlights=-40))
    assert slider.value_label.text() == "-40"
    window.undo_action.trigger()
    assert window.develop.basic["highlights"].value() == 0
    assert window.develop.basic["contrast"].value() == 10


def test_a_saved_preset_keeps_the_basic_controls(qtbot, window, monkeypatch):
    open_editor(qtbot, window)
    window.session.commit(Params(exposure=1.0, basic=Basic(shadows=30, blacks=-20)))
    save_preset_as(monkeypatch, window, "Lifted")
    window.session.commit(Params())

    window.session.apply_preset("Lifted")

    assert window.session.params == Params(basic=Basic(shadows=30, blacks=-20))


def test_the_colour_sliders_commit_their_control_and_show_the_edit(qtbot, window):
    open_editor(qtbot, window)
    window.session.commit(Params(exposure=0.5, colour=Colour(saturation=10)))
    slider = window.develop.colour["vibrance"]

    slider.slider.setSliderDown(True)
    slider.slider.setValue(35)
    slider.slider.setSliderDown(False)

    assert window.session.params == Params(exposure=0.5, colour=Colour(vibrance=35, saturation=10))
    assert slider.value_label.text() == "+35"
    window.undo_action.trigger()
    assert window.develop.colour["vibrance"].value() == 0
    assert window.develop.colour["saturation"].value() == 10


def test_a_saved_preset_keeps_the_colour_controls(qtbot, window, monkeypatch):
    open_editor(qtbot, window)
    window.session.commit(Params(exposure=1.0, colour=Colour(vibrance=40, saturation=-10)))
    save_preset_as(monkeypatch, window, "Vivid")
    window.session.commit(Params())

    window.session.apply_preset("Vivid")

    assert window.session.params == Params(colour=Colour(vibrance=40, saturation=-10))


def drag(slider, value):
    slider.slider.setSliderDown(True)
    slider.slider.setValue(value)
    slider.slider.setSliderDown(False)


def save_preset_as(monkeypatch, window, name, sections=None):
    """Save the edit as a preset with the dialog filled in (with `sections` ticked, or
    as it opens) and Save pressed."""

    def fill_in(dialog):
        dialog.name_edit.setText(name)
        if sections is not None:
            dialog.set_sections(sections)
        dialog.save_button.click()
        return dialog.result()

    monkeypatch.setattr(PresetDialog, "exec", fill_in)
    window.develop.save_preset()


def settled(qtbot, window):
    # The saves of the edits have refreshed the grid: nothing is left to arrive when the
    # window and its database are closed
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)


def choose(group, name):
    for button in group.buttons():
        if button.property("choice") == name:
            button.click()
            return
    raise AssertionError(name)


def test_the_hsl_sliders_work_on_the_band_chosen(qtbot, window):
    open_editor(qtbot, window)
    drag(window.develop.hsl["saturation"], -30)
    assert window.session.params.hsl == Hsl(red=Band(saturation=-30))

    choose(window.develop.band_group, "blue")
    # The sliders show the band now chosen, which has nothing yet
    assert window.develop.hsl["saturation"].value() == 0
    drag(window.develop.hsl["hue"], 40)
    drag(window.develop.hsl["luminance"], -15)

    assert window.session.params.hsl == Hsl(
        red=Band(saturation=-30), blue=Band(hue=40, luminance=-15)
    )
    choose(window.develop.band_group, "red")
    assert window.develop.hsl["saturation"].value() == -30
    assert window.develop.hsl["hue"].value() == 0
    window.undo_action.trigger()
    window.undo_action.trigger()
    assert window.develop.hsl["hue"].value() == 0
    assert window.session.params.hsl == Hsl(red=Band(saturation=-30))
    settled(qtbot, window)


def test_the_grading_sliders_work_on_the_range_chosen_and_show_the_edit(qtbot, window):
    open_editor(qtbot, window)
    assert window.develop.blending.value() == 50
    drag(window.develop.wheel["hue"], 220)
    drag(window.develop.wheel["saturation"], 35)
    choose(window.develop.range_group, "highlights")
    drag(window.develop.wheel["luminance"], 20)
    drag(window.develop.blending, 80)
    drag(window.develop.balance, -25)

    assert window.session.params.grading == Grading(
        shadows=Wheel(hue=220, saturation=35),
        highlights=Wheel(luminance=20),
        blending=80,
        balance=-25,
    )
    assert window.develop.wheel["hue"].value_label.text() == "0°"
    choose(window.develop.range_group, "shadows")
    assert window.develop.wheel["hue"].value() == 220
    assert window.develop.wheel["hue"].value_label.text() == "220°"
    assert window.develop.blending.value() == 80
    settled(qtbot, window)


def test_a_saved_preset_keeps_the_hsl_bands_and_grading(qtbot, window, monkeypatch):
    open_editor(qtbot, window)
    look = Params(
        hsl=Hsl(green=Band(hue=-30)), grading=Grading(midtones=Wheel(hue=30, saturation=20))
    )
    window.session.commit(look.with_changes(exposure=1.0))
    save_preset_as(monkeypatch, window, "Graded")
    window.session.commit(Params())

    window.session.apply_preset("Graded")

    assert window.session.params == look


def test_each_band_button_is_marked_with_its_colour(qtbot, window):
    open_editor(qtbot, window)
    buttons = window.develop.band_group.buttons()

    hues = [round(button.colour.hueF() * 360) for button in buttons]

    assert hues == [0, 30, 60, 120, 180, 240, 270, 300]
    assert all(button.colour.isValid() for button in buttons)
    # The grading ranges have no colour of their own
    assert all(button.colour is None for button in window.develop.range_group.buttons())
    window.develop.grab()


def test_the_preset_dialog_keeps_what_is_ticked_and_the_choice_next_time(
    qtbot, window, monkeypatch
):
    open_editor(qtbot, window)
    look = Params(
        exposure=1.0,
        temperature=20,
        colour=Colour(vibrance=30),
        crop=Crop(left=0.1),
        hsl=Hsl(red=Band(hue=10)),
    )
    window.session.commit(look)

    save_preset_as(monkeypatch, window, "Exposure and crop", ["exposure", "crop"])
    window.session.commit(Params())
    window.session.apply_preset("Exposure and crop")

    assert window.session.params == Params(exposure=1.0, crop=Crop(left=0.1))
    # The next dialog starts with the same parts ticked
    dialog = PresetDialog()
    qtbot.addWidget(dialog)
    assert dialog.sections() == ["exposure", "crop"]
    settled(qtbot, window)


def test_the_preset_dialog_starts_with_the_look_and_not_the_framing(qtbot):
    dialog = PresetDialog()
    qtbot.addWidget(dialog)

    assert dialog.sections() == [
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


def test_the_preset_dialog_needs_a_name_and_a_part_and_says_when_it_replaces(qtbot):
    dialog = PresetDialog(["Warm"])
    qtbot.addWidget(dialog)
    assert not dialog.save_button.isEnabled()

    dialog.name_edit.setText("  Cool ")
    assert dialog.save_button.isEnabled() and dialog.name() == "Cool"
    assert dialog.save_button.text() == "Save"
    dialog.name_edit.setText("warm")
    assert dialog.save_button.text() == "Replace"
    dialog.set_sections([])
    assert not dialog.save_button.isEnabled()


def test_the_effects_sliders_commit_their_control_and_show_the_edit(qtbot, window):
    open_editor(qtbot, window)
    assert window.develop.vignette["midpoint"].value() == 50
    assert window.develop.grain["size"].value() == 25

    drag(window.develop.vignette["amount"], -40)
    drag(window.develop.vignette["feather"], 70)
    drag(window.develop.grain["amount"], 35)
    drag(window.develop.grain["size"], 60)

    assert window.session.params == Params(
        vignette=Vignette(amount=-40, feather=70), grain=Grain(amount=35, size=60)
    )
    assert window.develop.vignette["amount"].value_label.text() == "-40"
    window.undo_action.trigger()
    assert window.develop.grain["size"].value() == 25
    assert window.develop.grain["amount"].value() == 35
    settled(qtbot, window)


def test_a_saved_preset_can_keep_the_effects(qtbot, window, monkeypatch):
    open_editor(qtbot, window)
    look = Params(vignette=Vignette(amount=-30), grain=Grain(amount=20, size=40))
    window.session.commit(look.with_changes(exposure=1.0))
    save_preset_as(monkeypatch, window, "Filmic", ["vignette", "grain"])
    window.session.commit(Params())

    window.session.apply_preset("Filmic")

    assert window.session.params == look
    settled(qtbot, window)


# The crop's aspect ratio


def crop_shape(window):
    """The crop's width over its height in pixels, on the photo (300 x 200 here)."""
    crop = window.session.params.crop
    aspect = window.session.photo_aspect()
    return (crop.right - crop.left) * aspect / (crop.bottom - crop.top)


def choose_aspect(window, text):
    combo = window.develop.aspect
    combo.setCurrentIndex(combo.findText(text))
    combo.activated.emit(combo.currentIndex())


def test_choosing_an_aspect_ratio_opens_the_crop_and_shapes_it(qtbot, window):
    open_editor(qtbot, window)
    assert window.session.photo_aspect() == pytest.approx(1.5)
    assert not window.develop.swap_ratio.isEnabled()

    choose_aspect(window, "1:1")

    assert window.develop.is_cropping()
    assert crop_shape(window) == pytest.approx(1.0, rel=1e-3)
    assert window.develop.lock_ratio.isChecked()
    assert window.develop.swap_ratio.isEnabled()
    choose_aspect(window, "16:9")
    assert crop_shape(window) == pytest.approx(16 / 9, rel=1e-3)
    choose_aspect(window, "Original")
    assert crop_shape(window) == pytest.approx(1.5, rel=1e-3)
    settled(qtbot, window)


def test_the_ratio_follows_the_orientation_of_the_photo_and_can_be_swapped(qtbot, window):
    open_editor(qtbot, window)
    choose_aspect(window, "4:5")
    # A wide photo gets the wide frame: 5 wide, 4 high
    assert crop_shape(window) == pytest.approx(5 / 4, rel=1e-3)

    window.develop.swap_ratio.click()
    assert crop_shape(window) == pytest.approx(4 / 5, rel=1e-3)
    window.develop.swap_ratio.click()
    assert crop_shape(window) == pytest.approx(5 / 4, rel=1e-3)
    settled(qtbot, window)


def test_turning_the_photo_turns_the_frame_and_the_ratio_with_it(qtbot, window):
    open_editor(qtbot, window)
    choose_aspect(window, "3:2")

    window.develop.rotate_right.click()

    assert window.session.photo_aspect() == pytest.approx(1 / 1.5)
    assert window.develop.image.ratio == pytest.approx(1 / 1.5, rel=1e-3)
    assert crop_shape(window) == pytest.approx(1 / 1.5, rel=1e-3)
    settled(qtbot, window)


def test_a_custom_ratio_is_asked_for(qtbot, window, monkeypatch):
    open_editor(qtbot, window)
    monkeypatch.setattr(RatioDialog, "exec", lambda self: RatioDialog.DialogCode.Accepted)
    monkeypatch.setattr(RatioDialog, "ratio", lambda self: (5, 7))

    choose_aspect(window, "Custom…")

    # Taller than wide, as typed, on a wide photo
    assert crop_shape(window) == pytest.approx(5 / 7, rel=1e-3)

    monkeypatch.setattr(RatioDialog, "exec", lambda self: RatioDialog.DialogCode.Rejected)
    window.develop.aspect.setCurrentIndex(window.develop.aspect.findText("1:1"))
    window.develop.aspect.activated.emit(window.develop.aspect.currentIndex())
    assert crop_shape(window) == pytest.approx(1.0, rel=1e-3)
    choose_aspect(window, "Custom…")
    # Cancelled: the choice before stays
    assert window.develop.aspect.currentText() == "1:1"
    assert crop_shape(window) == pytest.approx(1.0, rel=1e-3)
    settled(qtbot, window)


def test_free_lets_the_crop_go_and_the_lock_holds_its_current_shape(qtbot, window):
    open_editor(qtbot, window)
    choose_aspect(window, "1:1")
    choose_aspect(window, "Free")
    assert window.develop.image.ratio is None
    assert not window.develop.lock_ratio.isChecked()
    window.session.commit(window.session.params.with_changes(crop=Crop(0.1, 0.1, 0.7, 0.5)))
    shape = crop_shape(window)

    window.develop.lock_ratio.click()

    assert window.develop.lock_ratio.isChecked()
    assert window.develop.image.ratio == pytest.approx(shape, rel=1e-3)
    window.develop.lock_ratio.click()
    assert window.develop.image.ratio is None
    assert window.develop.aspect.currentText() == "Free"
    settled(qtbot, window)


def test_dragging_a_corner_with_a_ratio_keeps_the_shape(qtbot, window):
    open_editor(qtbot, window)
    choose_aspect(window, "1:1")
    view = window.develop.image
    qtbot.waitUntil(lambda: view.image_rect() is not None, timeout=TIMEOUT)
    target = view.image_rect()
    frame = view._frame(target)
    corner = frame.bottomRight()
    end = QPointF(corner.x() - target.width() * 0.1, corner.y() - target.height() * 0.05)

    with qtbot.waitSignal(view.crop_committed, timeout=TIMEOUT):
        qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=corner.toPoint())
        qtbot.mouseMove(view, end.toPoint())
        qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end.toPoint())

    assert crop_shape(window) == pytest.approx(1.0, rel=0.01)
    # Smaller than the frame it started as (a square, 1/6 to 5/6 across)
    assert window.session.params.crop.right < 5 / 6 - 0.01
    settled(qtbot, window)


# The fine grid while straightening


def test_the_fine_grid_shows_while_the_straighten_slider_moves(qtbot, window):
    open_editor(qtbot, window)
    view = window.develop.image
    slider = window.develop.straighten
    assert not view.grid

    slider.slider.setSliderDown(True)
    slider.slider.setValue(15)
    assert view.grid
    slider.slider.setSliderDown(False)

    assert not view.grid
    assert window.session.params.straighten == pytest.approx(1.5)
    # It paints without trouble
    view.set_grid(True)
    assert not view.grab().isNull()
    settled(qtbot, window)


# Before and after


def shown_centre(window):
    image = window.develop.image.pixmap().toImage()
    colour = image.pixelColor(image.width() // 2, image.height() // 2)
    return colour.red() + colour.green() + colour.blue()


def test_the_original_is_shown_while_the_button_is_down_and_the_edit_comes_back(qtbot, window):
    open_editor(qtbot, window)
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.session.commit(Params(exposure=1.0))
    edited = shown_centre(window)

    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.develop.before_button.click()
    original = shown_centre(window)

    assert window.session.is_before() and window.develop.image.before
    assert original < edited
    # Only the view: the edit and its controls are as they were
    assert window.session.params == Params(exposure=1.0)
    assert window.develop.exposure.value() == 1.0
    with qtbot.waitSignal(window.session.image_ready, timeout=TIMEOUT):
        window.develop.before_button.click()
    assert not window.session.is_before() and not window.develop.image.before
    assert shown_centre(window) == edited
    settled(qtbot, window)


def test_the_backslash_key_shows_the_original_while_held(qtbot, window):
    open_editor(qtbot, window)
    window.session.commit(Params(exposure=1.0))

    qtbot.keyPress(window.develop, Qt.Key.Key_Backslash)
    assert window.session.is_before()
    qtbot.keyRelease(window.develop, Qt.Key.Key_Backslash)
    assert not window.session.is_before()

    # With the button pressed in, letting go of the key leaves the original showing
    window.develop.before_button.click()
    qtbot.keyPress(window.develop, Qt.Key.Key_Backslash)
    qtbot.keyRelease(window.develop, Qt.Key.Key_Backslash)
    assert window.session.is_before()
    settled(qtbot, window)


def test_the_original_goes_with_the_photo(qtbot, window):
    open_editor(qtbot, window)
    qtbot.keyPress(window.develop, Qt.Key.Key_Backslash)
    assert window.session.is_before()

    window.session.close()

    assert not window.session.is_before() and not window.develop.image.before
    settled(qtbot, window)


def test_the_presence_sliders_commit_their_control_and_show_the_edit(qtbot, window):
    open_editor(qtbot, window)

    drag(window.develop.presence["clarity"], 35)
    drag(window.develop.presence["dehaze"], -20)

    assert window.session.params == Params(presence=Presence(clarity=35, dehaze=-20))
    assert window.develop.presence["clarity"].value_label.text() == "+35"
    window.undo_action.trigger()
    assert window.develop.presence["dehaze"].value() == 0
    assert window.develop.presence["clarity"].value() == 35
    settled(qtbot, window)


def test_a_saved_preset_can_keep_the_presence(qtbot, window, monkeypatch):
    open_editor(qtbot, window)
    look = Params(presence=Presence(texture=20, clarity=30, dehaze=10))
    window.session.commit(look.with_changes(exposure=1.0))
    save_preset_as(monkeypatch, window, "Crisp", ["presence"])
    window.session.commit(Params())

    window.session.apply_preset("Crisp")

    assert window.session.params == look
    settled(qtbot, window)


def test_the_sharpening_and_noise_reduction_sliders_commit_their_control(qtbot, window):
    open_editor(qtbot, window)
    assert window.develop.sharpening["radius"].value() == pytest.approx(1.0)
    assert window.develop.sharpening["detail"].value() == 25
    assert window.develop.noise["detail"].value() == 50

    drag(window.develop.sharpening["amount"], 70)
    drag(window.develop.sharpening["radius"], 15)
    drag(window.develop.sharpening["masking"], 40)
    drag(window.develop.noise["luminance"], 30)
    drag(window.develop.noise["colour"], 55)

    assert window.session.params == Params(
        sharpening=Sharpening(amount=70, radius=1.5, masking=40),
        noise=NoiseReduction(luminance=30, colour=55),
    )
    assert window.develop.sharpening["radius"].value_label.text() == "1.5 px"
    window.undo_action.trigger()
    assert window.develop.noise["colour"].value() == 0
    assert window.develop.noise["luminance"].value() == 30
    settled(qtbot, window)


def test_a_saved_preset_can_keep_sharpening_and_noise_reduction(qtbot, window, monkeypatch):
    open_editor(qtbot, window)
    look = Params(sharpening=Sharpening(amount=50, radius=0.8), noise=NoiseReduction(luminance=20))
    window.session.commit(look.with_changes(exposure=1.0))
    save_preset_as(monkeypatch, window, "Clean", ["sharpening", "noise"])
    window.session.commit(Params())

    window.session.apply_preset("Clean")

    assert window.session.params == look
    settled(qtbot, window)


# The sections of the panel: order, hidden, folded


def panel_order(view):
    root = view._panel.layout()
    names = {id(section): name for name, section in view._sections.items()}
    return [
        names[id(w)]
        for w in map(lambda i: root.itemAt(i).widget(), range(root.count()))
        if w and id(w) in names
    ]


def test_the_panel_follows_the_stored_order_and_hidden_sections(qtbot, window):
    view = window.develop
    assert tuple(panel_order(view)) == develop_view.SECTION_NAMES

    settings.set_develop_order(["matte", "light", "bogus"])  # default: presets first
    settings.set_hidden_sections(["hsl"])
    view.arrange()
    order = panel_order(view)
    # Unknown names go, the ones left out come back at their place in the default order
    # A section left out follows the one before it in the default order
    assert order[:3] == ["presets", "matte", "effects"]
    assert order.index("curve") == order.index("light") + 1
    assert order.index("presence") == order.index("curve") + 1
    assert "bogus" not in order and len(order) == len(develop_view.SECTION_NAMES)
    assert view._sections["hsl"].isHidden()
    assert not view._sections["light"].isHidden()

    settings.reset_develop_panel()
    view.arrange()
    assert tuple(panel_order(view)) == develop_view.SECTION_NAMES
    assert not view._sections["hsl"].isHidden()


def test_a_hidden_section_keeps_its_values_in_the_edit(qtbot, window):
    open_editor(qtbot, window)
    view = window.develop
    settings.set_hidden_sections(["light"])
    view.arrange()
    window.session.commit(window.session.params.with_changes(exposure=1.5))
    assert view._sections["light"].isHidden()
    assert window.session.params.exposure == 1.5
    assert view.exposure.value() == 1.5


def test_a_section_folds_and_the_fold_is_remembered(qtbot, window):
    view = window.develop
    section = view._sections["matte"]
    section.header.click()
    assert section.collapsed and section.content.isHidden()
    assert settings.collapsed_sections() == {"matte"}
    section.header.click()
    assert not section.collapsed
    assert settings.collapsed_sections() == set()

    settings.set_collapsed_sections(["effects", "hsl"])
    view.arrange()
    assert view._sections["effects"].collapsed and view._sections["hsl"].collapsed
    assert not view._sections["matte"].collapsed
    # Applying what is stored does not rewrite it
    assert settings.collapsed_sections() == {"effects", "hsl"}


def test_merged_order_puts_a_new_section_at_its_place():
    default = ("a", "b", "c", "d")
    assert settings.merged_order(["d", "a", "c", "x"], default) == ["d", "a", "b", "c"]
    assert settings.merged_order([], default) == list(default)
    assert settings.merged_order(["c", "a", "a"], default) == ["c", "d", "a", "b"]
