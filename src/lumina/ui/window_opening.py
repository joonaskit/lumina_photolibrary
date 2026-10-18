"""opening: the part of `MainWindow` for it (see main_window.py)."""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from lumina.ui.quick_view import QuickView


class OpeningMixin:
    # Opening from outside: the file manager, or a second `lumina PATH`

    def open_paths(self, paths):
        """Show what the command line or the file manager asked for: a folder is added
        to the library and shown, a photo is opened. The window comes forward for what
        is in the library; a photo that is not is shown in a window of its own."""
        for path in paths:
            if os.path.isdir(path):
                self.present()
                self.add_folder(path)
            else:
                self.open_photo(path)

    def present(self):
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def open_photo(self, path):
        """Open the photo at `path` in the viewer, if its folder is in the library; else
        in a window of its own that asks whether to add the folder. Anything that is not
        a photo is told so."""
        if not self.service.is_photo_file(path):
            QMessageBox.warning(
                self, "Cannot Open", f"{path}\n\nThis is not a photo Lumina can open."
            )
            return
        folder, _photo = self.service.locate_photo(path)
        if folder is None:
            self.show_quick(path)
            return
        self.present()
        self._opening = os.path.realpath(path)
        self.sidebar.select(folder)
        self._open_arrived()

    def show_quick(self, path):
        view = QuickView(os.path.realpath(path))
        view.add_requested.connect(lambda folder, view=view: self._add_from_quick(view, folder))
        view.destroyed.connect(lambda _=None, view=view: self._forget_quick(view))
        self.quick_views.append(view)
        view.show()
        view.raise_()
        view.activateWindow()

    def _forget_quick(self, view):
        if view in self.quick_views:
            self.quick_views.remove(view)

    def _add_from_quick(self, view, folder):
        path = view.path
        view.close()
        self.present()
        if self.add_folder(folder) is not None:
            self._opening = path
            self._open_arrived()

    def _open_arrived(self, loaded=False, scanned=False):
        """The photo asked for, once the grid has it. The grid fills while the folder is
        scanned. It is given up when the grid has loaded and the library has the photo but
        the grid not (a filter hides it), or when the scan ended and the library still
        has no such photo."""
        path = self._opening
        if path is None:
            return
        _folder, photo = self.service.locate_photo(path)
        row = self.model.row_of(photo) if photo is not None else None
        if row is not None:
            self._opening = None
            self.show_photo(row)
        elif (scanned and photo is None) or (loaded and photo is not None):
            self._opening = None
