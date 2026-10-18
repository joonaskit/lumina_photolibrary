"""Exporting for the UI: photos to a folder as a job (core/exporting.py).

One export at a time is run, on a thread of its own (workers.EXPORT, by the JobRunner of
qt_models/jobs.py); each file is rendered in a worker process, so the window never waits
for a render and a file that crashes its decoder fails only itself. A job queued or
stopped by a quit is run again at the next start, from the file it had got to; one whose
photos are on a drive that is not connected waits for it.

The photo open in the Develop view is rendered in the Develop process, from the decode
it holds there, instead of being decoded again in another.
"""

import os
from concurrent.futures.process import BrokenProcessPool
from functools import partial

from PySide6.QtCore import QObject, Signal

from lumina.core import db as dbm
from lumina.core import exporting
from lumina.core.edit import session as _session
from lumina.core.errors import PhotoError
from lumina.qt_models import workers
from lumina.qt_models.jobs import JobRunner


class ExportService(QObject):
    # A JobStatus, while one runs and when it ends
    job_changed = Signal(object)
    # An export ended: its job's id and its JobStatus
    ended = Signal(int, object)

    def __init__(self, library, parent=None):
        """`library` is the LibraryService: its database and drives."""
        super().__init__(parent)
        self.library = library
        self.jobs = JobRunner(
            self,
            library,
            {dbm.EXPORT},
            workers.EXPORT,
            prepare=lambda _job: partial(_export, lambda: self._in_develop),
            name=self._folder_name,
            changed=self.job_changed.emit,
            ended=lambda job, status: self.ended.emit(job.id, status),
            failed=lambda _job, error: f"Could not export: {error}",
        )
        library.mounts_changed.connect(self.jobs.wake)
        # The path of the photo open in the Develop process, or None: read on the export
        # thread
        self._in_develop = None

    def start(self):
        """Resume the exports a quit or a crash stopped."""
        self.jobs.start()

    def close(self):
        """For the app's exit: an export in progress stops and is queued for the next
        start."""
        self.jobs.close()

    def export(self, entries, folder, options, replace=False):
        """Queue an export of `entries` [(photo id, virtual copy)] to `folder` as `options`
        (ExportOptions) say. With `replace`, a file that is there is written over; without,
        it is kept and named as a failure. Returns the job's id."""
        if not entries:
            raise ValueError("No photos to export")
        job = exporting.queue(
            self.library.db, entries, folder, options, replace, self.library.mounts
        )
        self.jobs.next()
        return job

    def export_photo(self, photo_id, copy, destination, options, replace=False):
        """Queue an export of one photo, with the edit it has now, to the file
        `destination`. Returns the job's id."""
        # The edit the window saved last is the one exported
        self.library.settle()
        job = exporting.queue_file(self.library.db, photo_id, copy, destination, options, replace)
        self.jobs.next()
        return job

    def opened_in_develop(self, path):
        """The Develop view holds the photo at `path` decoded now (None: none)."""
        self._in_develop = os.path.realpath(path) if path is not None else None

    def is_busy(self):
        return self.jobs.is_busy()

    def cancel(self):
        """Stop the running export and drop those waiting."""
        self.jobs.cancel()

    def _folder_name(self, job):
        found = self.library.db.export_job(job.id)
        folder = found[2] if found else ""
        return folder.rstrip("/").rsplit("/", 1)[-1] or folder or "a folder"


def _render(in_develop, source, params, destination, options, replace, _should_stop):
    """On the export thread: the file is rendered in a worker process, after what is
    in view; in the Develop process when it is the photo open there."""
    try:
        if in_develop() == os.path.realpath(source):
            made = workers.call(
                workers.DEVELOP,
                _session.export_open,
                source,
                params,
                destination,
                options,
                replace,
                priority=workers.LATER,
            )
            if made is not None:
                return made
            # Closed meanwhile
        return workers.call(
            workers.DECODE,
            exporting.render_blind,
            source,
            params,
            destination,
            options,
            replace,
            priority=workers.LATER,
        )
    except BrokenProcessPool as error:
        raise PhotoError(source, "the decoder crashed on this file") from error


def _export(in_develop, library, job_id, mounts, should_stop, report):
    """On the export thread."""
    return exporting.run(library, job_id, mounts, should_stop, report, partial(_render, in_develop))
