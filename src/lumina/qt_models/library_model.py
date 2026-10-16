"""The photos the grid shows: the library's photos through a Filter, oldest first.

The query runs on a worker thread with a connection of its own, so a library of a
few hundred thousand photos never stalls the window. While a scan writes, the rows
are read again every few seconds (the service says when); the view keeps its place
through `about_to_refresh` and `refreshed`.

With a search (a text, or "like this photo"), the same rows are shown ranked by the
SearchService, best first, and only those that match. The search is run again when
the rows are read again and while indexing makes more photos findable.

Flags (thumbs up or down), ratings (stars) and keywords narrow what is shown here, not in the
query, so that flagging and rating are instant and a photo flagged or rated out of the
view stays until the user moves on (`settle`): a photo given a thumbs down while
thumbs-down photos are hidden does not vanish under the pointer. Changes of either can
be undone, in one order, for any photo, also one no longer shown.
"""

import json
import os
import time
from dataclasses import dataclass, replace

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, Qt, QTimer, Signal

from lumina.core import db as dbm
from lumina.core import keywords as keywordm
from lumina.core import smart
from lumina.qt_models import workers
from lumina.qt_models.thumbnails import Thumbnails

# For the UI, which does not import core/
Filter = dbm.Filter
PICKED, UNFLAGGED, REJECTED = dbm.PICKED, dbm.UNFLAGGED, dbm.REJECTED
# Keyword paths (their names and levels), and a smart collection's saved filter
keyword_names = keywordm
SmartQuery = smart.SmartQuery

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
ID_ROLE = Qt.ItemDataRole.UserRole + 2
FLAG_ROLE = Qt.ItemDataRole.UserRole + 3
# Whether the cell is drawn greyed out: a thumbs-down photo, while they are dimmed
DIMMED_ROLE = Qt.ItemDataRole.UserRole + 4
# The photo's stars, 0 to 5
RATING_ROLE = Qt.ItemDataRole.UserRole + 5
# What is known of damage to the photo's raw data, as a sentence, or None
DAMAGE_ROLE = Qt.ItemDataRole.UserRole + 6
# The paths of the photo's keywords, as a list
KEYWORDS_ROLE = Qt.ItemDataRole.UserRole + 7

# What a drag of photos from the grid carries: the photo ids as JSON
PHOTOS_MIME = "application/x-lumina-photos"

# What becomes of thumbs-down photos when no flag filter is chosen
SHOW, DIM, HIDE = "show", "dim", "hide"

