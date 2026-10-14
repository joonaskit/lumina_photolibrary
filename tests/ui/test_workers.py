from photos import make_jpeg
from PySide6.QtCore import QObject, Qt
from shiboken6 import delete

from lumina.qt_models import workers
from lumina.qt_models.photo_model import PhotoModel

TIMEOUT = 20000


def _wait_done(qtbot, future):
    qtbot.waitUntil(future.done, timeout=TIMEOUT)
    # The result reaches the main thread as a queued call: let it be delivered
    qtbot.wait(50)


def test_a_result_for_a_deleted_owner_is_dropped(qtbot):
    owner = QObject()
    called = []
    future = workers.submit(
        sum, [1, 2], on_done=called.append, on_error=called.append, owner=owner, process=False
    )
    delete(owner)

    _wait_done(qtbot, future)

    assert called == []


def test_a_thumbnail_that_fails_after_its_model_is_gone_is_dropped(qtbot, tmp_path):
    # A window closed while its thumbnails are decoding: what arrives later must not
    # call into the deleted model
    (tmp_path / "broken.jpg").write_bytes(b"not a photo")
    make_jpeg(tmp_path / "a.jpg")
    model = PhotoModel()
    model.set_folder(tmp_path)
    qtbot.waitUntil(lambda: not model.is_scanning(), timeout=TIMEOUT)
    for row in range(model.rowCount()):
        model.data(model.index(row), Qt.ItemDataRole.DecorationRole)
    futures = list(model._pending.values())
    delete(model)

    for future in futures:
        _wait_done(qtbot, future)
