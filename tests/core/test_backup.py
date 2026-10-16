"""Backups of the library database: taken while it is in use, checked, rotated, made
before a migration, and put back."""

import os
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

from lumina.core import backup
from lumina.core import db as dbm
from lumina.core.db import Library

NOW = datetime(2026, 10, 16, 9, 30, 12)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "library.sqlite3")


@pytest.fixture
def folder(tmp_path):
    return str(tmp_path / "backups")


def fill(path, photos=3, edited=True):
    """A library with a folder of `photos` photos, the first one edited."""
    with Library(path) as library:
        folder = library.add_folder("uuid:A", "Pictures")
        volume = library.volume_id("uuid:A")
        rows = [(f"Pictures/{n}.jpg", 1, 1, "2026-01-01 00:00:00.000000") for n in range(photos)]
        library.write_scan(folder, volume, new=rows)
        if edited and photos:
            first = library.db.execute("SELECT MIN(id) FROM photos").fetchone()[0]
            library.save_edit(first, '{"version": 1}')


def photo_paths(path):
    with Library(path) as library:
        return [photo.path for photo in library.photos()]


def test_a_backup_is_a_sound_copy_named_for_when_it_was_taken(path, folder):
    fill(path)

    made = backup.back_up(path, folder, now=NOW)

    assert os.path.basename(made) == "library-2026-10-16-093012.sqlite3"
    assert os.listdir(folder) == [os.path.basename(made)]
    # One file of its own, not WAL
    assert sqlite3.connect(made).execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert photo_paths(made) == photo_paths(path)
    held = backup.contents(made)
    assert (held.photos, held.edits, held.schema) == (3, 1, dbm.SCHEMA_VERSION)
    [found] = backup.backups(path, folder)
    assert (found.path, found.kind, found.taken) == (made, backup.DAILY, NOW)


def test_two_in_the_same_second_both_kept(path, folder):
    fill(path)

    first = backup.back_up(path, folder, now=NOW)
    second = backup.back_up(path, folder, now=NOW)

    assert first != second
    assert len(backup.backups(path, folder)) == 2


def test_a_backup_taken_while_another_connection_writes_is_consistent(path, folder):
    fill(path, photos=0)
    stop = threading.Event()
    written = []

    def write():
        # Each transaction adds a photo and its edit: a sound copy has as many of each
        with Library(path) as library:
            folder_id = library.add_folder("uuid:A", "Pictures")
            volume = library.volume_id("uuid:A")
            n = 0
            while not stop.is_set():
                with library.db:
                    cursor = library.db.execute(
                        "INSERT INTO photos (folder_id, volume_id, path, size, mtime_ns, "
                        "file_time, added_at) VALUES (?, ?, ?, 1, 1, '2026', 0)",
                        (folder_id, volume, f"Pictures/{n}.jpg"),
                    )
                    library.db.execute(
                        "INSERT INTO edits (photo_id, copy, params, updated_at) "
                        "VALUES (?, 0, '{}', 0)",
                        (cursor.lastrowid,),
                    )
                n += 1
                written.append(n)

    writer = threading.Thread(target=write)
    writer.start()
    try:
        made = []
        while len(made) < 5:
            if len(written) > len(made) * 50:
                made.append(backup.back_up(path, folder, now=NOW))
    finally:
        stop.set()
        writer.join()

    counts = [backup.contents(copy) for copy in made]
    assert all(held.photos == held.edits for held in counts)
    # Taken while it grew, not all before or after
    assert counts[0].photos < counts[-1].photos <= len(written)


def test_a_failed_copy_leaves_no_partial_backup(path, folder, monkeypatch):
    fill(path)

    def no_room(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup.derived, "place", no_room)
    with pytest.raises(backup.BackupError, match="No space left"):
        backup.back_up(path, folder, now=NOW)

    assert os.listdir(folder) == []


def test_a_damaged_library_is_not_backed_up(path, folder):
    fill(path)
    with open(path, "r+b") as file:
        file.seek(4096)
        file.write(b"\xff" * 4096)

    with pytest.raises(backup.BackupError):
        backup.back_up(path, folder, now=NOW)

    assert os.listdir(folder) == []


