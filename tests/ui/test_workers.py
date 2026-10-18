from conftest import TIMEOUT
from fakes import work_or_die
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
        sum, [1, 2], on_done=called.append, on_error=called.append, owner=owner, lane=workers.IO
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


def test_a_call_that_kills_its_worker_does_not_fail_those_in_flight_with_it(qtbot):
    owner = QObject()
    done, failed = [], []
    for value in (1, 2, 0, 3, 4):
        workers.submit(
            work_or_die,
            value,
            on_done=done.append,
            on_error=lambda error, value=value: failed.append((value, type(error).__name__)),
            owner=owner,
        )

    qtbot.waitUntil(lambda: len(done) + len(failed) == 5, timeout=TIMEOUT)

    assert sorted(done) == [1, 2, 3, 4]
    assert failed == [(0, "BrokenProcessPool")]


def test_a_lane_takes_the_soonest_priority_first_then_the_order_asked():
    import threading

    lane = workers.Lane("test-order", False, 1)
    hold = threading.Event()
    ran = []
    first = workers.schedule(lane, hold.wait, 5)
    later = workers.schedule(lane, ran.append, "later", priority=workers.LATER)
    soon = [workers.schedule(lane, ran.append, f"soon {i}") for i in range(2)]
    now = workers.schedule(lane, ran.append, "now", priority=workers.NOW)

    hold.set()
    for future in (first, later, *soon, now):
        future.result(timeout=5)

    assert ran == ["now", "soon 0", "soon 1", "later"]


def test_a_request_cancelled_while_it_waits_never_runs():
    import threading

    lane = workers.Lane("test-cancel", False, 1)
    hold = threading.Event()
    ran = []
    first = workers.schedule(lane, hold.wait, 5)
    dropped = workers.schedule(lane, ran.append, "dropped")
    kept = workers.schedule(lane, ran.append, "kept")

    assert dropped.cancel()
    hold.set()
    first.result(timeout=5)
    kept.result(timeout=5)

    assert ran == ["kept"]


def test_a_token_is_cancelled_with_its_parent():
    quitting = workers.Token()
    job = workers.Token(quitting)
    other = workers.Token(quitting)

    job.cancel()
    assert job() and not other.cancelled() and not quitting.cancelled()
    quitting.cancel()
    assert other.cancelled()


def test_coalescing_runs_once_more_however_often_it_was_asked():
    work = workers.Coalescing()

    assert work.begin()
    assert not work.begin() and not work.begin()
    assert work.end()
    assert work.begin()
    assert not work.end()
    assert not work.busy()
