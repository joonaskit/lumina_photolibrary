"""A preview of a panorama, for the dialog that asks how to stitch it.

The same plan and stitch as the job's (core/panorama.py), on half-size decodes of the
photos, so the projection and the crop can be seen before the job runs. The photos are
decoded once, into a work folder of its own; a new projection or crop plans and stitches
them again. The steps run in the merge process (workers.MERGING), one after another, so
the UI thread never waits for one, and a change made while one runs shows the newest
result only.
"""

import os
import shutil
import tempfile
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from lumina.core import panorama
from lumina.core.errors import PhotoError
from lumina.qt_models import workers

# A work folder this old is one a crash left behind
_STALE = 24 * 3600


class PanoramaPreview(QObject):
    # What it is doing, in words: "Reading photo 2 of 5…"
    progress = Signal(str)
    # The panorama, for the projection and crop last asked for
    picture = Signal(QImage)
    # Why there is none, in words, naming the photo
    failed = Signal(str)

    def __init__(self, paths, folder, parent=None):
        """`paths`: the photos, as taken. Work folders are made in `folder`."""
        super().__init__(parent)
        self._paths = list(paths)
        self._folder = folder
        self._work = None
        self._decoded = 0
        self._wanted = None
        self._generation = 0
        self._failure = None
        self._closed = False
        self._future = None

    def start(self, projection, crop):
        """Show the panorama for `projection` and `crop`: the first call reads the
        photos, then each call plans and stitches them."""
        if self._closed:
            return
        self._wanted = (projection, crop)
        self._generation += 1
        if self._work is None:
            self._work = _new_folder(self._folder)
            self._decode_next()
        elif self._failure is not None:
            self.failed.emit(self._failure)
        elif self._decoded == len(self._paths):
            self._plan()

    def close(self):
        """Stop, and remove what was made. A step that is running finishes first."""
        if self._closed:
            return
        self._closed = True
        work, future = self._work, self._future
        if work is None:
            return
        if future is None or future.cancel() or future.done():
            shutil.rmtree(work, ignore_errors=True)
        else:
            future.add_done_callback(lambda _done: shutil.rmtree(work, ignore_errors=True))

    # Reading the photos

    def _decode_next(self):
        if self._decoded == len(self._paths):
            self._plan()
            return
        index = self._decoded
        self.progress.emit(f"Reading photo {index + 1} of {len(self._paths)}…")
        self._future = workers.submit(
            panorama.preview_frame,
            self._paths[index],
            index,
            self._work,
            self._paths[0],
            on_done=self._decoded_one,
            on_error=self._decoding_failed,
            owner=self,
            process=workers.MERGING,
        )

    def _decoded_one(self, _result):
        if self._closed:
            return
        self._decoded += 1
        self._decode_next()

    def _decoding_failed(self, error):
        if self._closed:
            return
        # Not retried: the photos are what they are
        self._failure = _words(error)
        self.failed.emit(self._failure)

    # Planning and stitching

    def _plan(self):
        generation = self._generation
        projection, crop = self._wanted
        self.progress.emit("Putting the photos together…")
        self._future = workers.submit(
            panorama.preview_plan,
            self._work,
            len(self._paths),
            projection,
            crop,
            self._paths,
            on_done=lambda result: self._planned(generation, result),
            on_error=lambda error: self._plan_failed(generation, error),
            owner=self,
            process=workers.MERGING,
        )

    def _planned(self, generation, result):
        if self._closed or generation != self._generation:
            return
        width, height, rows = result
        image = QImage(rows, width, height, 3 * width, QImage.Format.Format_RGB888)
        # A copy: the image would otherwise show bytes that are about to go
        self.picture.emit(image.copy())

    def _plan_failed(self, generation, error):
        if self._closed or generation != self._generation:
            return
        self.failed.emit(_words(error))


def _words(error):
    if isinstance(error, PhotoError):
        return f"{os.path.basename(error.path)} {error.message}"
    return f"The photos could not be put together ({error})"


def _new_folder(folder):
    os.makedirs(folder, exist_ok=True)
    now = time.time()
    for name in os.listdir(folder):
        old = os.path.join(folder, name)
        try:
            if now - os.stat(old).st_mtime > _STALE:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    return tempfile.mkdtemp(prefix="preview-", dir=folder)