def test_a_part_left_by_a_crash_is_removed_by_the_next_backup(path, folder):
    fill(path)
    os.makedirs(folder)
    stale = os.path.join(folder, ".library-backup-999.part")
    with open(stale, "wb") as file:
        file.write(b"half")

    backup.back_up(path, folder, now=NOW)

    assert not os.path.exists(stale)


def test_a_backup_is_due_a_day_after_the_last_daily_one(path, folder):
    fill(path)
    assert backup.is_due(path, folder, NOW)

    backup.back_up(path, folder, now=NOW)
    assert not backup.is_due(path, folder, NOW + timedelta(hours=23))
    assert backup.is_due(path, folder, NOW + timedelta(hours=24))

    # Those of other kinds don't count
    other = os.path.join(folder, "other")
    backup.back_up(path, other, backup.BEFORE_RESTORE, now=NOW)
    assert backup.is_due(path, other, NOW)


def touch(folder, name):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "wb"):
        pass
    return name


def test_rotation_keeps_the_last_days_and_then_one_a_week(path, folder):
    def daily(when):
        return touch(folder, f"library-{when:%Y-%m-%d-%H%M%S}.sqlite3")

    # Friday 16 October back to Monday 7 September, twice on some days
    start = datetime(2026, 10, 16, 20, 0, 0)
    days = [start - timedelta(days=n) for n in range(40)]
    names = {when: daily(when) for when in days}
    earlier = {when: daily(when - timedelta(hours=10)) for when in days[:2]}
    kept_apart = [
        touch(folder, "library-2026-08-01-100000-before-schema-7.sqlite3"),
        touch(folder, "library-2026-08-02-100000-before-restore.sqlite3"),
        touch(folder, "library-2026-08-03-100000-damaged.sqlite3"),
        touch(folder, "other-2026-08-03-100000.sqlite3"),
        touch(folder, "notes.txt"),
    ]

    deleted = backup.rotate(path, folder, daily=3, weekly=2)

    expected = {
        # The newest of each of the last three days
        names[days[0]],
        names[days[1]],
        names[days[2]],
        # Then the newest of each of two weeks: Tuesday 13 October, the rest of that
        # week, then Sunday 11 October, the newest of the week before
        names[days[3]],
        names[days[5]],
        *kept_apart,
    }
    assert set(os.listdir(folder)) == expected
    assert set(map(os.path.basename, deleted)) == {
        *(n for n in names.values() if n not in expected),
        *earlier.values(),
    }


def test_vectors_are_left_out_of_a_daily_backup_when_they_are_most_of_it(path, folder):
    fill(path)
    with Library(path) as library:
        # 400 photos' worth of 768 half floats
        library.write_embeddings(
            "model", vectors=[(f"c{n}", os.urandom(1536)) for n in range(400)], failed=[("x", "?")]
        )

    daily = backup.back_up(path, folder, now=NOW)
    whole = backup.back_up(path, folder, backup.BEFORE_RESTORE, now=NOW)

    assert not backup.contents(daily).vectors
    assert os.path.getsize(daily) < os.path.getsize(whole) / 4
    assert backup.contents(whole).vectors
    # What could not be read is still known
    with Library(daily) as copy:
        assert copy.count_pending_embeddings("model", ["uuid:A"]) == 0
        assert [tuple(r) for r in copy.db.execute("SELECT error FROM embeddings")] == [("?",)]


def test_a_few_vectors_are_kept(path, folder):
    fill(path, photos=500)
    with Library(path) as library:
        library.write_embeddings("model", vectors=[("c", os.urandom(1536))])

    assert backup.contents(backup.back_up(path, folder, now=NOW)).vectors


def test_a_backup_is_made_before_a_migration_and_kept_apart(path, monkeypatch):
    monkeypatch.setattr(dbm, "_MIGRATIONS", dbm._MIGRATIONS[:5])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 5)
    fill(path)
    monkeypatch.undo()
    folder = backup.default_folder(path)
    assert not os.path.exists(folder)

    Library(path).close()

    [made] = backup.backups(path, folder)
    assert (made.kind, made.schema) == (backup.BEFORE_MIGRATION, 5)
    assert backup.contents(made.path).schema == 5
    assert backup.contents(made.path).photos == 3
    # Not rotated away, nor counted as the day's backup
    assert backup.rotate(path, folder, daily=0, weekly=0) == []
    assert backup.is_due(path, folder)

    # Opened again: up to date, no other backup
    Library(path).close()
    assert len(backup.backups(path, folder)) == 1


