"""Copying settings: some sections of one photo's edit, pasted over other photos' edits,
or synced from the current photo to the others chosen."""

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from lumina.core.edit.params import NewerParams
from lumina.ui import shortcuts
from lumina.ui.preset_dialog import CopyDialog
from lumina.ui.window_common import _DEVELOP, _GRID, _NO_PHOTO, _VIEWER, photo_name


class SettingsCopying(QObject):
    def __init__(self, nav, model, service, session, actions, paste_button, messages, parent=None):
        """`session` is the DevelopSession; `actions` has the window's copy, paste and
        sync actions; `paste_button` is the Develop view's Paste button and `messages` the
        window's status bar. The parent is the window, which the dialogs are over."""
        super().__init__(parent)
        self.nav = nav
        self.model = model
        self.service = service
        self.session = session
        self.actions = actions
        self.paste_button = paste_button
        self.messages = messages
        nav.chosen_changed.connect(self.update_actions)
        service.settings_copied.connect(self.update_actions)

    def settings_source(self):
        """The id of the photo whose settings Copy takes: the one open, shown or current."""
        page = self.nav.page()
        if page == _DEVELOP:
            return self.session.photo_id
        if page == _VIEWER:
            return self.nav.viewed_id
        if page == _GRID and self.nav.current_row() is not None:
            return self.model.photo_id(self.nav.current_row())
        return None

    def sync_rows(self):
        """The rows Sync would copy to: the ones chosen in the grid but the current one;
        none unless the current one is among them and there are others."""
        current = self.nav.current_row()
        if self.nav.page() != _GRID or current is None:
            return []
        chosen = {index.row() for index in self.nav.grid.selectionModel().selectedIndexes()}
        return sorted(chosen - {current}) if current in chosen else []

    def update_actions(self, *_args):
        actions = self.actions
        page = self.nav.page()
        copied = self.service.copied
        shortcuts.enable(actions.copy, self.settings_source() is not None, _NO_PHOTO)
        can_paste = copied is not None and (
            page == _DEVELOP or (page in (_GRID, _VIEWER) and bool(self.nav.chosen_rows()))
        )
        shortcuts.enable(
            actions.paste, can_paste, "Nothing copied yet" if copied is None else _NO_PHOTO
        )
        shortcuts.enable(
            actions.sync, bool(self.sync_rows()), "Choose two or more photos in the grid"
        )
        self.paste_button.setEnabled(copied is not None)

    def copy_settings(self):
        """Keep some sections of the edit of the photo shown, open or current, to paste
        over other photos."""
        photo_id = self.settings_source()
        if photo_id is None:
            return
        dialog = CopyDialog(parent=self.parent())
        if dialog.exec() != CopyDialog.DialogCode.Accepted:
            return
        sections, name = dialog.sections(), photo_name(self.service, photo_id)
        try:
            if self.nav.page() == _DEVELOP:
                # The edit as it is now, saved or not
                document = self.session.settings_document(sections)
            else:
                document = self.service.settings_document(photo_id, sections)
        except NewerParams as error:
            QMessageBox.warning(
                self.parent(),
                "Copy Settings",
                f"The edit of {name} was made by a newer Lumina ({error}).",
            )
            return
        self.service.copy_settings(document, name, sections)
        self.messages.showMessage(f"Copied settings of {name}", 5000)

    def paste_settings(self):
        """Put the copied settings over the edit of the photo open, shown, or the photos
        chosen in the grid."""
        copied = self.service.copied
        if copied is None:
            return
        if self.nav.page() == _DEVELOP:
            self.session.paste_settings(copied.document)
            self.messages.showMessage(f"Pasted settings of {copied.name}", 5000)
            return
        ids = [self.model.photo_id(row) for row in self.nav.chosen_rows()]
        if not ids:
            return
        if len(ids) > 1:
            answer = QMessageBox.question(
                self.parent(),
                "Paste Settings",
                f"Paste the settings of {copied.name} to {len(ids)} photos?\n\n"
                "They replace the same parts of those photos' edits; the rest of each "
                "edit stays as it is.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.service.paste_settings(ids, on_done=lambda result: self._pasted(result, copied.name))

    def sync_settings(self):
        """Copy sections of the edit of the current photo to the others chosen."""
        rows = self.sync_rows()
        if not rows:
            return
        source = self.model.photo_id(self.nav.current_row())
        name = photo_name(self.service, source)
        dialog = CopyDialog(
            "Sync Settings",
            f"Copy these parts of the edit of {name} to the {len(rows)} other "
            f"photo{'' if len(rows) == 1 else 's'} chosen:",
            "Sync",
            parent=self.parent(),
        )
        if dialog.exec() != CopyDialog.DialogCode.Accepted:
            return
        try:
            document = self.service.settings_document(source, dialog.sections())
        except NewerParams as error:
            QMessageBox.warning(
                self.parent(),
                "Sync Settings",
                f"The edit of {name} was made by a newer Lumina ({error}).",
            )
            return
        ids = [self.model.photo_id(row) for row in rows]
        self.service.paste_settings(
            ids, document, on_done=lambda result: self._pasted(result, name)
        )

    def _pasted(self, result, source_name):
        """Tell of a paste that went to photos in the library."""
        if self.nav.page() == _VIEWER and self.nav.viewed_row() is not None:
            # The photo shown has a new edit
            self.nav.show_photo(self.nav.viewed_row())
        photos = f"{result.done} photo{'' if result.done == 1 else 's'}"
        self.messages.showMessage(f"Settings of {source_name} put on {photos}", 6000)
        if result.failed:
            lines = "\n".join(f"{name}: {why}" for name, why in result.failed)
            QMessageBox.warning(
                self.parent(), "Paste Settings", f"These photos were left as they were:\n\n{lines}"
            )
