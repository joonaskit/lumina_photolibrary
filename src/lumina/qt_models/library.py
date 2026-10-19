"""The library for the UI: its folders, its scan jobs, and which drives are connected.

Scans run one at a time on a thread of their own (workers.SCAN, by the JobRunner of
qt_models/jobs.py), each with its own database connection; progress comes back to the
main thread as queued calls. The mounts are read again
every few seconds: a drive that is plugged in has its folders scanned, one that is
unplugged has its photos shown as not connected.

At the start every unfinished job (a quit or a crash in the middle of one) is run
again, and every connected folder is scanned for what changed while the app was
not running. A quit in the middle of a job leaves it queued for the next start.
"""

import os
import queue
import threading
import time
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from lumina.core import bursts, embed, images, lensdb, metadata, raw, similar, thumbs, volumes
from lumina.core import db as dbm
from lumina.core import library as scanning
from lumina.core import sidecars as sidecarsm
from lumina.core.edit import params as _params
from lumina.qt_models import workers
from lumina.qt_models.jobs import JobRunner
from lumina.qt_models.watcher import FolderWatcher

# For the application, which tells the user
NewerDatabase = dbm.NewerDatabase
NoBackup = dbm.NoBackup

# How often the mounts are read, and how often the grid hears of photos a scan wrote
_MOUNTS_MS = 3000
_PHOTOS_EVERY = 2.0
# How long after the last change the sidecars are written, in milliseconds
_SIDECARS_AFTER_MS = 3000
# How soon after the thumbnails were pruned a job's end prunes them again, in seconds
_PRUNE_EVERY = 600.0
# The time left is told once this many photos tell the rate
_ETA_AFTER_PHOTOS = 100


def _write_sidecars(path, mounts, quitting):
    """On a worker thread, with a connection of its own."""
    with dbm.Library(path) as library:
        return sidecarsm.write_pending(library, mounts, quitting)


def _count_keywords(path):
    with dbm.Library(path) as library:
        return library.keyword_counts()


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


def _photo_name(db, photo_id):
    photo = db.photo(photo_id)
    return os.path.basename(photo.path) if photo else str(photo_id)


class _Writer:
    """The one thread the window's writes (flags, ratings, keywords, collections, edits,
    copies, stacks, presets) run on, with a connection of its own: a write that waits
    for a scan's write lock waits here, not in the window. Writes run in the order they
    were queued."""

    def __init__(self, path, done):
        self._path = path
        self._done = done
        self._queue = queue.Queue()
        self._pending = 0
        self._idle = threading.Condition()
        self._thread = threading.Thread(target=self._run, name="lumina-writer", daemon=True)
        self._thread.start()

    def submit(self, work, then, failed):
        with self._idle:
            self._pending += 1
        self._queue.put((work, then, failed))

    def settle(self):
        """Wait until every write queued so far has been made."""
        with self._idle:
            while self._pending:
                self._idle.wait()

    def idle(self):
        with self._idle:
            return not self._pending

    def close(self):
        self._queue.put(None)
        self._thread.join()

    def _run(self):
        library = None
        try:
            while (item := self._queue.get()) is not None:
                work, then, failed = item
                try:
                    if library is None:
                        library = dbm.Library(self._path)
                    outcome = (then, work(library), None)
                except Exception as error:
                    outcome = (then, None, (failed, error))
                self._done(outcome)
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()
        finally:
            if library is not None:
                library.close()


