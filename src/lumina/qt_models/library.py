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

from lumina.core import bursts, embed, images, lensdb, metadata, raw, similar, thumbs, volumes
from lumina.core import db as dbm
from lumina.core import library as scanning
from lumina.core import sidecars as sidecarsm
from lumina.core.edit import params as _params
from lumina.qt_models import workers
from lumina.qt_models.watcher import FolderWatcher

# For the application, which tells the user
NewerDatabase = dbm.NewerDatabase
NoBackup = dbm.NoBackup
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
FINDING, DETAILS, SIDECARS = scanning.FINDING, scanning.DETAILS, scanning.SIDECARS

# How often the mounts are read, and how often the grid hears of photos a scan wrote
_MOUNTS_MS = 3000
_PHOTOS_EVERY = 2.0
# How long after the last change the sidecars are written, in milliseconds
_SIDECARS_AFTER_MS = 3000
# How soon after the thumbnails were pruned a job's end prunes them again, in seconds
_PRUNE_EVERY = 600.0
# The time left is told once this many photos, and this many seconds, tell the rate
_ETA_AFTER_PHOTOS = 100
_ETA_AFTER_SECONDS = 5.0


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
    # Seconds left of the details, as far as the rate so far tells; None when not known
    eta: float | None = None


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
        # Every keyword as (id, path, count), by path, and {id: path}: kept up to date,
        # for the grid's model and the window to read
        self.keyword_items = []
        self.keyword_paths = {}
        self._keyword_counts = {}
        self._counting = 0
        self._quitting = threading.Event()
        self._remove_after = set()
        # Folder id -> the directories its queued scan is limited to (none: all of it), and
        # those that changed while its scan ran, to scan when it ends
        self._partial = {}
        self._later = {}
        self._photos_told = 0.0
        # The most the thumbnail cache may take, in bytes, or None for no limit; the
        # application sets it from the settings
        self.thumbnail_limit = None
        # Whether a large scan rests between batches and reads at a low priority; the
        # application sets it from the settings
        self.gentle_scans = True
        # (job, phase, when it began, how many were done then): for the time left
        self._phase_began = None
        # Whether XMP sidecars are written and read; the application sets it from the
        # settings (`set_sidecars`)
        self.sidecars = False
        self._writing_sidecars = False
        self._sidecars_again = False
        self._sidecar_timer = QTimer(self)
        self._sidecar_timer.setSingleShot(True)
        self._sidecar_timer.setInterval(_SIDECARS_AFTER_MS)
        self._sidecar_timer.timeout.connect(self.write_sidecars)
        # Off until the application turns it on (from the settings)
        self.watcher = FolderWatcher(self, parent=self)
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
        if self.sidecars:
            self.write_sidecars()

    def close(self):
        """For the app's exit: a running job stops and stays queued for the next start."""
        self._timer.stop()
        self._sidecar_timer.stop()
        self.watcher.close()
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
        self.keyword_list()
        self.keywords_changed.emit()
        self.collections_changed.emit(None)

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
            self.db.add_job(scanning.SCAN, folder)
        self._next_job()

    def _rescan_directories(self, folder_id, directories):
        if self._running is not None and self._running[1] == folder_id:
            # Its scan may have passed them: again when it ends
            self._later.setdefault(folder_id, set()).update(directories)
            return
        if any(job.folder_id == folder_id for job in self._scans()):
            # Waiting: a scan of all of it covers these, one of some gets them too
            if folder_id in self._partial:
                self._partial[folder_id] |= directories
            return
        self._partial[folder_id] = set(directories)
        self.db.add_job(scanning.SCAN, folder_id)
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
        """Everything the library knows of one photo, as a dict, or None."""
        return self.db.photo(photo_id)

    # Flags and ratings

    def set_flags(self, flags):
        """{photo id: flag}. The grid's model keeps its rows up to date itself."""
        self.db.set_flags(flags)
        self.sidecars_due()

    def flags(self, ids):
        return self.db.flags(ids)

    def set_ratings(self, ratings):
        """{photo id: stars, 0 for none}."""
        self.db.set_ratings(ratings)
        self.sidecars_due()

    def ratings(self, ids):
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
            process=False,
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
        if counting == self._counting and not self._quitting.is_set():
            self._keyword_counts = counts
            self._list_keywords()
            self.keywords_changed.emit()

    def ensure_keywords(self, paths):
        """{path: id} of the keywords `paths` (see core/keywords.parse), made when new."""
        made = self.db.ensure_keywords(paths)
        self.keyword_list()
        self.keywords_changed.emit()
        return made

    def photo_keywords(self, ids):
        return self.db.photo_keywords(ids)

    def set_photo_keywords(self, values):
        """{photo id: ids of its keywords}. The grid's model keeps its rows up to date."""
        self.db.set_photo_keywords(values)
        self.sidecars_due()
        self.keyword_list()
        self.keywords_changed.emit()

    def rename_keyword(self, path, new_path):
        """Rename a keyword and those below it. Returns False when there is no such
        keyword; a ValueError when the new name is another keyword's."""
        renamed = self.db.rename_keyword(path, new_path)
        self.sidecars_due()
        self.keyword_list()
        self.keywords_changed.emit()
        return renamed

    def delete_keyword(self, path):
        gone = self.db.delete_keyword(path)
        self.sidecars_due()
        self.keyword_list()
        self.keywords_changed.emit()
        return gone

    # Collections

    def collections(self):
        return self.db.collections()

    def last_import(self):
        """The id of the last import job that copied photos, or None."""
        return self.db.last_import()

    def add_collection(self, name, query=None):
        """Make a collection (smart with a saved filter's JSON); returns its id. A
        ValueError says what is wrong with the name."""
        made = self.db.add_collection(name, query)
        self.collections_changed.emit(made)
        return made

    def rename_collection(self, collection_id, name):
        self.db.rename_collection(collection_id, name)
        self.collections_changed.emit(collection_id)

    def set_collection_query(self, collection_id, query):
        self.db.set_collection_query(collection_id, query)
        self.collections_changed.emit(collection_id)

    def delete_collection(self, collection_id):
        self.db.delete_collection(collection_id)
        self.collections_changed.emit(collection_id)

    def add_to_collection(self, collection_id, photo_ids):
        """Returns how many photos were added (those in it already are not)."""
        added = self.db.add_to_collection(collection_id, photo_ids)
        self.collections_changed.emit(collection_id)
        return added

    def remove_from_collection(self, collection_id, photo_ids):
        self.db.remove_from_collection(collection_id, photo_ids)
        self.collections_changed.emit(collection_id)

    # Edits and presets

    def edit_json(self, photo_id, copy=0):
        """The photo's edit (its own, or virtual copy `copy`) as parameters JSON, or None."""
        return self.db.edit(photo_id, copy)

    def copies(self, photo_id):
        """The numbers of the photo's virtual copies, in order (its own edit is 0)."""
        return [copy for copy in self.db.copies(photo_id) if copy > 0]

    def make_copy(self, photo_id, copy=0, params_json=None):
        """A new virtual copy of the photo, with the edit of `copy` (or `params_json`, when
        the caller has an edit not stored yet). The original file is not touched, and nor
        is any edit already stored. Returns the new copy's number."""
        if params_json is None:
            params_json = self.db.edit(photo_id, copy)
        if params_json is None:
            params_json = _params.Params().to_json()
        number = self.db.new_copy(photo_id, params_json)
        self.sidecars_due()
        self.photos_changed.emit()
        return number

    def make_copies(self, photo_ids):
        """A virtual copy of each photo, from its own edit, in one go."""
        plain = _params.Params().to_json()
        self.db.new_copies({photo_id: self.db.edit(photo_id) or plain for photo_id in photo_ids})
        self.sidecars_due()
        self.photos_changed.emit()

    def delete_copy(self, photo_id, copy):
        """Remove virtual copy `copy`. The photo's own edit (0) can't be removed this way."""
        if copy <= 0:
            raise ValueError("The photo's own edit is not a virtual copy")
        self.db.delete_edit(photo_id, copy)
        self.sidecars_due()
        self.photos_changed.emit()

    def lens_shot(self, photo_id):
        """How the photo was taken, as the lens database wants it (lensdb.Shot), or None."""
        photo = self.db.photo(photo_id)
        if photo is None:
            return None
        return lensdb.Shot(
            photo["make"],
            photo["model"],
            photo["lens"],
            photo["focal_length"],
            photo["aperture"],
        )

    def raw_files_of(self, model, limit):
        """Paths of up to `limit` connected RAW files of the camera `model`, spread over the
        time they were taken."""
        found = []
        for row in self.db.photos(dbm.Filter(model=model)):
            if not row.available:
                continue
            path = self.resolve(row.volume_key, row.path)
            if path is not None and raw.is_raw(path) and os.path.exists(path):
                found.append(os.fspath(path))
        if len(found) > limit:
            step = len(found) / limit
            found = [found[int(i * step)] for i in range(limit)]
        return found

    # Stacks

    def make_stack(self, photo_ids):
        """Stack these photos, the first on top. Fewer than two make none."""
        if self.db.make_stack(photo_ids) is not None:
            self._stacks_changed()

    def stack_bursts(self, rows):
        """Stack the bursts among `rows` (PhotoRows): shots a moment apart that look alike,
        the best on top (core/bursts.py). Photos already stacked stay as they are. Returns
        how many stacks were made."""
        vectors = self.db.vectors_of(embed.MODEL.name, [r.content_id for r in rows])
        vectors = dict(zip(vectors, embed.from_blobs(list(vectors.values())), strict=True))
        found = bursts.find(rows, vectors)
        for group in found:
            self.db.make_stack(group)
        if found:
            self._stacks_changed()
        return len(found)

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
            process=False,
        )

    def unstack(self, photo_ids):
        """Take apart the stacks these photos are in."""
        self.db.unstack(photo_ids)
        self._stacks_changed()

    def leave_stack(self, photo_ids):
        """Take these photos out of their stacks."""
        self.db.leave_stack(photo_ids)
        self._stacks_changed()

    def set_stack_top(self, photo_id):
        self.db.set_stack_top(photo_id)
        self._stacks_changed()

    def set_stacks_collapsed(self, stack_ids, collapsed):
        self.db.set_stacks_collapsed(stack_ids, collapsed)
        self.photos_changed.emit()

    def _stacks_changed(self):
        self.sidecars_due()
        self.photos_changed.emit()

    def save_edit(self, photo_id, params_json, copy=0):
        """Store an edit (None: none, the original; for a virtual copy always an edit).
        The grid hears of it."""
        if params_json is None and copy > 0:
            raise ValueError("A virtual copy always has an edit")
        if params_json is None:
            self.db.delete_edit(photo_id)
        else:
            self.db.save_edit(photo_id, params_json, copy)
        self.sidecars_due()
        self.edits_changed.emit({photo_id})

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
        self.sidecars_due()
        if edits:
            self.edits_changed.emit(set(edits))
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
        if self.sidecars and not self._quitting.is_set():
            self._sidecar_timer.start()

    def write_sidecars(self):
        """Write the sidecars that are out of date, on a thread of its own; one pass at a
        time, and another after it when something changed meanwhile."""
        if not self.sidecars or self._quitting.is_set():
            return
        if self._writing_sidecars:
            self._sidecars_again = True
            return
        self._writing_sidecars = True
        self._sidecars_again = False
        quitting = self._quitting
        workers.submit(
            _write_sidecars,
            self.path,
            self.mounts,
            # Turned off meanwhile: no more
            lambda: quitting.is_set() or not self.sidecars,
            on_done=self._sidecars_done,
            on_error=lambda error: self._sidecars_done(
                sidecarsm.Written(failures=[("sidecars", str(error))])
            ),
            owner=self,
            process=workers.SIDECARS,
        )

    def _sidecars_done(self, result):
        self._writing_sidecars = False
        if self._quitting.is_set():
            return
        self.sidecars_written.emit(result)
        if self._sidecars_again:
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
        only = self._partial.pop(job.folder_id, None)
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
            self.gentle_scans,
            self.sidecars,
            only,
            lambda *args: workers.call_on_main(self, self._on_progress, job.id, name, *args),
            on_done=lambda state: self._on_ended(job.id, job.folder_id, name, state),
            on_error=lambda error: self._on_failed(job.id, job.folder_id, name, error),
            owner=self,
            process=False,
        )

    def _on_progress(self, job_id, name, phase, done, total, changed):
        if self._quitting.is_set() or self._running is None or self._running[0] != job_id:
            return
        # The phase the scan reported, not the one the database has by the time this is
        # delivered: a quick phase would otherwise never be told
        status = replace(self._status(job_id, name), phase=phase)
        self.job_changed.emit(replace(status, eta=self._time_left(job_id, phase, done, total)))
        now = time.monotonic()
        if changed and now - self._photos_told >= _PHOTOS_EVERY:
            self._photos_told = now
            self.photos_changed.emit()

    def _on_ended(self, job_id, folder_id, name, _state):
        self._running = self._stop = None
        if self._quitting.is_set():
            return
        later = self._later.pop(folder_id, None)
        if later:
            self._rescan_directories(folder_id, later)
        status = self._status(job_id, name)
        if folder_id in self._remove_after:
            self._remove_after.discard(folder_id)
            self.db.remove_folder(folder_id)
        self.folders_changed.emit()
        self.photos_changed.emit()
        # Sidecars read may have changed what the library holds, and the library's own
        # change may be newer than a sidecar read: written to it
        self.sidecars_due()
        # Details read may have brought keywords in
        self.keyword_list()
        self.keywords_changed.emit()
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

    def _time_left(self, job_id, phase, done, total):
        """Seconds until the details are all read at the rate so far, or None: not told
        before enough has been read to say, and not for the walk, whose end is unknown."""
        now = time.monotonic()
        if self._phase_began is None or self._phase_began[:2] != (job_id, phase):
            self._phase_began = (job_id, phase, now, done)
            return None
        _job, _phase, began, first = self._phase_began
        read = done - first
        if phase != scanning.DETAILS or not total or read < _ETA_AFTER_PHOTOS:
            return None
        if now - began < _ETA_AFTER_SECONDS:
            return None
        return (total - done) * (now - began) / read

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


def _run_scan(
    path, job_id, folder_id, mounts, stop, quitting, exiftool, gentle, read_sidecars, only, report
):
    """On a worker thread, with a connection of its own."""
    with dbm.Library(path) as library:
        state = scanning.scan(
            library,
            job_id,
            folder_id,
            mounts,
            stop.is_set,
            report,
            exiftool=exiftool,
            gentle=gentle,
            read_sidecars=read_sidecars,
            only=only,
        )
        if state == dbm.CANCELLED and quitting.is_set():
            # Stopped by a quit, not by the user: run again at the next start
            library.update_job(job_id, state=dbm.QUEUED, message=None)
        return state


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
