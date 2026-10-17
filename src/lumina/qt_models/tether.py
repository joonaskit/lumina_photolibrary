"""Shooting tethered for the UI (#58): a camera session (core/tether.py) in a process of
its own, and its photos into the library.

A session's frames go into a folder of a library folder, laid out as an import's
(`2026/2026-10-17/Studio`). The library folder is tethered (qt_models/hot_folder.py)
for the session, so each frame saved is taken in at once, gets the session's preset,
opens as the newest photo, and is put in the session's collection.

The process is spoken to through two queues; a thread here reads what it tells and
hands each event to the main thread. It ends when told to stop, at the app's exit, or by
itself (a crash in libgphoto2 takes only the process: told as an error). Every frame the
camera told of is noted by the process before it is fetched, so frames a crash or an
unplug cut short are fetched at the next session, into the folder of their own session.
"""

import os
import queue
import threading
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from lumina.core import db as dbm
from lumina.core import importing, tether, volumes
from lumina.qt_models import workers

CARD, COMPUTER = tether.CARD, tether.COMPUTER
SETTINGS, READINGS = tether.SETTINGS, tether.READINGS
available = tether.available
LAYOUTS = tuple(importing.LAYOUTS)
subfolder = importing.subfolder

# How long the process has to end when told to, at the app's exit, in seconds
_END_WITHIN = 3.0


@dataclass(frozen=True)
class Failure:
    """A frame that was not saved: the camera's name for it, why, and whether it can be
    fetched again (it is on the card)."""

    item: int
    name: str
    why: str


