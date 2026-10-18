"""Collections: manual ones the photos are put in, smart ones that keep the filters and
the search, and the sidebar's list of them."""

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QInputDialog, QMessageBox

from lumina.core.smart import SmartQuery
from lumina.qt_models.library_model import TEXT
from lumina.ui.window_common import _VIEWER


class Collecting(QObject):
    def __init__(
        self, nav, model, service, browser, searching, sidebar, filters, messages, parent=None
    ):
        """`browser` is the Browser, `searching` the Searching; `messages` is the window's
        status bar. The parent is the window, which the dialogs are over."""
        super().__init__(parent)
        self.nav = nav
        self.model = model
        self.service = service
        self.browser = browser
        self.searching = searching
        self.sidebar = sidebar
        self.filters = filters
        self.messages = messages
        service.collections_changed.connect(self._on_collections_changed)

    def on_collection_selected(self, collection_id, smart):
        self.browser.leave_folders()
        if smart:
            self._apply_smart(collection_id)
        self.browser.apply_filter()
        if self.nav.page() == _VIEWER:
            self.nav.show_grid()

    def _on_collections_changed(self, collection_id):
        chosen = self.sidebar.current_collection()
        self.sidebar.set_collections(self.service.collections())
        if chosen is not None and self.sidebar.current_collection() is None:
            # The one shown was deleted
            self.sidebar.select(None)
        elif chosen is not None and chosen[0] == collection_id and not chosen[1]:
            self.model.refresh()
        self.browser.update_title()

    def add_menu(self, menu):
        """Add to Collection, and Remove from This Collection when one is shown, to `menu`."""
        manual = [c for c in self.service.collections() if c.query is None]
        sub = menu.addMenu("Add to Collection")
        for collection in manual:
            sub.addAction(
                collection.name, lambda _on=False, c=collection.id: self.add_to_collection(c)
            )
        if manual:
            sub.addSeparator()
        sub.addAction("New Collection…", lambda: self.new_collection(add_chosen=True))
        sub.setEnabled(bool(self.nav.chosen_rows()))
        shown = self.sidebar.current_collection()
        if shown is not None and not shown[1]:
            action = menu.addAction(
                "Remove from This Collection", lambda: self.remove_from_collection(shown[0])
            )
            action.setEnabled(bool(self.nav.chosen_rows()))

    def _ask_name(self, title, text=""):
        name, ok = QInputDialog.getText(self.parent(), title, "Name:", text=text)
        return name.strip() if ok and name.strip() else None

    def _chosen_ids(self):
        return [self.model.photo_id(row) for row in self.nav.chosen_rows()]

    def new_collection(self, add_chosen=False):
        """Make a manual collection, asking its name; the photos chosen go into it."""
        name = self._ask_name("New Collection")
        if name is None:
            return
        # The photos chosen now, not when the collection is made
        ids = self._chosen_ids() if add_chosen else []

        def made(collection_id):
            if ids:
                self.add_ids_to_collection(collection_id, ids)

        self.service.add_collection(
            name, on_done=made, on_error=lambda error: self._name_refused("New Collection", error)
        )

    def _name_refused(self, title, error):
        QMessageBox.warning(self.parent(), title, str(error))

    def add_to_collection(self, collection_id):
        self.add_ids_to_collection(collection_id, self._chosen_ids())

    def add_ids_to_collection(self, collection_id, photo_ids):
        def added(count):
            found = (c.name for c in self.service.collections() if c.id == collection_id)
            name = next(found, "")
            left = len(photo_ids) - count
            text = f"Added {count} photo{'' if count == 1 else 's'} to {name}"
            if left:
                text += f" ({left} already there)"
            self.messages.showMessage(text, 5000)

        self.service.add_to_collection(collection_id, photo_ids, on_done=added)

    def remove_from_collection(self, collection_id):
        ids = self._chosen_ids()
        if ids:
            self.service.remove_from_collection(collection_id, ids)

    def rename_collection(self, collection_id):
        current = next((c.name for c in self.service.collections() if c.id == collection_id), "")
        name = self._ask_name("Rename Collection", current)
        if name is None or name == current:
            return
        self.service.rename_collection(
            collection_id,
            name,
            on_error=lambda error: self._name_refused("Rename Collection", error),
        )

    def delete_collection(self, collection_id):
        found = next((c for c in self.service.collections() if c.id == collection_id), None)
        if found is None:
            return
        answer = QMessageBox.question(
            self.parent(),
            "Delete Collection",
            f"Delete the collection {found.name}? The photos stay in the library.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.service.delete_collection(collection_id)

    # Smart collections

    def _current_query(self):
        """The filters now set, as a smart collection's query."""
        year, camera, lens = self.filters.values()
        search = self.model.search
        return SmartQuery(
            year=year,
            model=camera,
            lens=lens,
            flag=self.filters.flag_value(),
            rating=self.filters.rating_value(),
            edit=self.filters.edit_value(),
            keyword=self.filters.keyword_value(),
            text=search.query if search is not None and search.kind == TEXT else None,
        )

    def new_smart_collection(self):
        """Save the filters (and the search) now set as a smart collection."""
        query = self._current_query()
        if query.is_empty():
            return
        name = self._ask_name("New Smart Collection")
        if name is None:
            return
        self.service.add_collection(
            name,
            query.to_json(),
            on_done=self.sidebar.select_collection,
            on_error=lambda error: self._name_refused("New Smart Collection", error),
        )

    def update_smart_collection(self, collection_id):
        query = self._current_query()
        if query.is_empty():
            return
        self.service.set_collection_query(collection_id, query.to_json())
        self.messages.showMessage("Smart collection saved with the filters now set", 4000)

    def _apply_smart(self, collection_id):
        found = next((c for c in self.service.collections() if c.id == collection_id), None)
        if found is None or found.query is None:
            return
        query = SmartQuery.from_json(found.query)
        self.filters.apply(
            {
                "year": query.year,
                "camera": query.model,
                "lens": query.lens,
                "flag": query.flag,
                "rating": query.rating,
                "edit": query.edit,
                "keyword": query.keyword,
            }
        )
        searching = self.searching
        if query.text and searching.search is not None and searching.model_ready():
            searching.field.set_text(query.text)
        elif self.model.is_searching():
            searching.end_search()
