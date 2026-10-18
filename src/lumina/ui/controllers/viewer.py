"""The viewer's page: one photo, its thumbnail at once, then an image the screen's size,
and the photo's full size once zoomed in past that."""

import os

from PySide6.QtCore import QObject, Signal

from lumina.qt_models.previews import PreviewLoader
from lumina.ui.window_common import _VIEWER, VIEWER_MAX_SIDE


class ViewerPage(QObject):
    # The zoom changed, or the image shown, which may now be short of what the zoom shows
    zoom_changed = Signal()

    def __init__(self, view, pages, model, service, parent=None):
        """`view` is the viewer's ImageView, `pages` the window's stack of pages."""
        super().__init__(parent)
        self.view = view
        self.pages = pages
        self.model = model
        self.service = service
        self.previews = PreviewLoader(self)
        self.previews.ready.connect(self._on_ready)
        self.previews.failed.connect(self._on_failed)
        # (photo id, copy, path) of the photo shown
        self._shown = None
        # The photo whose full size was asked for, so it is asked for once
        self._full_requested = None
        view.zoom_changed.connect(self._on_zoom_changed)

    def show(self, row, photo_id, copy):
        """The photo of `row`, edit `copy` of it: its thumbnail, or why there is none
        yet. `load` asks for the larger image."""
        path = self.model.path(row)
        self._shown = (photo_id, copy, path)
        self._full_requested = None
        thumbnail = self.model.thumbnail_pixmap(row)
        if thumbnail is not None:
            self.view.set_image(thumbnail)
        elif path is None:
            self.view.set_message(f"{self.model.name(row)} is on a drive that is not connected")
        else:
            self.view.set_message("Loading…")

    def load(self):
        """Ask for the image of the photo shown, decoded at the screen's size; zoomed, the
        same place of it at full size, as the last one's."""
        if self._shown is None or self._shown[2] is None:
            self.previews.cancel()
            return
        if self.view.is_zoomed():
            self._request_full()
        else:
            self.previews.request(self._shown[2], self.side(), self._edit())

    def cancel(self):
        self.previews.cancel()
        self._shown = None

    def side(self):
        """The longest side to decode for: the screen's, up to a 4K screen's."""
        screen = self.view.screen()
        size = screen.size() * screen.devicePixelRatio()
        return min(VIEWER_MAX_SIDE, max(size.width(), size.height()))

    def _edit(self):
        # From the library, not the grid's rows: an edit just made may not be in them yet
        photo_id, copy, _path = self._shown
        return self.service.edit_json(photo_id, copy)

    def _request_full(self):
        if self._shown is None:
            return
        photo_id, copy, path = self._shown
        if path is None or self._full_requested == (photo_id, copy):
            return
        self._full_requested = (photo_id, copy)
        self.previews.request(path, None, self._edit())

    def _on_zoom_changed(self):
        if self.pages.currentIndex() == _VIEWER and self.view.needs_full():
            self._request_full()
        self.zoom_changed.emit()

    def _on_ready(self, path, image, full):
        if self._shown is not None and self._shown[2] == path:
            self.view.set_image(image, full)
            self._on_zoom_changed()

    def _on_failed(self, path, message):
        if self._shown is not None and self._shown[2] == path:
            self.view.set_message(f"Cannot show {os.path.basename(path)}\n{message}")
