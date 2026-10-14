"""The photos the grid shows: the library's photos through a Filter, oldest first.

The query runs on a worker thread with a connection of its own, so a library of a
few hundred thousand photos never stalls the window. While a scan writes, the rows
are read again every few seconds (the service says when); the view keeps its place
through `about_to_refresh` and `refreshed`.

With a search (a text, or "like this photo"), the same rows are shown ranked by the
SearchService, best first, and only those that match. The search is run again when
the rows are read again and while indexing makes more photos findable.
"""

import os
import time
from dataclasses import dataclass

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, QTimer, Signal

from lumina.core import db as dbm
from lumina.qt_models import workers
from lumina.qt_models.thumbnails import Thumbnails

# For the UI, which does not import core/
Filter = dbm.Filter

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
ID_ROLE = Qt.ItemDataRole.UserRole + 2

# While indexing, a search is run again at most this often, in seconds
_RESEARCH_EVERY = 3.0

TEXT, SIMILAR = "text", "similar"


@dataclass(frozen=True)
class Search:
    kind: str
    # The text, or the content id of the photo
    query: str
    # The text, or the photo's name: what the UI shows
    label: str


class LibraryModel(QAbstractListModel):
    # Before and after the rows are read again: the view saves and restores its place
    about_to_refresh = Signal()
    refreshed = Signal()
    loading_changed = Signal(bool)
    # The search changed, or its results came
    search_changed = Signal()

    def __init__(self, service, search=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.search_service = search
        self.filter = dbm.Filter()
        self.search = None
        # Every row of the filter; `_rows` is what is shown, ranked while searching
        self._all = []
        self._rows = []
        self._index = {}
        self._loading = False
        self._generation = 0
        self.thumbnails = Thumbnails(self)
        self.thumbnails.changed.connect(self._thumbnail_changed)
        self.failed = self.thumbnails.failed
        service.photos_changed.connect(self.refresh)
        service.mounts_changed.connect(self._on_mounts_changed)
        self._searched = 0.0
        self._research = QTimer(self)
        self._research.setSingleShot(True)
        self._research.timeout.connect(self._run_search)
        if search is not None:
            search.index_changed.connect(self._on_index_changed)

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

    # Searching

    def search_text(self, text):
        """Show the photos that match `text`, best first; an empty text ends the search."""
        text = text.strip()
        self._start_search(Search(TEXT, text, text) if text else None)

    def find_similar(self, row):
        """Show the photos most like the one in `row`, best first. Returns False when it
        has no vector yet (not indexed, or not read)."""
        photo = self._rows[row]
        if photo.content_id is None or self.search_service is None:
            return False
        self._start_search(Search(SIMILAR, photo.content_id, os.path.basename(photo.path)))
        return True

    def clear_search(self):
        self._start_search(None)

    def is_searching(self):
        return self.search is not None

    def _start_search(self, search):
        if search == self.search:
            return
        self.search = search
        self.thumbnails.clear()
        self.search_changed.emit()
        if search is None:
            self._show(self._all, reset=True)
        else:
            self._run_search(reset=True)

    def _run_search(self, reset=False):
        search = self.search
        if search is None or self.search_service is None:
            return
        self._searched = time.monotonic()
        self._set_loading(True)
        generation = self._generation

        def done(ranked):
            if generation == self._generation and search == self.search:
                self._show([photo for photo, _cosine in ranked], reset)
                self._set_loading(False)
                self.search_changed.emit()

        if search.kind == TEXT:
            self.search_service.search_text(search.query, self._all, done)
        else:
            self.search_service.similar(search.query, self._all, done)

    def _on_index_changed(self):
        if self.search is None or self._research.isActive():
            return
        wait = _RESEARCH_EVERY - (time.monotonic() - self._searched)
        self._research.start(max(0, int(wait * 1000)))

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
        self._all = rows
        if self.search is not None:
            self._run_search(reset)
            return
        self._show(rows, reset)
        self._set_loading(False)

    def _show(self, rows, reset):
        if reset or rows != self._rows:
            if not reset:
                self.about_to_refresh.emit()
            self.beginResetModel()
            self._rows = rows
            self._index = {row.id: number for number, row in enumerate(rows)}
            self.endResetModel()
            if not reset:
                self.refreshed.emit()

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
