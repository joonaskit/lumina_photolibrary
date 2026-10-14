"""The photos the grid shows: the library's photos through a Filter, oldest first.

The query runs on a worker thread with a connection of its own, so a library of a
few hundred thousand photos never stalls the window. While a scan writes, the rows
are read again every few seconds (the service says when); the view keeps its place
through `about_to_refresh` and `refreshed`.
"""

import os

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal

from lumina.core import db as dbm
from lumina.qt_models import workers
from lumina.qt_models.thumbnails import Thumbnails

# For the UI, which does not import core/
Filter = dbm.Filter

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
ID_ROLE = Qt.ItemDataRole.UserRole + 2


class LibraryModel(QAbstractListModel):
    # Before and after the rows are read again: the view saves and restores its place
    about_to_refresh = Signal()
    refreshed = Signal()
    loading_changed = Signal(bool)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.filter = dbm.Filter()
        self._rows = []
        self._index = {}
        self._loading = False
        self._generation = 0
        self.thumbnails = Thumbnails(self)
        self.thumbnails.changed.connect(self._thumbnail_changed)
        self.failed = self.thumbnails.failed
        service.photos_changed.connect(self.refresh)
        service.mounts_changed.connect(self._on_mounts_changed)

    def is_loading(self):
        return self._loading

    def set_filter(self, shown):
        """Show what `shown` (a db.Filter) selects."""
        self.filter = shown
        self.thumbnails.clear()
        self._load(reset=True)

    def refresh(self):
        """Read the rows again with the same filter: after a scan wrote some."""
        self._load(reset=False)

    def _load(self, reset):
        self._generation += 1
        generation = self._generation
        self._set_loading(True)
        workers.submit(
            _query,
            self.service.path,
            self.filter,
            on_done=lambda rows: self._loaded(generation, rows, reset),
            on_error=lambda _error: self._loaded(generation, [], reset),
            owner=self,
            process=False,
        )

    def _loaded(self, generation, rows, reset):
        if generation != self._generation:
            return
        if reset or rows != self._rows:
            if not reset:
                self.about_to_refresh.emit()
            self.beginResetModel()
            self._rows = rows
            self._index = {row.id: number for number, row in enumerate(rows)}
            self.endResetModel()
            if not reset:
                self.refreshed.emit()
        self._set_loading(False)

    def _set_loading(self, on):
        if on != self._loading:
            self._loading = on
            self.loading_changed.emit(on)

    # The model

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def photo_id(self, row):
        return self._rows[row].id

    def row_of(self, photo_id):
        return self._index.get(photo_id)

    def path(self, row):
        """Where the photo of `row` is now, or None when its drive is not connected."""
        photo = self._rows[row]
        return self.service.resolve(photo.volume_key, photo.path)

    def name(self, row):
        return os.path.basename(self._rows[row].path)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        photo = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return os.path.basename(photo.path)
        if role == ID_ROLE:
            return photo.id
        path = self.path(index.row())
        if role == Qt.ItemDataRole.DecorationRole:
            return self.thumbnails.icon(self._key(photo), path, photo.content_id, photo.edit)
        if role == Qt.ItemDataRole.ToolTipRole:
            if path is None:
                return f"{photo.path}\nOn a drive that is not connected"
            error = self.thumbnails.error(self._key(photo))
            return f"{path}\n{error}" if error else path
        if role == PATH_ROLE:
            return path
        return None

    def edit(self, row):
        """The parameters JSON of the photo's edit, or None."""
        return self._rows[row].edit

    def thumbnail_pixmap(self, row):
        """The thumbnail of a row if it is made already, else None."""
        return self.thumbnails.pixmap(self._key(self._rows[row]))

    @staticmethod
    def _key(photo):
        # A new edit is a new thumbnail
        return (photo.id, photo.edit)

    def _thumbnail_changed(self, key):
        row = self._index.get(key[0])
        if row is not None:
            index = self.index(row)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])

    def _on_mounts_changed(self):
        # Photos that were not connected may be now, and the other way round
        self.thumbnails.clear()
        if self._rows:
            self.dataChanged.emit(self.index(0), self.index(len(self._rows) - 1))


def _query(path, shown):
    with dbm.Library(path) as library:
        return library.photos(shown)