class LibraryService(QObject):
    folders_changed = Signal()
    # Photos were added or changed: the grid reads them again
    photos_changed = Signal()
    # The edits of these photos (a set of ids) changed: the grid updates their rows and
    # does not read them all again
    edits_changed = Signal(object)
    # The drives connected changed
    mounts_changed = Signal()
    presets_changed = Signal()
    # A pass writing sidecars ended: a core.sidecars.Written
    sidecars_written = Signal(object)
    # The keywords (their names, or the photos' counts) changed
    keywords_changed = Signal()
    # A collection was made, renamed, deleted or had photos put in or taken out: its id
    collections_changed = Signal(object)
    # Settings were copied: `copied` has them
    settings_copied = Signal()
    # A JobStatus, while one runs and when it ends
    job_changed = Signal(object)
    # The job ended: a JobStatus
    job_ended = Signal(object)
    # The thumbnail cache was looked after: a thumbs.Pruned
    thumbnails_pruned = Signal(object)
    # Something done on a thread failed: what to tell the user
    failed = Signal(str)
    # A write made on the writer thread ended: (what to do next, its result, its error)
    _written = Signal(object)

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
        self._written.connect(self._on_written)
        self._writer = _Writer(self.path, self._written.emit)
        self.mounts = read_mounts()
        self.jobs = JobRunner(
            self,
            self,
            {dbm.SCAN},
            workers.SCAN,
            prepare=self._prepare_scan,
            name=lambda job: self._folder_name(job.folder_id),
            changed=self.job_changed.emit,
            ended=self._scan_ended,
            progress=self._scan_progress,
            # The walk's end is not known; the details are read in batches
            timed={dbm.DETAILS},
            eta_after=_ETA_AFTER_PHOTOS,
            # Unplugged as it started: the mounts tell it, and it waits for the drive
            waits=lambda _job, _error: self._unplugged(),
            ready=lambda job: (
                job.folder_id not in self._removing and self.is_connected(job.folder_id)
            ),
        )
        self._quitting = self.jobs.quitting
        # What Copy Settings kept, or None
        self.copied = None
        # Every keyword as (id, path, count), by path, and {id: path}: kept up to date,
        # for the grid's model and the window to read
        self.keyword_items = []
        self.keyword_paths = {}
        self._keyword_counts = {}
        self._counting = 0
        self._remove_after = set()
        # Folder id -> the directories its queued scan is limited to (none: all of it), and
        # those that changed while its scan ran, to scan when it ends
        self._partial = {}
        self._later = {}
        # Folders being forgotten on a thread: not scanned meanwhile
        self._removing = set()
        self._photos_told = 0.0
        # The most the thumbnail cache may take, in bytes, or None for no limit; the
        # application sets it from the settings
        self.thumbnail_limit = None
        # Whether a large scan rests between batches and reads at a low priority; the
        # application sets it from the settings
        self.gentle_scans = True
        # Whether XMP sidecars are written and read; the application sets it from the
        # settings (`set_sidecars`)
        self.sidecars = False
        self._sidecar_pass = workers.Coalescing()
        self._sidecar_timer = QTimer(self)
        self._sidecar_timer.setSingleShot(True)
        self._sidecar_timer.setInterval(_SIDECARS_AFTER_MS)
        self._sidecar_timer.timeout.connect(self.write_sidecars)
        # Off until the application turns it on (from the settings)
        self.watcher = FolderWatcher(self, parent=self)
        self._pruning = workers.Coalescing()
        self._pruned_at = None
        self.prune_future = None
        self._timer = QTimer(self)
        self._timer.setInterval(mounts_ms)
        self._timer.timeout.connect(self.check_mounts)

    def start(self):
        """Resume unfinished jobs, scan every connected folder, and follow the drives."""
        for folder in self.db.folders():
            self.db.add_job(dbm.SCAN, folder.id)
        self._timer.start()
        self.jobs.start()
        self.prune_thumbnails()
        if self.sidecars:
            self.write_sidecars()

    def close(self):
        """For the app's exit: a running job stops and stays queued for the next start."""
        self._timer.stop()
        self._sidecar_timer.stop()
        self.watcher.close()
        self.jobs.close()
        # The queued writes are made, not dropped
        self._writer.close()
        self.db.close()

    # Writes on the writer thread

    def _write(self, work, then=None, on_error=None):
        """Make `work(library)` on the writer thread, then `then(result)` here. The
        window does not wait; a failure goes to `on_error(error)` here, or without one
        is told by `failed`."""
        self._writer.submit(work, then, on_error)

    def _on_written(self, outcome):
        then, result, error = outcome
        if self._quitting.cancelled():
            return
        if error is not None:
            on_error, error = error
            if on_error is not None:
                on_error(error)
            else:
                self.failed.emit(str(error))
        elif then is not None:
            then(result)

    def _then(self, told, on_done):
        # `told(result)`, what the service says of a write, then the caller's `on_done`
        def then(result):
            told(result)
            if on_done is not None:
                on_done(result)

        return then

    def settle(self):
        """Wait for the writes queued so far, before reading what they change. Short
        unless a scan holds the write lock."""
        self._writer.settle()

    def flush(self):
        """`settle`, and hear of the writes made at once, and of those they queued (for
        tests, and the exit)."""
        while True:
            self.settle()
            QCoreApplication.sendPostedEvents(self, 0)
            if self._writer.idle():
                return

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
        if self._scanning(folder_id):
            self._remove_after.add(folder_id)
            self.jobs.forget(folder_id)
            self.jobs.stop()
            return
        self._forget_folder(folder_id)

    def _forget_folder(self, folder_id):
        """On a thread (a folder of 100,000 photos is a long delete): the folder and its
        photos go, then the window hears of it."""
        self._partial.pop(folder_id, None)
        self._later.pop(folder_id, None)
        self._removing.add(folder_id)

        def done(_result):
            self._removing.discard(folder_id)
            if self._quitting.cancelled():
                return
            self.folders_changed.emit()
            self.photos_changed.emit()
            self.keyword_list()
            self.keywords_changed.emit()
            self.collections_changed.emit(None)

        workers.submit(
            _remove_folder,
            self.path,
            folder_id,
            on_done=done,
            on_error=lambda error: (
                self._removing.discard(folder_id),
                self.failed.emit(str(error)),
            ),
            owner=self,
            lane=workers.IO,
        )

    def rescan(self, folder_id=None, directories=None):
        """Scan one folder, or all. With `directories` (absolute) of one folder, just
        those, without what is below them: where something changed, as the watcher says."""
        if directories is not None and folder_id is not None:
            self._rescan_directories(folder_id, set(directories))
            return
        ids = [folder_id] if folder_id is not None else [f.id for f in self.db.folders()]
        for folder in ids:
            # All of it, whatever was asked for before
            self._partial.pop(folder, None)
            self._later.pop(folder, None)
            self.jobs.add(dbm.SCAN, folder)

    def _rescan_directories(self, folder_id, directories):
        if self._scanning(folder_id):
            # Its scan may have passed them: again when it ends
            self._later.setdefault(folder_id, set()).update(directories)
            return
        if any(job.folder_id == folder_id for job in self.jobs.jobs()):
            # Waiting: a scan of all of it covers these, one of some gets them too
            if folder_id in self._partial:
                self._partial[folder_id] |= directories
            return
        self._partial[folder_id] = set(directories)
        self.jobs.add(dbm.SCAN, folder_id)

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

    def is_photo_file(self, path):
        """Whether `path` is a file of a kind the library reads."""
        return os.path.isfile(path) and images.is_photo(path)

    def locate_photo(self, path):
        """Where the file at `path` stands in the library: (folder id, photo id), each
        None when the library has no such folder or photo. (None, None) too when the
        file is not a photo the library reads or no mounted filesystem holds it."""
        path = os.path.realpath(path)
        if not self.is_photo_file(path):
            return None, None
        where = volumes.locate(path, self.mounts)
        if where is None:
            return None, None
        return self.db.folder_holding(*where), self.db.photo_at(*where)

    def folder_holding(self, path):
        """The id of the library folder that is the folder at `path` or has it inside, or
        None when no folder of the library does."""
        where = volumes.locate(os.path.realpath(path), self.mounts)
        return self.db.folder_holding(*where) if where is not None else None

    def photo_details(self, photo_id):
        """Everything the library knows of one photo (a Photo), or None."""
        self.settle()
        return self.db.photo(photo_id)

    # Flags and ratings

    def set_flags(self, flags):
        """{photo id: flag}. The grid's model keeps its rows up to date itself."""
        self._write(lambda db: db.set_flags(flags), lambda _: self.sidecars_due())

    def flags(self, ids):
        self.settle()
        return self.db.flags(ids)

    def set_ratings(self, ratings):
        """{photo id: stars, 0 for none}."""
        self._write(lambda db: db.set_ratings(ratings), lambda _: self.sidecars_due())

    def ratings(self, ids):
        self.settle()
        return self.db.ratings(ids)

    # Keywords

    def keyword_list(self):
        """Read the keywords again: their names now, and `keyword_items` (id, path, count)
        and `keyword_paths` with them; the photo counts a moment later, from a thread
        (they are a query over every link), when `keywords_changed` is told again."""
        self.keyword_paths = self.db.keyword_paths()
        self._list_keywords()
        self._counting += 1
        counting = self._counting
        workers.submit(
            _count_keywords,
            self.path,
            on_done=lambda counts: self._keywords_counted(counting, counts),
            on_error=lambda _error: None,
            owner=self,
            lane=workers.IO,
        )
        return self.keyword_items

    def _list_keywords(self):
        self.keyword_items = sorted(
            (
                (keyword, path, self._keyword_counts.get(keyword, 0))
                for keyword, path in self.keyword_paths.items()
            ),
            key=lambda item: [name.casefold() for name in item[1].split(" > ")],
        )

    def _keywords_counted(self, counting, counts):
        # The newest read only
        if counting == self._counting and not self._quitting.cancelled():
            self._keyword_counts = counts
            self._list_keywords()
            self.keywords_changed.emit()

    def ensure_keywords(self, paths, on_done=None):
        """Make the keywords `paths` (see core/keywords.parse) that are new, then
        `on_done({path: id})`."""

        def told(_made):
            self.keyword_list()
            self.keywords_changed.emit()

        self._write(lambda db: db.ensure_keywords(paths), self._then(told, on_done))

    def photo_keywords(self, ids):
        self.settle()
        return self.db.photo_keywords(ids)

    def set_photo_keywords(self, values):
        """{photo id: ids of its keywords}. The grid's model keeps its rows up to date."""

        def done(_):
            self.sidecars_due()
            self.keyword_list()
            self.keywords_changed.emit()

        self._write(lambda db: db.set_photo_keywords(values), done)

    def rename_keyword(self, path, new_path, on_done=None, on_error=None):
        """Rename a keyword and those below it, then `on_done(renamed)`: False when there
        is no such keyword. `on_error` hears a ValueError when the new name is another
        keyword's."""
        self._write(
            lambda db: db.rename_keyword(path, new_path),
            self._then(self._keywords_written, on_done),
            on_error,
        )

    def delete_keyword(self, path, on_done=None):
        self._write(lambda db: db.delete_keyword(path), self._then(self._keywords_written, on_done))

    def _keywords_written(self, _result):
        self.sidecars_due()
        self.keyword_list()
        self.keywords_changed.emit()

    # Collections

    def collections(self):
        self.settle()
        return self.db.collections()

    def last_import(self):
        """The id of the last import job that copied photos, or None."""
        return self.db.last_import()

    def add_collection(self, name, query=None, on_done=None, on_error=None):
        """Make a collection (smart with a saved filter's JSON), then `on_done(its id)`.
        `on_error` hears a ValueError saying what is wrong with the name."""
        self._write(
            lambda db: db.add_collection(name, query),
            self._then(self.collections_changed.emit, on_done),
            on_error,
        )

    def rename_collection(self, collection_id, name, on_error=None):
        self._write(
            lambda db: db.rename_collection(collection_id, name),
            lambda _: self.collections_changed.emit(collection_id),
            on_error,
        )

    def set_collection_query(self, collection_id, query):
        self._write(
            lambda db: db.set_collection_query(collection_id, query),
            lambda _: self.collections_changed.emit(collection_id),
        )

    def delete_collection(self, collection_id):
        self._write(
            lambda db: db.delete_collection(collection_id),
            lambda _: self.collections_changed.emit(collection_id),
        )

    def add_to_collection(self, collection_id, photo_ids, on_done=None):
        """Put the photos in the collection, then `on_done(how many were added)`: those
        in it already are not."""
        self._write(
            lambda db: db.add_to_collection(collection_id, photo_ids),
            self._then(lambda _: self.collections_changed.emit(collection_id), on_done),
        )

    def remove_from_collection(self, collection_id, photo_ids):
        self._write(
            lambda db: db.remove_from_collection(collection_id, photo_ids),
            lambda _: self.collections_changed.emit(collection_id),
        )

    # Shooting tethered

    def start_tether_session(self, name, volume_key, folder, keep, on_done, on_error):
        """Write a camera session, in the manual collection `name` (made when there is
        none), then `on_done(its TetherSession)`."""

        def then(result):
            session, made = result
            if made is not None:
                self.collections_changed.emit(made)
            on_done(session)

        self._write(
            lambda db: db.start_tether_session(name, volume_key, folder, keep), then, on_error
        )

    # Edits and presets

    def edit_json(self, photo_id, copy=0, settle=True):
        """The photo's edit (its own, or virtual copy `copy`) as parameters JSON, or None.
        Without `settle`, as the library has it now, not waiting for the writes queued:
        for painting, which must not wait for a scan's write lock."""
        if settle:
            self.settle()
        return self.db.edit(photo_id, copy)

    def edit_times(self, photo_id):
        """{copy: when its edit was last saved} of the photo's edits (its own is 0)."""
        self.settle()
        return self.db.edit_times(photo_id)

    def copies(self, photo_id):
        self.settle()
        """The numbers of the photo's virtual copies, in order (its own edit is 0)."""
        return [copy for copy in self.db.copies(photo_id) if copy > 0]

    def make_copy(self, photo_id, copy=0, params_json=None, on_done=None):
        """A new virtual copy of the photo, with the edit of `copy` (or `params_json`, when
        the caller has an edit not stored yet), then `on_done(the new copy's number)`.
        The original file is not touched, and nor is any edit already stored."""

        def work(db):
            stored = params_json if params_json is not None else db.edit(photo_id, copy)
            return db.new_copy(photo_id, stored or _params.Params().to_json())

        def told(_number):
            self.sidecars_due()
            self.photos_changed.emit()

        self._write(work, self._then(told, on_done))

    def make_copies(self, photo_ids):
        """A virtual copy of each photo, from its own edit, in one go."""
        plain = _params.Params().to_json()
        photo_ids = list(photo_ids)

        def work(db):
            db.new_copies({photo_id: db.edit(photo_id) or plain for photo_id in photo_ids})

        self._write(work, lambda _: self._stacks_changed())

    def delete_copy(self, photo_id, copy):
        """Remove virtual copy `copy`. The photo's own edit (0) can't be removed this way."""
        if copy <= 0:
            raise ValueError("The photo's own edit is not a virtual copy")
        self._write(lambda db: db.delete_edit(photo_id, copy), lambda _: self._stacks_changed())

    def lens_shot(self, photo_id):
        """How the photo was taken, as the lens database wants it (lensdb.Shot), or None."""
        photo = self.db.photo(photo_id)
        if photo is None:
            return None
        return lensdb.Shot(
            photo.make,
            photo.model,
            photo.lens,
            photo.focal_length,
            photo.aperture,
        )

    def raw_files_of(self, model, limit, on_done, on_error):
        """On a thread, then `on_done(paths)`: up to `limit` connected RAW files of the
        camera `model`, spread over the time they were taken."""
        workers.submit(
            _raw_files_of,
            self.path,
            self.mounts,
            model,
            limit,
            on_done=on_done,
            on_error=on_error,
            owner=self,
            lane=workers.IO,
        )

    # Stacks

    def make_stack(self, photo_ids):
        """Stack these photos, the first on top. Fewer than two make none."""
        photo_ids = list(photo_ids)

        def done(made):
            if made is not None:
                self._stacks_changed()

        self._write(lambda db: db.make_stack(photo_ids), done)

    def stack_bursts(self, rows, on_done, on_error):
        """Stack the bursts among `rows` (PhotoRows): shots a moment apart that look alike,
        the best on top (core/bursts.py). Photos already stacked stay as they are. On a
        thread, then `on_done(how many stacks were made)`."""

        def done(made):
            if made:
                self._stacks_changed()
            on_done(made)

        workers.submit(
            _stack_bursts,
            self.path,
            rows,
            on_done=done,
            on_error=on_error,
            owner=self,
            lane=workers.IO,
        )

    def find_similar(self, similarity, only, on_done, on_error):
        """Look through the library, or only the photos with the ids `only`, on a thread,
        for photos that look alike or are copies of one file (core/similar.py), then
        `on_done([(exact, [PhotoRow, ...])])`, the best photo of each group first,
        largest groups first."""
        workers.submit(
            _find_similar,
            self.path,
            similarity,
            None if only is None else frozenset(only),
            on_done=on_done,
            on_error=on_error,
            owner=self,
            lane=workers.IO,
        )

    def unstack(self, photo_ids):
        """Take apart the stacks these photos are in."""
        photo_ids = list(photo_ids)
        self._write(lambda db: db.unstack(photo_ids), lambda _: self._stacks_changed())

    def leave_stack(self, photo_ids):
        """Take these photos out of their stacks."""
        photo_ids = list(photo_ids)
        self._write(lambda db: db.leave_stack(photo_ids), lambda _: self._stacks_changed())

    def set_stack_top(self, photo_id):
        self._write(lambda db: db.set_stack_top(photo_id), lambda _: self._stacks_changed())

    def set_stacks_collapsed(self, stack_ids, collapsed):
        stack_ids = list(stack_ids)
        self._write(
            lambda db: db.set_stacks_collapsed(stack_ids, collapsed),
            lambda _: self.photos_changed.emit(),
        )

    def _stacks_changed(self):
        self.sidecars_due()
        self.photos_changed.emit()

    def save_edit(self, photo_id, params_json, copy=0):
        """Store an edit (None: none, the original; for a virtual copy always an edit).
        The grid hears of it."""
        if params_json is None and copy > 0:
            raise ValueError("A virtual copy always has an edit")

        def work(db):
            if params_json is None:
                db.delete_edit(photo_id)
            else:
                db.save_edit(photo_id, params_json, copy)

        def done(_):
            self.sidecars_due()
            self.edits_changed.emit({photo_id})

        self._write(work, done)

    # Copying settings from one photo to others

    def settings_document(self, photo_id, sections):
        """The `sections` of the photo's own edit as a document to put over other photos
        (see `paste_settings`). Raises NewerParams for an edit from a newer Lumina."""
        self.settle()
        stored = self.db.edit(photo_id)
        edit = _params.from_json(stored) if stored else _params.Params()
        return _params.preset(edit, sections)

    def copy_settings(self, document, name, sections):
        """Keep `document` (a photo's `settings_document`, or the Develop view's) of the
        photo called `name` to paste later."""
        self.copied = CopiedSettings(document, name, tuple(sections))
        self.settings_copied.emit()

    def paste_settings(self, photo_ids, document=None, on_done=None):
        """Put the sections of `document` (what was copied, without one) over the edits
        of the photos `photo_ids`, in one go, then `on_done(PasteResult)`: the other
        sections of each edit stay as they were. A photo whose edit is from a newer
        Lumina is left alone and named in the result's `failed`. The photos' files are
        not touched, nor is a photo on a drive that is not connected left out: edits
        live in the library."""
        document = document if document is not None else self.copied.document
        photo_ids = list(photo_ids)

        def work(db):
            edits, failed = {}, []
            with db.transaction():
                for photo_id in photo_ids:
                    try:
                        edits[photo_id] = _params.pasted(db.edit(photo_id), document)
                    except _params.NewerParams as error:
                        failed.append((_photo_name(db, photo_id), str(error)))
                db.save_edits(edits)
            return set(edits), PasteResult(len(edits), tuple(failed))

        def then(outcome):
            pasted, result = outcome
            self.sidecars_due()
            if pasted:
                self.edits_changed.emit(pasted)
            if on_done is not None:
                on_done(result)

        self._write(work, then)

    def presets(self):
        self.settle()
        return self.db.presets()

    def preset(self, name):
        """The parameters JSON of the preset `name`, or None."""
        self.settle()
        return self.db.preset(name)

    def save_preset(self, name, params_json):
        self._write(
            lambda db: db.save_preset(name, params_json), lambda _: self.presets_changed.emit()
        )

    def delete_preset(self, name):
        self._write(lambda db: db.delete_preset(name), lambda _: self.presets_changed.emit())

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
                self.db.add_job(dbm.SCAN, folder.id)
        self.jobs.wake()
        # Photos on a drive that came can have their sidecars written now
        self.sidecars_due()

    # XMP sidecars

    def set_sidecars(self, on):
        """Write sidecars for what the library marks and edits, and read the ones beside
        the photos when they are scanned (core/sidecars.py). Turning it on writes them
        for the whole library, and scans every folder to read those already there."""
        on = bool(on)
        if on == self.sidecars:
            return
        self.sidecars = on
        if on:
            self.write_sidecars()
            self.rescan()
        else:
            self._sidecar_timer.stop()

    def sidecars_due(self):
        """Something sidecars hold changed: write them once changes stop for a moment."""
        if self.sidecars and not self._quitting.cancelled():
            self._sidecar_timer.start()

    def write_sidecars(self):
        """Write the sidecars that are out of date, on a thread of its own; one pass at a
        time, and another after it when something changed meanwhile."""
        if not self.sidecars or self._quitting.cancelled() or not self._sidecar_pass.begin():
            return
        quitting = self._quitting
        workers.submit(
            _write_sidecars,
            self.path,
            self.mounts,
            # Turned off meanwhile: no more
            lambda: quitting.cancelled() or not self.sidecars,
            on_done=self._sidecars_done,
            on_error=lambda error: self._sidecars_done(
                sidecarsm.Written(failures=[("sidecars", str(error))])
            ),
            owner=self,
            lane=workers.SIDECARS,
        )

    def _sidecars_done(self, result):
        again = self._sidecar_pass.end()
        if self._quitting.cancelled():
            return
        self.sidecars_written.emit(result)
        if again:
            self.write_sidecars()

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
        if self.thumbnail_limit is None or self._quitting.cancelled() or not self._pruning.begin():
            return
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
            lane=workers.CACHE,
        )

    def _on_pruned(self, result):
        again = self._pruning.end()
        self._pruned_at = time.monotonic()
        if self._quitting.cancelled():
            return
        if result is not None:
            self.thumbnails_pruned.emit(result)
        if again:
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
            lane=workers.CACHE,
        )

    # Jobs

    def is_busy(self):
        return self.jobs.is_busy()

    def cancel(self):
        """Stop the running scan and drop the queued ones. Indexing for search is the
        SearchService's."""
        self.jobs.cancel()

    def _scanning(self, folder_id):
        return self.jobs.running is not None and self.jobs.running.folder_id == folder_id

    def _prepare_scan(self, job):
        only = self._partial.pop(job.folder_id, None)
        return partial(_scan, job.folder_id, self._exiftool, self.gentle_scans, self.sidecars, only)

    def _scan_progress(self, job, status, changed):
        self.job_changed.emit(status)
        now = time.monotonic()
        if changed and now - self._photos_told >= _PHOTOS_EVERY:
            self._photos_told = now
            self.photos_changed.emit()

    def _unplugged(self):
        self.mounts = self._read_mounts()
        self.folders_changed.emit()

    def _scan_ended(self, job, status):
        folder_id = job.folder_id
        later = self._later.pop(folder_id, None)
        if later:
            self._rescan_directories(folder_id, later)
        if folder_id in self._remove_after:
            self._remove_after.discard(folder_id)
            self._forget_folder(folder_id)
        self.folders_changed.emit()
        self.photos_changed.emit()
        # Sidecars read may have changed what the library holds, and the library's own
        # change may be newer than a sidecar read: written to it
        self.sidecars_due()
        # Details read may have brought keywords in
        self.keyword_list()
        self.keywords_changed.emit()
        self.job_ended.emit(status)
        if self._pruned_at is None or time.monotonic() - self._pruned_at >= _PRUNE_EVERY:
            self.prune_thumbnails()

    def _folder_name(self, folder_id):
        info = next((f for f in self.folders() if f.id == folder_id), None)
        return info.name if info is not None else "folder"


