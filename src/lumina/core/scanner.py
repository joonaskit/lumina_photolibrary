"""Finding the photos under a folder. Reads directories only; writes nothing."""

import os

from lumina.core.images import is_photo


def find_photos(root, should_stop=lambda: False):
    """Yield the path of every photo under `root`, folder by folder in name order.
    Hidden folders and files are skipped, and so are folders that cannot be read.
    Stops early once `should_stop()` is true."""
    for directory, folders, files in os.walk(root, followlinks=False):
        if should_stop():
            return
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            if not name.startswith(".") and is_photo(name):
                yield os.path.join(directory, name)
