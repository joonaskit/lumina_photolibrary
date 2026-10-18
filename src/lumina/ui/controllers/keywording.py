"""Keywords: giving them to the photos and taking them off, the sidebar's list of them,
and showing only the photos of one."""

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QInputDialog, QMessageBox

from lumina.ui.keywords_dialog import KeywordsDialog


class Keywording(QObject):
    def __init__(self, nav, model, service, sidebar, filters, messages, parent=None):
        """`messages` is the window's status bar; the parent is the window, which the
        dialogs are over."""
        super().__init__(parent)
        self.nav = nav
        self.model = model
        self.service = service
        self.sidebar = sidebar
        self.filters = filters
        self.messages = messages
        service.keywords_changed.connect(self.on_keywords_changed)

    def _changed(self):
        self.nav.chosen_changed.emit()
        self.nav.status_changed.emit()

    def set_keyword_filter(self, path):
        """Show only the photos given keyword `path` or one below it, or all."""
        self.model.set_keyword_filter(path)
        self.sidebar.select_keyword(path)
        self.nav.status_changed.emit()

    def on_keywords_changed(self):
        items = self.service.keyword_items
        self.sidebar.set_keywords(items)
        self.filters.set_keywords([path for _id, path, _count in items])
        self.model.refilter()
        self._changed()

    def edit_keywords(self):
        """Ask which keywords the photos chosen (or shown) get or lose."""
        rows = self.nav.chosen_rows()
        if not rows:
            return
        dialog = KeywordsDialog(self.parent())

        def refresh():
            have = {}
            for row in rows:
                for path in self.model.keywords(row):
                    have[path] = have.get(path, 0) + 1
            names = self.model.name(rows[0]) if len(rows) == 1 else f"{len(rows)} photos"
            known = [path for _id, path, _count in self.service.keyword_items]
            dialog.set_state(f"Keywords of {names}", known, have, len(rows))

        def add(text):
            given = self.model.add_keywords(rows, text.split(","))
            if not given:
                self.messages.showMessage("Nothing to add", 3000)
            refresh()

        def remove(paths):
            self.model.remove_keywords(rows, paths)
            refresh()

        dialog.add_requested.connect(add)
        dialog.remove_requested.connect(remove)
        # Keywords that were new are given once they are made
        self.service.keywords_changed.connect(refresh)
        refresh()
        try:
            dialog.exec()
        finally:
            self.service.keywords_changed.disconnect(refresh)
        self._changed()

    def rename_keyword(self, path):
        names = path.split(" > ")
        text, ok = QInputDialog.getText(
            self.parent(), "Rename Keyword", f"New name for {path}:", text=names[-1]
        )
        new = " > ".join([*names[:-1], text.strip()]) if ok and text.strip() else None
        if new is None or new == path:
            return

        def renamed(_found):
            if self.filters.keyword_value() == path:
                self.filters.set_keyword(new)

        self.service.rename_keyword(
            path,
            new,
            on_done=renamed,
            on_error=lambda error: QMessageBox.warning(self.parent(), "Rename Keyword", str(error)),
        )

    def delete_keyword(self, path):
        answer = QMessageBox.question(
            self.parent(),
            "Delete Keyword",
            f"Take {path}, and any keywords inside it, off every photo? The photos stay.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.service.delete_keyword(path)
