"""Tethering: a folder whose new photos are taken in and shown as they come, and a camera
session that shoots into one, with its bar, its live view and the last photo taken."""

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from lumina.qt_models import tether as camera_service
from lumina.qt_models.library import NewerParams
from lumina.qt_models.previews import PreviewLoader
from lumina.ui import settings, shortcuts
from lumina.ui.camera import CameraDialog
from lumina.ui.tether import TetherDialog
from lumina.ui.window_common import _DEVELOP, _EMPTY, _GRID, _LIVE, _VIEWER, photo_name


class Tethering(QObject):
    def __init__(
        self,
        nav,
        model,
        service,
        tether,
        camera,
        widgets,
        capture_action,
        messages,
        parent=None,
    ):
        """`tether` is the HotFolder, or None; `camera` the CameraService (which takes its
        photos in through `tether`), or None. `widgets` has the window's sidebar,
        camera_bar, live_view (the LivePanel) and tether_status; `messages` is the
        window's status bar. The parent is the window, which the dialogs are over."""
        super().__init__(parent)
        self.nav = nav
        self.model = model
        self.service = service
        self.tether = tether
        self.camera = camera
        self.sidebar = widgets.sidebar
        self.camera_bar = widgets.camera_bar
        self.live_view = widgets.live_view
        self.tether_status = widgets.tether_status
        self.capture_action = capture_action
        self.messages = messages
        # A tethered folder's newest photo, and the one to show once the grid has it, with
        # whether in the Develop view
        self._last_shot = None
        self._showing_shot = None
        # The page the live view was opened from, to go back to
        self._live_from = _GRID
        self._page = nav.page()
        self.last_shot = None
        nav.set_back(_LIVE, self.leave_live)
        nav.page_changed.connect(self._on_page_changed)
        model.rowsInserted.connect(lambda: self._show_shot())
        model.loading_changed.connect(lambda loading: loading or self._show_shot(loaded=True))
        if camera is not None:
            self._follow_camera(camera)
        if tether is not None:
            self.tether_status.stop_requested.connect(self.stop_tethering)
            self.sidebar.stop_tether_requested.connect(self.stop_tethering)
            tether.changed.connect(self._on_tether_changed)
            tether.arrived.connect(self._on_shot_arrived)
            tether.failed.connect(self._on_shot_failed)
            tether.stopped.connect(lambda why: self.messages.showMessage(why, 15000))

    def _follow_camera(self, camera):
        bar, live = self.camera_bar, self.live_view
        bar.capture_requested.connect(camera.capture)
        bar.setting_chosen.connect(camera.set_setting)
        bar.fetch_again_requested.connect(camera.fetch_again)
        bar.end_requested.connect(camera.stop)
        camera.changed.connect(self._on_camera_changed)
        camera.failed.connect(
            lambda name, why: self.messages.showMessage(f"{name} was not saved: {why}", 15000)
        )
        camera.error.connect(lambda why: self.messages.showMessage(why, 15000))
        bar.live_requested.connect(self._on_live_requested)
        bar.focus_requested.connect(camera.focus)
        camera.live_changed.connect(self._on_live_changed)
        camera.frame_ready.connect(lambda: live.show_frame(camera.frame))
        camera.saved.connect(self._on_camera_saved)
        live.live_toggled.connect(camera.set_live)
        live.zoom_chosen.connect(camera.zoom)
        live.point_chosen.connect(camera.move_point)
        live.clipping_toggled.connect(camera.set_clipping)
        live.last_shot_wanted.connect(self._load_last_shot)
        self.last_shot = PreviewLoader(self)
        self.last_shot.ready.connect(lambda _path, image, _full: live.set_last_shot(image))
        QApplication.instance().applicationStateChanged.connect(
            lambda state: camera.set_active(state == Qt.ApplicationState.ApplicationActive)
        )

    def _on_page_changed(self, page):
        if self._page == _LIVE and page != _LIVE and self.camera is not None:
            # Live view runs only while it is shown
            self.camera.set_live(False)
        self._page = page
        self.camera_bar.set_live_shown(page == _LIVE)

    # A tethered folder

    def tether_folder(self, folder_id):
        """Ask for the preset, then tether the folder `folder_id` and show it."""
        if self.tether is None:
            return
        info = next((f for f in self.service.folders() if f.id == folder_id), None)
        if info is None:
            return
        presets = [preset.name for preset in self.service.presets()]
        dialog = TetherDialog(info.name, presets, settings.tether_preset(), self.parent())
        if dialog.exec() != TetherDialog.DialogCode.Accepted:
            return
        preset = dialog.chosen_preset()
        settings.set_tether_preset(preset)
        try:
            self.tether.start(folder_id, preset)
        except (KeyError, NewerParams) as error:
            QMessageBox.warning(
                self.parent(), "Cannot Tether", f"The preset can't be used: {error}"
            )
            return
        self._last_shot = None
        self.sidebar.select(folder_id)

    def stop_tethering(self):
        """Stop the tethered folder, or the camera session whose photos come through it."""
        if self.camera is not None and self.camera.running:
            self.camera.stop()
        elif self.tether is not None:
            self.tether.stop()

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
        page = self.nav.page()
        following = page != _LIVE and (
            page in (_GRID, _EMPTY)
            or self._last_shot is None
            or self.nav.viewed_id == self._last_shot
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
                name = photo_name(self.service, photo_id)
                self.messages.showMessage(f"New photo {name} is not among those shown", 10000)
            return
        self._showing_shot = None
        if develop:
            self.nav.develop_from = _VIEWER
            self.nav.show_develop(row)
        else:
            self.nav.show_photo(row)

    def _on_shot_failed(self, path, why):
        self.messages.showMessage(f"Could not read {path}: {why}", 15000)

    # A camera

    def tether_camera(self):
        """Ask where a camera session's photos go, then start it."""
        if self.camera is None:
            return
        if self.camera.running:
            self.camera_bar.show()
            return
        if not camera_service.available():
            QMessageBox.information(
                self.parent(),
                "Tether Camera",
                "Shooting with a camera needs python-gphoto2, which is not installed.\n\n"
                "Install it with: pip install 'lumina[tether]'",
            )
            return
        folders = [f for f in self.service.folders() if f.connected]
        presets = [preset.name for preset in self.service.presets()]
        dialog = CameraDialog(folders, presets, settings.camera_choices(), self.parent())
        if dialog.exec() != CameraDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        settings.set_camera_choices(chosen)
        try:
            self.camera.start(
                chosen["folder"], chosen["name"], chosen["layout"], chosen["keep"], chosen["preset"]
            )
        except (ValueError, KeyError, NewerParams) as error:
            QMessageBox.warning(self.parent(), "Cannot Tether", str(error))
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
        if self.tether is not None:
            self._on_tether_changed()
        if self.nav.page() == _LIVE:
            if self.camera.running:
                self._on_live_changed()
            else:
                self.nav.go_back()

    # Live view

    def show_live(self):
        """The camera's live view, on a page of its own; it runs while that is shown."""
        if self.camera is None or not self.camera.running:
            return
        if self.nav.page() != _LIVE:
            self._live_from = self.nav.page()
            if self._live_from == _DEVELOP:
                self.nav.close_edit()
                self._live_from = _VIEWER
        self.nav.show_page(_LIVE)
        self.live_view.set_crop(self.camera.crop_guide())
        self.camera.set_live(True)
        self._on_live_changed()
        self.live_view.canvas.setFocus()

    def leave_live(self):
        self.camera.set_live(False)
        self.last_shot.cancel()
        row = self.nav.viewed_row()
        if self._live_from == _VIEWER and row is not None:
            self.nav.show_photo(row, self.nav.viewed_copy)
        else:
            self.nav.show_grid()

    def _on_live_changed(self):
        camera = self.camera
        # Whether it can focus comes with its live view's state
        self.camera_bar.show_focus(camera)
        self.live_view.show_live(camera.live, camera.connected)
        self.live_view.set_message(None if camera.connected else camera.message)
        self.camera_bar.set_live_shown(self.nav.page() == _LIVE)

    def _on_live_requested(self, on):
        if on:
            self.show_live()
        elif self.nav.page() == _LIVE:
            self.leave_live()

    def _on_camera_saved(self, _path):
        if self.nav.page() == _LIVE and self.live_view.last.isChecked():
            self._load_last_shot()
        else:
            # Loaded when it is wanted
            self.live_view.set_last_shot(None)

    def _load_last_shot(self):
        path = self.camera.last_saved
        if path is not None:
            self.last_shot.request(path, max(self.live_view.width(), self.live_view.height()))
