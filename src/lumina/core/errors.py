"""Errors from `core/` that the layers above see. A `PhotoError` names the file it is about."""


class PhotoError(Exception):
    def __init__(self, path, message):
        super().__init__(path, message)
        self.path = str(path)
        self.message = message

    def __str__(self):
        return f"{self.path}: {self.message}"


class NotConnected(Exception):
    """The folder's volume (or the camera) is not connected: the job waits for it."""


class CameraBusy(NotConnected):
    """Another program has the camera: the message says which and how to let it go."""
