"""merging: the part of `MainWindow` for it (see main_window.py)."""

import os

from PySide6.QtWidgets import QMessageBox

from lumina.qt_models.library import NewerParams
from lumina.ui.merge_dialog import MergeDialog, PanoramaDialog
from lumina.ui.preset_dialog import CopyDialog
from lumina.ui.window_common import _DEVELOP, _VIEWER


class MergingMixin:
    # Merging

    def merge_hdr(self):
        """Merge the photos chosen, or the bracket of the one chosen or shown, into one HDR
        photo, after asking how."""
        if self.merge is None:
            return
        ids = [self.model.photo_id(row) for row in self._flag_rows()]
        if not ids:
            return
        ids = self.merge.bracket(ids)
        problem = self.merge.check(ids)
        if problem is not None:
            if len(ids) == 1:
                problem = (
                    f"{self._photo_name(ids[0])} is not in a bracket that Lumina can find. "
                    "Choose the photos to merge in the grid."
                )
            QMessageBox.information(self, "Merge to HDR", problem)
            return
        dialog = MergeDialog(self.merge.frames(ids), parent=self)
        if dialog.exec() != MergeDialog.DialogCode.Accepted:
            return
        try:
            self.merge.merge_hdr(ids, dialog.deghost(), dialog.align())
        except ValueError as error:
            QMessageBox.information(self, "Merge to HDR", str(error))

    def stitch_panorama(self):
        """Stitch the photos chosen into a panorama, after asking how."""
        if self.merge is None:
            return
        ids = [self.model.photo_id(row) for row in self._flag_rows()]
        if not ids:
            return
        problem = self.merge.check_panorama(ids)
        if problem is not None:
            if len(ids) == 1:
                problem = "Choose the photos to stitch in the grid: two or more that overlap."
            QMessageBox.information(self, "Stitch Panorama", problem)
            return
        dialog = PanoramaDialog(
            self.merge.panorama_frames(ids), self.merge.preview_panorama(ids), parent=self
        )
        if dialog.exec() != PanoramaDialog.DialogCode.Accepted:
            return
        try:
            self.merge.stitch_panorama(ids, dialog.projection(), dialog.crop())
        except ValueError as error:
            QMessageBox.information(self, "Stitch Panorama", str(error))

    def _photo_name(self, photo_id):
        photo = self.service.photo_details(photo_id)
        return os.path.basename(photo.path) if photo else str(photo_id)

    def copy_settings(self):
        """Keep some sections of the edit of the photo shown, open or current, to paste
        over other photos."""
        photo_id = self._settings_source()
        if photo_id is None:
            return
        dialog = CopyDialog(parent=self)
        if dialog.exec() != CopyDialog.DialogCode.Accepted:
            return
        sections, name = dialog.sections(), self._photo_name(photo_id)
        try:
            if self.pages.currentIndex() == _DEVELOP:
                # The edit as it is now, saved or not
                document = self.session.settings_document(sections)
            else:
                document = self.service.settings_document(photo_id, sections)
        except NewerParams as error:
            QMessageBox.warning(
                self, "Copy Settings", f"The edit of {name} was made by a newer Lumina ({error})."
            )
            return
        self.service.copy_settings(document, name, sections)
        self.statusBar().showMessage(f"Copied settings of {name}", 5000)

    def paste_settings(self):
        """Put the copied settings over the edit of the photo open, shown, or the photos
        chosen in the grid."""
        copied = self.service.copied
        if copied is None:
            return
        if self.pages.currentIndex() == _DEVELOP:
            self.session.paste_settings(copied.document)
            self.statusBar().showMessage(f"Pasted settings of {copied.name}", 5000)
            return
        ids = [self.model.photo_id(row) for row in self._flag_rows()]
        if not ids:
            return
        if len(ids) > 1:
            answer = QMessageBox.question(
                self,
                "Paste Settings",
                f"Paste the settings of {copied.name} to {len(ids)} photos?\n\n"
                "They replace the same parts of those photos' edits; the rest of each "
                "edit stays as it is.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.service.paste_settings(ids, on_done=lambda result: self._pasted(result, copied.name))

    def sync_settings(self):
        """Copy sections of the edit of the current photo to the others chosen."""
        rows = self._sync_rows()
        if not rows:
            return
        source = self.model.photo_id(self.grid.currentIndex().row())
        name = self._photo_name(source)
        dialog = CopyDialog(
            "Sync Settings",
            f"Copy these parts of the edit of {name} to the {len(rows)} other "
            f"photo{'' if len(rows) == 1 else 's'} chosen:",
            "Sync",
            parent=self,
        )
        if dialog.exec() != CopyDialog.DialogCode.Accepted:
            return
        try:
            document = self.service.settings_document(source, dialog.sections())
        except NewerParams as error:
            QMessageBox.warning(
                self, "Sync Settings", f"The edit of {name} was made by a newer Lumina ({error})."
            )
            return
        ids = [self.model.photo_id(row) for row in rows]
        self.service.paste_settings(
            ids, document, on_done=lambda result: self._pasted(result, name)
        )

    def _pasted(self, result, source_name):
        """Tell of a paste that went to photos in the library."""
        if self.pages.currentIndex() == _VIEWER and self._viewed_row() is not None:
            # The photo shown has a new edit
            self.show_photo(self._viewed_row())
        photos = f"{result.done} photo{'' if result.done == 1 else 's'}"
        self.statusBar().showMessage(f"Settings of {source_name} put on {photos}", 6000)
        if result.failed:
            lines = "\n".join(f"{name}: {why}" for name, why in result.failed)
            QMessageBox.warning(
                self, "Paste Settings", f"These photos were left as they were:\n\n{lines}"
            )
