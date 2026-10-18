"""Finding the photos under a folder. Reads directories only; writes nothing."""

import os
import time

from lumina.core.images import is_photo

# What our own jobs write beside the photos while they work (a sidecar, a tethered frame,
# a merge, an import, an export), and rename into place when done
_PART_PREFIXES = (".lumina-xmp-", ".lumina-tether-", ".lumina-merge-", ".lumina-import-")
_PART_PREFIXES += (".lumina-export-",)
STALE_AFTER = 3600.0


def is_stale_part(name, mtime, now=None):
    """Whether a file of this name and modification time (seconds) is one of ours left by
    a job that died: no job is still writing to a part file that has not changed in an
    hour."""
    if not (name.startswith(_PART_PREFIXES) and name.endswith(".part")):
        return False
    return (time.time() if now is None else now) - mtime > STALE_AFTER


def find_photos(root, should_stop=lambda: False, on_error=None, on_stale=None):
    """Yield the path of every photo under `root`, folder by folder in name order.
    Hidden folders and files are skipped. A folder that cannot be read is skipped
    too, and passed to `on_error(path, message)`. Stops early once `should_stop()`
    is true. A part file of ours that a crash left (`is_stale_part`) is passed to
    `on_stale(path)`, if given."""

    def failed(error):
        if on_error is not None:
            on_error(error.filename or root, error.strerror or str(error))

    for directory, folders, files in os.walk(root, onerror=failed, followlinks=False):
        if should_stop():
            return
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            if not name.startswith("."):
                if is_photo(name):
                    yield os.path.join(directory, name)
            elif on_stale is not None and name.endswith(".part"):
                path = os.path.join(directory, name)
                try:
                    stale = is_stale_part(name, os.lstat(path).st_mtime)
                except OSError:
                    continue
                if stale:
                    on_stale(path)


def find_photos_in(directory, on_error=None, on_stale=None):
    """Yield the path of every photo directly in `directory` (not below it), in name
    order, as `find_photos` does, with the same hidden files skipped and stale part files
    passed to `on_stale`. A directory that cannot be read is passed to `on_error`."""
    try:
        names = sorted(os.listdir(directory))
    except OSError as error:
        if on_error is not None:
            on_error(error.filename or directory, error.strerror or str(error))
        return
    for name in names:
        path = os.path.join(directory, name)
        if name.startswith("."):
            if on_stale is not None and name.endswith(".part"):
                try:
                    stale = is_stale_part(name, os.lstat(path).st_mtime)
                except OSError:
                    continue
                if stale:
                    on_stale(path)
        elif is_photo(name) and os.path.isfile(path):
            yield path
