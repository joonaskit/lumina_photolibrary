"""Backups of the library database, and putting one back.

A backup is a copy made with SQLite's online backup API, so the app goes on reading and
writing while it is taken (in WAL mode a reader never blocks a writer). It is written
to a hidden part file, checked with `PRAGMA integrity_check`, synced, then put in place
under a name that says when it was taken and why:

    library-2026-10-16-093012.sqlite3                    daily, rotated
    library-2026-10-16-093012-before-schema-8.sqlite3    before a migration, kept
    library-2026-10-16-093012-before-restore.sqlite3     the library a restore replaced, kept
    library-2026-10-16-093012-damaged.sqlite3            a damaged library a restore moved aside

A part file left by a crash is removed by the next backup into that folder; a failed
backup leaves nothing behind. Only files named as above are ever deleted, and only
daily ones, by rotation.
"""

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from lumina.core import derived
from lumina.core.errors import PhotoError

# A backup's kind
DAILY, BEFORE_MIGRATION, BEFORE_RESTORE, DAMAGED = "daily", "migration", "restore", "damaged"
# How many daily backups are kept (the newest of each day), and after those how many
# weekly ones (the newest of each week)
KEEP_DAILY, KEEP_WEEKLY = 7, 4
# A daily backup is due when the newest is older than this
EVERY = timedelta(days=1)
# The vectors for search are left out of a daily backup when they are more than this
# share of the file: they can be made again, the rest can't
_VECTORS_SHARE = 0.5

_STAMP = "%Y-%m-%d-%H%M%S"
_NAME = re.compile(
    r"^(?P<stem>.+)-(?P<stamp>\d{4}-\d{2}-\d{2}-\d{6})"
    r"(?:-before-schema-(?P<schema>\d+)|-(?P<other>before-restore|damaged))?"
    r"(?:-\d+)?\.sqlite3$"
)
_KINDS = {"before-restore": BEFORE_RESTORE, "damaged": DAMAGED}


class BackupError(Exception):
    """A backup or a restore could not be made. Nothing was left half done."""


@dataclass(frozen=True)
class Backup:
    path: str
    kind: str
    # Local time, from its name
    taken: datetime
    size: int
    # For a backup before a migration: the schema the library had
    schema: int | None = None


@dataclass(frozen=True)
class Contents:
    """What a backup holds, read from it."""

    photos: int
    edits: int
    schema: int
    # Whether the vectors for search are in it
    vectors: bool


def default_folder(db_path):
    """`backups` beside the database."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def _stem(db_path):
    return os.path.splitext(os.path.basename(db_path))[0]


def backups(db_path, folder):
    """The backups of the database at `db_path` in `folder`, newest first. A folder that
    is not there (a drive not connected) has none."""
    stem = _stem(db_path)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = []
    for name in names:
        match = _NAME.match(name)
        if match is None or match["stem"] != stem:
            continue
        path = os.path.join(folder, name)
        try:
            size = os.stat(path).st_size
        except OSError:
            continue
        if match["schema"] is not None:
            kind, schema = BEFORE_MIGRATION, int(match["schema"])
        else:
            kind, schema = _KINDS.get(match["other"], DAILY), None
        taken = datetime.strptime(match["stamp"], _STAMP)
        found.append(Backup(path, kind, taken, size, schema))
    found.sort(key=lambda backup: (backup.taken, backup.path), reverse=True)
    return found


def is_due(db_path, folder, now=None):
    """Whether the newest daily backup in `folder` is older than a day, or there is none."""
    now = now or datetime.now()
    daily = [b for b in backups(db_path, folder) if b.kind == DAILY]
    return not daily or now - daily[0].taken >= EVERY


def back_up(db_path, folder, kind=DAILY, schema=None, vectors=None, now=None):
    """Copy the database at `db_path` into `folder` and return the new backup's path.
    `schema` names the schema a backup before a migration is of. `vectors`: True keeps
    the vectors for search, False leaves them out, None leaves them out of a daily
    backup when they are most of it. Raises BackupError."""
    if not os.path.isfile(db_path):
        raise BackupError(f"{db_path}: there is no library to back up")
    now = now or datetime.now()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        raise BackupError(f"{folder}: {error.strerror or error}") from error
    stem = _stem(db_path)
    _remove_parts(folder, stem)
    part = os.path.join(folder, f".{stem}-backup-{os.getpid()}.part")
    if vectors is None:
        vectors = kind != DAILY
    try:
        _copy(db_path, part, vectors)
        name = f"{stem}-{now.strftime(_STAMP)}" + _suffix(kind, schema)
        path = derived.place(part, folder, name, ".sqlite3")
        _sync_folder(folder)
        return path
    except (sqlite3.Error, OSError, PhotoError) as error:
        raise BackupError(f"{db_path}: could not back up into {folder}: {error}") from error
    finally:
        _remove(part)
        _remove(part + "-journal")


def _suffix(kind, schema):
    if kind == BEFORE_MIGRATION:
        return f"-before-schema-{schema}"
    if kind == BEFORE_RESTORE:
        return "-before-restore"
    if kind == DAMAGED:
        return "-damaged"
    return ""


def _copy(source_path, target_path, vectors=True):
    """`source_path` copied page by page into a new `target_path`, a database of its
    own (no WAL), checked and synced. Raises BackupError when the copy is not sound."""
    _remove(target_path)
    source = sqlite3.connect(source_path, timeout=10)
    try:
        target = sqlite3.connect(target_path)
        try:
            # All pages in one step, in one read transaction: a consistent copy, which
            # in WAL mode holds up no writer
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
            if not vectors and _vectors_are_most(target, target_path):
                with target:
                    target.execute("DELETE FROM embeddings WHERE vector IS NOT NULL")
                target.execute("VACUUM")
            check = [row[0] for row in target.execute("PRAGMA integrity_check")]
            if check != ["ok"]:
                raise BackupError(f"the copy is not sound: {'; '.join(check[:3])}")
        finally:
            target.close()
    finally:
        source.close()
    with open(target_path, "rb+") as file:
        os.fsync(file.fileno())


def _vectors_are_most(connection, path):
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embeddings'"
    ).fetchone()
    if not has_table:
        return False
    size = connection.execute("SELECT COALESCE(SUM(LENGTH(vector)), 0) FROM embeddings")
    return size.fetchone()[0] > _VECTORS_SHARE * os.path.getsize(path)


def rotate(db_path, folder, daily=KEEP_DAILY, weekly=KEEP_WEEKLY):
    """Delete the daily backups in `folder` beyond the newest of each of the last
    `daily` days and, older than those, the newest of each of `weekly` weeks. Backups of
    other kinds are kept. Returns the paths deleted."""
    found = [b for b in backups(db_path, folder) if b.kind == DAILY]
    keep, days, weeks = set(), [], []
    for backup in found:
        day = backup.taken.date()
        if len(days) < daily:
            if day not in days:
                days.append(day)
                keep.add(backup.path)
            continue
        if day in days:
            continue
        week = day.isocalendar()[:2]
        if len(weeks) < weekly and week not in weeks:
            weeks.append(week)
            keep.add(backup.path)
    deleted = []
    for backup in found:
        if backup.path not in keep:
            _remove(backup.path)
            deleted.append(backup.path)
    if deleted:
        _sync_folder(folder)
    return deleted


def contents(path):
    """What the backup (or any library database) at `path` holds. Raises BackupError
    when it can't be read."""
    try:
        connection = sqlite3.connect(f"file:{quote(os.path.abspath(path))}?immutable=1", uri=True)
        try:
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
            photos = connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            edits = _count(connection, "edits")
            vectors = _count(connection, "embeddings", "vector IS NOT NULL")
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupError(f"{path}: {error}") from error
    return Contents(photos, edits, schema, vectors > 0)


