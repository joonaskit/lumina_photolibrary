"""tether: the part of `MainWindow` for it (see main_window.py)."""

from PySide6.QtWidgets import QMessageBox

from lumina.qt_models import tether as camera_service
from lumina.qt_models.library import NewerParams
from lumina.qt_models.library_model import Filter
from lumina.ui import settings, shortcuts
from lumina.ui.camera import CameraDialog
from lumina.ui.tether import TetherDialog
from lumina.ui.window_common import _DEVELOP, _EMPTY, _GRID, _LIVE, _VIEWER


class TetherMixin:
    # Tethering

    def tether_folder(self, folder_id):
        """Ask for the preset, then tether the folder `folder_id` and show it."""
        if self.tether is None:
            return
        info = next((f for f in self.service.folders() if f.id == folder_id), None)
        if info is None:
            return
        presets = [preset.name for preset in self.service.presets()]
        dialog = TetherDialog(info.name, presets, settings.tether_preset(), self)
        if dialog.exec() != TetherDialog.DialogCode.Accepted:
            return
        preset = dialog.chosen_preset()
        settings.set_tether_preset(preset)
        try:
            self.tether.start(folder_id, preset)
        except (KeyError, NewerParams) as error:
            QMessageBox.warning(self, "Cannot Tether", f"The preset can't be used: {error}")
            return
        self._last_shot = None
        self.sidebar.select(folder_id)

    def stop_tethering(self):
        """Stop the tethered folder, or the camera session whose photos come through it."""
        if self.camera is not None and self.camera.running:
            self.camera.stop()
        elif self.tether is not None:
            self.tether.stop()

    def tether_camera(self):
        """Ask where a camera session's photos go, then start it."""
        if self.camera is None:
            return
        if self.camera.running:
            self.camera_bar.show()
            return
        if not camera_service.available():
            QMessageBox.information(
                self,
                "Tether Camera",
                "Shooting with a camera needs python-gphoto2, which is not installed.\n\n"
                "Install it with: pip install 'lumina[tether]'",
            )
            return
        folders = [f for f in self.service.folders() if f.connected]
        presets = [preset.name for preset in self.service.presets()]
        dialog = CameraDialog(folders, presets, settings.camera_choices(), self)
        if dialog.exec() != CameraDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        settings.set_camera_choices(chosen)
        try:
            self.camera.start(
                chosen["folder"], chosen["name"], chosen["layout"], chosen["keep"], chosen["preset"]
            )
        except (ValueError, KeyError, NewerParams) as error:
            QMessageBox.warning(self, "Cannot Tether", str(error))
            return
        self._last_shot = None
        self.sidebar.select(chosen["folder"])

    def capture(self):
        if self.camera is not None and self.camera.connected:
            self.camera.capture(self.camera_bar.focus.isChecked())

    def _on_camera_changed(self):
        self.camera_bar.show_service(self.camera)
        shortcuts.enable(
            self.capture_action,
            self.camera.connected and not self.camera.ending,
            "No camera connected",
        )
        self._on_tether_changed()
        if self.pages.currentIndex() == _LIVE:
            if self.camera.running:
                self._on_live_changed()
            else:
                self.go_back()

    # Live view

    def show_live(self):
        """The camera's live view, on a page of its own; it runs while that is shown."""
        if self.camera is None or not self.camera.running:
            return
        if self.pages.currentIndex() != _LIVE:
            self._live_from = self.pages.currentIndex()
            if self._live_from == _DEVELOP:
                self.session.close()
                self._live_from = _VIEWER
        self._show_page(_LIVE)
        self.live_view.set_crop(self.camera.crop_guide())
        self.camera.set_live(True)
        self._on_live_changed()
        self.live_view.canvas.setFocus()

    def _leave_live(self):
        self.camera.set_live(False)
        self.last_shot.cancel()
        row = self._viewed_row()
        if self._live_from == _VIEWER and row is not None:
            self.show_photo(row, self._viewed_copy)
        else:
            self.show_grid()

    def _on_live_changed(self):
        camera = self.camera
        # Whether it can focus comes with its live view's state
        self.camera_bar.show_focus(camera)
        self.live_view.show_live(camera.live, camera.connected)
        self.live_view.set_message(None if camera.connected else camera.message)
        self.camera_bar.set_live_shown(self.pages.currentIndex() == _LIVE)

    def _on_live_requested(self, on):
        if on:
            self.show_live()
        elif self.pages.currentIndex() == _LIVE:
            self._leave_live()

    def _on_camera_saved(self, path):
        if self.pages.currentIndex() == _LIVE and self.live_view.last.isChecked():
            self._load_last_shot()
        else:
            # Loaded when it is wanted
            self.live_view.set_last_shot(None)

    def _load_last_shot(self):
        path = self.camera.last_saved
        if path is not None:
            self.last_shot.request(path, max(self.live_view.width(), self.live_view.height()))

    def _on_tether_changed(self):
        folder = self.tether.folder
        info = next((f for f in self.service.folders() if f.id == folder), None)
        self.sidebar.set_tethered(folder)
        # A camera session's folder is its bar's
        shooting = self.camera is not None and self.camera.running
        self.tether_status.show_state(
            info.name if info is not None and not shooting else None,
            self.tether.count,
            self.tether.preset,
        )

    def _on_shot_arrived(self, photo_id):
        """A tethered folder's new photo: shown in the viewer, or in the Develop view while
        that is open, unless an older photo is being looked at."""
        page = self.pages.currentIndex()
        following = page != _LIVE and (
            page in (_GRID, _EMPTY) or self._last_shot is None or self._viewed_id == self._last_shot
        )
        self._last_shot = photo_id
        if following:
            self._showing_shot = (photo_id, page == _DEVELOP)
            self._show_shot()

    def _show_shot(self, loaded=False):
        """The tethered photo to show, once the grid has it; given up when the grid has
        loaded without it (a filter or the folder chosen hides it)."""
        if self._showing_shot is None:
            return
        photo_id, develop = self._showing_shot
        row = self.model.row_of(photo_id)
        if row is None:
            if loaded and not self.model.is_loading():
                self._showing_shot = None
                name = self._photo_name(photo_id)
                self.statusBar().showMessage(f"New photo {name} is not among those shown", 10000)
            return
        self._showing_shot = None
        if develop:
            self._develop_from = _VIEWER
            self.show_develop(row)
        else:
            self.show_photo(row)

    def _on_shot_failed(self, path, why):
        self.statusBar().showMessage(f"Could not read {path}: {why}", 15000)

    def remove_folder(self, folder_id):
        info = next((f for f in self.service.folders() if f.id == folder_id), None)
        if info is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove from Library",
            f"Remove “{info.name}” from the library?\n\n"
            "Lumina forgets its photos and what it knows of them. "
            "The files themselves are not touched.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Ok:
            self.service.remove_folder(folder_id)

    def _refresh_folders(self, select=None):
        folders = self.service.folders()
        current = select if select is not None else self.sidebar.current_folder()
        if current is not None and all(f.id != current for f in folders):
            current = None
        self.sidebar.set_folders(folders, current)
        if not folders:
            self._show_page(_EMPTY)
        elif self.pages.currentIndex() == _EMPTY:
            self._show_page(_GRID)
        self._on_folder_selected(current, force=select is not None)

    def _on_folder_selected(self, folder_id, force=True):
        changed = folder_id != self.model.filter.folder_id
        settings.set_selected_folder(folder_id)
        self._refresh_choices(folder_id)
        if changed or force:
            self._apply_filter()
        if self.pages.currentIndex() == _VIEWER and changed:
            self.show_grid()

    def _on_collection_selected(self, collection_id, smart):
        settings.set_selected_folder(None)
        self._refresh_choices(None)
        if smart:
            self._apply_smart(collection_id)
        self._apply_filter()
        if self.pages.currentIndex() == _VIEWER:
            self.show_grid()

    def _on_import_selected(self, _job_id):
        settings.set_selected_folder(None)
        self._refresh_choices(None)
        self._apply_filter()
        if self.pages.currentIndex() == _VIEWER:
            self.show_grid()

    def _refresh_last_import(self):
        self.sidebar.set_last_import(self.service.last_import())
        if self.sidebar.current_import() is not None:
            self._apply_filter()

    def _refresh_choices(self, folder_id=None):
        folder_id = folder_id if folder_id is not None else self.sidebar.current_folder()
        self.filters.blockSignals(True)
        self.filters.set_choices(*self.service.choices(folder_id))
        self.filters.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self):
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
        self._update_title()
