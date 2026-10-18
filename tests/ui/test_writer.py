"""The window's point writes run on the writer thread: they do not wait for a scan's
write lock, they are made in order, and a read after one sees it."""

import sqlite3
import time

from conftest import TIMEOUT

from lumina.core import db as dbm


def add_photo(service):
    folder = service.db.add_folder("K", "p")
    volume = service.db.volume_id("K")
    service.db.write_scan(folder, volume, new=[("a.jpg", 1, 1, "2020-01-01 00:00:00.000000")])
    return next(iter(service.db.photo_states(folder).values()))[0]


def test_a_write_does_not_wait_for_another_connections_write_lock(service, qtbot):
    photo = add_photo(service)
    service.db.db.execute("PRAGMA busy_timeout = 10000")
    scan = sqlite3.connect(service.path, timeout=10)
    scan.execute("BEGIN IMMEDIATE")

    began = time.monotonic()
    service.set_flags({photo: dbm.PICKED})
    assert time.monotonic() - began < 0.5

    scan.rollback()
    scan.close()
    service.settle()
    assert service.flags([photo]) == {photo: dbm.PICKED}


def test_writes_are_made_in_order_and_heard_of(service, qtbot):
    photo = add_photo(service)
    heard = []
    service.edits_changed.connect(heard.append)

    service.save_edit(photo, '{"version": 1}')
    service.save_edit(photo, None)

    qtbot.waitUntil(lambda: len(heard) == 2, timeout=TIMEOUT)
    assert service.edit_json(photo) is None


def test_a_failed_write_is_told_and_the_next_one_is_made(service, qtbot):
    photo = add_photo(service)
    service._write(lambda db: db.db.execute("SELECT * FROM no_such_table"))
    with qtbot.waitSignal(service.failed, timeout=TIMEOUT):
        pass
    service.set_ratings({photo: 3})
    service.settle()
    assert service.ratings([photo]) == {photo: 3}
