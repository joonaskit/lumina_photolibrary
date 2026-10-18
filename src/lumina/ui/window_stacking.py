"""stacking: the part of `MainWindow` for it (see main_window.py)."""

from PySide6.QtWidgets import QMessageBox

from lumina.qt_models.library import SIMILAR_DEFAULT
from lumina.qt_models.library_model import REJECTED, STACK_ROLE
from lumina.ui import shortcuts
from lumina.ui.export_dialog import ExportDialog
from lumina.ui.similar_dialog import SHOWN, SimilarDialog
from lumina.ui.window_common import _GRID


class StackingMixin:
    # Stacks

    def _stack_rows(self):
        """The rows chosen in the grid (stacks are made and taken apart there)."""
        if self.pages.currentIndex() != _GRID:
            return []
        return self._flag_rows()

    def _update_stack_actions(self, *_args):
        rows = self._stack_rows()
        stacked = [row for row in rows if self.model.stack(row) is not None]
        shortcuts.enable(self.stack_action, len(rows) > 1, "Choose two or more photos")
        shortcuts.enable(
            self.export_selected_action,
            self.exporter is not None and bool(rows),
            "Choose photos in the grid",
        )
        shortcuts.enable(
            self.stack_bursts_action,
            self.pages.currentIndex() == _GRID and self.model.rowCount() > 1,
            "In the grid, with more than one photo",
        )
        for action in (self.unstack_action, self.leave_stack_action, self.toggle_stack_action):
            shortcuts.enable(action, bool(stacked), "No photo in a stack chosen")
        shortcuts.enable(
            self.stack_top_action,
            len(rows) == 1 and bool(stacked) and not self.model.is_stack_top(rows[0]),
            "Choose one photo of a stack, not its top",
        )

    def export_selected(self):
        """Export the photos chosen in the grid, each with its edit (a virtual copy's own),
        to a folder: a job, with Cancel in the status bar, that goes on after a restart."""
        rows = self._stack_rows()
        if self.exporter is None or not rows:
            return
        entries = [(self.model.photo_id(row), self.model.copy(row)) for row in rows]
        first = self.model.path(rows[0]) or self.model.name(rows[0])
        dialog = ExportDialog(first, self, count=len(rows))
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return
        try:
            self.exporter.export(
                entries,
                dialog.destination_folder(),
                dialog.options(),
                dialog.replace.isChecked(),
            )
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "Export Failed", str(error))

    def stack_photos(self):
        """Group the photos chosen in one stack, the first shown on top, collapsed."""
        rows = self._stack_rows()
        if len(rows) < 2:
            return
        self.service.make_stack([self.model.photo_id(row) for row in rows])
        self.statusBar().showMessage(f"Stacked {len(rows)} photos", 4000)

    def stack_bursts(self):
        """Stack the bursts among the photos chosen, or among all those shown when fewer
        than two are chosen."""
        rows = self._stack_rows()
        if len(rows) > 1:
            ids = {self.model.photo_id(row) for row in rows}
            photos = [p for p in self.model.photo_rows() if p.id in ids]
        else:
            photos = self.model.photo_rows()
        self.statusBar().showMessage("Looking for bursts…")

        def done(made):
            self.statusBar().showMessage(
                f"Stacked {made} burst{'s' if made != 1 else ''}" if made else "No bursts found",
                4000,
            )

        self.service.stack_bursts(
            photos,
            done,
            lambda error: self.statusBar().showMessage(f"Could not stack bursts: {error}", 8000),
        )

    def find_duplicates(self):
        """Open the list of photos across the library that look alike or are copies."""
        if self.similar_dialog is None:
            # What the grid shows now is what "here" means, whatever is shown later
            self._similar_scope = {p.id for p in self.model.photo_rows()}
            shown = None if self._shows_library() else self.title.text()
            dialog = SimilarDialog(
                lambda photo: self.service.resolve(photo.volume_key, photo.path),
                lambda photo: self.service.edit_json(photo.id, settle=False),
                SIMILAR_DEFAULT,
                shown,
                self,
            )
            dialog.search_requested.connect(self._search_similar)
            dialog.stack_requested.connect(self._stack_similar)
            dialog.reject_requested.connect(self._reject_similar)
            dialog.finished.connect(lambda _result: setattr(self, "similar_dialog", None))
            self.similar_dialog = dialog
        self.similar_dialog.show()
        self.similar_dialog.raise_()
        self._search_similar(self.similar_dialog.similarity(), self.similar_dialog.scope())

    def _shows_library(self):
        """Whether the grid shows every photo, so there is no narrower place to look."""
        return (
            self.sidebar.current_folder() is None
            and self.sidebar.current_collection() is None
            and self.sidebar.current_import() is None
            and self.model.filter == self.model.filter.__class__()
        )

    def _search_similar(self, similarity, scope):
        dialog = self.similar_dialog
        dialog.searching()
        self.service.find_similar(
            similarity,
            self._similar_scope if scope == SHOWN else None,
            lambda groups: self._similar_found(dialog, similarity, groups),
            lambda error: dialog.failed(str(error)),
        )

    def _similar_found(self, dialog, similarity, groups):
        # An answer to a slider position since moved is not the one wanted
        if dialog is self.similar_dialog and similarity == dialog.similarity():
            dialog.set_groups(groups)

    def _stack_similar(self, groups):
        """Stack each group chosen in the dialog (`groups`: lists of ids, best first)."""
        for ids in groups:
            self.service.make_stack(ids)
        self.statusBar().showMessage(
            f"Stacked {len(groups)} group{'s' if len(groups) != 1 else ''}", 4000
        )

    def _reject_similar(self, ids):
        count = self.model.flag_photos(ids, REJECTED)
        self.statusBar().showMessage(
            f"Gave {count} photo{'s' if count != 1 else ''} a thumbs down", 4000
        )

    def unstack_photos(self):
        """Take apart the stacks the photos chosen are in."""
        ids = [self.model.photo_id(row) for row in self._stack_rows()]
        if ids:
            self.service.unstack(ids)

    def leave_stack(self):
        """Take the photos chosen out of their stacks; the rest of each stack stays."""
        ids = [
            self.model.photo_id(row)
            for row in self._stack_rows()
            if self.model.stack(row) is not None
        ]
        if ids:
            self.service.leave_stack(ids)

    def toggle_stack_of(self, row):
        """Open the stack of `row` if it is collapsed, else close it."""
        stack = self.model.stack(row)
        if stack is not None:
            collapsed = self.model.data(self.model.index(row), STACK_ROLE)[2]
            self.service.set_stacks_collapsed([stack], not collapsed)

    def toggle_stacks(self):
        """Expand the collapsed stacks among the photos chosen, or collapse them when
        none is collapsed."""
        rows = [row for row in self._stack_rows() if self.model.stack(row) is not None]
        if not rows:
            return
        collapsed = {
            self.model.stack(row): self.model.data(self.model.index(row), STACK_ROLE)[2]
            for row in rows
        }
        expand = any(collapsed.values())
        self.service.set_stacks_collapsed(list(collapsed), not expand)

    def set_stack_top(self):
        """Make the photo chosen the top of its stack, the cell shown while it is collapsed."""
        rows = self._stack_rows()
        if len(rows) == 1 and self.model.stack(rows[0]) is not None:
            self.service.set_stack_top(self.model.photo_id(rows[0]))
