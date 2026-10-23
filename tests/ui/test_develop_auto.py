import pytest
from conftest import TIMEOUT
from PySide6.QtCore import QPoint, Qt

from lumina.core.edit.params import Params
from lumina.qt_models.develop import DevelopSession
from lumina.ui import develop_view


@pytest.fixture
def library(service, drive, qtbot):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    return service


def open_photo(library, session, qtbot):
    row = next(p for p in library.db.photos() if p.path.endswith("a.jpg"))
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT):
        session.open(row.id, library.resolve(row.volume_key, row.path))


@pytest.fixture
def session(library, qtbot):
    made = DevelopSession(library)
    open_photo(library, made, qtbot)
    yield made
    made.close()


@pytest.fixture
def view(library, qtbot):
    session = DevelopSession(library)
    made = develop_view.DevelopView(session)
    qtbot.addWidget(made)
    made.resize(1200, 800)
    made.show()
    open_photo(library, session, qtbot)
    qtbot.waitUntil(lambda: made.image.pixmap() is not None, timeout=TIMEOUT)
    yield made
    session.close()


def test_auto_tone_is_one_step_to_undo(session, qtbot):
    session.commit(Params(temperature=10.0))

    with qtbot.waitSignal(session.params_changed, timeout=TIMEOUT):
        session.auto_tone()

    after = session.params
    assert (after.exposure, after.basic) != (0.0, Params().basic)
    assert after.temperature == 10.0
    session.undo()
    assert session.params == Params(temperature=10.0)


def test_auto_white_balance_and_the_picker_set_the_white_balance(session, qtbot):
    with qtbot.waitSignal(session.params_changed, timeout=TIMEOUT):
        session.auto_white_balance()
    assert (session.params.temperature, session.params.tint) != (0.0, 0.0)
    session.undo()

    with qtbot.waitSignal(session.params_changed, timeout=TIMEOUT):
        session.pick_white_balance((0.5, 0.5))

    assert (session.params.temperature, session.params.tint) != (0.0, 0.0)
    session.undo()
    assert session.params == Params()


def test_the_buttons_in_the_panel(view, qtbot):
    with qtbot.waitSignal(view.session.params_changed, timeout=TIMEOUT):
        view.auto_white_balance.click()
    assert view.temperature.value() != 0 or view.tint.value() != 0

    view.as_shot.click()

    assert view.session.params.temperature == 0 and view.session.params.tint == 0
    assert view.temperature.value() == 0 and view.tint.value() == 0


def test_picking_a_neutral_spot_with_a_click(view, qtbot):
    view.pick_white_balance.click()
    assert view.is_picking() and view.image.picking == 1
    rect = view.image.image_rect()
    point = QPoint(int(rect.center().x()), int(rect.center().y()))

    with qtbot.waitSignal(view.session.params_changed, timeout=TIMEOUT):
        qtbot.mouseClick(view.image, Qt.MouseButton.LeftButton, pos=point)

    assert not view.is_picking() and not view.image.picking
    assert not view.pick_white_balance.isChecked()
    assert (view.session.params.temperature, view.session.params.tint) != (0.0, 0.0)


def test_picking_is_cancelled_and_does_not_mix_with_the_colour_checker(view):
    view.pick_white_balance.click()
    view.cancel_picking()
    assert not view.is_picking() and not view.image.picking

    view.pick_white_balance.click()
    view.checker_button.setChecked(True)
    assert not view.pick_white_balance.isChecked()
    assert view.image.picking == 4
    view.cancel_picking()
