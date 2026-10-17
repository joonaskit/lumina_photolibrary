"""Watching the library's folders while Lumina runs, so a photo copied in, moved or
deleted shows up without a Rescan.

Qt's file system watcher is inotify on Linux, which watches one directory at a time:
every directory under a folder needs a watch of its own, and the system has a limit
(`max_user_watches`) shared with every other program. Lumina takes at most a quarter of
it. A folder with more directories than there is room for is not watched at all, as a
half-watched folder would say less than nothing: it is left to the scans at the start,
when a drive is plugged in, and Rescan.

A change is not scanned for at once: after a quiet moment, so a folder being copied
into is read when the copy is done, not once per file. A file that was still being
written then is found changed by the next scan. Nothing here writes to a folder.

A tethered folder (#57, qt_models/hot_folder.py) is watched even with watching off, and
a change in it is told at once (`hot_changed`), besides the quiet moment's scan when
watching is on.
"""

import os

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from lumina.qt_models import workers

# After the last change in a folder, in milliseconds, before it is scanned
_QUIET_MS = 8000
# The share of the system's watches Lumina takes, and the most whatever the system says
_SHARE = 4
_MOST = 20000
# Directories given to the watcher at a time, so that the window stays responsive
_CHUNK = 400


def system_limit():
    """The most inotify watches one user may have, or None when it can't be told."""
    try:
        with open("/proc/sys/fs/inotify/max_user_watches") as file:
            return int(file.read())
    except (OSError, ValueError):
        return None


def default_budget():
    """How many directories Lumina may watch: 0 where watching is not there."""
    limit = system_limit()
    if limit is None:
        return 0
    return min(limit // _SHARE, _MOST)


def directories(base, most):
    """`base` and every directory under it, hidden ones skipped (as the scan does), or
    None when there are more than `most`. On a worker thread: it walks the folder."""
    found = []
    for directory, folders, _files in os.walk(base, followlinks=False):
        folders[:] = [name for name in folders if not name.startswith(".")]
        found.append(directory)
        if len(found) > most:
            return None
    return found


class FolderWatcher(QObject):
    # The folders watched, or not, changed: `watched` and `unwatched` say which
    status_changed = Signal()
    # A directory of the tethered folder changed: the folder's id and the directory
    hot_changed = Signal(int, str)

    def __init__(self, service, quiet_ms=_QUIET_MS, budget=None, parent=None):
        """`service` is the LibraryService. `budget` is how many directories may be
        watched (default: from the system's limit)."""
        super().__init__(parent)
        self.service = service
        self.budget = default_budget() if budget is None else budget
        self.enabled = False
        # The id of the tethered folder, or None
        self.hot = None
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_changed)
        # Directory -> the id of the folder it is in, and folder id -> its directories
        self._owner = {}
        self._dirs = {}
        # Folder ids with a walk running, those too large to watch, and those changed
        self._walking = set()
        self.unwatched = set()
        self._changed = set()
        self._queue = []
        self._quitting = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(quiet_ms)
        self._timer.timeout.connect(self._scan)
        self._adder = QTimer(self)
        self._adder.setInterval(0)
        self._adder.timeout.connect(self._add_some)
        service.folders_changed.connect(self.sync)
        service.mounts_changed.connect(self.sync)

    @property
    def watched(self):
        """The ids of the folders being watched."""
        return {folder for folder, paths in self._dirs.items() if paths}

    def set_enabled(self, on):
        self.enabled = on
        self.sync()

    def set_hot(self, folder_id):
        """Watch the folder `folder_id` (None: none) whatever `enabled` is, and tell of a
        change in it at once."""
        self.hot = folder_id
        self.sync()

    def close(self):
        self._quitting = True
        self._timer.stop()
        self._adder.stop()

    def sync(self):
        """Watch the folders that are connected and not yet watched, and let go of those
        that are gone, not connected, or not wanted."""
        if self._quitting:
            return
        connected = {f.id for f in self.service.folders() if self.service.is_connected(f.id)}
        wanted = connected if self.enabled else connected & {self.hot}
        for folder in set(self._dirs) - wanted:
            self._forget(folder)
        self.unwatched &= wanted
        for folder in wanted - set(self._dirs) - self._walking - self.unwatched:
            self._walk(folder)
        self.status_changed.emit()

    def _walk(self, folder):
        info = next((f for f in self.service.folders() if f.id == folder), None)
        if info is None:
            return
        self._walking.add(folder)
        used = sum(len(paths) for paths in self._dirs.values())
        workers.submit(
            directories,
            info.path,
            self.budget - used,
            on_done=lambda found: self._walked(folder, found),
            on_error=lambda _error: self._walked(folder, None),
            owner=self,
            process=workers.WATCH,
        )

    def _walked(self, folder, found):
        self._walking.discard(folder)
        if self._quitting or not self.service.is_connected(folder):
            return
        if not self.enabled and folder != self.hot:
            return
        if found is None:
            # Too many directories for what is left of the budget
            self.unwatched.add(folder)
        else:
            self._dirs[folder] = set()
            self._queue.extend((folder, path) for path in found)
            self._adder.start()
        self.status_changed.emit()

    def _add_some(self):
        chunk, self._queue = self._queue[:_CHUNK], self._queue[_CHUNK:]
        for folder, path in chunk:
            # A folder let go of while its directories were being added
            if folder in self._dirs:
                self._owner[path] = folder
                self._dirs[folder].add(path)
                self._watcher.addPath(path)
        if not self._queue:
            self._adder.stop()
            self.status_changed.emit()

    def _forget(self, folder):
        paths = self._dirs.pop(folder, set())
        for path in paths:
            self._owner.pop(path, None)
        if paths:
            self._watcher.removePaths(list(paths))
        self._queue = [item for item in self._queue if item[0] != folder]
        self._changed.discard(folder)

    def _on_changed(self, path):
        folder = self._owner.get(path)
        if folder is None:
            return
        made = self._add_new_directories(folder, path)
        if folder == self.hot:
            for directory in (path, *made):
                self.hot_changed.emit(folder, directory)
        if self.enabled:
            self._changed.add(folder)
            self._timer.start()

    def _add_new_directories(self, folder, path):
        """A directory made in `path` needs a watch of its own, as long as there is room.
        Returns the directories given one."""
        try:
            names = os.listdir(path)
        except OSError:
            return []
        made = []
        used = sum(len(paths) for paths in self._dirs.values())
        for name in names:
            child = os.path.join(path, name)
            if name.startswith(".") or child in self._owner or not os.path.isdir(child):
                continue
            if os.path.islink(child) or used >= self.budget:
                continue
            self._owner[child] = folder
            self._dirs[folder].add(child)
            self._watcher.addPath(child)
            used += 1
            made.append(child)
            # Its own directories, if it came with some
            made.extend(self._add_new_directories(folder, child))
        return made

    def _scan(self):
        changed, self._changed = self._changed, set()
        # What Qt let go of when its directory went
        gone = set(self._owner) - set(self._watcher.directories())
        for path in gone:
            folder = self._owner.pop(path)
            if folder in self._dirs:
                self._dirs[folder].discard(path)
        for folder in sorted(changed):
            if self.service.is_connected(folder):
                self.service.rescan(folder)
