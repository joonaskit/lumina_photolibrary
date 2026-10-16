"""Merging photos for the UI: HDR merges of brackets (core/hdr.py), as jobs.

One merge at a time, on a thread of its own (workers.MERGE), its steps in a worker
process of its own (workers.MERGING), so neither a scan nor the grid's thumbnails wait
for it, nor it for them. Jobs are in the library's database: one queued or stopped by
a quit is run again at the next start, from the frames it had decoded; one whose drive
is not connected waits for it. When a merge is done its folder is scanned, which
puts the new photo in the grid.
"""

import json
import os
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from lumina.core import db as dbm
from lumina.core import hdr
from lumina.core import library as scanning
from lumina.qt_models import workers

DECODING, PLANNING, MERGING = hdr.DECODING, hdr.PLANNING, hdr.MERGING
DEGHOSTING = tuple(hdr.DEGHOST)
# How far around a photo a bracket is looked for, in seconds
_NEAR = 60


@dataclass(frozen=True)
class Frame:
    """A photo as the merge dialog lists it."""

    id: int
    name: str
    # "1/60 s · f/8 · ISO 100", or "" when not known
    exposure: str


@dataclass(frozen=True)
class MergeStatus:
    # The photo the result is named after
    name: str
    state: str
    phase: str | None
    done: int
    total: int | None
    message: str | None
    failures: tuple
    # Merges waiting after this one
    queued: int


class MergeService(QObject):
    # A MergeStatus, while one runs and when it ends
    job_changed = Signal(object)

    def __init__(self, library, work_root=None, parent=None):
        """`library` is the LibraryService: its database, drives and scans."""
        super().__init__(parent)
        self.library = library
        self.work_root = work_root or hdr.default_work_root()
        self._quitting = threading.Event()
        self._stop = None
        self._running = None
        # Jobs whose drive was not connected when last tried: waiting for the drives
        self._waiting = set()
        self.future = None
        library.mounts_changed.connect(self._drives_changed)

    def start(self):
        """Resume the merges a quit or a crash stopped."""
        self._next_job()

    def close(self):
        """For the app's exit: a merge in progress stops and is queued for the next start,
        its decoded frames kept."""
        self._quitting.set()
        if self._stop is not None:
            self._stop.set()

    # Choosing

    def bracket(self, photo_ids):
        """The photos to merge for the ones chosen: those, or with one chosen, the bracket
        it was shot in (photos moments apart at different exposures), if it is in one."""
        db = self.library.db
        if len(photo_ids) != 1:
            return list(photo_ids)
        [row] = db.photo_rows(photo_ids) or [None]
        if row is None:
            return list(photo_ids)
        near = db.photos_near(row["folder_id"], row["taken"], _NEAR)
        return hdr.bracket_around(near, row["id"]) or list(photo_ids)

    def frames(self, photo_ids):
        """[Frame] of the photos, in the order a merge takes them: by exposure."""
        rows = self.library.db.photo_rows(photo_ids)
        rows.sort(key=lambda row: hdr.exposure_value(row) or 0.0)
        return [Frame(row["id"], os.path.basename(row["path"]), _exposure(row)) for row in rows]

    def check(self, photo_ids):
        """Why the photos cannot be merged, or None when they can."""
        rows = self.library.db.photo_rows(photo_ids)
        try:
            hdr.check_frames(rows)
        except ValueError as error:
            return str(error)
        missing = next((row for row in rows if not row["available"]), None)
        if missing is not None:
            return f"{os.path.basename(missing['path'])} is missing"
        return None

    def merge_hdr(self, photo_ids, deghost="off", align=True):
        """Queue an HDR merge of the photos. Raises ValueError saying why they cannot be."""
        hdr.queue(self.library.db, photo_ids, deghost, align)
        self._next_job()

    # Jobs

    def is_busy(self):
        return self._running is not None

    def cancel(self):
        """Stop the running merge and drop those waiting."""
        for job in self._merges():
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
        job = next((job for job in self._merges() if job.id not in self._waiting), None)
        if job is not None:
            self._run(job)

    def _run(self, job):
        stop = threading.Event()
        self._stop = stop
        self._running = job.id
        name = self._name(job)
        self.job_changed.emit(self._status(job.id, name))
        self.future = workers.submit(
            _run_merge,
            self.library.path,
            job.id,
            self.library.mounts,
            self.work_root,
            stop,
            self._quitting,
            lambda *args: workers.call_on_main(self, self._on_progress, job.id, name, *args),
            on_done=lambda _state: self._on_ended(job, name),
            on_error=lambda error: self._on_failed(job, name, error),
            owner=self,
            process=workers.MERGE,
        )

    def _on_progress(self, job_id, name, phase, done, total, _changed):
        if self._quitting.is_set() or self._running != job_id:
            return
        status = self._status(job_id, name)
        self.job_changed.emit(
            MergeStatus(name, dbm.RUNNING, phase, done, total, None, (), status.queued)
        )

    def _on_ended(self, job, name):
        self._running = self._stop = None
        if self._quitting.is_set():
            return
        status = self._status(job.id, name)
        self.job_changed.emit(status)
        if status.state == dbm.DONE and job.folder_id is not None:
            # The new photo goes in the grid with the folder's next scan
            self.library.rescan(job.folder_id)
        self._next_job()

    def _on_failed(self, job, name, error):
        if isinstance(error, scanning.NotConnected):
            self._running = self._stop = None
            self._waiting.add(job.id)
            self._next_job()
            return
        if not self._quitting.is_set():
            self.library.db.update_job(
                job.id, state=dbm.FAILED_JOB, message=f"Could not merge: {error}"
            )
        self._on_ended(job, name)

    def _status(self, job_id, name):
        db = self.library.db
        job = db.job(job_id)
        failures = tuple(db.failures(job_id)[:20]) if job.failures else ()
        queued = sum(1 for other in self._merges() if other.id != job_id)
        return MergeStatus(
            name, job.state, job.phase, job.done, job.total, job.message, failures, queued
        )

    def _merges(self):
        return [job for job in self.library.db.unfinished_jobs() if job.kind == hdr.HDR]

    def _name(self, job):
        derived = self.library.db.derived(job.derived_id) if job.derived_id else None
        if derived is None:
            return "photos"
        return json.loads(derived.settings).get("name", "photos")


def _exposure(row):
    parts = []
    time = row.get("exposure_time")
    if time:
        parts.append(f"1/{round(1 / time)} s" if time < 0.5 else f"{time:g} s")
    if row.get("aperture"):
        parts.append(f"f/{row['aperture']:g}")
    if row.get("iso"):
        parts.append(f"ISO {row['iso']}")
    return " · ".join(parts)


def _call_in_process(fn, *args):
    return workers.call(workers.MERGING, fn, *args)


def _run_merge(path, job_id, mounts, work_root, stop, quitting, report):
    """On the merge thread, with a connection of its own."""
    with dbm.Library(path) as library:
        state = hdr.run(
            library,
            job_id,
            mounts,
            call=_call_in_process,
            should_stop=stop.is_set,
            report=report,
            work_root=work_root,
            keep_work=quitting.is_set,
        )
        if state == dbm.CANCELLED and quitting.is_set():
            # Stopped by a quit, not by the user: run again at the next start
            library.update_job(job_id, state=dbm.QUEUED, message=None)
        return state
