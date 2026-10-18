"""The window's writes run on the writer thread: they do not wait for a scan's write
lock, they are made in order, a read after one sees it, and those that give something
back give it through `on_done`."""

import json
import sqlite3
import time

import pytest
from conftest import TIMEOUT

from lumina.core import db as dbm
from lumina.qt_models.library import PasteResult


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


@pytest.mark.parametrize(
    "write",
    [
        lambda service, photo, done: service.add_collection("Best", on_done=done),
        lambda service, photo, done: service.make_copy(photo, on_done=done),
        lambda service, photo, done: service.paste_settings([photo], {}, on_done=done),
        lambda service, photo, done: service.ensure_keywords(["Cat"], on_done=done),
        lambda service, photo, done: service.rename_keyword("Dog", "Cat", on_done=done),
        lambda service, photo, done: service.delete_keyword("Dog", on_done=done),
    ],
)
def test_a_write_that_gives_something_back_does_not_wait_either(service, qtbot, write):
    photo = add_photo(service)
    service.db.db.execute("PRAGMA busy_timeout = 10000")
    scan = sqlite3.connect(service.path, timeout=10)
    scan.execute("BEGIN IMMEDIATE")
    given = []

    began = time.monotonic()
    write(service, photo, given.append)
    assert time.monotonic() - began < 0.5 and given == []

    scan.rollback()
    scan.close()
    qtbot.waitUntil(lambda: len(given) == 1, timeout=TIMEOUT)


def test_what_a_write_gives_back_is_what_it_made(service, qtbot):
    photo = add_photo(service)
    given = []

    service.add_collection("Best", on_done=given.append)
    service.make_copy(photo, on_done=given.append)
    service.paste_settings([photo], {"exposure": 1.0}, on_done=given.append)
    service.flush()

    [best] = service.collections()
    assert given == [best.id, 1, PasteResult(1, ())]
    assert service.copies(photo) == [1]
    assert json.loads(service.edit_json(photo))["exposure"] == 1.0


def test_a_refused_write_goes_to_its_caller_and_is_not_told_as_a_failure(service, qtbot):
    service.flush()
    refused, failed = [], []
    service.failed.connect(failed.append)

    service.add_collection("", on_error=refused.append)
    service.flush()

    assert [type(error) for error in refused] == [ValueError] and failed == []
    assert service.collections() == []
