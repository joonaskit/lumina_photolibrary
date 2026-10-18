"""collecting: the part of `MainWindow` for it (see main_window.py)."""

from PySide6.QtWidgets import QInputDialog, QMessageBox

from lumina.qt_models.library_model import TEXT, SmartQuery


class CollectingMixin:
    # Collections

    def _on_collections_changed(self, collection_id):
        chosen = self.sidebar.current_collection()
        self.sidebar.set_collections(self.service.collections())
        if chosen is not None and self.sidebar.current_collection() is None:
            # The one shown was deleted
            self.sidebar.select(None)
        elif chosen is not None and chosen[0] == collection_id and not chosen[1]:
            self.model.refresh()
        self._update_title()

    def _add_collection_menu(self, menu):
        manual = [c for c in self.service.collections() if c.query is None]
        sub = menu.addMenu("Add to Collection")
        for collection in manual:
            sub.addAction(
                collection.name, lambda _on=False, c=collection.id: self.add_to_collection(c)
            )
        if manual:
            sub.addSeparator()
        sub.addAction("New Collection…", lambda: self.new_collection(add_chosen=True))
        sub.setEnabled(bool(self._flag_rows()))
        shown = self.sidebar.current_collection()
        if shown is not None and not shown[1]:
            action = menu.addAction(
                "Remove from This Collection", lambda: self.remove_from_collection(shown[0])
            )
            action.setEnabled(bool(self._flag_rows()))

    def _ask_name(self, title, text=""):
        name, ok = QInputDialog.getText(self, title, "Name:", text=text)
        return name.strip() if ok and name.strip() else None

    def new_collection(self, add_chosen=False):
        """Make a manual collection, asking its name; the photos chosen go into it."""
        name = self._ask_name("New Collection")
        if name is None:
            return None
        try:
            made = self.service.add_collection(name)
        except ValueError as error:
            QMessageBox.warning(self, "New Collection", str(error))
            return None
        if add_chosen:
            self.add_to_collection(made)
        return made

    def add_to_collection(self, collection_id):
        rows = self._flag_rows()
        self.add_ids_to_collection(collection_id, [self.model.photo_id(row) for row in rows])

    def add_ids_to_collection(self, collection_id, photo_ids):
        added = self.service.add_to_collection(collection_id, photo_ids)
        name = next((c.name for c in self.service.collections() if c.id == collection_id), "")
        left = len(photo_ids) - added
        text = f"Added {added} photo{'' if added == 1 else 's'} to {name}"
        if left:
            text += f" ({left} already there)"
        self.statusBar().showMessage(text, 5000)

    def remove_from_collection(self, collection_id):
        ids = [self.model.photo_id(row) for row in self._flag_rows()]
        if ids:
            self.service.remove_from_collection(collection_id, ids)

    def rename_collection(self, collection_id):
        current = next((c.name for c in self.service.collections() if c.id == collection_id), "")
        name = self._ask_name("Rename Collection", current)
        if name is None or name == current:
            return
        try:
            self.service.rename_collection(collection_id, name)
        except ValueError as error:
            QMessageBox.warning(self, "Rename Collection", str(error))

    def delete_collection(self, collection_id):
        found = next((c for c in self.service.collections() if c.id == collection_id), None)
        if found is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete Collection",
            f"Delete the collection {found.name}? The photos stay in the library.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.service.delete_collection(collection_id)

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
        try:
            made = self.service.add_collection(name, query.to_json())
        except ValueError as error:
            QMessageBox.warning(self, "New Smart Collection", str(error))
            return
        self.sidebar.select_collection(made)

    def update_smart_collection(self, collection_id):
        query = self._current_query()
        if query.is_empty():
            return
        self.service.set_collection_query(collection_id, query.to_json())
        self.statusBar().showMessage("Smart collection saved with the filters now set", 4000)

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
        if query.text and self.search is not None and self._model_ready():
            self.search_field.set_text(query.text)
        elif self.model.is_searching():
            self.end_search()
