"""Thumbnails wait their turn, the newest first, and rows scrolled past are dropped."""

from concurrent.futures import Future

import pytest
from conftest import TIMEOUT
from photos import make_jpeg
from PySide6.QtCore import QPoint

from lumina.qt_models import thumbnails
from lumina.qt_models.thumbnails import Thumbnails
from lumina.ui.main_window import MainWindow


class FakeWorkers:
    """Takes the work and lets the test finish it, in any order."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, on_done, on_error, owner, process=True):
        future = Future()
        self.submitted.append((args, future, on_done, on_error))
        return future

    def started(self):
        return [call[0][0] for call in self.submitted]

    def finish(self, number, pixels=None):
        _args, _future, on_done, _on_error = self.submitted[number]
        on_done(pixels)


@pytest.fixture
def fake(monkeypatch):
    fake = FakeWorkers()
    monkeypatch.setattr(thumbnails.workers, "submit", fake.submit)
    return fake


def ask(queue, *names):
    for name in names:
        queue.icon(name, f"/photos/{name}.jpg")


def test_only_a_few_are_with_the_workers_and_the_rest_wait(qtbot, fake):
    queue = Thumbnails()
    names = [f"p{n:02}" for n in range(30)]

    ask(queue, *names)

    # The first asked for go at once, the others wait: the workers have only their share
    assert len(fake.submitted) == thumbnails._IN_FLIGHT
    assert queue.waiting() == 30
    assert fake.started() == [f"/photos/{n}.jpg" for n in names[:8]]


def test_the_newest_request_goes_next(qtbot, fake):
    queue = Thumbnails()
    ask(queue, *[f"p{n:02}" for n in range(12)])
    first = len(fake.submitted)

    # One is made: the workers take the newest of those still waiting, not the oldest
    fake.finish(0, pixels=None)

    assert len(fake.submitted) == first + 1
    # p08 to p11 wait; p11 was asked for last
    assert fake.started()[-1] == "/photos/p11.jpg"
    assert queue.waiting() == 12 - 1


def test_asking_again_puts_a_waiting_one_first(qtbot, fake):
    queue = Thumbnails()
    ask(queue, *[f"p{n:02}" for n in range(12)])

    # p08 is the oldest of those waiting; the view comes back to it
    queue.icon("p08", "/photos/p08.jpg")
    fake.finish(0)

    assert fake.started()[-1] == "/photos/p08.jpg"


def test_rows_far_from_the_view_are_dropped_and_asked_for_again_later(qtbot, fake):
    queue = Thumbnails()
    ask(queue, *[f"p{n:02}" for n in range(30)])
    sent = len(fake.submitted)

    queue.keep_only({"p29", "p28", "p05"})

    # What the workers have is theirs (p05 among it); of what waited, only p28 and p29
    assert queue.waiting() == sent + 2
    fake.finish(0)
    assert fake.started()[-1] == "/photos/p29.jpg"
    fake.finish(1)
    assert fake.started()[-1] == "/photos/p28.jpg"
    fake.finish(2)
    assert len(fake.submitted) == sent + 2

    # A dropped one comes back when its row is painted again
    queue.icon("p10", "/photos/p10.jpg")
    assert fake.started()[-1] == "/photos/p10.jpg"


def test_a_failure_gives_the_workers_the_next_too(qtbot, fake):
    queue = Thumbnails()
    ask(queue, *[f"p{n:02}" for n in range(10)])
    before = len(fake.submitted)

    _args, _future, _done, on_error = fake.submitted[0]
    on_error(RuntimeError("broken"))

    assert len(fake.submitted) == before + 1


def test_clear_forgets_what_waits(qtbot, fake):
    queue = Thumbnails()
    ask(queue, *[f"p{n:02}" for n in range(20)])

    queue.clear()
    fake.finish(0)

    assert queue.waiting() == 0
    assert len(fake.submitted) == thumbnails._IN_FLIGHT


def photos_window(qtbot, service, drive, count):
    folder = drive / "Photos"
    for n in range(count):
        make_jpeg(folder / f"more{n:03}.jpg", size=(60, 40))
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.resize(1000, 700)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.browser.add_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    return window


def test_the_grid_asks_to_keep_only_the_thumbnails_near_the_view(qtbot, service, drive):
    window = photos_window(qtbot, service, drive, 200)
    first, last = window.grid_page.visible_rows()
    cell = window.grid.gridSize()
    columns = window.grid.viewport().width() // cell.width()
    screenful = columns * (window.grid.viewport().height() // cell.height() + 1)

    # At the top: nothing above, a screenful and then some below
    assert (first, last) == (0, 2 * screenful)
    assert 0 < last < window.model.rowCount()

    kept = []
    window.model.thumbnails.keep_only = kept.append
    bar = window.grid.verticalScrollBar()
    bar.setValue(bar.maximum())
    qtbot.waitUntil(lambda: bool(kept), timeout=TIMEOUT)

    # Scrolled to the end: the rows at the top are not waited for, the last ones are
    first_now, last_now = window.grid_page.visible_rows()
    assert first_now > 0 and last_now >= window.model.rowCount() - 1
    wanted = kept[-1]
    rows = window.model._rows
    assert window.model._key(rows[-1]) in wanted
    assert window.model._key(rows[0]) not in wanted
    assert window.grid.indexAt(QPoint(5, 5)).row() <= first_now + screenful


def test_nothing_is_dropped_without_rows(qtbot, service):
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()

    assert window.grid_page.visible_rows() is None
    window.grid_page.trim_thumbnails()
