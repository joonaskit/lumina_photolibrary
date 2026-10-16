"""Importing for the UI: reading a card, then copying its photos into the library as a
job (core/importing.py).

A card is read on a thread, newest read wins. One import at a time is copied, on a thread
of its own (workers.IMPORT), so a scan or a merge never waits for it, nor it for them.
Jobs are in the library's database: one queued or stopped by a quit is run again at
the next start, from the photo it had got to; one whose card or drive is not connected
waits for it. When an import is done its folder is scanned, which puts the photos in
the grid.
"""

import os
import shutil
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from lumina.core import db as dbm
from lumina.core import importing, metadata
from lumina.core import library as scanning
from lumina.qt_models import workers

READING, COPYING = importing.READING, importing.COPYING
LAYOUTS = tuple(importing.LAYOUTS)
NAMINGS = importing.NAMINGS
subfolder = importing.subfolder
file_name = importing.file_name


@dataclass(frozen=True)
class ImportStatus:
    # Where the photos go: the library folder's name
    name: str
    state: str
    phase: str | None
    done: int
    total: int | None
    message: str | None
    failures: tuple
    # Imports waiting after this one
    queued: int


class ImportService(QObject):
    # An ImportStatus, while one runs and when it ends
    job_changed = Signal(object)
    # The cards mounted changed: the list of their mount points
    cards_changed = Signal(object)
    # A card was plugged in: its mount point
    card_arrived = Signal(str)

    def __init__(self, library, exiftool=metadata.ExifTool, parent=None):
        """`library` is the LibraryService: its database, drives and scans."""
        super().__init__(parent)
        self.library = library
        self._exiftool = exiftool
        self._quitting = threading.Event()
        self._stop = None
        self._running = None
        self._waiting = set()
        self._reading = None
        self.future = None
        self.read_future = None
        self._cards = importing.find_cards(library.mounts)
        library.mounts_changed.connect(self._drives_changed)

    def start(self):
        """Resume the imports a quit or a crash stopped."""
        self._next_job()

    def close(self):
        """For the app's exit: an import in progress stops and is queued for the next
        start; a card being read stops."""
        self._quitting.set()
        if self._stop is not None:
            self._stop.set()
        if self._reading is not None:
            self._reading.set()

    # Reading a card

    def cards(self):
        """The mount points of the cards mounted now."""
        return list(self._cards)

    def read(self, path, on_done, on_progress=None):
        """Read the photos under `path` (a card or a folder) on a thread: then
        `on_done(Reading or None)` (None when stopped by a newer read) or, when it
        cannot be read, `on_done` with the error. `on_progress(done, total)` meanwhile.
        Only reads."""
        self.stop_reading()
        stop = threading.Event()
        self._reading = stop
        report = on_progress or (lambda done, total: None)
        self.read_future = workers.submit(
            _read,
            self.library.path,
            path,
            stop,
            self._exiftool,
            lambda done, total: workers.call_on_main(self, _unless(stop, report), done, total),
            on_done=_unless(stop, on_done),
            on_error=_unless(stop, on_done),
            owner=self,
            process=False,
        )

    def stop_reading(self):
        if self._reading is not None:
            self._reading.set()
            self._reading = None

    def free_space(self, path):
        """Bytes free on the filesystem of `path` (or of the nearest folder above it that
        is there), or None when it can't be told."""
        while not os.path.isdir(path) and os.path.dirname(path) != path:
            path = os.path.dirname(path)
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    # Importing

    def import_photos(self, candidates, destination, layout, naming, backup=None):
        """Queue an import of `candidates` (a Reading's) into the folder `destination`, in
        the library, and a copy of each into `backup` too unless None. Raises ValueError
        saying why it can't be."""
        importing.queue(
            self.library.db, candidates, destination, self.library.mounts, layout, naming, backup
        )
        self._next_job()

    def is_busy(self):
        return self._running is not None

    def cancel(self):
        """Stop the running import and drop those waiting."""
        for job in self._imports():
            if job.id != self._running:
                self.library.db.update_job(job.id, state=dbm.CANCELLED, message="Cancelled")
        if self._stop is not None:
            self._stop.set()

    def _drives_changed(self):
        cards = importing.find_cards(self.library.mounts)
        if cards != self._cards:
            arrived = [card for card in cards if card not in self._cards]
            self._cards = cards
            self.cards_changed.emit(list(cards))
            for card in arrived:
                self.card_arrived.emit(card)
        self._waiting.clear()
        self._next_job()

    def _next_job(self):
        if self._running is not None or self._quitting.is_set():
            return
        job = next((job for job in self._imports() if job.id not in self._waiting), None)
        if job is not None:
            self._run(job)

    def _run(self, job):
        stop = threading.Event()
        self._stop = stop
        self._running = job.id
        name = self._folder_name(job.folder_id)
        self.job_changed.emit(self._status(job.id, name))
        self.future = workers.submit(
            _run_import,
            self.library.path,
            job.id,
            self.library.mounts,
            stop,
            self._quitting,
            lambda *args: workers.call_on_main(self, self._on_progress, job.id, name, *args),
            on_done=lambda _state: self._on_ended(job, name),
            on_error=lambda error: self._on_failed(job, name, error),
            owner=self,
            process=workers.IMPORT,
        )

    def _on_progress(self, job_id, name, phase, done, total, _changed):
        if self._quitting.is_set() or self._running != job_id:
            return
        status = self._status(job_id, name)
        self.job_changed.emit(
            ImportStatus(name, dbm.RUNNING, phase, done, total, None, (), status.queued)
        )

    def _on_ended(self, job, name):
        self._running = self._stop = None
        if self._quitting.is_set():
            return
        status = self._status(job.id, name)
        self.job_changed.emit(status)
        if job.folder_id is not None and status.state in (dbm.DONE, dbm.CANCELLED):
            # What was copied goes in the grid with the folder's next scan
            self.library.rescan(job.folder_id)
        self._next_job()

    def _on_failed(self, job, name, error):
        if isinstance(error, scanning.NotConnected):
            self._running = self._stop = None
            self._waiting.add(job.id)
            self.job_changed.emit(
                ImportStatus(
                    name,
                    dbm.QUEUED,
                    None,
                    0,
                    None,
                    f"Waiting for {error} to be connected",
                    (),
                    0,
                )
            )
            self._next_job()
            return
        if not self._quitting.is_set():
            self.library.db.update_job(
                job.id, state=dbm.FAILED_JOB, message=f"Could not import: {error}"
            )
        self._on_ended(job, name)

    def _status(self, job_id, name):
        db = self.library.db
        job = db.job(job_id)
        failures = tuple(db.failures(job_id)[:20]) if job.failures else ()
        queued = sum(1 for other in self._imports() if other.id != job_id)
        return ImportStatus(
            name, job.state, job.phase, job.done, job.total, job.message, failures, queued
        )

    def _imports(self):
        return [job for job in self.library.db.unfinished_jobs() if job.kind == importing.IMPORT]

    def _folder_name(self, folder_id):
        info = next((f for f in self.library.folders() if f.id == folder_id), None)
        return info.name if info is not None else "the library"


def _unless(stop, fn):
    """`fn`, but doing nothing once `stop` is set: the answer of a read no longer wanted."""

    def call(*args):
        if not stop.is_set():
            fn(*args)

    return call


def _read(path, root, stop, exiftool, report):
    """On a worker thread, with a connection of its own."""
    with dbm.Library(path) as library:
        return importing.read_source(library, root, stop.is_set, report, exiftool)


def _run_import(path, job_id, mounts, stop, quitting, report):
    """On the import thread, with a connection of its own."""
    with dbm.Library(path) as library:
        state = importing.run(library, job_id, mounts, stop.is_set, report)
        if state == dbm.CANCELLED and quitting.is_set():
            # Stopped by a quit, not by the user: run again at the next start
            library.update_job(job_id, state=dbm.QUEUED, message=None)
        return state
