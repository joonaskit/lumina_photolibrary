"""Collections: the part of `Library` for it (see db.py)."""

import sqlite3
import time

from lumina.core.dbtypes import Collection


class CollectionsStore:
    # Collections

    def collections(self):
        """Every collection, by name, manual ones with their photo counts."""
        rows = self.db.execute(
            "SELECT c.id, c.name, c.query, "
            "(SELECT COUNT(*) FROM collection_photos m WHERE m.collection_id = c.id) "
            "FROM collections c ORDER BY c.name COLLATE NOCASE"
        )
        return [Collection(r[0], r[1], r[2], None if r[2] is not None else r[3]) for r in rows]

    def add_collection(self, name, query=None):
        """A new collection, smart when it has a `query` (JSON). Returns its id; a
        ValueError when the name is empty or another collection has it."""
        name = " ".join(name.split())
        if not name:
            raise ValueError("A collection needs a name")
        try:
            with self.transaction():
                cursor = self.db.execute(
                    "INSERT INTO collections (name, query, created_at) VALUES (?, ?, ?)",
                    (name, query, time.time()),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"There is a collection called {name} already") from error
        return cursor.lastrowid

    def rename_collection(self, collection_id, name):
        name = " ".join(name.split())
        if not name:
            raise ValueError("A collection needs a name")
        try:
            with self.transaction():
                self.db.execute(
                    "UPDATE collections SET name = ? WHERE id = ?", (name, collection_id)
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"There is a collection called {name} already") from error

    def set_collection_query(self, collection_id, query):
        """Change a smart collection's saved filter (JSON)."""
        with self.transaction():
            self.db.execute(
                "UPDATE collections SET query = ? WHERE id = ? AND query IS NOT NULL",
                (query, collection_id),
            )

    def delete_collection(self, collection_id):
        """Forget a collection; its photos stay in the library."""
        with self.transaction():
            self.db.execute("DELETE FROM collections WHERE id = ?", (collection_id,))

    def add_to_collection(self, collection_id, photo_ids):
        """Put photos in a manual collection (those in it already stay). Returns how many
        were added."""
        now = time.time()
        with self.transaction():
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO collection_photos (collection_id, photo_id, added_at) "
                "SELECT c.id, p.id, ? FROM collections c, photos p "
                "WHERE c.id = ? AND c.query IS NULL AND p.id = ?",
                [(now, collection_id, photo) for photo in photo_ids],
            )
            return self.db.total_changes - before

    def remove_from_collection(self, collection_id, photo_ids):
        with self.transaction():
            self.db.executemany(
                "DELETE FROM collection_photos WHERE collection_id = ? AND photo_id = ?",
                [(collection_id, photo) for photo in photo_ids],
            )

    def collection_photo_ids(self, collection_id):
        return {
            row[0]
            for row in self.db.execute(
                "SELECT photo_id FROM collection_photos WHERE collection_id = ?",
                (collection_id,),
            )
        }
