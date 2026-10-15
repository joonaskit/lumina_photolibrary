"""The library for the UI: its folders, its scan jobs, and which drives are connected.

Jobs run one at a time on a worker thread, each with its own database connection;
progress comes back to the main thread as queued calls. The mounts are read again
every few seconds: a drive that is plugged in has its folders scanned, one that is
unplugged has its photos shown as not connected.

At the start every unfinished job (a quit or a crash in the middle of one) is run
again, and every connected folder is scanned for what changed while the app was
not running. A quit in the middle of a job leaves it queued for the next start.
"""

import os
import threading
import time
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, QTimer, Signal

from lumina.core import db as dbm
from lumina.core import library as scanning
from lumina.core import metadata, thumbs, volumes
from lumina.core.edit import params as _params
from lumina.qt_models import workers

# For the application, which tells the user
NewerDatabase = dbm.NewerDatabase
# For the UI: an edit made by a newer Lumina
NewerParams = _params.NewerParams

# For the UI, which does not import core/: a job's states and steps
QUEUED, RUNNING, DONE, FAILED, CANCELLED = (
    dbm.QUEUED,
    dbm.RUNNING,
    dbm.DONE,
    dbm.FAILED_JOB,
    dbm.CANCELLED,
)
FINDING, DETAILS = scanning.FINDING, scanning.DETAILS

# How often the mounts are read, and how often the grid hears of photos a scan wrote
_MOUNTS_MS = 3000
_PHOTOS_EVERY = 2.0
# How soon after the thumbnails were pruned a job's end prunes them again, in seconds
_PRUNE_EVERY = 600.0


@dataclass(frozen=True)
class FolderInfo:
    id: int
    name: str
    # Where it is now, or where it was when last seen
    path: str
    connected: bool
    # The drive's label, for one that is not connected
    volume_label: str | None


@dataclass(frozen=True)
class JobStatus:
    folder_name: str
    state: str
    phase: str | None
    done: int
    total: int | None
    message: str | None
    failures: tuple
    # Jobs waiting after this one
    queued: int


@dataclass(frozen=True)
class CopiedSettings:
    """Sections of an edit, kept to put over other photos."""

    document: dict
    # What the photo was called, and which sections: for telling the user
    name: str
    sections: tuple


@dataclass(frozen=True)
class PasteResult:
    # How many photos got the settings, and (name, why) for each that could not
    done: int
    failed: tuple


