"""Search: the photos that match words, or that look like one photo, and the model that
search needs, downloaded once."""

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from lumina.qt_models.library_model import SIMILAR
from lumina.ui.window_common import _DEVELOP, _EMPTY, _GRID, _VIEWER


class Searching(QObject):
    def __init__(self, nav, model, search, field, search_status, messages, parent=None):
        """`search` is the SearchService, or None; `field` the toolbar's SearchField and
        `search_status` the status bar's SearchStatus; `messages` the window's status bar.
        The parent is the window, which the dialogs are over."""
        super().__init__(parent)
        self.nav = nav
        self.model = model
        self.search = search
        self.field = field
        self.search_status = search_status
        self.messages = messages
        field.searched.connect(self.search_text)
        field.similar_cleared.connect(model.clear_search)
        model.search_changed.connect(self._on_search_changed)
        if search is not None:
            search_status.cancel_download.connect(search.cancel_download)
            search_status.cancel_indexing.connect(search.cancel_indexing)
            search.download_progress.connect(search_status.show_download)
            search.download_failed.connect(self._on_download_failed)
            search.model_changed.connect(self._on_model_changed)
            search.indexing_changed.connect(search_status.show_indexing)
            search.indexing_changed.connect(nav.status_changed)

    def focus_search(self):
        if self.search is None or self.nav.page() == _EMPTY:
            return
        if self.nav.page() != _GRID:
            self.nav.go_back() if self.nav.page() == _DEVELOP else self.nav.show_grid()
            if self.nav.page() != _GRID:
                return
        self.field.focus()

    def search_text(self, text):
        """Show the photos that match `text`; "" shows them all again."""
        if self.search is None:
            return
        if text and not self.model_ready():
            return
        if text and self.nav.page() in (_VIEWER, _DEVELOP):
            # Typed in the field above a photo: the results are in the grid. The Develop
            # view saves the edit on the way out
            if self.nav.page() == _DEVELOP:
                self.nav.close_edit()
            self.nav.show_grid()
            # Still typing: nothing selected, or the next key would replace it
            self.field.edit.setFocus()
        self.model.search_text(text)

    def find_similar(self):
        """Show the photos most like the one chosen in the grid or shown in the viewer."""
        if self.search is None:
            return
        if self.nav.page() == _VIEWER:
            row = self.nav.viewed_row()
        elif self.nav.page() == _GRID and self.nav.current_row() is not None:
            row = self.nav.current_row()
        else:
            return
        if row is None or not self.model_ready():
            return
        if self.nav.page() == _VIEWER:
            self.nav.show_grid()
        self.field.clear()
        if not self.model.find_similar(row):
            self.messages.showMessage(f"{self.model.name(row)} is not indexed for search yet", 6000)

    def end_search(self):
        self.field.clear()
        self.model.clear_search()

    def model_ready(self):
        """Whether search can run: if the model is not there, offer to fetch it."""
        if self.search.is_ready():
            return True
        if self.search.is_downloading():
            self.messages.showMessage("Search works once the model is downloaded", 6000)
            return False
        box = QMessageBox(self.parent())
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
        if self.search.is_ready() and self.field.text():
            self.model.search_text(self.field.text())

    def _on_download_failed(self, error):
        self.search_status.download_ended(error)
        QMessageBox.warning(
            self.parent(),
            "Download Failed",
            f"The search model could not be downloaded.\n\n{error}\n\n"
            "What came is kept: the next try continues from there.",
        )

    def _on_search_changed(self):
        search = self.model.search
        self.field.show_similar(
            search.label if search is not None and search.kind == SIMILAR else None
        )
        self.nav.status_changed.emit()
