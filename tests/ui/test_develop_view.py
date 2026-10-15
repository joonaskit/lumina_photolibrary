import os

import pytest
from conftest import TIMEOUT
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtWidgets import QMessageBox

from lumina.qt_models.develop import Basic, Crop, Params
from lumina.ui import develop_view
from lumina.ui.controls import ValueSlider
from lumina.ui.curve_editor import CurveEditor
from lumina.ui.export_dialog import ExportDialog
from lumina.ui.main_window import MainWindow


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
    assert window.editing.isVisible() and not window.browsing.isVisible()
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
    monkeypatch.setattr(develop_view.QInputDialog, "getText", lambda *a, **k: ("Lifted", True))
    window.develop.save_preset()
    window.session.commit(Params())

    window.session.apply_preset("Lifted")

    assert window.session.params == Params(basic=Basic(shadows=30, blacks=-20))