class LibraryService(QObject):
    folders_changed = Signal()
    # Photos were added or changed: the grid reads them again
    photos_changed = Signal()
    # The drives connected changed
    mounts_changed = Signal()
    presets_changed = Signal()
    # Settings were copied: `copied` has them
    settings_copied = Signal()
    # A JobStatus, while one runs and when it ends
    job_changed = Signal(object)
    # The job ended: a JobStatus
    job_ended = Signal(object)
    # The thumbnail cache was looked after: a thumbs.Pruned
    thumbnails_pruned = Signal(object)

    def __init__(
        self,
        path=None,
        read_mounts=volumes.read_mounts,
        exiftool=metadata.ExifTool,
        mounts_ms=_MOUNTS_MS,
        parent=None,
    ):
        super().__init__(parent)
        self.path = os.fspath(path or dbm.default_path())
        self._read_mounts = read_mounts
        self._exiftool = exiftool
        # The main thread's connection: small reads (folders, filter choices) only
        self.db = dbm.Library(self.path)
        self.mounts = read_mounts()
        self._running = None
        self._stop = None
        self.future = None
        # What Copy Settings kept, or None
        self.copied = None
        self._quitting = threading.Event()
        self._remove_after = set()
        self._photos_told = 0.0
        # The most the thumbnail cache may take, in bytes, or None for no limit; the
        # application sets it from the settings
        self.thumbnail_limit = None
        self._pruning = False
        self._prune_again = False
        self._pruned_at = None
        self.prune_future = None
        self._timer = QTimer(self)
        self._timer.setInterval(mounts_ms)
        self._timer.timeout.connect(self.check_mounts)

    def start(self):
        """Resume unfinished jobs, scan every connected folder, and follow the drives."""
        for folder in self.db.folders():
            self.db.add_job(scanning.SCAN, folder.id)
        self._timer.start()
        self._next_job()
        self.prune_thumbnails()

    def close(self):
        """For the app's exit: a running job stops and stays queued for the next start."""
        self._timer.stop()
        self._quitting.set()
        if self._stop is not None:
            self._stop.set()
        self.db.close()

    # Folders

    def folders(self):
        result = []
        for folder in self.db.folders():
            where = volumes.resolve(folder.volume_key, folder.path, self.mounts)
            shown = where or _last_seen(folder)
            name = os.path.basename(shown.rstrip("/")) or shown
            result.append(FolderInfo(folder.id, name, shown, where is not None, folder.label))
        return result

    def add_folder(self, path):
        """Add the folder at `path` and scan it. Returns its id (the id of the folder
        that has it already, when it is in the library). Raises ValueError with the
        reason when it can't be added."""
        path = os.path.realpath(path)
        if not os.path.isdir(path):
            raise ValueError(f"Not a folder: {path}")
        self.mounts = self._read_mounts()
        where = volumes.locate(path, self.mounts)
        if where is None:
            raise ValueError(f"No mounted filesystem holds {path}")
        mount = volumes.mount_of(path, self.mounts)
        folder = self.db.add_folder(*where, label=mount.label, mount=mount.mount_point)
        self.folders_changed.emit()
        self.rescan(folder)
        return folder

    def remove_folder(self, folder_id):
        """Forget a folder and its photos; the files are not touched. A scan of it in
        progress is stopped first."""
        if self._running is not None and self._running[1] == folder_id:
            self._remove_after.add(folder_id)
            self._stop.set()
            return
        self.db.remove_folder(folder_id)
        self.folders_changed.emit()
        self.photos_changed.emit()

    def rescan(self, folder_id=None):
        """Scan one folder, or all."""
        ids = [folder_id] if folder_id is not None else [f.id for f in self.db.folders()]
        for folder in ids:
            self.db.add_job(scanning.SCAN, folder)
        self._next_job()

    def is_connected(self, folder_id):
        folder = self.db.folder(folder_id)
        return folder is not None and (
            volumes.resolve(folder.volume_key, folder.path, self.mounts) is not None
        )

    def resolve(self, volume_key, path):
        """Where a photo is now, or None when its drive is not connected."""
        return volumes.resolve(volume_key, path, self.mounts)

    def choices(self, folder_id=None):
        return self.db.choices(folder_id)

    def photo_details(self, photo_id):
        """Everything the library knows of one photo, as a dict, or None."""
        return self.db.photo(photo_id)

    # Flags and ratings

    def set_flags(self, flags):
        """{photo id: flag}. The grid's model keeps its rows up to date itself."""
        self.db.set_flags(flags)

    def flags(self, ids):
        return self.db.flags(ids)

    def set_ratings(self, ratings):
        """{photo id: stars, 0 for none}."""
        self.db.set_ratings(ratings)

    def ratings(self, ids):
        return self.db.ratings(ids)

    # Edits and presets

    def edit_json(self, photo_id):
        """The photo's edit as parameters JSON, or None."""
        return self.db.edit(photo_id)

    def save_edit(self, photo_id, params_json):
        """Store an edit (None: none, the original). The grid hears of it."""
        if params_json is None:
            self.db.delete_edit(photo_id)
        else:
            self.db.save_edit(photo_id, params_json)
        self.photos_changed.emit()

    # Copying settings from one photo to others

    def settings_document(self, photo_id, sections):
        """The `sections` of the photo's own edit as a document to put over other photos
        (see `paste_settings`). Raises NewerParams for an edit from a newer Lumina."""
        stored = self.db.edit(photo_id)
        edit = _params.from_json(stored) if stored else _params.Params()
        return _params.preset(edit, sections)

    def copy_settings(self, document, name, sections):
        """Keep `document` (a photo's `settings_document`, or the Develop view's) of the
        photo called `name` to paste later."""
        self.copied = CopiedSettings(document, name, tuple(sections))
        self.settings_copied.emit()

    def paste_settings(self, photo_ids, document=None):
        """Put the sections of `document` (what was copied, without one) over the edits
        of the photos `photo_ids`, in one go: the other sections of each edit stay as
        they were. A photo whose edit is from a newer Lumina is left alone and named in
        the result's `failed`. The photos' files are not touched, nor is a photo on a
        drive that is not connected left out: edits live in the library."""
        document = document if document is not None else self.copied.document
        edits, failed = {}, []
        for photo_id in photo_ids:
            try:
                edits[photo_id] = _params.pasted(self.db.edit(photo_id), document)
            except _params.NewerParams as error:
                failed.append((self._photo_name(photo_id), str(error)))
        self.db.save_edits(edits)
        if edits:
            self.photos_changed.emit()
        return PasteResult(len(edits), tuple(failed))

    def _photo_name(self, photo_id):
        photo = self.db.photo(photo_id)
        return os.path.basename(photo["path"]) if photo else str(photo_id)

    def presets(self):
        return self.db.presets()

    def save_preset(self, name, params_json):
        self.db.save_preset(name, params_json)
        self.presets_changed.emit()

    def delete_preset(self, name):
        self.db.delete_preset(name)
        self.presets_changed.emit()

    # Drives

    def check_mounts(self):
        mounts = self._read_mounts()
        if mounts == self.mounts:
            return
        before = volumes.mounted_ids(self.mounts)
        self.mounts = mounts
        self.mounts_changed.emit()
        self.folders_changed.emit()
        # A drive came: what is on it may have changed while it was away
        arrived = volumes.mounted_ids(mounts) - before
        for folder in self.db.folders():
            if folder.volume_key in arrived:
                self.db.add_job(scanning.SCAN, folder.id)
        self._next_job()

    # The thumbnail cache

    def set_thumbnail_limit(self, limit):
        """The most the cache may take, in bytes, or None for no limit: and prune to it."""
        self.thumbnail_limit = limit
        self.prune_thumbnails()

    def prune_thumbnails(self):
        """Delete the thumbnails used longest ago while the cache is over its limit, on a
        thread of its own; those of photos on drives that are not connected last. At the
        start, when the limit changes, and after a scan. It is the cache only: no photo,
        and nothing in the library, is touched."""
        if self.thumbnail_limit is None or self._quitting.is_set():
            return
        if self._pruning:
            self._prune_again = True
            return
        self._pruning = True
        self._prune_again = False
        keys = [mount.volume_id for mount in self.mounts]
        self.prune_future = workers.submit(
            _prune_thumbnails,
            self.path,
            thumbs.default_cache_dir(),
            self.thumbnail_limit,
            keys,
            on_done=self._on_pruned,
            on_error=lambda _error: self._on_pruned(None),
            owner=self,
            process=workers.CACHE,
        )

    def _on_pruned(self, result):
        self._pruning = False
        self._pruned_at = time.monotonic()
        if self._quitting.is_set():
            return
        if result is not None:
            self.thumbnails_pruned.emit(result)
        if self._prune_again:
            self.prune_thumbnails()

    def thumbnail_usage(self, on_done):
        """`on_done(bytes)` with what the thumbnails take, or None when it can't be
        told: counted on a thread, as it is a walk of every thumbnail."""
        workers.submit(
            thumbs.cache_size,
            thumbs.default_cache_dir(),
            on_done=on_done,
            on_error=lambda _error: on_done(None),
            owner=self,
            process=workers.CACHE,
        )

    # Jobs

    def is_busy(self):
        return self._running is not None

    def cancel(self):
        """Stop the running scan and drop the queued ones. Indexing for search is the
        SearchService's."""
        for job in self._scans():
            if self._running is None or job.id != self._running[0]:
                self.db.update_job(job.id, state=dbm.CANCELLED, message="Cancelled")
        if self._stop is not None:
            self._stop.set()

    def _next_job(self):
        if self._running is not None or self._quitting.is_set():
            return
        for job in self._scans():
            if self.is_connected(job.folder_id):
                self._run(job)
                return
        # What is left waits for its drive

    def _run(self, job):
        stop = threading.Event()
        self._stop = stop
        self._running = (job.id, job.folder_id)
        name = self._folder_name(job.folder_id)
        quitting = self._quitting
        self._tell(job.id, name)
        # Kept for the tests, which wait for a job to end
        self.future = workers.submit(
            _run_scan,
            self.path,
            job.id,
            job.folder_id,
            self.mounts,
            stop,
            quitting,
            self._exiftool,
            lambda *args: workers.call_on_main(self, self._on_progress, job.id, name, *args),
            on_done=lambda state: self._on_ended(job.id, job.folder_id, name, state),
            on_error=lambda error: self._on_failed(job.id, job.folder_id, name, error),
            owner=self,
            process=False,
        )

    def _on_progress(self, job_id, name, phase, _done, _total, changed):
        if self._quitting.is_set() or self._running is None or self._running[0] != job_id:
            return
        # The phase the scan reported, not the one the database has by the time this is
        # delivered: a quick phase would otherwise never be told
        self.job_changed.emit(replace(self._status(job_id, name), phase=phase))
        now = time.monotonic()
        if changed and now - self._photos_told >= _PHOTOS_EVERY:
            self._photos_told = now
            self.photos_changed.emit()

    def _on_ended(self, job_id, folder_id, name, _state):
        self._running = self._stop = None
        if self._quitting.is_set():
            return
        status = self._status(job_id, name)
        if folder_id in self._remove_after:
            self._remove_after.discard(folder_id)
            self.db.remove_folder(folder_id)
        self.folders_changed.emit()
        self.photos_changed.emit()
        self.job_changed.emit(status)
        self.job_ended.emit(status)
        self._next_job()
        if self._pruned_at is None or time.monotonic() - self._pruned_at >= _PRUNE_EVERY:
            self.prune_thumbnails()

    def _on_failed(self, job_id, folder_id, name, error):
        if isinstance(error, scanning.NotConnected):
            # Unplugged as it started: the job waits for the drive
            self._running = self._stop = None
            self.mounts = self._read_mounts()
            self.folders_changed.emit()
            self._next_job()
            return
        message = f"{type(error).__name__}: {error}"
        if not self._quitting.is_set():
            self.db.update_job(job_id, state=dbm.FAILED_JOB, message=message)
        self._on_ended(job_id, folder_id, name, dbm.FAILED_JOB)

    def _tell(self, job_id, name):
        self.job_changed.emit(self._status(job_id, name))

    def _status(self, job_id, name):
        job = self.db.job(job_id)
        failures = tuple(self.db.failures(job_id)[:20]) if job.failures else ()
        queued = sum(1 for other in self._scans() if other.id != job_id)
        return JobStatus(
            name, job.state, job.phase, job.done, job.total, job.message, failures, queued
        )

    def _scans(self):
        return [job for job in self.db.unfinished_jobs() if job.kind == scanning.SCAN]

    def _folder_name(self, folder_id):
        info = next((f for f in self.folders() if f.id == folder_id), None)
        return info.name if info is not None else "folder"


def _last_seen(folder):
    if folder.last_mount:
        return os.path.join(folder.last_mount, folder.path) if folder.path else folder.last_mount
    return "/" + folder.path


def _prune_thumbnails(path, root, limit, volume_keys):
    """On the cache thread, with a connection of its own."""
    with dbm.Library(path) as library:
        keep = library.offline_content_ids(volume_keys)
    return thumbs.prune(root, limit, keep)


def _run_scan(path, job_id, folder_id, mounts, stop, quitting, exiftool, report):
    """On a worker thread, with a connection of its own."""
    with dbm.Library(path) as library:
        state = scanning.scan(
            library, job_id, folder_id, mounts, stop.is_set, report, exiftool=exiftool
        )
        if state == dbm.CANCELLED and quitting.is_set():
            # Stopped by a quit, not by the user: run again at the next start
            library.update_job(job_id, state=dbm.QUEUED, message=None)
        return state
