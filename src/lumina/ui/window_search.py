"""search: the part of `MainWindow` for it (see main_window.py)."""

from PySide6.QtWidgets import QMenu, QMessageBox

from lumina.qt_models.library_model import SIMILAR
from lumina.ui.window_common import _DEVELOP, _EMPTY, _GRID, _VIEWER


class SearchMixin:
    # Search

    def focus_search(self):
        if self.search is None or self.pages.currentIndex() == _EMPTY:
            return
        if self.pages.currentIndex() != _GRID:
            self.go_back() if self.pages.currentIndex() == _DEVELOP else self.show_grid()
            if self.pages.currentIndex() != _GRID:
                return
        self.search_field.focus()

    def search_text(self, text):
        """Show the photos that match `text`; "" shows them all again."""
        if self.search is None:
            return
        if text and not self._model_ready():
            return
        if text and self.pages.currentIndex() in (_VIEWER, _DEVELOP):
            # Typed in the field above a photo: the results are in the grid. The Develop
            # view saves the edit on the way out
            if self.pages.currentIndex() == _DEVELOP:
                self.session.close()
            self.show_grid()
            # Still typing: nothing selected, or the next key would replace it
            self.search_field.edit.setFocus()
        self.model.search_text(text)

    def find_similar(self):
        """Show the photos most like the one chosen in the grid or shown in the viewer."""
        if self.search is None:
            return
        if self.pages.currentIndex() == _VIEWER:
            row = self._viewed_row()
        elif self.pages.currentIndex() == _GRID and self.grid.currentIndex().isValid():
            row = self.grid.currentIndex().row()
        else:
            return
        if row is None or not self._model_ready():
            return
        if self.pages.currentIndex() == _VIEWER:
            self.show_grid()
        self.search_field.clear()
        if not self.model.find_similar(row):
            self.statusBar().showMessage(
                f"{self.model.name(row)} is not indexed for search yet", 6000
            )

    def end_search(self):
        self.search_field.clear()
        self.model.clear_search()

    def _model_ready(self):
        """Whether search can run: if the model is not there, offer to fetch it."""
        if self.search.is_ready():
            return True
        if self.search.is_downloading():
            self.statusBar().showMessage("Search works once the model is downloaded", 6000)
            return False
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Download the Search Model?")
        box.setText("Search needs a model that is downloaded once.")
        box.setInformativeText(
            f"It is {self.search.download_size() / 1e6:,.0f} MB, from Hugging Face "
            "(SigLIP 2 by Google). After that, everything runs on this computer: "
            "your photos and searches never leave it.\n\n"
            "Lumina then indexes your photos in the background, newest first."
        )
        download = box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not Now", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(download)
        box.exec()
        if box.clickedButton() is download:
            self.search.download()
        return False

    def _on_model_changed(self):
        if self.search.is_downloading():
            self.search_status.show_download(0, self.search.download_size())
            return
        self.search_status.download_ended()
        # Typed while it came: search now
        if self.search.is_ready() and self.search_field.text():
            self.model.search_text(self.search_field.text())

    def _on_download_failed(self, error):
        self.search_status.download_ended(error)
        QMessageBox.warning(
            self,
            "Download Failed",
            f"The search model could not be downloaded.\n\n{error}\n\n"
            "What came is kept: the next try continues from there.",
        )

    def _on_search_changed(self):
        search = self.model.search
        self.search_field.show_similar(
            search.label if search is not None and search.kind == SIMILAR else None
        )
        self._update_status()

    def _grid_menu(self, point):
        index = self.grid.indexAt(point)
        if not index.isValid():
            return
        if not self.grid.selectionModel().isSelected(index):
            self.grid.setCurrentIndex(index)
        else:
            # Keep the selection: flagging from the menu is for all of it
            self.grid.selectionModel().setCurrentIndex(
                index, self.grid.selectionModel().SelectionFlag.NoUpdate
            )
        self._photo_menu(in_grid=True).exec(self.grid.viewport().mapToGlobal(point))

    def _photo_menu(self, in_grid):
        """For the photos chosen in the grid or the one in the viewer. Syncing is from
        one photo chosen in the grid to the others."""
        menu = QMenu(self)
        menu.addAction(self.edit_action)
        if self.search is not None:
            menu.addAction(self.similar_action)
        menu.addSeparator()
        menu.addAction(self.keywords_action)
        self._add_collection_menu(menu)
        menu.addSeparator()
        menu.addAction(self.pick_action)
        menu.addAction(self.reject_action)
        menu.addAction(self.unflag_action)
        rating = menu.addMenu("Rating")
        for action in self.rate_actions[1:]:
            rating.addAction(action)
        rating.addSeparator()
        rating.addAction(self.rate_actions[0])
        menu.addSeparator()
        menu.addAction(self.make_copy_action)
        if not in_grid and self._copy_to_delete() is not None:
            menu.addAction(self.delete_copy_action)
        else:
            rows = self._flag_rows()
            if len(rows) == 1 and self.model.copies(rows[0]):
                shown = self.model.copies_open(rows[0])
                menu.addAction(
                    "Hide Virtual Copies" if shown else "Show Virtual Copies",
                    lambda: self.model.toggle_copies(self.model.photo_id(rows[0])),
                )
            if self._copy_to_delete() is not None:
                menu.addAction(self.delete_copy_action)
        if in_grid:
            menu.addSeparator()
            for action in self.stack_menu_actions:
                menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.copy_settings_action)
        menu.addAction(self.paste_settings_action)
        if in_grid:
            menu.addAction(self.sync_settings_action)
            if self.exporter is not None:
                menu.addSeparator()
                menu.addAction(self.export_selected_action)
        if self.merge is not None:
            menu.addSeparator()
            menu.addAction(self.merge_hdr_action)
            menu.addAction(self.stitch_panorama_action)
        return menu