def test_a_new_library_is_not_backed_up(path):
    Library(path).close()
    assert not os.path.exists(backup.default_folder(path))


def test_without_a_backup_the_migration_is_not_made(path, monkeypatch):
    monkeypatch.setattr(dbm, "_MIGRATIONS", dbm._MIGRATIONS[:5])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 5)
    fill(path)
    monkeypatch.undo()
    # The folder can't be made: a file is in the way
    touch(os.path.dirname(path), "backups")

    with pytest.raises(dbm.NoBackup, match="left as it is"):
        Library(path)

    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 5


def test_restore_puts_the_backup_back_and_keeps_what_it_replaced(path, folder):
    fill(path, photos=2)
    made = backup.back_up(path, folder, now=NOW)
    # Later: a photo more, and the edit gone
    with Library(path) as library:
        library.write_scan(
            library.folders()[0].id,
            library.volume_id("uuid:A"),
            new=[("Pictures/z.jpg", 1, 1, "2026")],
        )
        library.delete_edit(library.photos()[0].id)

    later = NOW + timedelta(days=1)
    backup.restore(made, path, folder, newest_schema=dbm.SCHEMA_VERSION, now=later)

    assert photo_paths(path) == ["Pictures/0.jpg", "Pictures/1.jpg"]
    assert backup.contents(path).edits == 1
    # The library as it was, backed up first
    replaced = [b for b in backup.backups(path, folder) if b.kind == backup.BEFORE_RESTORE]
    assert [b.taken for b in replaced] == [later]
    assert backup.contents(replaced[0].path).photos == 3
    # No WAL of the old one to be applied to it, and no part left
    assert sorted(os.listdir(os.path.dirname(path))) == ["backups", "library.sqlite3"]
    # It opens, and works, as any library
    with Library(path) as library:
        library.set_flags({library.photos()[0].id: dbm.PICKED})


def test_restore_of_an_older_schema_is_migrated_when_opened(path, folder, monkeypatch):
    monkeypatch.setattr(dbm, "_MIGRATIONS", dbm._MIGRATIONS[:5])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 5)
    fill(path)
    old = backup.back_up(path, folder, now=NOW)
    monkeypatch.undo()
    Library(path).close()

    backup.restore(old, path, folder, newest_schema=dbm.SCHEMA_VERSION)

    assert backup.contents(path).schema == 5
    with Library(path) as library:
        assert len(library.photos()) == 3
    assert backup.contents(path).schema == dbm.SCHEMA_VERSION


def test_a_damaged_library_is_found_and_moved_aside_by_a_restore(path, folder):
    fill(path)
    made = backup.back_up(path, folder, now=NOW)
    assert backup.check(path) is None
    with open(path, "r+b") as file:
        file.seek(4096)
        file.write(b"\xff" * 4096)
    assert backup.check(path)

    backup.restore(made, path, folder, now=NOW + timedelta(days=1))

    assert backup.check(path) is None
    assert len(photo_paths(path)) == 3
    [damaged] = [b for b in backup.backups(path, folder) if b.kind == backup.DAMAGED]
    assert backup.check(damaged.path)


def test_a_file_that_is_no_database_is_damaged(path):
    with open(path, "wb") as file:
        file.write(b"not a database" * 1000)

    assert "not a database" in backup.check(path)


def test_a_restore_that_fails_leaves_the_library_as_it_was(path, folder):
    fill(path)
    made = backup.back_up(path, folder, now=NOW)
    with open(made, "r+b") as file:
        file.seek(4096)
        file.write(b"\xff" * 4096)
    before = photo_paths(path)

    with pytest.raises(backup.BackupError):
        backup.restore(made, path, folder)

    assert photo_paths(path) == before
    assert backup.check(path) is None
    assert [b.kind for b in backup.backups(path, folder)] == [backup.DAILY]
    assert sorted(os.listdir(os.path.dirname(path)))[:2] == ["backups", "library.sqlite3"]


def test_a_backup_from_a_newer_lumina_is_not_restored(path, folder):
    fill(path)
    made = backup.back_up(path, folder, now=NOW)
    connection = sqlite3.connect(made)
    connection.execute(f"PRAGMA user_version = {dbm.SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(backup.BackupError, match="newer Lumina"):
        backup.restore(made, path, folder, newest_schema=dbm.SCHEMA_VERSION)
