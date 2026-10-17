"""A tethered folder for the UI (#57): one library folder whose new photos are taken in
as each is written (core/hot_folder.py), for a capture program that writes into it.

The folder watcher watches it whatever the watching setting says and tells of a change
at once. Each change goes on a queue that one pass at a time reads, on a thread of its
own (workers.TETHER), so a shot never waits behind a scan, however long. A photo's row
is written, and told (`arrived`), before its details are read: the window can show it
while exiftool reads it. Search indexing is asked for once its details are in.

Tethering is not remembered across a quit: a shot written while Lumina was not running
is found by the scan at the start, as any photo is. The preset chosen is remembered by
the window (ui/settings.py).
"""

import json
import queue
import threading

from PySide6.QtCore import QObject, Signal

from lumina.core import db as dbm
from lumina.core import hot_folder, volumes
from lumina.core.edit import params as _params
from lumina.qt_models import workers


class HotFolder(QObject):
    # Tethering began or ended, or took in photos: `folder`, `preset` and `count` say how
    changed = Signal()
    # A photo was taken in: its id, the newest of those written together
    arrived = Signal(int)
    # A photo could not be read: its path and why
    failed = Signal(str, str)
    # Tethering ended by itself: why (the folder was removed, its drive unplugged, it
    # has too many directories to watch)
    stopped = Signal(str)

    def __init__(
        self,
        library,
        search=None,
        exiftool=None,
        settle=hot_folder.SETTLE,
        linger=hot_folder.LINGER,
        parent=None,
    ):
        """`library` is the LibraryService, `search` the SearchService (None: no indexing).
        `exiftool` makes an ExifTool; the library's scans' by default."""
        super().__init__(parent)
        self.library = library
        self.search = search
        self._exiftool = exiftool or library._exiftool
        self._settle = settle
        self._linger = linger
        # The id of the tethered folder, or None; the preset's name; photos taken in
        self.folder = None
        self.preset = None
        self.count = 0
        self._edit = None
        self._changes = None
        self._stop = None
        self._running = False
        # Kept for the tests, which wait for a pass to end
        self.future = None
        library.watcher.hot_changed.connect(self._on_changed)
        library.watcher.status_changed.connect(self._check)
        library.folders_changed.connect(self._check)

    def start(self, folder_id, preset=None):
        """Tether the folder `folder_id`, giving each new photo the preset named `preset`
        (None: none). Another folder tethered stops being so. Raises NewerParams for a
        preset from a newer Lumina, KeyError for one there is not."""
        edit = None
        if preset is not None:
            document = dict(self.library.presets())[preset]
            edit = _params.pasted(None, json.loads(document))
        self._end()
        self.folder, self.preset, self.count, self._edit = folder_id, preset, 0, edit
        self._changes = queue.SimpleQueue()
        self._stop = threading.Event()
        self.library.watcher.set_hot(folder_id)
        # What was written since the last scan, and not seen by a watch yet
        base = self._base()
        if base is not None:
            self._changes.put(base)
            self._run()
        self.changed.emit()
        self._check()

    def stop(self):
        """Stop tethering; the folder goes back to being watched as the others are."""
        if self.folder is None:
            return
        self._end()
        self.changed.emit()

    def close(self):
        """For the app's exit."""
        if self._stop is not None:
            self._stop.set()

    def _end(self):
        if self._stop is not None:
            self._stop.set()
        self.folder = self.preset = self._edit = self._changes = self._stop = None
        self._running = False
        self.library.watcher.set_hot(None)

    def _base(self):
        folder = self.library.db.folder(self.folder)
        if folder is None:
            return None
        return volumes.resolve(folder.volume_key, folder.path, self.library.mounts)

    def _check(self):
        """Stop when the folder can't be tethered any more, saying why."""
        if self.folder is None:
            return
        info = next((f for f in self.library.folders() if f.id == self.folder), None)
        if info is None:
            why = "The tethered folder was removed from the library"
        elif not info.connected:
            why = f"Tethering stopped: {info.name} is not connected"
        elif self.folder in self.library.watcher.unwatched:
            why = f"{info.name} can't be tethered: it has too many folders inside to watch"
        else:
            return
        self._end()
        self.changed.emit()
        self.stopped.emit(why)

    def _on_changed(self, folder_id, directory):
        if folder_id != self.folder or self._changes is None:
            return
        self._changes.put(directory)
        self._run()

    def _run(self):
        if self._running:
            return
        self._running = True
        stop, changes = self._stop, self._changes
        self.future = workers.submit(
            _run_pass,
            self.library.path,
            self.folder,
            self.library.mounts,
            changes,
            stop,
            self._exiftool,
            self._edit,
            self._settle,
            self._linger,
            lambda added: workers.call_on_main(self, self._added, stop, added),
            lambda added, failures: workers.call_on_main(self, self._read, added, failures),
            on_done=lambda _none: self._ended(stop),
            on_error=lambda error: self._failed(stop, error),
            owner=self,
            process=workers.TETHER,
        )

    def _added(self, stop, added):
        # Written to the library whether or not tethering is still on: the grid hears
        self.library.photos_changed.emit()
        if stop is not self._stop:
            return
        self.count += len(added)
        self.changed.emit()
        self.arrived.emit(added[-1][0])

    def _read(self, _added, failures):
        self.library.photos_changed.emit()
        for path, message in failures:
            self.failed.emit(path, message)
        if self.search is not None:
            self.search.index()

    def _ended(self, stop):
        if stop is not self._stop:
            return
        self._running = False
        # A change that came after the pass last looked
        if not self._changes.empty() and not stop.is_set():
            self._run()

    def _failed(self, stop, error):
        if stop is not self._stop:
            return
        self._running = False
        self.failed.emit(self._base() or "", f"{type(error).__name__}: {error}")


def _run_pass(path, folder_id, mounts, changes, stop, exiftool, edit, settle, linger, added, read):
    """On the tether thread, with a connection of its own."""
    with dbm.Library(path) as library:
        folder = library.folder(folder_id)
        if folder is None:
            return
        base = volumes.resolve(folder.volume_key, folder.path, mounts)
        if base is None:
            return
        mount = volumes.mount_of(base, mounts)
        volume = library.volume_id(
            folder.volume_key, mount=mount.mount_point if mount is not None else None
        )
        hot_folder.run(
            library,
            folder,
            volume,
            base,
            changes,
            stop.is_set,
            added,
            read,
            exiftool=exiftool,
            edit_json=edit,
            settle=settle,
            linger=linger,
        )
