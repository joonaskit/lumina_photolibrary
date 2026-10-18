"""Virtual copies: other edits of one photo, made, deleted and stepped through."""

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from lumina.ui import shortcuts
from lumina.ui.window_common import _DEVELOP, _GRID, _NO_PHOTO, _VIEWER


class VirtualCopies(QObject):
    def __init__(self, nav, model, service, session, actions, messages, parent=None):
        """`session` is the DevelopSession; `actions` has the window's make, delete,
        previous and next copy actions; `messages` is the window's status bar. The parent
        is the window, which the dialogs are over."""
        super().__init__(parent)
        self.nav = nav
        self.model = model
        self.service = service
        self.session = session
        self.actions = actions
        self.messages = messages
        nav.chosen_changed.connect(self.update_actions)
        model.refreshed.connect(self.update_actions)

    def update_actions(self, *_args):
        actions = self.actions
        page = self.nav.page()
        viewing = page in (_VIEWER, _DEVELOP)
        shortcuts.enable(actions.make, bool(self.nav.chosen_rows()), _NO_PHOTO)
        shortcuts.enable(
            actions.delete, self.copy_to_delete() is not None, "No virtual copy chosen"
        )
        several = (
            viewing
            and self.nav.viewed_id is not None
            and bool(self.service.copies(self.nav.viewed_id))
        )
        why = "The photo has no virtual copies" if viewing else "With a photo open"
        shortcuts.enable(actions.previous, several, why)
        shortcuts.enable(actions.next, several, why)

    def copy_to_delete(self):
        """(photo id, copy) of the virtual copy the viewer or the Develop view shows, or of
        the one cell chosen in the grid when it is a copy's; else None."""
        page = self.nav.page()
        if page in (_VIEWER, _DEVELOP):
            if self.nav.viewed_id is not None and self.nav.viewed_copy > 0:
                return self.nav.viewed_id, self.nav.viewed_copy
            return None
        rows = self.nav.chosen_rows() if page == _GRID else []
        if len(rows) == 1 and self.model.copy(rows[0]) > 0:
            return self.model.photo_id(rows[0]), self.model.copy(rows[0])
        return None

    def make_virtual_copy(self):
        """A new virtual copy of the photo shown, from the edit as it is, and shown. In
        the grid one of each photo chosen, from its own edit, and nothing else changes."""
        nav = self.nav
        page = nav.page()
        if page == _GRID:
            ids = [self.model.photo_id(row) for row in nav.chosen_rows()]
            if ids:
                self.service.make_copies(ids)
                what = "a photo" if len(ids) == 1 else f"{len(ids)} photos"
                self.messages.showMessage(f"Made a virtual copy of {what}", 4000)
            return
        if page not in (_VIEWER, _DEVELOP) or nav.viewed_id is None:
            return
        photo_id = nav.viewed_id

        def made(number):
            # Shown when the photo it is a copy of is still the one shown
            if nav.viewed_id == photo_id:
                nav.show_edit(number)

        if page == _DEVELOP and self.session.is_open():
            self.service.make_copy(
                photo_id, params_json=self.session.params.to_json(), on_done=made
            )
        else:
            self.service.make_copy(photo_id, nav.viewed_copy, on_done=made)

    def delete_virtual_copy(self):
        """Delete the virtual copy shown or chosen, after asking. In the viewer or Develop
        view the one before it is shown after."""
        target = self.copy_to_delete()
        if target is None:
            return
        photo_id, copy = target
        answer = QMessageBox.question(
            self.parent(),
            "Delete Virtual Copy",
            f"Delete copy {copy} of this photo and its edit? "
            "The photo and its other edits are not touched.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.nav.page() == _GRID:
            self.service.delete_copy(photo_id, copy)
            return
        # Closed first, so that nothing is saved over the copy once it is gone
        if self.nav.page() == _DEVELOP:
            self.nav.close_edit()
        numbers = [0, *self.service.copies(photo_id)]
        before = numbers[max(0, numbers.index(copy) - 1)]
        self.service.delete_copy(photo_id, copy)
        self.nav.show_edit(before)

    def step_copy(self, delta):
        """The next or previous edit of the photo shown: its own, then its copies."""
        nav = self.nav
        if nav.viewed_id is None:
            return
        numbers = [0, *self.service.copies(nav.viewed_id)]
        if nav.viewed_copy not in numbers:
            return
        place = numbers.index(nav.viewed_copy) + delta
        if 0 <= place < len(numbers):
            nav.show_edit(numbers[place])
