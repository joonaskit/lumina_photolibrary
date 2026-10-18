"""Exporting for the UI: photos to a folder as a job (core/exporting.py).

One export at a time is run, on a thread of its own (workers.EXPORT); each file is
rendered in a worker process, so the window never waits for a render and a file that
crashes its decoder fails only itself. Jobs are in the library's database: one queued or
stopped by a quit is run again at the next start, from the file it had got to; one whose
photos are on a drive that is not connected waits for it.
"""

import threading
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from lumina.core import db as dbm
from lumina.core import exporting
from lumina.core import library as scanning
from lumina.core.edit import export as _export
from lumina.core.errors import PhotoError
from lumina.qt_models import workers

EXPORTING = exporting.EXPORTING
ExportOptions = _export.ExportOptions
FORMATS = _export.FORMATS


@dataclass(frozen=True)
class ExportStatus:
    # Where the files go: the folder's name
    name: str
    state: str
    phase: str | None
    done: int
    total: int | None
    message: str | None
    failures: tuple
    # Exports waiting after this one
    queued: int


class ExportService(QObject):
    # An ExportStatus, while one runs and when it ends
    job_changed = Signal(object)

    def __init__(self, library, parent=None):
        """`library` is the LibraryService: its database and drives."""
        super().__init__(parent)
        self.library = library
        self._quitting = threading.Event()
        self._stop = None
        self._running = None
        self._waiting = set()
        self.future = None
        library.mounts_changed.connect(self._drives_changed)

    def start(self):
        """Resume the exports a quit or a crash stopped."""
        self._next_job()

    def close(self):
        """For the app's exit: an export in progress stops and is queued for the next
        start."""
        self._quitting.set()
        if self._stop is not None:
            self._stop.set()

    def export(self, entries, folder, options, replace=False):
        """Queue an export of `entries` [(photo id, virtual copy)] to `folder` as `options`
        (ExportOptions) say. With `replace`, a file that is there is written over; without,
        it is kept and named as a failure. Returns the job's id."""
        if not entries:
            raise ValueError("No photos to export")
        job = exporting.queue(
            self.library.db, entries, folder, options, replace, self.library.mounts
        )
        self._next_job()
        return job

    def is_busy(self):
        return self._running is not None

    def cancel(self):
        """Stop the running export and drop those waiting."""
        for job in self._exports():
            if job.id != self._running:
                self.library.db.update_job(job.id, state=dbm.CANCELLED, message="Cancelled")
        if self._stop is not None:
            self._stop.set()

    def _drives_changed(self):
        self._waiting.clear()
        self._next_job()

    def _next_job(self):
        if self._running is not None or self._quitting.is_set():
            return
        job = next((job for job in self._exports() if job.id not in self._waiting), None)
        if job is not None:
            self._run(job)

    def _run(self, job):
        stop = threading.Event()
        self._stop = stop
        self._running = job.id
        name = self._folder_name(job.id)
        self.job_changed.emit(self._status(job.id, name))
        self.future = workers.submit(
            _run_export,
            self.library.path,
            job.id,
            self.library.mounts,
            stop,
            self._quitting,
            lambda *args: workers.call_on_main(self, self._on_progress, job.id, name, *args),
            on_done=lambda _state: self._on_ended(job.id, name),
            on_error=lambda error: self._on_failed(job.id, name, error),
            owner=self,
            process=workers.EXPORT,
        )

    def _on_progress(self, job_id, name, phase, done, total, _changed):
        if self._quitting.is_set() or self._running != job_id:
            return
        status = self._status(job_id, name)
        self.job_changed.emit(
            ExportStatus(name, dbm.RUNNING, phase, done, total, None, (), status.queued)
        )

    def _on_ended(self, job_id, name):
        self._running = self._stop = None
        if self._quitting.is_set():
            return
        self.job_changed.emit(self._status(job_id, name))
        self._next_job()

    def _on_failed(self, job_id, name, error):
        if isinstance(error, scanning.NotConnected):
            self._running = self._stop = None
            self._waiting.add(job_id)
            self.job_changed.emit(
                ExportStatus(
                    name, dbm.QUEUED, None, 0, None, f"Waiting for {error} to be connected", (), 0
                )
            )
            self._next_job()
            return
        if not self._quitting.is_set():
            self.library.db.update_job(
                job_id, state=dbm.FAILED_JOB, message=f"Could not export: {error}"
            )
        self._on_ended(job_id, name)

    def _status(self, job_id, name):
        db = self.library.db
        job = db.job(job_id)
        failures = tuple(db.failures(job_id)[:20]) if job.failures else ()
        queued = sum(1 for other in self._exports() if other.id != job_id)
        return ExportStatus(
            name, job.state, job.phase, job.done, job.total, job.message, failures, queued
        )

    def _exports(self):
        return [job for job in self.library.db.unfinished_jobs() if job.kind == exporting.EXPORT]

    def _folder_name(self, job_id):
        found = self.library.db.export_job(job_id)
        folder = found[2] if found else ""
        return folder.rstrip("/").rsplit("/", 1)[-1] or folder or "a folder"


def _render(source, params, destination, options, replace, _should_stop):
    """On the export thread: the file is rendered in a worker process."""
    try:
        return workers.call(
            True, exporting.render_blind, source, params, destination, options, replace
        )
    except BrokenProcessPool as error:
        raise PhotoError(source, "the decoder crashed on this file") from error


def _run_export(path, job_id, mounts, stop, quitting, report):
    """On the export thread, with a connection of its own."""
    with dbm.Library(path) as library:
        state = exporting.run(library, job_id, mounts, stop.is_set, report, _render)
        if state == dbm.CANCELLED and quitting.is_set():
            # Stopped by a quit, not by the user: run again at the next start
            library.update_job(job_id, state=dbm.QUEUED, message=None)
        return state
