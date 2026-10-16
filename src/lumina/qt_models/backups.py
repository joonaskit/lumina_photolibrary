"""Backups of the library database for the UI (core/backup.py).

A daily backup is taken at the start, and checked for every hour after, when the
newest is over a day old, into the
backups folder beside the database and into a second folder the user chose, if any,
each rotated after; on a thread of its own (workers.BACKUP), as it reads the whole
database. The app goes on using the database meanwhile.

A restore can't be made with the database open, so it is made by the next start: the
app quits, then starts again with the backup to restore (`application.run`), which
puts it in place before anything opens the database.
"""

import os
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from lumina.core import backup
from lumina.core import db as dbm
from lumina.qt_models import workers

# For the UI, which does not import core/: a backup's kinds, and how many are kept
DAILY, BEFORE_MIGRATION, BEFORE_RESTORE, DAMAGED = (
    backup.DAILY,
    backup.BEFORE_MIGRATION,
    backup.BEFORE_RESTORE,
    backup.DAMAGED,
)
KEEP_DAILY, KEEP_WEEKLY = backup.KEEP_DAILY, backup.KEEP_WEEKLY

# How often a running app looks whether a daily backup is due
_DUE_MS = 3600 * 1000


@dataclass(frozen=True)
class BackupInfo:
    path: str
    kind: str
    # Local time
    taken: datetime
    size: int
    # For a backup before a migration: the schema the library had
    schema: int | None = None


def default_path():
    return dbm.default_path()


def default_folder(path=None):
    return backup.default_folder(path or dbm.default_path())


def check(path=None):
    """None when the library database is sound; else what is wrong with it."""
    return backup.check(os.fspath(path or dbm.default_path()))


def backups(path, folders):
    """The backups of the database at `path` in `folders`, newest first."""
    found = []
    for folder in folders:
        for made in backup.backups(path, folder):
            found.append(BackupInfo(made.path, made.kind, made.taken, made.size, made.schema))
    found.sort(key=lambda made: (made.taken, made.path), reverse=True)
    return found


def contents(backup_path):
    """What a backup holds (photos, edits, schema, vectors), or None when it can't be read."""
    try:
        return backup.contents(backup_path)
    except backup.BackupError:
        return None


def restore(backup_path, path=None):
    """Put the backup in place of the library database, which nothing may have open.
    None when done, else why not; the database is then as it was."""
    try:
        backup.restore(
            backup_path, os.fspath(path or dbm.default_path()), newest_schema=dbm.SCHEMA_VERSION
        )
    except backup.BackupError as error:
        return str(error)
    return None


class BackupService(QObject):
    # The backups there are changed: one was made or rotated away
    changed = Signal()
    # A backup could not be made: why, naming the folder
    failed = Signal(str)
    # The user chose a backup to restore (`restore_from`): the app quits and restores it
    restore_requested = Signal()

    def __init__(self, path=None, due_ms=_DUE_MS, parent=None):
        super().__init__(parent)
        self.path = os.fspath(path or dbm.default_path())
        # A second folder backed up to (another drive), or None; the application sets
        # it and how many are kept from the settings
        self.extra_folder = None
        self.keep_daily = KEEP_DAILY
        self.keep_weekly = KEEP_WEEKLY
        # The backup the user chose to restore, or None
        self.restore_from = None
        self.future = None
        self._timer = QTimer(self)
        self._timer.setInterval(due_ms)
        self._timer.timeout.connect(lambda: self.back_up(due_only=True))

    def folders(self):
        """Where backups go: beside the database, and the folder chosen, if any."""
        folders = [backup.default_folder(self.path)]
        if self.extra_folder and os.path.abspath(self.extra_folder) != folders[0]:
            folders.append(self.extra_folder)
        return folders

    def backups(self):
        return backups(self.path, self.folders())

    def is_busy(self):
        return self.future is not None

    def start(self):
        """Back up into each folder whose newest daily backup is over a day old, now and
        from now on."""
        self.back_up(due_only=True)
        self._timer.start()

    def close(self):
        """For the app's exit. A backup being made is finished: it is short."""
        self._timer.stop()

    def back_up(self, due_only=False):
        """Back up now (into the folders where one is due, with `due_only`), then rotate.
        One at a time: asked for while one runs, nothing more is done."""
        if self.future is not None:
            return
        self.future = workers.submit(
            _back_up,
            self.path,
            self.folders(),
            self.keep_daily,
            self.keep_weekly,
            due_only,
            on_done=self._on_done,
            on_error=lambda error: self._on_done([str(error)]),
            owner=self,
            process=workers.BACKUP,
        )

    def _on_done(self, problems):
        self.future = None
        self.changed.emit()
        for problem in problems:
            self.failed.emit(problem)

    def contents(self, backup_path, on_done):
        """`on_done(Contents or None)`, read on a thread."""
        workers.submit(
            contents,
            backup_path,
            on_done=on_done,
            on_error=lambda _error: on_done(None),
            owner=self,
            process=workers.BACKUP,
        )

    def request_restore(self, backup_path):
        """Restore `backup_path` at once: the app quits, and starts again on it."""
        self.restore_from = backup_path
        self.restore_requested.emit()


def _back_up(path, folders, keep_daily, keep_weekly, due_only):
    """On the backup thread: [why] for each folder that could not be backed up into."""
    problems = []
    for number, folder in enumerate(folders):
        # A folder of the user's that is not there is a drive not connected: not made,
        # or the backup would go on the disk under where it is mounted
        if number > 0 and not os.path.isdir(folder):
            problems.append(f"Could not back up the library into {folder}: it is not there")
            continue
        if due_only and not backup.is_due(path, folder):
            continue
        try:
            backup.back_up(path, folder)
            backup.rotate(path, folder, keep_daily, keep_weekly)
        except (backup.BackupError, OSError) as error:
            problems.append(f"Could not back up the library: {error}")
    return problems
