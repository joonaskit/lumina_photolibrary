"""Thumbnails for a model's rows: asked for when a row is painted, made in a worker
process (or read from the cache on disk), and kept in a bounded cache in memory.

A photo that can be read is read by path. One that can't (its drive is not
connected) is looked up in the cache on disk by its content id, which works when
it was seen before.
"""

from collections import OrderedDict

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon, QPixmap

from lumina.core import thumbs
from lumina.integration import style
from lumina.qt_models import workers
from lumina.qt_models.images import to_image

# Longest side of a thumbnail, in pixels: enough for the grid on a dense screen
THUMB_SIDE = 320
# About 400 KB each: some 300 MB at most
_CACHE_SIZE = 800


def photo_icon(pixmap):
    """An icon of a photo that looks the same selected: the style would otherwise tint
    a selected icon with the highlight colour, and a photo's colours are the point."""
    icon = QIcon()
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Selected, QIcon.Mode.Active):
        icon.addPixmap(pixmap, mode)
    return icon


class Thumbnails(QObject):
    # The key of a thumbnail that is now made, or failed
    changed = Signal(object)
    # A thumbnail could not be made: the path and why
    failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icons = OrderedDict()
        self._pending = {}
        self._errors = {}
        self._generation = 0
        self._placeholder = None
        self._broken = None
        self._offline = None
        style.themed.changed.connect(self._on_themed)

    def clear(self):
        """Forget everything, and do not make what was asked for and not started."""
        self._generation += 1
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()
        self._icons.clear()
        self._errors.clear()

    def error(self, key):
        return self._errors.get(key)

    def pixmap(self, key):
        icon = self._icons.get(key)
        return icon.pixmap(THUMB_SIDE) if icon is not None else None

    def icon(self, key, path, content_id=None, edit_json=None):
        """The thumbnail of `key` if made, else a placeholder while it is made. `path`
        is None for a photo that can't be read now: then only the cache is looked in.
        `edit_json` is the photo's edit: the key must change when it does."""
        icon = self._icons.get(key)
        if icon is not None:
            self._icons.move_to_end(key)
            return icon
        if key in self._errors:
            return self._broken_icon() if path is not None else self._offline_icon()
        if key not in self._pending:
            if path is None and content_id is None:
                self._errors[key] = "Not connected"
                return self._offline_icon()
            generation = self._generation
            # Read here, not in the worker: a worker keeps the environment it began with
            root = thumbs.default_cache_dir()
            if path is not None:
                call = (thumbs.load_thumbnail, path, THUMB_SIDE, root, edit_json)
            else:
                call = (thumbs.cached_thumbnail, content_id, THUMB_SIDE, root, edit_json)
            self._pending[key] = workers.submit(
                *call,
                on_done=lambda pixels: self._made(generation, key, path, pixels),
                on_error=lambda error: self._failed(generation, key, path, error),
                owner=self,
            )
        return self._placeholder_icon()

    def _made(self, generation, key, path, pixels):
        if generation != self._generation:
            return
        self._pending.pop(key, None)
        if pixels is None:
            # Not connected, and never seen
            self._errors[key] = "Not connected"
        else:
            self._icons[key] = photo_icon(QPixmap.fromImage(to_image(pixels)))
            while len(self._icons) > _CACHE_SIZE:
                self._icons.popitem(last=False)
        self.changed.emit(key)

    def _failed(self, generation, key, path, error):
        if generation != self._generation:
            return
        self._pending.pop(key, None)
        # A PhotoError names the file already; anything else is told with the path
        message = getattr(error, "message", None) or str(error) or type(error).__name__
        self._errors[key] = message
        if path is not None:
            self.failed.emit(path, message)
        self.changed.emit(key)

    def _placeholder_icon(self):
        if self._placeholder is None:
            self._placeholder = style.icon("image-x-generic", colour="textTertiary")
        return self._placeholder

    def _broken_icon(self):
        if self._broken is None:
            self._broken = style.icon("image-missing", "dialog-error", colour="danger")
        return self._broken

    def _offline_icon(self):
        if self._offline is None:
            self._offline = style.icon(
                "drive-removable-media", "network-offline", colour="textTertiary"
            )
        return self._offline

    def _on_themed(self):
        self._placeholder = self._broken = self._offline = None
