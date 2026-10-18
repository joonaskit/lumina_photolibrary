"""Importing for the UI: reading a card, then copying its photos into the library as a
job (core/importing.py).

A card is read on a thread, newest read wins. One import at a time is copied, on a thread
of its own (workers.IMPORT, by the JobRunner of qt_models/jobs.py), so a scan or a merge
never waits for it, nor it for them. A job queued or stopped by a quit is run again at
the next start, from the photo it had got to; one whose card or drive is not connected
waits for it. When an import is done its folder is scanned, which puts the photos in
the grid.
"""

import contextlib
import os
import shutil
import threading
from functools import partial

from PySide6.QtCore import QObject, QTimer, Signal

from lumina.core import db as dbm
from lumina.core import importing, metadata, tether, volumes
from lumina.core.errors import CameraBusy
from lumina.qt_models import workers
from lumina.qt_models.jobs import JobRunner, waiting_for

LAYOUTS = tuple(importing.LAYOUTS)
NAMINGS = importing.NAMINGS
subfolder = importing.subfolder
file_name = importing.file_name

# How often an import waiting for its camera looks for it again, in milliseconds
_CAMERA_RETRY = 4000


class ImportService(QObject):
    # A JobStatus, while one runs and when it ends
    job_changed = Signal(object)
    # The cards mounted changed: the list of their mount points
    cards_changed = Signal(object)
    # A card was plugged in: its mount point
    card_arrived = Signal(str)

    def __init__(
        self,
        library,
        exiftool=metadata.ExifTool,
        camera=tether.GPhotoCamera,
        detect=tether.detect,
        parent=None,
    ):
        """`library` is the LibraryService: its database, drives and scans. `camera` makes
        the camera photos are imported from over USB, `detect` finds the cameras there."""
        super().__init__(parent)
        self.library = library
        self._exiftool = exiftool
        self._camera = camera
        self._detect = detect
        # One program at a time has the camera: a read of it or an import from it
        self._camera_lock = threading.Lock()
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.setInterval(_CAMERA_RETRY)
        self._retry.timeout.connect(self._look_for_camera)
        self.jobs = JobRunner(
            self,
            library,
            {dbm.IMPORT},
            workers.IMPORT,
            prepare=self._prepare,
            name=self._folder_name,
            changed=self.job_changed.emit,
            ended=self._ended,
            waits=self._waits,
            failed=lambda _job, error: f"Could not import: {error}",
        )
        self._reading = None
        self.read_future = None
        self._cards = importing.find_cards(library.mounts)
        library.mounts_changed.connect(self._drives_changed)

    def start(self):
        """Resume the imports a quit or a crash stopped."""
        self.jobs.start()

    def close(self):
        """For the app's exit: an import in progress stops and is queued for the next
        start; a card being read stops."""
        self.jobs.close()
        self.stop_reading()

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
        stop = workers.Token()
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
            lane=workers.IO,
        )

    def cameras(self):
        """[(model, port)] of the cameras on USB now."""
        return self._detect()

    def read_camera(self, on_done, on_progress=None):
        """Read the photos on the camera on USB as `read` does a card: then `on_done(Reading
        or None)`, or the error (NotConnected, CameraBusy with who has it and how to let
        it go). Only reads."""
        self.stop_reading()
        stop = workers.Token()
        self._reading = stop
        report = on_progress or (lambda done, total: None)
        self.read_future = workers.submit(
            _read_camera,
            self.library.path,
            self._camera,
            self._camera_lock,
            stop,
            lambda done, total: workers.call_on_main(self, _unless(stop, report), done, total),
            on_done=_unless(stop, on_done),
            on_error=_unless(stop, on_done),
            owner=self,
            lane=workers.IO,
        )

    def stop_reading(self):
        if self._reading is not None:
            self._reading.cancel()
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
        self.jobs.next()

    def is_busy(self):
        return self.jobs.is_busy()

    def cancel(self):
        """Stop the running import and drop those waiting."""
        self.jobs.cancel()

    def _drives_changed(self):
        cards = importing.find_cards(self.library.mounts)
        if cards != self._cards:
            arrived = [card for card in cards if card not in self._cards]
            self._cards = cards
            self.cards_changed.emit(list(cards))
            for card in arrived:
                self.card_arrived.emit(card)
        self.jobs.wake()

    def _look_for_camera(self):
        # The bar keeps saying it waits: it does not flicker
        self.jobs.wake(tell=False)

    def _prepare(self, job):
        if any(importing.is_camera(i.source_key) for i in self.library.db.import_items(job.id)):
            # The camera is for this import: a read of it for the dialog gives way
            self.stop_reading()
        return partial(_run_import, self._camera, self._camera_lock)

    def _ended(self, job, status):
        if job.folder_id is not None and status.state in (dbm.DONE, dbm.CANCELLED):
            # What was copied goes in the grid with the folder's next scan
            self.library.rescan(job.folder_id)

    def _waits(self, _job, error):
        if str(error).startswith("the camera") or isinstance(error, CameraBusy):
            # A camera plugged in changes no mount: look for it now and then
            self._retry.start()
        return str(error) if isinstance(error, CameraBusy) else waiting_for(error)

    def _folder_name(self, job):
        info = next((f for f in self.library.folders() if f.id == job.folder_id), None)
        return info.name if info is not None else "the library"


def _unless(stop, fn):
    """`fn`, but doing nothing once `stop` is set: the answer of a read no longer wanted."""

    def call(*args):
        if not stop.cancelled():
            fn(*args)

    return call


def _read(path, root, stop, exiftool, report):
    """On a worker thread, with a connection of its own."""
    with dbm.Library(path) as library:
        return importing.read_source(library, root, stop, report, exiftool)


def _read_camera(path, make_camera, lock, stop, report):
    """On a worker thread, with a connection of its own."""
    if not lock.acquire(blocking=False):
        raise CameraBusy("Lumina is importing from the camera: wait until it is done.")
    try:
        with dbm.Library(path) as library:
            return importing.read_camera(library, make_camera, stop, report)
    finally:
        lock.release()


def _run_import(make_camera, lock, library, job_id, mounts, should_stop, report):
    """On the import thread."""
    uses_camera = any(importing.is_camera(item.source_key) for item in library.import_items(job_id))
    with lock if uses_camera else contextlib.nullcontext():
        return importing.run(
            library, job_id, mounts, should_stop, report, make_camera, volumes.read_mounts
        )
