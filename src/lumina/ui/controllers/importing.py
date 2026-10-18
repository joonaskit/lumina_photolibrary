"""Importing from a memory card into a folder of the library."""

import os

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from lumina.ui.import_dialog import ImportDialog


class Importing(QObject):
    def __init__(self, service, importer, browser, sidebar, messages, parent=None):
        """`importer` is the ImportService, or None; `browser` the Browser and `messages`
        the window's status bar. The parent is the window, which the dialogs are over."""
        super().__init__(parent)
        self.service = service
        self.importer = importer
        self.browser = browser
        self.sidebar = sidebar
        self.messages = messages
        if importer is not None:
            importer.card_arrived.connect(self._on_card_arrived)

    def import_photos(self, card=None):
        """Ask what to import from a card (`card`, or the first one connected) and where
        to, then queue the import. A folder chosen that is not in the library is added."""
        if self.importer is None:
            return
        dialog = ImportDialog(self.importer, self.service.folders(), card, self.parent())
        accepted = dialog.exec() == ImportDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted:
            return
        destination = dialog.destination()
        folder = self.service.folder_holding(destination)
        if folder is None:
            folder = self.browser.add_folder(destination)
            if folder is None:
                return
        try:
            self.importer.import_photos(
                dialog.chosen(),
                destination,
                dialog.layout_name(),
                dialog.naming(),
                dialog.backup(),
            )
        except ValueError as error:
            QMessageBox.warning(self.parent(), "Cannot Import", str(error))
            return
        self.sidebar.select(folder)

    def _on_card_arrived(self, path):
        name = os.path.basename(path.rstrip("/")) or path
        self.messages.showMessage(f"Memory card “{name}” connected: Ctrl+I to import", 15000)