def _count(connection, table, where="1"):
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if not has_table:
        return 0
    return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def check(db_path):
    """None when the database at `db_path` is sound, or not there yet; else what is wrong
    with it (`PRAGMA quick_check`, which reads all of it)."""
    if not os.path.exists(db_path):
        return None
    try:
        connection = sqlite3.connect(db_path, timeout=10)
        try:
            problems = [row[0] for row in connection.execute("PRAGMA quick_check(5)")]
        finally:
            connection.close()
    except sqlite3.Error as error:
        return str(error)
    return None if problems == ["ok"] else "; ".join(problems)


def restore(backup_path, db_path, folder=None, newest_schema=None, now=None):
    """Put the backup at `backup_path` in place of the database at `db_path`, which
    nothing may have open. The database as it was is backed up first into `folder`
    (`backups` beside it by default), or, when it is damaged, moved there as it is.
    A backup of a schema newer than `newest_schema` is refused. Raises BackupError,
    with the database as it was."""
    folder = folder or default_folder(db_path)
    held = contents(backup_path)
    if newest_schema is not None and held.schema > newest_schema:
        raise BackupError(
            f"{backup_path} is from a newer Lumina (schema {held.schema}, this one knows "
            f"{newest_schema})"
        )
    home = os.path.dirname(os.path.abspath(db_path))
    part = os.path.join(home, f".{_stem(db_path)}.restore.part")
    try:
        _copy(backup_path, part)
        if os.path.exists(db_path):
            _put_aside(db_path, folder, now)
        os.replace(part, db_path)
        _sync_folder(home)
    except (sqlite3.Error, OSError, PhotoError) as error:
        raise BackupError(f"{backup_path}: could not restore it: {error}") from error
    finally:
        _remove(part)
        _remove(part + "-journal")


def _put_aside(db_path, folder, now):
    """The database as it is, out of the way of the one restored: backed up, and its WAL
    folded in and gone; or, damaged, moved into `folder` with its WAL."""
    if check(db_path) is None:
        back_up(db_path, folder, BEFORE_RESTORE, vectors=True, now=now)
        connection = sqlite3.connect(db_path, timeout=10)
        try:
            busy = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            if busy:
                raise BackupError(f"{db_path} is still in use")
            # Out of WAL mode: the WAL is folded in and removed, so none is left for
            # SQLite to apply to the file restored
            connection.execute("PRAGMA journal_mode = DELETE")
        finally:
            connection.close()
    else:
        os.makedirs(folder, exist_ok=True)
        name = f"{_stem(db_path)}-{(now or datetime.now()).strftime(_STAMP)}-damaged"
        aside = derived.place(db_path, folder, name, ".sqlite3")
        if os.path.exists(db_path + "-wal"):
            os.replace(db_path + "-wal", aside + "-wal")
        _sync_folder(folder)
    _remove(db_path + "-wal")
    _remove(db_path + "-shm")


def _remove_parts(folder, stem):
    """Part files a crash left in `folder`."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith(f".{stem}-backup-") and (
            name.endswith(".part") or name.endswith(".part-journal")
        ):
            _remove(os.path.join(folder, name))


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _sync_folder(folder):
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def age_text(taken, now=None):
    """How long ago a backup was taken, for people: "today", "yesterday", "3 days ago"."""
    now = now or datetime.now()
    days = (now.date() - taken.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


# For tests and the time a daily backup is due
monotonic = time.monotonic
