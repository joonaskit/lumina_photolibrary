"""Finding the photos under a folder. Reads directories only; writes nothing."""

import os

from lumina.core.images import is_photo


def find_photos(root, should_stop=lambda: False, on_error=None):
    """Yield the path of every photo under `root`, folder by folder in name order.
    Hidden folders and files are skipped. A folder that cannot be read is skipped
    too, and passed to `on_error(path, message)`. Stops early once `should_stop()`
    is true."""

    def failed(error):
        if on_error is not None:
            on_error(error.filename or root, error.strerror or str(error))

    for directory, folders, files in os.walk(root, onerror=failed, followlinks=False):
        if should_stop():
            return
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            if not name.startswith(".") and is_photo(name):
                yield os.path.join(directory, name)
