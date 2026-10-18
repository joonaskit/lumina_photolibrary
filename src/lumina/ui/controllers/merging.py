"""Merges: a bracket of exposures into one HDR photo, overlapping photos into a panorama."""

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from lumina.ui.merge_dialog import MergeDialog, PanoramaDialog
from lumina.ui.window_common import photo_name


class Merging(QObject):
    def __init__(self, nav, model, service, merge, parent=None):
        """`merge` is the MergeService, or None. The parent is the window, which the
        dialogs are over."""
        super().__init__(parent)
        self.nav = nav
        self.model = model
        self.service = service
        self.merge = merge

    def _chosen_ids(self):
        return [self.model.photo_id(row) for row in self.nav.chosen_rows()]

    def merge_hdr(self):
        """Merge the photos chosen, or the bracket of the one chosen or shown, into one HDR
        photo, after asking how."""
        if self.merge is None:
            return
        ids = self._chosen_ids()
        if not ids:
            return
        ids = self.merge.bracket(ids)
        problem = self.merge.check(ids)
        if problem is not None:
            if len(ids) == 1:
                problem = (
                    f"{photo_name(self.service, ids[0])} is not in a bracket that Lumina can "
                    "find. Choose the photos to merge in the grid."
                )
            QMessageBox.information(self.parent(), "Merge to HDR", problem)
            return
        dialog = MergeDialog(self.merge.frames(ids), parent=self.parent())
        if dialog.exec() != MergeDialog.DialogCode.Accepted:
            return
        try:
            self.merge.merge_hdr(ids, dialog.deghost(), dialog.align())
        except ValueError as error:
            QMessageBox.information(self.parent(), "Merge to HDR", str(error))

    def stitch_panorama(self):
        """Stitch the photos chosen into a panorama, after asking how."""
        if self.merge is None:
            return
        ids = self._chosen_ids()
        if not ids:
            return
        problem = self.merge.check_panorama(ids)
        if problem is not None:
            if len(ids) == 1:
                problem = "Choose the photos to stitch in the grid: two or more that overlap."
            QMessageBox.information(self.parent(), "Stitch Panorama", problem)
            return
        dialog = PanoramaDialog(
            self.merge.panorama_frames(ids), self.merge.preview_panorama(ids), parent=self.parent()
        )
        if dialog.exec() != PanoramaDialog.DialogCode.Accepted:
            return
        try:
            self.merge.stitch_panorama(ids, dialog.projection(), dialog.crop())
        except ValueError as error:
            QMessageBox.information(self.parent(), "Stitch Panorama", str(error))
