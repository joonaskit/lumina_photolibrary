"""importing: the part of `MainWindow` for it (see main_window.py)."""

import os

from PySide6.QtWidgets import QMessageBox

from lumina.ui.import_dialog import ImportDialog


class ImportingMixin:
    # Importing from a card

    def import_photos(self, card=None):
        """Ask what to import from a card (`card`, or the first one connected) and where
        to, then queue the import. A folder chosen that is not in the library is added."""
        if self.importer is None:
            return
        dialog = ImportDialog(self.importer, self.service.folders(), card, self)
        accepted = dialog.exec() == ImportDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted:
            return
        destination = dialog.destination()
        folder = self.service.folder_holding(destination)
        if folder is None:
            folder = self.add_folder(destination)
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
            QMessageBox.warning(self, "Cannot Import", str(error))
            return
        self.sidebar.select(folder)

    def _on_card_arrived(self, path):
        name = os.path.basename(path.rstrip("/")) or path
        self.statusBar().showMessage(f"Memory card “{name}” connected: Ctrl+I to import", 15000)
