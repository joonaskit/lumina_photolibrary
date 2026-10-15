"""The library's folders are watched while Lumina runs: a photo copied in or deleted is
found without a Rescan, after a quiet moment."""

import os

import pytest
from conftest import TIMEOUT
from photos import make_jpeg

from lumina.qt_models import watcher as watching
from lumina.qt_models.watcher import FolderWatcher


@pytest.fixture
def watched(qtbot, service, drive):
    """The service with `drive/Photos` scanned, and a watcher with a short quiet moment."""
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        folder = service.add_folder(drive / "Photos")
    found = FolderWatcher(service, quiet_ms=100, budget=1000, parent=service)
    found.set_enabled(True)
    qtbot.waitUntil(lambda: folder in found.watched and not found._queue, timeout=TIMEOUT)
    return service, found, folder


def count(service, folder):
    from lumina.core import db as dbm

    return len(service.db.photos(dbm.Filter(folder_id=folder)))


def test_a_photo_copied_in_is_found_after_a_quiet_moment(qtbot, watched, drive):
    service, _found, folder = watched
    assert count(service, folder) == 3

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        make_jpeg(drive / "Photos" / "new.jpg", colour=(5, 5, 5))

    assert count(service, folder) == 4


def test_a_photo_in_a_folder_made_later_is_found_too(qtbot, watched, drive):
    service, found, folder = watched

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        (drive / "Photos" / "trip" / "day1").mkdir(parents=True)
    # Its photos need the watch of the new directories: given when they were made
    qtbot.waitUntil(
        lambda: str(drive / "Photos" / "trip" / "day1") in found._owner, timeout=TIMEOUT
    )

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        make_jpeg(drive / "Photos" / "trip" / "day1" / "deep.jpg", colour=(9, 9, 9))

    assert count(service, folder) == 4


def test_a_deleted_photo_is_marked_missing_not_forgotten(qtbot, watched, drive):
    service, _found, folder = watched

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        os.remove(drive / "Photos" / "a.jpg")

    photos = service.db.photos()
    assert len(photos) == 3
    assert [p.available for p in photos if p.path.endswith("a.jpg")] == [False]


def test_many_changes_are_one_scan(qtbot, watched, drive):
    service, _found, _folder = watched
    ended = []
    service.job_ended.connect(ended.append)

    for number in range(5):
        make_jpeg(drive / "Photos" / f"burst{number}.jpg", colour=(number, 1, 1))
    qtbot.waitUntil(lambda: bool(ended), timeout=TIMEOUT)
    qtbot.wait(400)

    assert len(ended) == 1


def test_nothing_is_watched_unless_enabled_and_it_lets_go_when_disabled(qtbot, service, drive):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        folder = service.add_folder(drive / "Photos")
    found = FolderWatcher(service, quiet_ms=50, budget=1000, parent=service)
    found.sync()
    assert found.watched == set() and not found._watcher.directories()

    found.set_enabled(True)
    qtbot.waitUntil(lambda: folder in found.watched, timeout=TIMEOUT)
    assert found._watcher.directories()

    found.set_enabled(False)
    assert found.watched == set() and not found._watcher.directories()


def test_a_folder_with_too_many_directories_is_not_watched_at_all(qtbot, service, drive):
    for number in range(6):
        (drive / "Photos" / f"sub{number}").mkdir()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        folder = service.add_folder(drive / "Photos")
    found = FolderWatcher(service, quiet_ms=50, budget=4, parent=service)
    changes = []
    found.status_changed.connect(lambda: changes.append(1))

    found.set_enabled(True)
    qtbot.waitUntil(lambda: folder in found.unwatched, timeout=TIMEOUT)

    # Not half of it: the Rescan and the scans at the start are what it has
    assert found.watched == set() and not found._watcher.directories()


def test_an_unplugged_drive_is_let_go_of_and_watched_again_when_back(qtbot, watched, mounts, drive):
    service, found, folder = watched

    mounts.plugged = False
    with qtbot.waitSignal(service.mounts_changed, timeout=TIMEOUT):
        pass
    assert folder not in found.watched and not found._watcher.directories()

    mounts.plugged = True
    with qtbot.waitSignal(service.mounts_changed, timeout=TIMEOUT):
        pass
    qtbot.waitUntil(lambda: folder in found.watched and not found._queue, timeout=TIMEOUT)


def test_the_directories_of_a_folder_skip_hidden_ones_and_stop_at_the_budget(tmp_path):
    base = tmp_path / "watched"
    for name in ("a", "b", ".hidden", "a/inner"):
        (base / name).mkdir(parents=True)
    (base / ".hidden" / "x").mkdir()

    found = watching.directories(str(base), 10)

    assert sorted(os.path.relpath(path, base) for path in found) == [".", "a", "a/inner", "b"]
    assert watching.directories(str(base), 3) is None
    assert watching.directories(str(base), 4) is not None


def test_the_budget_is_a_share_of_the_systems_limit(monkeypatch):
    monkeypatch.setattr(watching, "system_limit", lambda: 8192)
    assert watching.default_budget() == 2048
    monkeypatch.setattr(watching, "system_limit", lambda: 10_000_000)
    assert watching.default_budget() == watching._MOST
    monkeypatch.setattr(watching, "system_limit", lambda: None)
    assert watching.default_budget() == 0
