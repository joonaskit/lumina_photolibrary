"""settings_copy: the part of `MainWindow` for it (see main_window.py)."""

from lumina.ui import shortcuts
from lumina.ui.window_common import _DEVELOP, _GRID, _NO_PHOTO, _VIEWER


class SettingsCopyMixin:
    # Copying settings from one photo to others

    def _settings_source(self):
        """The id of the photo whose settings Copy takes: the one open, shown or current."""
        page = self.pages.currentIndex()
        if page == _DEVELOP:
            return self.session.photo_id
        if page == _VIEWER:
            return self._viewed_id
        if page == _GRID and self.grid.currentIndex().isValid():
            return self.model.photo_id(self.grid.currentIndex().row())
        return None

    def _sync_rows(self):
        """The rows Sync would copy to: the ones chosen in the grid but the current one;
        none unless the current one is among them and there are others."""
        if self.pages.currentIndex() != _GRID or not self.grid.currentIndex().isValid():
            return []
        chosen = {index.row() for index in self.grid.selectionModel().selectedIndexes()}
        current = self.grid.currentIndex().row()
        return sorted(chosen - {current}) if current in chosen else []

    def _update_settings_actions(self):
        page = self.pages.currentIndex()
        shortcuts.enable(self.copy_settings_action, self._settings_source() is not None, _NO_PHOTO)
        can_paste = self.service.copied is not None and (
            page == _DEVELOP or (page in (_GRID, _VIEWER) and bool(self._flag_rows()))
        )
        shortcuts.enable(
            self.paste_settings_action,
            can_paste,
            "Nothing copied yet" if self.service.copied is None else _NO_PHOTO,
        )
        shortcuts.enable(
            self.sync_settings_action,
            bool(self._sync_rows()),
            "Choose two or more photos in the grid",
        )
        self.develop.paste_settings_button.setEnabled(self.service.copied is not None)
