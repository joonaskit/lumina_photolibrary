"""Which photos the grid shows: the folder, collection or import chosen in the sidebar,
and the filters above the grid; the library's folders; and the window's title."""

import os

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QFileDialog, QMessageBox

from lumina.qt_models.library_model import Filter
from lumina.ui import settings
from lumina.ui.window_common import _DEVELOP, _EMPTY, _GRID, _VIEWER, APP_NAME


class Browser(QObject):
    def __init__(self, nav, model, service, sidebar, filters, title, messages, parent=None):
        """`title` is the toolbar's title label; `messages` the window's status bar. The
        parent is the window, whose title it sets and which the dialogs are over."""
        super().__init__(parent)
        self.nav = nav
        self.model = model
        self.service = service
        self.sidebar = sidebar
        self.filters = filters
        self.title = title
        self.messages = messages
        nav.page_changed.connect(self.update_title)
        nav.viewed_changed.connect(self.update_title)

    # The library's folders

    def choose_folder(self):
        start = settings.last_folder() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self.parent(), "Add Folder to Library", start)
        if folder:
            self.add_folder(folder)

    def add_folder(self, path):
        """Add a folder to the library and show it. Returns its id, or None when it can't
        be added (the user is told why)."""
        try:
            folder = self.service.add_folder(path)
        except ValueError as error:
            QMessageBox.warning(self.parent(), "Cannot Add Folder", str(error))
            return None
        settings.set_last_folder(os.path.dirname(os.path.realpath(path)))
        self.sidebar.select(folder)
        return folder

    def remove_folder(self, folder_id):
        info = next((f for f in self.service.folders() if f.id == folder_id), None)
        if info is None:
            return
        answer = QMessageBox.question(
            self.parent(),
            "Remove from Library",
            f"Remove “{info.name}” from the library?\n\n"
            "Lumina forgets its photos and what it knows of them. "
            "The files themselves are not touched.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Ok:
            self.service.remove_folder(folder_id)

    def refresh_folders(self, select=None):
        folders = self.service.folders()
        current = select if select is not None else self.sidebar.current_folder()
        if current is not None and all(f.id != current for f in folders):
            current = None
        self.sidebar.set_folders(folders, current)
        if not folders:
            self.nav.show_page(_EMPTY)
        elif self.nav.page() == _EMPTY:
            self.nav.show_page(_GRID)
        self.on_folder_selected(current, force=select is not None)

    def on_job_ended(self, _status):
        self.refresh_last_import()
        self.refresh_choices()

    def on_sidecars_written(self, result):
        """Say what a pass of writing sidecars could not do; a quiet pass says nothing."""
        if not result.failures:
            return
        path, why = result.failures[0]
        more = f" (and {len(result.failures) - 1} more)" if len(result.failures) > 1 else ""
        self.messages.showMessage(
            f"{len(result.failures)} sidecar{'' if len(result.failures) == 1 else 's'} "
            f"not written: {path}: {why}{more}",
            10000,
        )

    # What the sidebar has chosen

    def on_folder_selected(self, folder_id, force=True):
        changed = folder_id != self.model.filter.folder_id
        settings.set_selected_folder(folder_id)
        self.refresh_choices(folder_id)
        if changed or force:
            self.apply_filter()
        if self.nav.page() == _VIEWER and changed:
            self.nav.show_grid()

    def on_import_selected(self, _job_id):
        self.leave_folders()
        self.apply_filter()
        if self.nav.page() == _VIEWER:
            self.nav.show_grid()

    def leave_folders(self):
        """A collection or an import is chosen in the sidebar, not a folder."""
        settings.set_selected_folder(None)
        self.refresh_choices(None)

    def refresh_last_import(self):
        self.sidebar.set_last_import(self.service.last_import())
        if self.sidebar.current_import() is not None:
            self.apply_filter()

    # The filters

    def refresh_choices(self, folder_id=None):
        folder_id = folder_id if folder_id is not None else self.sidebar.current_folder()
        self.filters.blockSignals(True)
        self.filters.set_choices(*self.service.choices(folder_id))
        self.filters.blockSignals(False)
        self.apply_filter()

    def apply_filter(self):
        year, camera, lens = self.filters.values()
        collection = self.sidebar.current_collection()
        shown = Filter(
            self.sidebar.current_folder(),
            year,
            camera,
            lens,
            collection[0] if collection is not None and not collection[1] else None,
            self.sidebar.current_import(),
        )
        if shown != self.model.filter or self.model.rowCount() == 0:
            self.model.set_filter(shown)
        self.update_title()

    def set_flag_filter(self, flag):
        """Show only photos with a thumbs up, with none, or with a thumbs down, or all."""
        self.model.set_flag_filter(flag)
        self.nav.status_changed.emit()

    def set_rating_filter(self, stars):
        """Show only photos with `stars` or more, only those without, or all."""
        self.model.set_rating_filter(stars)
        self.nav.status_changed.emit()

    def set_edit_filter(self, which):
        """Show only edited photos, only unedited ones, or all."""
        self.model.set_edit_filter(which)
        self.nav.status_changed.emit()

    def shows_everything(self):
        """Whether the grid shows every photo, so there is no narrower place to look."""
        return (
            self.sidebar.current_folder() is None
            and self.sidebar.current_collection() is None
            and self.sidebar.current_import() is None
            and self.model.filter == self.model.filter.__class__()
        )

    # The title

    def shown_name(self):
        """The name of what the grid shows: the folder, the collection, the last import, or
        all photos."""
        folder = self.sidebar.current_folder()
        info = next((f for f in self.service.folders() if f.id == folder), None)
        name = info.name if info is not None else "All Photos"
        collection = self.sidebar.current_collection()
        if self.sidebar.current_import() is not None:
            name = "Last Import"
        elif collection is not None:
            found = next((c for c in self.service.collections() if c.id == collection[0]), None)
            name = found.name if found is not None else name
        return name

    def update_title(self, *_args):
        """The photo looked at in the viewer and the Develop view; else what the grid
        shows."""
        if self.nav.page() in (_VIEWER, _DEVELOP):
            row = self.nav.viewed_row()
            if row is not None:
                self.title.setText(self.nav.viewed_title(row))
            return
        name = self.shown_name()
        self.title.setText(name if self.service.folders() else APP_NAME)
        self.parent().setWindowTitle(f"{name} — {APP_NAME}" if self.service.folders() else APP_NAME)
