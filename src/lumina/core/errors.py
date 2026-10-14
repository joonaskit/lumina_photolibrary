"""Errors from `core/`. Every one names the file it is about."""


class PhotoError(Exception):
    def __init__(self, path, message):
        super().__init__(path, message)
        self.path = str(path)
        self.message = message

    def __str__(self):
        return f"{self.path}: {self.message}"
