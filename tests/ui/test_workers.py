from conftest import TIMEOUT
from photos import make_jpeg
from PySide6.QtCore import QObject
from shiboken6 import delete

from lumina.qt_models import workers
from lumina.qt_models.thumbnails import Thumbnails


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


def test_a_thumbnail_that_fails_after_its_owner_is_gone_is_dropped(qtbot, tmp_path):
    # A window closed while its thumbnails are decoding: what arrives later must not
    # call into the deleted object
    (tmp_path / "broken.jpg").write_bytes(b"not a photo")
    make_jpeg(tmp_path / "a.jpg")
    thumbnails = Thumbnails()
    thumbnails.icon(1, str(tmp_path / "broken.jpg"))
    thumbnails.icon(2, str(tmp_path / "a.jpg"))
    futures = list(thumbnails._pending.values())
    delete(thumbnails)

    for future in futures:
        _wait_done(qtbot, future)


def test_an_offline_photo_seen_before_has_its_thumbnail(qtbot, tmp_path):
    from lumina.core import thumbs
    from lumina.core.fingerprint import content_id

    path = make_jpeg(tmp_path / "a.jpg", size=(200, 100))
    cid = content_id(path)
    thumbs.load_thumbnail(path, 320, thumbs.default_cache_dir())
    thumbnails = Thumbnails()

    thumbnails.icon(1, None, cid)
    qtbot.waitUntil(lambda: thumbnails.pixmap(1) is not None, timeout=TIMEOUT)

    assert thumbnails.pixmap(1).width() == 200
    thumbnails.icon(2, None, "never-seen")
    qtbot.waitUntil(lambda: thumbnails.error(2) is not None, timeout=TIMEOUT)
