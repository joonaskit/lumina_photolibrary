"""Culling: thumbs up and down, stars, and undoing them, for the photo shown or the ones
chosen in the grid."""

from PySide6.QtCore import QObject

from lumina.qt_models.library_model import PICKED, REJECTED
from lumina.ui import settings, shortcuts
from lumina.ui.window_common import _GRID, _NO_PHOTO, _VIEWER


class Culling(QObject):
    def __init__(self, nav, model, actions, rating_bar, messages, parent=None):
        """`actions` has the window's pick, reject, unflag, rate (a list, 0 to 5 stars),
        undo_flag and redo_flag actions; `rating_bar` is the toolbar's StarRating and
        `messages` the window's status bar."""
        super().__init__(parent)
        self.nav = nav
        self.model = model
        self.actions = actions
        self.rating_bar = rating_bar
        self.messages = messages
        nav.chosen_changed.connect(self.update_actions)

    def _changed(self):
        self.nav.chosen_changed.emit()
        self.nav.status_changed.emit()

    def flag_photos(self, flag):
        """Thumbs up (PICKED), down (REJECTED) or neither for the photo shown, or the
        ones chosen in the grid; when they have it already, clear it."""
        page = self.nav.page()
        rows = self.nav.chosen_rows()
        given = self.model.toggle_flag(rows, flag) if rows else None
        if given in (PICKED, REJECTED) and page == _VIEWER and settings.advance_after_flag():
            self.nav.step(1)
        self._changed()

    def rate_photos(self, stars):
        """`stars` (0 for none) for the photo shown, open or the ones chosen in the grid;
        when they have that many already, clear it."""
        page = self.nav.page()
        rows = self.nav.chosen_rows()
        given = self.model.toggle_rating(rows, stars) if rows else None
        if given and page == _VIEWER and settings.advance_after_flag():
            self.nav.step(1)
        self._changed()

    def undo_flags(self):
        what = {"rating": "Rating", "keywords": "Keywords"}.get(self.model.last_change(), "Flag")
        if self.model.undo_flags():
            self.messages.showMessage(f"{what} change undone", 3000)
        self._changed()

    def redo_flags(self):
        if self.model.redo_flags():
            self.messages.showMessage("Change redone", 3000)
        self._changed()

    def update_actions(self, *_args):
        actions = self.actions
        rows = self.nav.chosen_rows()
        given = {self.model.flag(row) for row in rows}
        for action, flag in ((actions.pick, PICKED), (actions.reject, REJECTED)):
            shortcuts.enable(action, bool(rows), _NO_PHOTO)
            action.setChecked(given == {flag})
        shortcuts.enable(actions.unflag, bool(rows), _NO_PHOTO)
        given_stars = {self.model.rating(row) for row in rows}
        for action in actions.rate:
            shortcuts.enable(action, bool(rows), _NO_PHOTO)
        for number, action in enumerate(actions.rate[1:], 1):
            action.setCheckable(True)
            action.setChecked(given_stars == {number})
        # Photos that differ show no stars, as neither flag button shows as set
        self.rating_bar.setEnabled(bool(rows))
        self.rating_bar.set_rating(next(iter(given_stars)) if len(given_stars) == 1 else 0)
        browsing = self.nav.page() in (_GRID, _VIEWER)
        shortcuts.enable(
            actions.undo_flag,
            browsing and self.model.can_undo_flags(),
            "Nothing to undo" if browsing else "In the grid and the viewer",
        )
        shortcuts.enable(
            actions.redo_flag,
            browsing and self.model.can_redo_flags(),
            "Nothing to redo" if browsing else "In the grid and the viewer",
        )
