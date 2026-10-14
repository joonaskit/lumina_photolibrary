"""What the app remembers between runs, in QSettings under the app's name."""

from PySide6.QtCore import QSettings

from lumina import APP_ID


def _settings():
    return QSettings(APP_ID, APP_ID)


def last_folder():
    value = _settings().value("library/last_folder")
    return value if isinstance(value, str) and value else None


def set_last_folder(path):
    _settings().setValue("library/last_folder", path)


def clear():
    _settings().clear()
