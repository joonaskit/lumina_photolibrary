"""The photos the grid shows: the library's photos through a Filter, oldest first.

The query runs on a worker thread with a connection of its own, so a library of a
few hundred thousand photos never stalls the window. While a scan writes, the rows
are read again every few seconds (the service says when); the view keeps its place
through `about_to_refresh` and `refreshed`.

With a search (a text, or "like this photo"), the same rows are shown ranked by the
SearchService, best first, and only those that match. The search is run again when
the rows are read again and while indexing makes more photos findable.

Flags (thumbs up or down) narrow what is shown here, not in the query, so that
flagging is instant and a photo flagged out of the view stays until the user moves
on (`settle`): a photo given a thumbs down while thumbs-down photos are hidden does
not vanish under the pointer. Flag changes can be undone, for any photo, also one no
longer shown.
"""

import os
import time
from dataclasses import dataclass, replace

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, QTimer, Signal

from lumina.core import db as dbm
from lumina.qt_models import workers
from lumina.qt_models.thumbnails import Thumbnails

# For the UI, which does not import core/
Filter = dbm.Filter
PICKED, UNFLAGGED, REJECTED = dbm.PICKED, dbm.UNFLAGGED, dbm.REJECTED

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
ID_ROLE = Qt.ItemDataRole.UserRole + 2
FLAG_ROLE = Qt.ItemDataRole.UserRole + 3
# Whether the cell is drawn greyed out: a thumbs-down photo, while they are dimmed
DIMMED_ROLE = Qt.ItemDataRole.UserRole + 4

# What becomes of thumbs-down photos when no flag filter is chosen
SHOW, DIM, HIDE = "show", "dim", "hide"

# While indexing, a search is run again at most this often, in seconds
_RESEARCH_EVERY = 3.0
# Flag changes that can be undone
_HISTORY = 200

