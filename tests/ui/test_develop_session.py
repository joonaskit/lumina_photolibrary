import json

import numpy as np
import pytest
from conftest import TIMEOUT

from lumina.core.edit.export_options import ExportOptions
from lumina.core.edit.params import Matte, Params
from lumina.qt_models import develop
from lumina.qt_models.develop import DevelopSession


@pytest.fixture
def library(service, drive, qtbot):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    return service


@pytest.fixture
def photo(library):
    row = next(p for p in library.db.photos() if p.path.endswith("a.jpg"))
    return row.id, library.resolve(row.volume_key, row.path)


@pytest.fixture
def session(library, photo, qtbot):
    made = DevelopSession(library)
    with qtbot.waitSignal(made.image_ready, timeout=TIMEOUT):
        made.open(*photo)
    yield made
    made.close()


def mean(image):
    bits = image.constBits()
    return np.frombuffer(bits, np.uint8, image.sizeInBytes()).mean()


def test_opening_renders_the_photo_with_a_histogram(library, photo, qtbot):
    session = DevelopSession(library)
    opened, ready = [], []
    session.opened.connect(lambda *size: opened.append(size))
    session.image_ready.connect(lambda *args: ready.append(args))

    session.open(*photo)
    qtbot.waitUntil(lambda: bool(ready), timeout=TIMEOUT)

    assert opened == [(300, 200)]
    image, histogram, kind = ready[0]
    assert kind == develop.SCREEN
    assert (image.width(), image.height()) == (300, 200)
    assert histogram.shape == (4, 256) and histogram[0].sum() == 300 * 200
    session.close()


def test_a_moving_control_renders_small_and_saves_nothing(session, library, photo, qtbot):
    with qtbot.waitSignal(session.image_ready, timeout=TIMEOUT) as ready:
        session.preview(Params(exposure=1.0))

    assert ready.args[2] == develop.PROXY
    assert library.edit_json(photo[0]) is None
    assert not session.can_undo()


def test_a_commit_saves_and_can_be_undone_and_redone(session, library, photo, qtbot):
    brighter = Params(exposure=1.0)
    session.commit(brighter)

    assert json.loads(library.edit_json(photo[0]))["exposure"] == 1.0
    assert session.can_undo() and not session.can_redo()
    session.undo()
    assert library.edit_json(photo[0]) is None
    assert session.params == Params()
    session.redo()
    assert session.params == brighter
    assert json.loads(library.edit_json(photo[0]))["exposure"] == 1.0


def test_a_new_change_after_undo_drops_the_redo(session):
    session.commit(Params(exposure=1.0))
    session.undo()

    session.commit(Params(exposure=-1.0))

    assert not session.can_redo()


def test_reset_goes_back_to_the_original_as_one_step(session, library, photo):
    session.commit(Params(exposure=1.0, matte=Matte(black=0.1)))

    session.reset()

    assert library.edit_json(photo[0]) is None
    session.undo()
    assert session.params.matte.black == 0.1


def test_the_newest_look_is_rendered_and_old_ones_are_skipped(session, qtbot):
    shown = []
    session.image_ready.connect(lambda image, _h, kind: shown.append((mean(image), kind)))

    for step in range(20):
        session.preview(Params(exposure=-2 + step * 0.2))
    session.commit()
    qtbot.waitUntil(lambda: bool(shown) and shown[-1][1] == develop.SCREEN, timeout=TIMEOUT)

    # Far fewer renders than moves, and the last one is the last look
    assert len(shown) < 15
    final = Params(exposure=-2 + 19 * 0.2)
    assert session.params == final
    assert shown[-1][0] > shown[0][0]


def test_an_edit_reopened_is_where_it_was_left(session, library, photo, qtbot):
    session.commit(Params(exposure=0.5))
    session.close()

    again = DevelopSession(library)
    with qtbot.waitSignal(again.image_ready, timeout=TIMEOUT):
        again.open(*photo)

    assert again.params == Params(exposure=0.5)
    again.close()


def test_presets_save_sections_and_apply_over_another_edit(session):
    session.commit(Params(exposure=1.0, matte=Matte(black=0.1, shadow_desaturation=0.5)))
    session.save_preset("Faded", ["matte"])
    session.commit(Params(exposure=-0.5))

    session.apply_preset("Faded")

    assert session.presets() == ["Faded"]
    assert session.params.exposure == -0.5
    assert session.params.matte == Matte(black=0.1, shadow_desaturation=0.5)
    session.undo()
    assert session.params == Params(exposure=-0.5)


def test_export_writes_the_edited_photo(session, tmp_path, qtbot):
    destination = tmp_path / "out.jpg"
    session.commit(Params(rotate=1))

    with qtbot.waitSignal(session.exported, timeout=TIMEOUT) as done:
        session.export(destination, ExportOptions())

    assert done.args == [str(destination), None]
    from PIL import Image

    with Image.open(destination) as image:
        assert image.size == (200, 300)


def test_export_tells_when_the_file_appeared_and_keeps_it(session, tmp_path, qtbot):
    destination = tmp_path / "out.jpg"
    destination.write_bytes(b"someone else's")

    with qtbot.waitSignal(session.export_exists, timeout=TIMEOUT):
        session.export(destination, ExportOptions())

    assert destination.read_bytes() == b"someone else's"


def test_an_edit_from_a_newer_lumina_is_not_opened_or_overwritten(library, photo, qtbot):
    newer = json.dumps({"version": 99, "exposure": 1})
    library.save_edit(photo[0], newer)
    session = DevelopSession(library)

    with qtbot.waitSignal(session.failed, timeout=TIMEOUT):
        session.open(*photo)
    session.commit(Params(exposure=2.0))

    assert not session.is_open()
    assert library.edit_json(photo[0]) == newer


def test_saving_tells_the_grid(session, library, qtbot):
    with qtbot.waitSignal(library.edits_changed, timeout=TIMEOUT):
        session.commit(Params(exposure=1.0))
