"""A tethered folder (#57): photos a capture program writes into a folder (gphoto2's
`--capture-tethered`, a camera maker's own program) taken into the library as soon as
each is complete, not after the watcher's quiet moment.

Which files: photos directly in a directory of the folder that changed, that the
library does not have as they are (core/library.new_files). When: once a file's size
and time have held still for SETTLE seconds, so a file still being written is not read
half way. A capture program writes a frame in one go; one that pauses for longer than
that is found changed by the folder's next scan, which reads it again.

One pass runs at a time, on a thread: it takes the directories that changed from a
queue as they come, waits for their new files to settle, writes each one's row as soon
as it has (so the photo can be shown), then reads its content id and metadata. It ends
once nothing has come for LINGER seconds, so a session keeps one exiftool between shots.

Nothing here writes to a photo.
"""

import os
import queue
import time

from lumina.core import library as scanning
from lumina.core import metadata

# How long a file's size and time must hold still before it is read, in seconds
SETTLE = 0.5
# How often files that are settling are looked at
POLL = 0.1
# How long a pass waits for more after the last file, in seconds
LINGER = 10.0


class Settling:
    """Files waiting to be complete. `check()` gives those whose size and time have not
    changed for `settle` seconds; an empty file is never complete."""

    def __init__(self, settle=SETTLE, clock=time.monotonic, stat=os.stat):
        self.settle = settle
        self._clock = clock
        self._stat = stat
        # path -> (size, mtime_ns, since when they are so)
        self._seen = {}

    def __len__(self):
        return len(self._seen)

    def add(self, path, size, mtime_ns):
        if path not in self._seen:
            self._seen[path] = (size, mtime_ns, self._clock())

    def check(self):
        """[(path, size, mtime_ns)] of the files that are complete, no longer waited
        for. A file that went is let go of."""
        now = self._clock()
        ready = []
        for path, (size, mtime, since) in list(self._seen.items()):
            try:
                info = self._stat(path)
            except OSError:
                del self._seen[path]
                continue
            if (info.st_size, info.st_mtime_ns) != (size, mtime):
                self._seen[path] = (info.st_size, info.st_mtime_ns, now)
            elif size > 0 and now - since >= self.settle:
                del self._seen[path]
                ready.append((path, size, mtime))
        return ready


def run(
    library,
    folder,
    volume,
    base,
    changed,
    should_stop,
    on_added,
    on_read,
    exiftool=metadata.ExifTool,
    edit_json=None,
    settle=SETTLE,
    poll=POLL,
    linger=LINGER,
    clock=time.monotonic,
):
    """One pass over the tethered `folder` (found at `base`, on the volume with id
    `volume`). `changed` is a queue.SimpleQueue of the directories that changed, which
    the watcher goes on filling. Each photo found complete is written at once (with
    `edit_json`, a preset's edit, when new) and told to `on_added([(photo id, path)])`;
    then its details are read and `on_read([(photo id, path)], failures)` is told, the
    failures being [(path, message)]. Ends when `should_stop()`, or when nothing has
    been waited for and nothing has come for `linger` seconds."""
    settling = Settling(settle, clock)
    tool = None
    idle_since = clock()
    try:
        while not should_stop():
            while True:
                try:
                    directory = changed.get_nowait()
                except queue.Empty:
                    break
                for found in scanning.new_files(library, folder, volume, base, directory):
                    settling.add(*found)
            ready = settling.check()
            if ready:
                added = scanning.take_in(library, folder, volume, base, ready, edit_json)
                if added:
                    on_added(added)
                    tool = _read(library, added, tool, exiftool, on_read)
            if len(settling) or ready:
                idle_since = clock()
            elif clock() - idle_since >= linger:
                return
            time.sleep(poll)
    finally:
        if tool is not None:
            tool.close()


def _read(library, added, tool, exiftool, on_read):
    """Read the details of `added`; returns the ExifTool to keep for the next, or None."""
    failures = []
    if tool is None:
        try:
            tool = exiftool()
        except metadata.MetadataUnavailable:
            # Left without details, as a scan leaves them; the next scan reads them
            on_read(added, failures)
            return None
    failures, stopped, _gone = scanning.read_details(library, dict(added), tool)
    if stopped is not None:
        tool.close()
        tool = None
    on_read(added, failures)
    return tool