# The edit filter: only photos with an edit (of their own, not a virtual copy's), or
# only those without
EDITED, UNEDITED = "edited", "unedited"

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
    # Flags were set, undone or redone: what can be undone changed
    flags_changed = Signal()

    def __init__(self, service, search=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.search_service = search
        self.filter = dbm.Filter()
        self.search = None
        # A flag to show only, or None for any
        self.flag_filter = None
        # EDITED or UNEDITED to show only those, or None for any
        self.edit_filter = None
        self.rejected = DIM
        # Every row of the filter; `_ranked` the ids a search found, best first; `_rows`
        # what is shown
        self._all = []
        # Photo id: its position in `_all`
        self._positions = {}
        self._ranked = None
        self._rows = []
        self._index = {}
        # Photos flagged out of the view, shown until the user moves on
        self._kept = set()
        self._undo = []
        self._redo = []
        self._loading = False
        self._querying = False
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
        self._kept.clear()
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
        self._kept.clear()
        self.thumbnails.clear()
        self.search_changed.emit()
        if search is None:
            self._ranked = None
            self._show(self._compose(), reset=True)
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
                self._ranked = [photo.id for photo, _cosine in ranked]
                self._show(self._compose(), reset)
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
        self._querying = True
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
        self._querying = False
        if not reset:
            self._keep_edited_photos(rows)
        self._all = rows
        self._positions = {row.id: number for number, row in enumerate(rows)}
        if self.search is not None:
            self._run_search(reset)
            return
        self._show(self._compose(), reset)
        self._set_loading(False)

    # What is shown

    def _compose(self):
        if self._ranked is None:
            base = self._all
        else:
            base = [self._all[self._positions[i]] for i in self._ranked if i in self._positions]
        return [photo for photo in base if self._passes(photo)]

    def _keep_edited_photos(self, rows):
        # A photo edited (or its edit removed) while the edit filter is on stays until
        # the user moves on, as one flagged out of the view does: it is the one in hand
        if self.edit_filter is None:
            return
        for photo in rows:
            shown = self._index.get(photo.id)
            if shown is not None and self._rows[shown].edit != photo.edit:
                self._kept.add(photo.id)

    def _edit_passes(self, photo):
        if self.edit_filter is None:
            return True
        return (photo.edit is not None) == (self.edit_filter == EDITED)

    def _passes(self, photo):
        if photo.id in self._kept:
            return True
        if not self._edit_passes(photo):
            return False
        if self.flag_filter is not None:
            return photo.flag == self.flag_filter
        return not (self.rejected == HIDE and photo.flag == REJECTED)

    def _show(self, rows, reset):
        old = [photo.id for photo in self._rows]
        new = [photo.id for photo in rows]
        if reset:
            self.beginResetModel()
            self._set_rows(rows)
            self.endResetModel()
        elif new == old:
            changed = [n for n, (a, b) in enumerate(zip(self._rows, rows, strict=True)) if a != b]
            self._set_rows(rows)
            if changed:
                self.dataChanged.emit(self.index(changed[0]), self.index(changed[-1]))
        elif _is_subsequence(new, old):
            # Rows left out (flags settled): removed as such, so the selection stays
            keep = set(new)
            for first, last in reversed(_runs([n for n, i in enumerate(old) if i not in keep])):
                self.beginRemoveRows(QModelIndex(), first, last)
                # The index too before the views hear of it: they may ask for a row
                self._set_rows(self._rows[:first] + self._rows[last + 1 :])
                self.endRemoveRows()
            changed = rows != self._rows
            self._set_rows(rows)
            if changed and rows:
                self.dataChanged.emit(self.index(0), self.index(len(rows) - 1))
        else:
            self.about_to_refresh.emit()
            self.beginResetModel()
            self._set_rows(rows)
            self.endResetModel()
            self.refreshed.emit()

    def _set_rows(self, rows):
        self._rows = list(rows)
        self._index = {row.id: number for number, row in enumerate(self._rows)}

    def _set_loading(self, on):
        if on != self._loading:
            self._loading = on
            self.loading_changed.emit(on)

    # Flags

    def set_flag_filter(self, flag):
        """Show only photos with `flag` (PICKED, UNFLAGGED or REJECTED), or any (None)."""
        if flag != self.flag_filter:
            self.flag_filter = flag
            self._kept.clear()
            self._show(self._compose(), reset=False)

    def set_edit_filter(self, which):
        """Show only photos with an edit (EDITED) or without one (UNEDITED), or any (None)."""
        if which != self.edit_filter:
            self.edit_filter = which
            self._kept.clear()
            self._show(self._compose(), reset=False)

    def set_rejected(self, mode):
        """SHOW, DIM or HIDE thumbs-down photos (with no flag filter)."""
        if mode != self.rejected:
            self.rejected = mode
            self._kept.clear()
            self._show(self._compose(), reset=False)

    def hidden_count(self):
        """How many photos of the filter or search the flags leave out."""
        if self.flag_filter is None and self.rejected != HIDE:
            return 0
        # Of the photos the edit filter lets through, those the flags leave out
        if self._ranked is None:
            base = self._all
        else:
            base = [self._all[self._positions[i]] for i in self._ranked if i in self._positions]
        return sum(1 for p in base if self._edit_passes(p)) - sum(
            1 for p in self._rows if self._edit_passes(p)
        )

    def flag(self, row):
        return self._rows[row].flag

    def toggle_flag(self, rows, flag):
        """Give the photos of `rows` `flag`; when they all have it already, clear it, as
        pressing the key again does. Returns the flag given, or None for no change."""
        photos = [self._rows[row] for row in rows if 0 <= row < len(self._rows)]
        if not photos:
            return None
        if all(photo.flag == flag for photo in photos):
            flag = UNFLAGGED
        changes = {photo.id: flag for photo in photos if photo.flag != flag}
        if not changes:
            return None
        before = {photo.id: photo.flag for photo in photos if photo.id in changes}
        self._write(changes)
        self._undo = [*self._undo[-(_HISTORY - 1) :], before]
        self._redo.clear()
        self.flags_changed.emit()
        return flag

    def can_undo_flags(self):
        return bool(self._undo)

    def can_redo_flags(self):
        return bool(self._redo)

    def undo_flags(self):
        """Put back the flags before the last change. Returns how many photos it set."""
        return self._step_history(self._undo, self._redo)

    def redo_flags(self):
        return self._step_history(self._redo, self._undo)

    def _step_history(self, source, target):
        if not source:
            return 0
        flags = source.pop()
        # From the library: a photo not in the rows still has its flag there
        target.append(self.service.flags(flags))
        self._write(flags)
        self.flags_changed.emit()
        return len(flags)

    def _write(self, flags):
        self.service.set_flags(flags)
        for photo_id, flag in flags.items():
            position = self._positions.get(photo_id)
            if position is not None:
                self._all[position] = replace(self._all[position], flag=flag)
            if photo_id in self._index:
                self._kept.add(photo_id)
        if self._querying:
            # The rows coming were read before this
            self._load(reset=False)
        self._show(self._compose(), reset=False)

    def settle(self, keep=None):
        """The user moved on: photos flagged out of the view leave it, all but `keep`
        (a photo id), the one they are on now."""
        kept = self._kept & {keep}
        if kept != self._kept:
            self._kept = kept
            self._show(self._compose(), reset=False)

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
        if role == FLAG_ROLE:
            return photo.flag
        if role == DIMMED_ROLE:
            return photo.flag == REJECTED and self.rejected != SHOW and self.flag_filter != REJECTED
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


def _is_subsequence(part, whole):
    items = iter(whole)
    return all(any(item == wanted for item in items) for wanted in part)


def _runs(numbers):
    """[(first, last)] of the runs of consecutive numbers in sorted `numbers`."""
    runs = []
    for number in numbers:
        if runs and runs[-1][1] == number - 1:
            runs[-1] = (runs[-1][0], number)
        else:
            runs.append((number, number))
    return runs


def _query(path, shown):
    with dbm.Library(path) as library:
        return library.photos(shown)
