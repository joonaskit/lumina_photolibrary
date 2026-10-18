"""The Develop view's page: the edit open in it, its Undo and Redo, and exporting it."""

import os

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from lumina.ui import shortcuts
from lumina.ui.export_dialog import ExportDialog


class DevelopPage(QObject):
    def __init__(self, view, session, undo_action, redo_action, messages, parent=None):
        """`view` is the DevelopView, `session` its DevelopSession; `messages` is the
        window's status bar. The parent is the window, which the dialogs are over."""
        super().__init__(parent)
        self.view = view
        self.session = session
        self.undo_action = undo_action
        self.redo_action = redo_action
        self.messages = messages
        self._export_options = None
        session.history_changed.connect(self.update_history)
        session.exported.connect(self._on_exported)
        session.export_exists.connect(self._on_export_exists)

    def load(self, title, side, fresh):
        """Get ready to open a photo called `title`, rendered for a screen side of `side`;
        `fresh` coming in from another page, which starts fitted."""
        if fresh:
            self.view.image.reset_zoom()
        self.view.show_loading(title)
        self.session.screen_side = side

    def open(self, photo_id, path, copy):
        self.session.open(photo_id, path, copy)
        self.view.setFocus()

    def close(self):
        self.session.close()

    def step_back(self):
        """Escape inside the view: cancel the crop or picking, or zoom back to fitted.
        Whether it did one."""
        if self.view.is_cropping():
            self.view.cancel_crop()
        elif self.view.is_picking():
            self.view.cancel_picking()
        elif self.view.image.is_zoomed():
            self.view.image.reset_zoom()
        else:
            return False
        return True

    def update_history(self, *_args):
        shortcuts.enable(self.undo_action, self.session.can_undo(), "Nothing to undo")
        shortcuts.enable(self.redo_action, self.session.can_redo(), "Nothing to redo")

    # Exporting the photo open

    def export_photo(self):
        if not self.session.is_open():
            return
        dialog = ExportDialog(self.session.path, self.parent(), self.session.copy)
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return
        destination, options = dialog.destination(), dialog.options()
        self._export_options = options
        replace = False
        if os.path.lexists(destination):
            if not self._ask_replace(destination):
                return
            replace = True
        self.messages.showMessage(f"Exporting {os.path.basename(destination)}…")
        self.session.export(destination, options, replace)

    def _ask_replace(self, destination):
        answer = QMessageBox.question(
            self.parent(),
            "Replace File?",
            f"“{os.path.basename(destination)}” already exists in "
            f"{os.path.dirname(destination)}.\n\nReplace it with this export?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_export_exists(self, destination):
        # Taken while the export was being written: ask, as before
        self.messages.clearMessage()
        if self._ask_replace(destination):
            self.session.export(destination, self._export_options, True)

    def _on_exported(self, destination, error):
        self.messages.clearMessage()
        if error is None:
            self.messages.showMessage(f"Exported to {destination}", 8000)
        else:
            QMessageBox.warning(self.parent(), "Export Failed", f"{destination}\n\n{error}")
