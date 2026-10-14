"""The larger image for the viewer, decoded in a worker process.

Only the newest request counts: an earlier one still waiting is cancelled, and the
result of one already running is dropped.
"""

from PySide6.QtCore import QObject, Signal

from lumina.core import images
from lumina.qt_models import workers
from lumina.qt_models.images import to_image


class PreviewLoader(QObject):
    # The path and its image
    ready = Signal(str, object)
    # The path and why it could not be read
    failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._token = 0
        self._future = None

    def request(self, path, max_side):
        self.cancel()
        token = self._token
        self._future = workers.submit(
            images.load_pixels,
            path,
            max_side,
            on_done=lambda pixels: self._done(token, path, pixels),
            on_error=lambda error: self._error(token, path, error),
        )

    def cancel(self):
        self._token += 1
        if self._future is not None:
            self._future.cancel()
            self._future = None

    def _done(self, token, path, pixels):
        if token == self._token:
            self._future = None
            self.ready.emit(path, to_image(pixels))

    def _error(self, token, path, error):
        if token == self._token:
            self._future = None
            self.failed.emit(path, getattr(error, "message", None) or str(error))