def _last_seen(folder):
    if folder.last_mount:
        return os.path.join(folder.last_mount, folder.path) if folder.path else folder.last_mount
    return "/" + folder.path


def _raw_files_of(path, mounts, model, limit):
    """On a worker thread: see LibraryService.raw_files_of."""
    found = []
    with dbm.Library(path) as library:
        rows = library.raw_candidates(model)
    for key, relative in rows:
        where = volumes.resolve(key, relative, mounts)
        if where is not None and raw.is_raw(where) and os.path.exists(where):
            found.append(os.fspath(where))
    if len(found) > limit:
        step = len(found) / limit
        found = [found[int(i * step)] for i in range(limit)]
    return found


def _stack_bursts(path, rows):
    """On a worker thread, with a connection of its own."""
    with dbm.Library(path) as library:
        vectors = library.vectors_of(embed.MODEL.name, [r.content_id for r in rows])
        vectors = dict(zip(vectors, embed.from_blobs(list(vectors.values())), strict=True))
        found = bursts.find(rows, vectors)
        for group in found:
            library.make_stack(group)
    return len(found)


def _remove_folder(path, folder_id):
    """On a worker thread, with a connection of its own."""
    with dbm.Library(path) as library:
        library.remove_folder(folder_id)


def _prune_thumbnails(path, root, limit, volume_keys):
    """On the cache thread, with a connection of its own."""
    with dbm.Library(path) as library:
        keep = library.offline_content_ids(volume_keys)
    return thumbs.prune(root, limit, keep)


def _scan(folder_id, exiftool, gentle, read_sidecars, only, library, job_id, mounts, stop, report):
    """On the scan thread."""
    return scanning.scan(
        library,
        job_id,
        folder_id,
        mounts,
        stop,
        report,
        exiftool=exiftool,
        gentle=gentle,
        read_sidecars=read_sidecars,
        only=only,
    )


# For the UI: where the similarity setting starts
SIMILAR_DEFAULT = similar.SIMILARITY


def _find_similar(path, similarity, only):
    """On a thread: the groups of photos in the library at `path` that are alike, among
    those with the ids `only` unless that is None."""
    with dbm.Library(path) as library:
        rows = [r for r in library.photos() if only is None or r.id in only]
        blobs = library.vectors_of(embed.MODEL.name, [r.content_id for r in rows])
    vectors = dict(zip(blobs, embed.from_blobs(list(blobs.values())), strict=True))
    by_id = {row.id: row for row in rows}
    return [
        (group.exact, [by_id[i] for i in group.ids])
        for group in similar.find(rows, vectors, similarity)
    ]
