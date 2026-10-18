"""Which page shows and which photo is looked at: the grid, the viewer, the Develop view
and the camera's live view, and the photos the actions are for."""

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox

from lumina.ui.window_common import _DEVELOP, _EMPTY, _GRID, _VIEWER


class Navigator(QObject):
    # The page shown, after it changed
    page_changed = Signal(int)
    # The photo looked at, or which of its edits, after it changed
    viewed_changed = Signal()
    # The photos the actions are for, or what they have (flags, stars, keywords): what the
    # actions can do may have changed. Parts that change what the photos have emit it
    chosen_changed = Signal()
    # What the status bar tells of may have changed. Parts that change what the grid shows,
    # or what the photos have, emit it
    status_changed = Signal()

    def __init__(self, pages, grid, model, service, viewer, develop, parent=None):
        """`pages` is the window's stack of pages, `grid` the grid's list view; `viewer`
        and `develop` are the ViewerPage and the DevelopPage."""
        super().__init__(parent)
        self.pages = pages
        self.grid = grid
        self.model = model
        self.service = service
        self.viewer = viewer
        self.develop = develop
        self.viewed_id = None
        # Which of its edits the viewer or the Develop view shows: 0 is the photo's own,
        # more are virtual copies
        self.viewed_copy = 0
        # Where the Develop view was opened from, to go back to
        self.develop_from = _GRID
        # What Escape does on a page some other part has (the live view's)
        self._back = {}
        grid.selectionModel().selectionChanged.connect(self.chosen_changed)
        grid.selectionModel().currentChanged.connect(self._on_current_changed)
        model.flags_changed.connect(self.chosen_changed)
        self.page_changed.connect(self.chosen_changed)
        self.page_changed.connect(self.status_changed)
        self.viewed_changed.connect(self.status_changed)

    def page(self):
        return self.pages.currentIndex()

    def show_page(self, page):
        self.pages.setCurrentIndex(page)
        self.page_changed.emit(page)

    def set_back(self, page, handler):
        """Escape on `page` calls `handler`."""
        self._back[page] = handler

    # The photo looked at

    def viewed_row(self):
        return self.model.row_of(self.viewed_id) if self.viewed_id is not None else None

    def viewed_path(self):
        row = self.viewed_row()
        return self.model.path(row) if row is not None else None

    def viewed_title(self, row):
        name = self.model.name(row)
        return f"{name} (copy {self.viewed_copy})" if self.viewed_copy else name

    def current_row(self):
        """The row of the grid's current photo, or None."""
        index = self.grid.currentIndex()
        return index.row() if index.isValid() else None

    def chosen_rows(self):
        """The rows the actions are for: the photo shown, or the ones chosen in the grid
        (the current one when none is)."""
        page = self.page()
        if page in (_VIEWER, _DEVELOP):
            row = self.viewed_row()
            return [row] if row is not None else []
        if page == _GRID:
            rows = sorted(index.row() for index in self.grid.selectionModel().selectedIndexes())
            if not rows and self.grid.currentIndex().isValid():
                rows = [self.grid.currentIndex().row()]
            return rows
        return []

    def _on_current_changed(self, current, _previous):
        self.chosen_changed.emit()
        if current.isValid():
            photo_id = self.model.photo_id(current.row())
            # Not while the selection model is telling of the change
            QTimer.singleShot(0, self, lambda: self.model.settle(photo_id))

    def _existing_copy(self, copy):
        """`copy` if the viewed photo has it, else its own edit (0)."""
        if copy and copy in self.service.copies(self.viewed_id):
            return copy
        return 0

    def _view(self, row, copy):
        """Look at the photo of `row`, edit `copy` of it; its row now."""
        self.viewed_id = self.model.photo_id(row)
        self.viewed_copy = self._existing_copy(copy)
        # Moved on: photos flagged out of the view leave it
        self.model.settle(self.viewed_id)
        return self.model.row_of(self.viewed_id)

    # The pages

    def show_grid(self):
        self.viewer.cancel()
        self.viewer.view.reset_zoom()
        if not self.service.folders():
            self.show_page(_EMPTY)
            return
        self.show_page(_GRID)
        row = self.viewed_row()
        if row is not None:
            index = self.model.index(row)
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)
        self.viewed_id = None
        self.grid.setFocus()

    def show_photo(self, row, copy=0):
        """The photo of `row`, as its own edit makes it or as virtual copy `copy` does."""
        if not 0 <= row < self.model.rowCount():
            return
        row = self._view(row, copy)
        self.viewer.show(row, self.viewed_id, self.viewed_copy)
        self.show_page(_VIEWER)
        self.viewer.view.setFocus()
        self.viewed_changed.emit()
        self.viewer.load()

    def show_develop(self, row, copy=0):
        """Edit the photo of `row`: its own edit, or virtual copy `copy`."""
        if not 0 <= row < self.model.rowCount():
            return
        path = self.model.path(row)
        if path is None:
            QMessageBox.information(
                self.parent(),
                "Not Connected",
                f"{self.model.name(row)} is on a drive that is not connected. "
                "Connect it to edit the photo.",
            )
            return
        self.viewer.cancel()
        # Stepping keeps the zoom, as in the viewer; coming in from outside starts fitted
        fresh = self.page() != _DEVELOP
        row = self._view(row, copy)
        self.develop.load(self.viewed_title(row), self.viewer.side(), fresh)
        self.show_page(_DEVELOP)
        self.develop.open(self.viewed_id, path, self.viewed_copy)
        self.viewed_changed.emit()

    def show_edit(self, copy):
        """The viewer or the Develop view on the same photo, as edit `copy` of it."""
        row = self.viewed_row()
        if row is None:
            return
        if self.page() == _DEVELOP:
            self.show_develop(row, copy)
        else:
            self.show_photo(row, copy)

    def close_edit(self):
        """Save and close the edit open in the Develop view, before leaving it."""
        self.develop.close()

    def edit_current(self):
        """Edit the photo shown, or the one chosen in the grid."""
        if self.page() == _VIEWER:
            row, copy = self.viewed_row(), self.viewed_copy
        elif self.page() == _GRID and self.current_row() is not None:
            row = self.current_row()
            copy = self.model.copy(row)
        else:
            return
        if row is not None:
            self.develop_from = self.page()
            self.show_develop(row, copy)

    def step(self, delta):
        if self.viewed_row() is None:
            return
        # From the cell of the copy shown (when open), onto the cell next to it
        row = self.model.row_of(self.viewed_id, self.viewed_copy)
        target = row + delta
        if not 0 <= target < self.model.rowCount():
            return
        copy = self.model.copy(target)
        if self.page() == _DEVELOP:
            self.show_develop(target, copy)
        else:
            self.show_photo(target, copy)

    def go_back(self):
        """Escape: cancel the crop, then out of the Develop view; in the viewer back to
        fitted, then to the grid. Out of live view to where it was opened from."""
        page = self.page()
        if page in self._back:
            self._back[page]()
            return
        if page == _VIEWER and self.viewer.view.is_zoomed():
            self.viewer.view.reset_zoom()
            return
        if page == _DEVELOP:
            if self.develop.step_back():
                return
            self.close_edit()
            row = self.viewed_row()
            if self.develop_from == _VIEWER and row is not None:
                self.show_photo(row, self.viewed_copy)
                return
        self.show_grid()

    def zoom_view(self):
        """The image view that zooms: the Develop view's while it shows, else the viewer's."""
        return self.develop.view.image if self.page() == _DEVELOP else self.viewer.view
