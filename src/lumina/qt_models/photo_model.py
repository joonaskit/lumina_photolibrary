"""The photos of a folder as a list model, with thumbnails made as they are shown.

The folder is scanned on a thread and the rows arrive in batches. A thumbnail is
asked for the first time its row is painted, decoded in a worker process, and kept
in a bounded cache: a row whose thumbnail was dropped asks again.
"""

import os
import threading
from collections import OrderedDict

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap

from lumina.core import images, scanner
from lumina.integration import style
from lumina.qt_models import workers
from lumina.qt_models.images import to_image

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
# Longest side of a thumbnail, in pixels: enough for the grid on a dense screen
THUMB_SIDE = 320
# About 400 KB each: some 300 MB at most
_CACHE_SIZE = 800
_BATCH = 200


class PhotoModel(QAbstractListModel):
    scanning_changed = Signal(bool)
    # A thumbnail could not be made: the path and why
    failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._paths = []
        self._rows = {}
        self._folder = None
        self._stop = threading.Event()
        self._scanning = False
        # Goes up with each folder, so results for an earlier one are dropped
        self._generation = 0
        self._thumbs = OrderedDict()
        # Path -> the future making its thumbnail
        self._pending = {}
        self._errors = {}
        self._placeholder = None
        self._broken = None
        style.themed.changed.connect(self._on_themed)

    # The folder

    def folder(self):
        return self._folder

    def is_scanning(self):
        return self._scanning

    def set_folder(self, folder):
        self._stop.set()
        self._stop = stop = threading.Event()
        self._generation += 1
        generation = self._generation
        self.beginResetModel()
        self._folder = os.fspath(folder)
        self._paths, self._rows = [], {}
        self._thumbs.clear()
        # The thumbnails of the folder before: those not started yet are not made
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()
        self._errors.clear()
        self.endResetModel()
        self._set_scanning(True)
        workers.submit(
            self._scan,
            self._folder,
            stop,
            generation,
            on_done=lambda _none: self._scan_ended(generation),
            on_error=lambda _error: self._scan_ended(generation),
            owner=self,
            process=False,
        )

    def stop(self):
        self._stop.set()

    def _scan(self, folder, stop, generation):
        # On a worker thread: nothing of the model is touched here
        batch = []
        for path in scanner.find_photos(folder, should_stop=stop.is_set):
            batch.append(path)
            if len(batch) >= _BATCH:
                workers.call_on_main(self, self._add, generation, batch)
                batch = []
        if batch and not stop.is_set():
            workers.call_on_main(self, self._add, generation, batch)

    def _add(self, generation, paths):
        if generation != self._generation:
            return
        first = len(self._paths)
        self.beginInsertRows(QModelIndex(), first, first + len(paths) - 1)
        for row, path in enumerate(paths, first):
            self._rows[path] = row
        self._paths.extend(paths)
        self.endInsertRows()

    def _scan_ended(self, generation):
        if generation == self._generation:
            self._set_scanning(False)

    def _set_scanning(self, on):
        if on != self._scanning:
            self._scanning = on
            self.scanning_changed.emit(on)

    # The model

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._paths)

    def path(self, row):
        return self._paths[row]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        path = self._paths[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return os.path.basename(path)
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail(path)
        if role == Qt.ItemDataRole.ToolTipRole:
            error = self._errors.get(path)
            return f"{path}\n{error}" if error else path
        if role == PATH_ROLE:
            return path
        return None

    def thumbnail_pixmap(self, row):
        """The thumbnail of a row if it is made already, else None: for the viewer to
        show while the larger one is decoded."""
        icon = self._thumbs.get(self._paths[row])
        return icon.pixmap(THUMB_SIDE) if icon is not None else None

    # Thumbnails

    def _thumbnail(self, path):
        icon = self._thumbs.get(path)
        if icon is not None:
            self._thumbs.move_to_end(path)
            return icon
        if path in self._errors:
            return self._broken_icon()
        if path not in self._pending:
            generation = self._generation
            self._pending[path] = workers.submit(
                images.load_pixels,
                path,
                THUMB_SIDE,
                on_done=lambda pixels: self._thumb_made(generation, path, pixels),
                on_error=lambda error: self._thumb_failed(generation, path, error),
                owner=self,
            )
        return self._placeholder_icon()

    def _thumb_made(self, generation, path, pixels):
        if generation != self._generation:
            return
        self._pending.pop(path, None)
        self._thumbs[path] = QIcon(QPixmap.fromImage(to_image(pixels)))
        while len(self._thumbs) > _CACHE_SIZE:
            self._thumbs.popitem(last=False)
        self._changed(path)

    def _thumb_failed(self, generation, path, error):
        if generation != self._generation:
            return
        self._pending.pop(path, None)
        # A PhotoError names the file already; anything else is told with the path
        message = getattr(error, "message", None) or str(error) or type(error).__name__
        self._errors[path] = message
        self.failed.emit(path, message)
        self._changed(path)

    def _changed(self, path):
        row = self._rows.get(path)
        if row is not None:
            index = self.index(row)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])

    def _placeholder_icon(self):
        if self._placeholder is None:
            self._placeholder = style.icon("image-x-generic", colour="textTertiary")
        return self._placeholder

    def _broken_icon(self):
        if self._broken is None:
            self._broken = style.icon("image-missing", "dialog-error", colour="danger")
        return self._broken

    def _on_themed(self):
        self._placeholder = self._broken = None
        if self._paths:
            self.dataChanged.emit(self.index(0), self.index(len(self._paths) - 1))