class CameraService(QObject):
    # Anything the bar shows changed: connected or not, the message, settings, counts
    changed = Signal()
    # A frame was saved: its path. Its photo comes through the hot folder.
    saved = Signal(str)
    # A frame was not saved: its name on the camera and why
    failed = Signal(str, str)
    # Something asked of the camera could not be done: why
    error = Signal(str)

    def __init__(self, library, hot, camera=tether.GPhotoCamera, in_process=True, parent=None):
        """`library` is the LibraryService, `hot` the HotFolder that takes the frames in.
        `camera` makes a camera in the session's process (picklable); `in_process` False
        runs the session on a thread, for the tests."""
        super().__init__(parent)
        self.library = library
        self.hot = hot
        self._camera = camera
        self._in_process = in_process
        self._timing = {}
        self.session = None
        self.where = None
        self.info = None
        self.settings = {}
        # Why there is no camera to shoot with (None while one is connected)
        self.message = None
        self.saved_count = 0
        self.fetching = {}
        self.failures = {}
        self._commands = None
        self._worker = None
        self._paths = set()
        self._ending = False
        hot.taken.connect(self._collect)

    @property
    def running(self):
        return self.session is not None

    @property
    def connected(self):
        return self.running and self.info is not None and self.message is None

    def start(self, folder_id, name, layout="year-day", keep=CARD, preset=None):
        """Shoot into the library folder `folder_id`, in `name` under `layout` (as an
        import lays out a day); each photo gets the preset `preset` (None: none). Raises
        ValueError when the folder is not connected, KeyError and NewerParams for the preset
        (as HotFolder.start)."""
        if self.running:
            raise ValueError("A session is running: end it first")
        folder = self.library.db.folder(folder_id)
        base = (
            volumes.resolve(folder.volume_key, folder.path, self.library.mounts) if folder else None
        )
        if base is None:
            raise ValueError("The folder is not connected")
        name = " ".join(name.replace("/", " ").split()) or f"Tethered {datetime.now():%Y-%m-%d}"
        where = os.path.join(base, importing.subfolder(datetime.now(), layout), name)
        relative = "/".join(part for part in (folder.path, os.path.relpath(where, base)) if part)
        self.hot.start(folder_id, preset)
        collection = self._collection(name)
        session_id = self.library.db.add_tether_session(
            name, folder.volume_key, relative, keep, collection
        )
        self.session = self.library.db.tether_session(session_id)
        self.where = where
        self.info, self.settings, self.message = None, {}, "Looking for the camera…"
        self.saved_count, self.fetching, self.failures, self._paths = 0, {}, {}, set()
        self._ending = False
        folders = self._folders()
        folders[session_id] = where
        session = tether.Session(session_id, where, keep, folders)
        commands, events = workers.make_queue(), workers.make_queue()
        self._commands = commands
        args = (self.library.path, session, commands, events, self._camera)
        if self._in_process:
            worker = workers.start_process(_serve, *args, self._timing)
        else:
            worker = threading.Thread(target=_serve, args=(*args, self._timing), daemon=True)
            worker.start()
        self._worker = worker
        reader = threading.Thread(target=self._read, args=(worker, events), daemon=True)
        reader.start()
        self.changed.emit()

    @property
    def ending(self):
        return self.running and self._ending

    def stop(self):
        """End the session: a frame being fetched is saved and taken in first, then the
        library folder goes back to being watched as before (`changed` tells)."""
        if not self.running or self._ending:
            return
        self._ending = True
        self.message = "Ending the session…"
        self._commands.put((tether.STOP,))
        self.changed.emit()

    def close(self):
        """For the app's exit: the process is told to stop and given a moment."""
        if self._commands is not None:
            self._commands.put((tether.STOP,))
        worker = self._worker
        if worker is not None:
            worker.join(_END_WITHIN)
            if self._in_process and worker.is_alive():
                worker.terminate()

    def capture(self, focus=True):
        self._send(tether.CAPTURE, focus)

    def set_setting(self, name, value):
        self._send(tether.SET, name, value)

    def fetch_again(self):
        """Fetch the frames that were not saved and are on the card again."""
        self._send(tether.FETCH, sorted(self.failures))

    def _send(self, *command):
        if self._commands is not None:
            self._commands.put(command)

    def _end(self):
        self._ending = False
        self.session = self.where = self.info = None
        self.settings, self.message, self._commands, self._worker = {}, None, None, None
        self.fetching = {}
        self.hot.stop()
        self.changed.emit()

    def _folders(self):
        """Where each session with frames still to fetch puts them, if connected."""
        found = {}
        for session in self.library.db.unfinished_tether_sessions():
            where = volumes.resolve(session.volume_key, session.folder, self.library.mounts)
            if where is not None:
                found[session.id] = where
        return found

    def _collection(self, name):
        """The id of the manual collection `name`, made if there is none; None when a smart
        collection has the name."""
        for each in self.library.collections():
            if each.name.casefold() == name.casefold():
                return each.id if each.query is None else None
        return self.library.add_collection(name)

    def _collect(self, added):
        """Photos the hot folder took in: those of this session go in its collection."""
        if self.session is None or self.session.collection_id is None:
            return
        ours = [photo for photo, path in added if path in self._paths]
        if ours:
            self.library.add_to_collection(self.session.collection_id, ours)

    # What the process tells

    def _read(self, worker, events):
        """On a thread of its own: what the process tells, to the main thread, until it
        ends; a process that died is told as an end too."""
        while True:
            try:
                event = events.get(timeout=0.5)
            except queue.Empty:
                if not worker.is_alive():
                    code = getattr(worker, "exitcode", None)
                    why = f"The camera session stopped (exit code {code})"
                    workers.call_on_main(self, self._on_event, worker, (tether.ERROR, why))
                    workers.call_on_main(self, self._on_event, worker, (tether.ENDED,))
                    return
                continue
            workers.call_on_main(self, self._on_event, worker, event)
            if event[0] == tether.ENDED:
                return

    def _on_event(self, worker, event):
        if worker is not self._worker:
            # A session ended already: only what it saved still counts
            if event[0] == tether.SAVED:
                self.hot.take(event[2])
            return
        kind = event[0]
        if self._ending and kind not in (tether.SAVED, tether.FAILED, tether.ENDED):
            return
        if kind in (tether.WAITING, tether.BUSY):
            self.message = event[1]
        elif kind == tether.CONNECTED:
            self.info, self.settings, self.message = event[1], event[2], None
        elif kind == tether.SETTINGS_TOLD:
            self.settings = event[1]
        elif kind == tether.DISCONNECTED:
            self.message = f"The camera was disconnected: connect it again to go on ({event[1]})"
        elif kind == tether.TAKEN:
            self.fetching[event[1]] = event[2]
        elif kind == tether.SAVED:
            item, path = event[1], event[2]
            self.fetching.pop(item, None)
            self.failures.pop(item, None)
            self.saved_count += 1
            self._paths.add(path)
            self.hot.take(path)
            self.saved.emit(path)
        elif kind == tether.FAILED:
            item, name, why = event[1:]
            self.fetching.pop(item, None)
            self.failures[item] = Failure(item, name, why)
            self.failed.emit(name, why)
        elif kind == tether.ERROR:
            self.error.emit(event[1])
        elif kind == tether.ENDED:
            self._end()
            return
        self.changed.emit()


def _serve(library_path, session, commands, events, camera, timing):
    """In the session's process (or thread)."""
    tether.serve(library_path, session, commands, events, camera, **timing)


# For the UI, which does not import core/
TetherSession = dbm.TetherSession
