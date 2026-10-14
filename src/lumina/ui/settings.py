"""What the app remembers between runs, in QSettings under the app's name."""

from PySide6.QtCore import QSettings

from lumina import APP_ID


def _settings():
    return QSettings(APP_ID, APP_ID)


def last_folder():
    """The folder last added, to start the folder chooser in."""
    value = _settings().value("library/last_folder")
    return value if isinstance(value, str) and value else None


def set_last_folder(path):
    _settings().setValue("library/last_folder", path)


def selected_folder():
    """The library folder shown last, or None for all photos."""
    value = _settings().value("library/selected_folder")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def set_selected_folder(folder_id):
    _settings().setValue("library/selected_folder", "" if folder_id is None else folder_id)


def clear():
    _settings().clear()


def export_choices():
    """What the export dialog had last time: a dict, empty at first."""
    value = _settings().value("export/choices")
    return dict(value) if isinstance(value, dict) else {}


def set_export_choices(choices):
    _settings().setValue("export/choices", choices)


def grid_size():
    """The grid's thumbnail size: "small", "medium" or "large"."""
    value = _settings().value("library/grid_size")
    return value if value in ("small", "medium", "large") else "medium"


def set_grid_size(name):
    _settings().setValue("library/grid_size", name)