# While indexing, a search is run again at most this often, in seconds
_RESEARCH_EVERY = 3.0
# Flag and rating changes that can be undone
_HISTORY = 200
# The service's methods that read and write a field of many photos
_READ = {"flag": "flags", "rating": "ratings", "keywords": "photo_keywords"}
_WRITE = {"flag": "set_flags", "rating": "set_ratings", "keywords": "set_photo_keywords"}
# The rating filter's "no stars": any other value is that many stars or more
NOT_RATED = 0
STARS = (1, 2, 3, 4, 5)

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
    # Flags or ratings were set, undone or redone: what can be undone changed
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
        # NOT_RATED for photos without stars, or a number of stars for those with that
        # many or more, or None for any
        self.rating_filter = None
        # A keyword path to show only the photos given it or one below it, or None for any
        self.keyword_filter = None
        self.rejected = DIM
        # Every row of the filter; `_ranked` the ids a search found, best first; `_rows`
        # what is shown
        self._all = []
        # Photo id: its position in `_all`
        self._positions = {}
        self._ranked = None
        self._rows = []
        self._index = {}
        # Photos flagged or rated out of the view, shown until the user moves on
        self._kept = set()
        # Changes that can be undone: (field, {photo id: value it had}) with field "flag",
        # "rating" or "keywords" (the ids of its keywords)
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

    def _rating_passes(self, photo):
        if self.rating_filter is None:
            return True
        if self.rating_filter == NOT_RATED:
            return photo.rating == 0
        return photo.rating >= self.rating_filter

    def _keyword_passes(self, photo):
        if self.keyword_filter is None:
            return True
        paths = self.service.keyword_paths
        return any(
            keywordm.within(paths[keyword], self.keyword_filter)
            for keyword in photo.keywords
            if keyword in paths
        )

    def _kept_passes(self, photo):
        return (
            self._edit_passes(photo) and self._rating_passes(photo) and self._keyword_passes(photo)
        )

    def _passes(self, photo):
        if photo.id in self._kept:
            return True
        if not self._kept_passes(photo):
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
        elif (steps := _steps(old, new)) is not None:
            # Rows left out (flags settled) or added (a scan wrote them) with the others
            # in their order: removed and inserted as such, so the selection stays
            gone, added = steps
            if added:
                self.about_to_refresh.emit()
            for first, last in reversed(gone):
                self.beginRemoveRows(QModelIndex(), first, last)
                # The index too before the views hear of it: they may ask for a row
                self._set_rows(self._rows[:first] + self._rows[last + 1 :])
                self.endRemoveRows()
            for first, last in added:
                self.beginInsertRows(QModelIndex(), first, last)
                self._set_rows(self._rows[:first] + rows[first : last + 1] + self._rows[first:])
                self.endInsertRows()
            changed = rows != self._rows
            self._set_rows(rows)
            if changed and rows:
                self.dataChanged.emit(self.index(0), self.index(len(rows) - 1))
            if added:
                self.refreshed.emit()
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

    def set_rating_filter(self, stars):
        """Show only photos without stars (NOT_RATED), or with `stars` or more, or any
        (None)."""
        if stars != self.rating_filter:
            self.rating_filter = stars
            self._kept.clear()
            self._show(self._compose(), reset=False)

    def set_keyword_filter(self, path):
        """Show only the photos given keyword `path` or one below it, or any (None)."""
        if path != self.keyword_filter:
            self.keyword_filter = path
            self._kept.clear()
            self._show(self._compose(), reset=False)

    def refilter(self):
        """The keywords' names changed (renamed, deleted): show what passes now."""
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
        # Of the photos the edit and rating filters let through, those the flags leave out
        if self._ranked is None:
            base = self._all
        else:
            base = [self._all[self._positions[i]] for i in self._ranked if i in self._positions]

        wanted = self._kept_passes

        return sum(1 for p in base if wanted(p)) - sum(1 for p in self._rows if wanted(p))

    def flag(self, row):
        return self._rows[row].flag

    def rating(self, row):
        return self._rows[row].rating

    def keywords(self, row):
        """The paths of the keywords of `row`'s photo, by path."""
        paths = self.service.keyword_paths
        return sorted((paths[k] for k in self._rows[row].keywords if k in paths), key=str.casefold)

    def add_keywords(self, rows, names):
        """Give the photos of `rows` the keywords `names` (as typed: "Places > Finland").
        Returns the keyword paths given, or [] for no change (nothing to add, or every
        photo has them)."""
        paths = [path for path in map(keywordm.parse, names) if path]
        photos = [self._rows[row] for row in rows if 0 <= row < len(self._rows)]
        if not paths or not photos:
            return []
        ids = self.service.ensure_keywords(paths)
        wanted = set(ids.values())
        changes = {
            photo.id: tuple(sorted(set(photo.keywords) | wanted))
            for photo in photos
            if not wanted <= set(photo.keywords)
        }
        self._change_keywords(photos, changes)
        return list(ids) if changes else []

    def remove_keywords(self, rows, names):
        """Take the keywords `names` off the photos of `rows` (not those below them: a
        photo with Places > Finland keeps it when Places is taken off). Returns how many
        photos changed."""
        paths = {path.casefold() for path in map(keywordm.parse, names) if path}
        photos = [self._rows[row] for row in rows if 0 <= row < len(self._rows)]
        known = self.service.keyword_paths
        unwanted = {k for k, path in known.items() if path.casefold() in paths}
        changes = {
            photo.id: tuple(k for k in photo.keywords if k not in unwanted)
            for photo in photos
            if unwanted & set(photo.keywords)
        }
        self._change_keywords(photos, changes)
        return len(changes)

    def _change_keywords(self, photos, changes):
        if not changes:
            return
        before = {photo.id: photo.keywords for photo in photos if photo.id in changes}
        self._write("keywords", changes)
        self._push_undo("keywords", before)

    def _push_undo(self, field, before):
        self._undo = [*self._undo[-(_HISTORY - 1) :], (field, before)]
        self._redo.clear()
        self.flags_changed.emit()

    def damage(self, row):
        """What is known of damage to the raw data of `row`'s photo, or None."""
        return damage_text(self._rows[row].damage_warning)

    def toggle_flag(self, rows, flag):
        """Give the photos of `rows` `flag`; when they all have it already, clear it, as
        pressing the key again does. Returns the flag given, or None for no change."""
        return self._toggle("flag", rows, flag, UNFLAGGED)

    def toggle_rating(self, rows, stars):
        """Give the photos of `rows` `stars` (0 clears); when they all have that many
        already, clear it, as pressing the key again does. Returns the stars given, or
        None for no change."""
        return self._toggle("rating", rows, stars, 0)

    def _toggle(self, field, rows, value, none):
        photos = [self._rows[row] for row in rows if 0 <= row < len(self._rows)]
        if not photos:
            return None
        if all(getattr(photo, field) == value for photo in photos):
            value = none
        changes = {photo.id: value for photo in photos if getattr(photo, field) != value}
        if not changes:
            return None
        before = {photo.id: getattr(photo, field) for photo in photos if photo.id in changes}
        self._write(field, changes)
        self._push_undo(field, before)
        return value

    def can_undo_flags(self):
        return bool(self._undo)

    def can_redo_flags(self):
        return bool(self._redo)

    def undo_flags(self):
        """Put back what the last change of flags or ratings changed. Returns how many
        photos it set."""
        return self._step_history(self._undo, self._redo)

    def redo_flags(self):
        return self._step_history(self._redo, self._undo)

    def last_change(self):
        """What undo would undo, "flag", "rating" or "keywords", or None."""
        return self._undo[-1][0] if self._undo else None

    def _step_history(self, source, target):
        if not source:
            return 0
        field, values = source.pop()
        # From the library: a photo not in the rows still has its value there
        target.append((field, getattr(self.service, _READ[field])(values)))
        self._write(field, values)
        self.flags_changed.emit()
        return len(values)

    def _write(self, field, values):
        getattr(self.service, _WRITE[field])(values)
        for photo_id, value in values.items():
            position = self._positions.get(photo_id)
            if position is not None:
                self._all[position] = replace(self._all[position], **{field: value})
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

    def flags(self, index):
        flags = super().flags(index)
        return flags | Qt.ItemFlag.ItemIsDragEnabled if index.isValid() else flags

    def mimeTypes(self):
        return [PHOTOS_MIME]

    def mimeData(self, indexes):
        """The ids of the photos dragged, for dropping on a collection."""
        data = QMimeData()
        ids = [self._rows[index.row()].id for index in indexes if index.isValid()]
        data.setData(PHOTOS_MIME, json.dumps(ids).encode())
        return data

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction

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
        if role == RATING_ROLE:
            return photo.rating
        if role == KEYWORDS_ROLE:
            return self.keywords(index.row())
        if role == DAMAGE_ROLE:
            return damage_text(photo.damage_warning)
        if role == DIMMED_ROLE:
            return photo.flag == REJECTED and self.rejected != SHOW and self.flag_filter != REJECTED
        path = self.path(index.row())
        if role == Qt.ItemDataRole.DecorationRole:
            return self.thumbnails.icon(self._key(photo), path, photo.content_id, photo.edit)
        if role == Qt.ItemDataRole.ToolTipRole:
            if path is None:
                lines = [photo.path, "On a drive that is not connected"]
            else:
                lines = [path, self.thumbnails.error(self._key(photo))]
            lines.append(damage_text(photo.damage_warning))
            return "\n".join(line for line in lines if line)
        if role == PATH_ROLE:
            return path
        return None

    def keep_thumbnails(self, first, last):
        """Stop waiting for the thumbnails of the rows outside `first` to `last`."""
        first, last = max(0, first), min(len(self._rows) - 1, last)
        self.thumbnails.keep_only({self._key(photo) for photo in self._rows[first : last + 1]})

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


# More runs of new rows than this are shown by starting over: every run is a pass over
# the rows, on the main thread
_MOST_INSERTS = 30


def _steps(old, new):
    """How to get from the ids `old` to `new` by removing and inserting rows, when the
    ones they share are in the same order: ([(first, last)] of the rows of `old` to
    remove, [(first, last)] of the rows of `new` to insert), else None. Also None when
    there are too many insertions for one pass."""
    old_set, new_set = set(old), set(new)
    if [i for i in old if i in new_set] != [i for i in new if i in old_set]:
        return None
    added = _runs([n for n, i in enumerate(new) if i not in old_set])
    if len(added) > _MOST_INSERTS:
        return None
    return _runs([n for n, i in enumerate(old) if i not in new_set]), added


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


def damage_text(warning):
    """What the grid's tooltip and the status bar say of exiftool's `warning` that the
    raw data may be damaged; None without one."""
    if not warning:
        return None
    return (
        f"The raw data may be damaged (exiftool: {warning}). The thumbnail is the "
        "camera's JPEG preview, which is stored apart; copy the file from the card again."
    )
