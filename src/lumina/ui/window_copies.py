"""copies: the part of `MainWindow` for it (see main_window.py)."""

import os

from PySide6.QtWidgets import QMessageBox

from lumina.ui import shortcuts
from lumina.ui.export_dialog import ExportDialog
from lumina.ui.window_common import _DEVELOP, _GRID, _LIVE, _NO_PHOTO, _VIEWER, VIEWER_MAX_SIDE


class CopiesMixin:
    # Virtual copies

    def _existing_copy(self, copy):
        """`copy` if the viewed photo has it, else its own edit (0)."""
        if copy and copy in self.service.copies(self._viewed_id):
            return copy
        return 0

    def _viewed_title(self, row):
        name = self.model.name(row)
        return f"{name} (copy {self._viewed_copy})" if self._viewed_copy else name

    def _update_copy_actions(self, *_args):
        page = self.pages.currentIndex()
        viewing = page in (_VIEWER, _DEVELOP)
        shortcuts.enable(self.make_copy_action, bool(self._flag_rows()), _NO_PHOTO)
        shortcuts.enable(
            self.delete_copy_action, self._copy_to_delete() is not None, "No virtual copy chosen"
        )
        several = viewing and self._viewed_id is not None and bool(self._others())
        why = "The photo has no virtual copies" if viewing else "With a photo open"
        shortcuts.enable(self.previous_copy_action, several, why)
        shortcuts.enable(self.next_copy_action, several, why)

    def _others(self):
        return self.service.copies(self._viewed_id)

    def _show_edit(self, copy):
        """The viewer or the Develop view on the same photo, as edit `copy` of it."""
        row = self._viewed_row()
        if row is None:
            return
        if self.pages.currentIndex() == _DEVELOP:
            self.show_develop(row, copy)
        else:
            self.show_photo(row, copy)

    def _copy_to_delete(self):
        """(photo id, copy) of the virtual copy the viewer or the Develop view shows, or of
        the one cell chosen in the grid when it is a copy's; else None."""
        page = self.pages.currentIndex()
        if page in (_VIEWER, _DEVELOP):
            if self._viewed_id is not None and self._viewed_copy > 0:
                return self._viewed_id, self._viewed_copy
            return None
        rows = self._flag_rows() if page == _GRID else []
        if len(rows) == 1 and self.model.copy(rows[0]) > 0:
            return self.model.photo_id(rows[0]), self.model.copy(rows[0])
        return None

    def make_virtual_copy(self):
        """A new virtual copy of the photo shown, from the edit as it is, and shown. In
        the grid one of each photo chosen, from its own edit, and nothing else changes."""
        page = self.pages.currentIndex()
        if page == _GRID:
            ids = [self.model.photo_id(row) for row in self._flag_rows()]
            if ids:
                self.service.make_copies(ids)
                what = "a photo" if len(ids) == 1 else f"{len(ids)} photos"
                self.statusBar().showMessage(f"Made a virtual copy of {what}", 4000)
            return
        if page not in (_VIEWER, _DEVELOP) or self._viewed_id is None:
            return
        if page == _DEVELOP and self.session.is_open():
            number = self.service.make_copy(
                self._viewed_id, params_json=self.session.params.to_json()
            )
        else:
            number = self.service.make_copy(self._viewed_id, self._viewed_copy)
        self._show_edit(number)

    def delete_virtual_copy(self):
        """Delete the virtual copy shown or chosen, after asking. In the viewer or Develop
        view the one before it is shown after."""
        target = self._copy_to_delete()
        if target is None:
            return
        photo_id, copy = target
        answer = QMessageBox.question(
            self,
            "Delete Virtual Copy",
            f"Delete copy {copy} of this photo and its edit? "
            "The photo and its other edits are not touched.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.pages.currentIndex() == _GRID:
            self.service.delete_copy(photo_id, copy)
            return
        # Closed first, so that nothing is saved over the copy once it is gone
        if self.pages.currentIndex() == _DEVELOP:
            self.session.close()
        numbers = [0, *self.service.copies(photo_id)]
        before = numbers[max(0, numbers.index(copy) - 1)]
        self.service.delete_copy(photo_id, copy)
        self._show_edit(before)

    def step_copy(self, delta):
        """The next or previous edit of the photo shown: its own, then its copies."""
        if self._viewed_id is None:
            return
        numbers = [0, *self.service.copies(self._viewed_id)]
        if self._viewed_copy not in numbers:
            return
        place = numbers.index(self._viewed_copy) + delta
        if 0 <= place < len(numbers):
            self._show_edit(numbers[place])

    def _update_history(self):
        shortcuts.enable(self.undo_action, self.session.can_undo(), "Nothing to undo")
        shortcuts.enable(self.redo_action, self.session.can_redo(), "Nothing to redo")

    def export_photo(self):
        if not self.session.is_open():
            return
        dialog = ExportDialog(self.session.path, self, self.session.copy)
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return
        destination, options = dialog.destination(), dialog.options()
        self._export_options = options
        replace = False
        if os.path.lexists(destination):
            if not self._ask_replace(destination):
                return
            replace = True
        self.statusBar().showMessage(f"Exporting {os.path.basename(destination)}…")
        self.session.export(destination, options, replace)

    def _ask_replace(self, destination):
        answer = QMessageBox.question(
            self,
            "Replace File?",
            f"“{os.path.basename(destination)}” already exists in "
            f"{os.path.dirname(destination)}.\n\nReplace it with this export?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_export_exists(self, destination):
        # Taken while the export was being written: ask, as before
        self.statusBar().clearMessage()
        if self._ask_replace(destination):
            self.session.export(destination, self._export_options, True)

    def _on_exported(self, destination, error):
        self.statusBar().clearMessage()
        if error is None:
            self.statusBar().showMessage(f"Exported to {destination}", 8000)
        else:
            QMessageBox.warning(self, "Export Failed", f"{destination}\n\n{error}")

    def _viewed_row(self):
        return self.model.row_of(self._viewed_id) if self._viewed_id is not None else None

    def _viewed_path(self):
        row = self._viewed_row()
        return self.model.path(row) if row is not None else None

    def _viewer_side(self):
        screen = self.screen()
        size = screen.size() * screen.devicePixelRatio()
        return min(VIEWER_MAX_SIDE, max(size.width(), size.height()))

    def _on_preview_ready(self, path, image, full):
        if self._viewed_path() == path:
            self.viewer.set_image(image, full)
            self._on_zoom_changed()

    def _on_preview_failed(self, path, message):
        if self._viewed_path() == path:
            self.viewer.set_message(f"Cannot show {os.path.basename(path)}\n{message}")

    def _show_page(self, page):
        if self.pages.currentIndex() == _LIVE and page != _LIVE and self.camera is not None:
            # Live view runs only while it is shown
            self.camera.set_live(False)
        self.pages.setCurrentIndex(page)
        self.camera_bar.set_live_shown(page == _LIVE)
        viewing = page in (_VIEWER, _DEVELOP)
        developing = page == _DEVELOP
        self.navigation.setVisible(viewing)
        self.editing.setVisible(developing)
        self.marking.setVisible(page in (_GRID, _VIEWER, _DEVELOP))
        self.search_field.setVisible(self.search is not None and page in (_GRID, _VIEWER, _DEVELOP))
        self.grid_view_options.setVisible(page == _GRID)
        for action in (self.back_action, self.previous_action, self.next_action):
            shortcuts.enable(action, viewing, "With a photo open")
        self.back_action.setEnabled(viewing or page == _LIVE)
        for action in (self.reset_action, self.export_action):
            shortcuts.enable(action, developing, "In the Develop view")
        shortcuts.enable(self.edit_action, page in (_GRID, _VIEWER), "In the grid and the viewer")
        self._update_history()
        self._update_flag_actions()
        if not viewing:
            self._update_title()
        self._update_status()
