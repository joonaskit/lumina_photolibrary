"""flagging: the part of `MainWindow` for it (see main_window.py)."""

from PySide6.QtCore import QTimer

from lumina.qt_models.library_model import PICKED, REJECTED
from lumina.ui import settings, shortcuts
from lumina.ui.window_common import _DEVELOP, _GRID, _NO_PHOTO, _VIEWER


class FlaggingMixin:
    # Flags

    def flag_photos(self, flag):
        """Thumbs up (PICKED), down (REJECTED) or neither for the photo shown, or the
        ones chosen in the grid; when they have it already, clear it."""
        page = self.pages.currentIndex()
        rows = self._flag_rows()
        given = self.model.toggle_flag(rows, flag) if rows else None
        if given in (PICKED, REJECTED) and page == _VIEWER and settings.advance_after_flag():
            self.step(1)
        self._update_flag_actions()
        self._update_status()

    def rate_photos(self, stars):
        """`stars` (0 for none) for the photo shown, open or the ones chosen in the grid;
        when they have that many already, clear it."""
        page = self.pages.currentIndex()
        rows = self._flag_rows()
        given = self.model.toggle_rating(rows, stars) if rows else None
        if given and page == _VIEWER and settings.advance_after_flag():
            self.step(1)
        self._update_flag_actions()
        self._update_status()

    def undo_flags(self):
        what = {"rating": "Rating", "keywords": "Keywords"}.get(self.model.last_change(), "Flag")
        if self.model.undo_flags():
            self.statusBar().showMessage(f"{what} change undone", 3000)
        self._update_flag_actions()
        self._update_status()

    def redo_flags(self):
        if self.model.redo_flags():
            self.statusBar().showMessage("Change redone", 3000)
        self._update_flag_actions()
        self._update_status()

    def set_flag_filter(self, flag):
        """Show only photos with a thumbs up, with none, or with a thumbs down, or all."""
        self.model.set_flag_filter(flag)
        self._update_status()

    def set_rating_filter(self, stars):
        """Show only photos with `stars` or more, only those without, or all."""
        self.model.set_rating_filter(stars)
        self._update_status()

    def set_edit_filter(self, which):
        """Show only edited photos, only unedited ones, or all."""
        self.model.set_edit_filter(which)
        self._update_status()

    def set_rejected(self, mode):
        """Show, dim or hide thumbs-down photos, and remember it."""
        settings.set_rejected_photos(mode)
        self.model.set_rejected(mode)
        self.grid.viewport().update()
        self._update_status()

    def _flag_rows(self):
        page = self.pages.currentIndex()
        if page in (_VIEWER, _DEVELOP):
            row = self._viewed_row()
            return [row] if row is not None else []
        if page == _GRID:
            rows = sorted(index.row() for index in self.grid.selectionModel().selectedIndexes())
            if not rows and self.grid.currentIndex().isValid():
                rows = [self.grid.currentIndex().row()]
            return rows
        return []

    def _update_flag_actions(self, *_args):
        rows = self._flag_rows()
        given = {self.model.flag(row) for row in rows}
        for action, flag in ((self.pick_action, PICKED), (self.reject_action, REJECTED)):
            shortcuts.enable(action, bool(rows), _NO_PHOTO)
            action.setChecked(given == {flag})
        shortcuts.enable(self.unflag_action, bool(rows), _NO_PHOTO)
        given_stars = {self.model.rating(row) for row in rows}
        for action in self.rate_actions:
            shortcuts.enable(action, bool(rows), _NO_PHOTO)
        for number, action in enumerate(self.rate_actions[1:], 1):
            action.setCheckable(True)
            action.setChecked(given_stars == {number})
        # Photos that differ show no stars, as neither flag button shows as set
        self.rating_bar.setEnabled(bool(rows))
        self.rating_bar.set_rating(next(iter(given_stars)) if len(given_stars) == 1 else 0)
        self._update_copy_actions()
        self._update_stack_actions()
        page = self.pages.currentIndex()
        browsing = page in (_GRID, _VIEWER)
        shortcuts.enable(
            self.undo_flag_action,
            browsing and self.model.can_undo_flags(),
            "Nothing to undo" if browsing else "In the grid and the viewer",
        )
        shortcuts.enable(
            self.redo_flag_action,
            browsing and self.model.can_redo_flags(),
            "Nothing to redo" if browsing else "In the grid and the viewer",
        )
        self._update_settings_actions()

    def _on_current_changed(self, current, _previous):
        self._update_flag_actions()
        if current.isValid():
            photo_id = self.model.photo_id(current.row())
            # Not while the selection model is telling of the change
            QTimer.singleShot(0, self, lambda: self.model.settle(photo_id))
