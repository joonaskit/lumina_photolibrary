"""Edits: the part of `Library` for it (see db.py)."""

import time

from lumina.core.dbtypes import Preset


class EditsStore:
    # Edits

    def edit(self, photo_id, copy=0):
        """The stored parameters JSON of a photo's edit, or None for no edit."""
        row = self.db.execute(
            "SELECT params FROM edits WHERE photo_id = ? AND copy = ?", (photo_id, copy)
        ).fetchone()
        return row[0] if row is not None else None

    def save_edit(self, photo_id, params_json, copy=0):
        with self.transaction():
            self.db.execute(
                "INSERT INTO edits (photo_id, copy, params, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (photo_id, copy) DO UPDATE SET "
                "params = excluded.params, updated_at = excluded.updated_at",
                (photo_id, copy, params_json, time.time()),
            )
            self._touch([photo_id])

    def save_edits(self, edits):
        """{photo id: parameters JSON, or None for no edit} as the photos' own edits, in
        one transaction: all of them or none."""
        now = time.time()
        with self.transaction():
            for photo_id, params_json in edits.items():
                if params_json is None:
                    self.db.execute(
                        "DELETE FROM edits WHERE photo_id = ? AND copy = 0", (photo_id,)
                    )
                    continue
                self.db.execute(
                    "INSERT INTO edits (photo_id, copy, params, updated_at) VALUES (?, 0, ?, ?) "
                    "ON CONFLICT (photo_id, copy) DO UPDATE SET "
                    "params = excluded.params, updated_at = excluded.updated_at",
                    (photo_id, params_json, now),
                )
            self._touch(edits)

    def delete_edit(self, photo_id, copy=0):
        """Back to the original. The photo's own edit and its copies are separate."""
        with self.transaction():
            self.db.execute("DELETE FROM edits WHERE photo_id = ? AND copy = ?", (photo_id, copy))
            self._touch([photo_id])

    def copies(self, photo_id):
        """The numbers of a photo's edits: 0 for its own, then its virtual copies."""
        rows = self.db.execute(
            "SELECT copy FROM edits WHERE photo_id = ? ORDER BY copy", (photo_id,)
        )
        return [row[0] for row in rows]

    def new_copy(self, photo_id, params_json):
        """A virtual copy of a photo with these parameters. Returns its number."""
        return self.new_copies({photo_id: params_json})[photo_id]

    def new_copies(self, params_by_photo):
        """A virtual copy of each photo {photo id: parameters JSON}, all or none. Returns
        {photo id: the copy's number}."""
        numbers = {}
        with self.transaction():
            for photo_id, params_json in params_by_photo.items():
                row = self.db.execute(
                    "SELECT COALESCE(MAX(copy), 0) + 1 FROM edits WHERE photo_id = ?", (photo_id,)
                ).fetchone()
                self.db.execute(
                    "INSERT INTO edits (photo_id, copy, params, updated_at) VALUES (?, ?, ?, ?)",
                    (photo_id, row[0], params_json, time.time()),
                )
                numbers[photo_id] = row[0]
            self._touch(list(params_by_photo))
        return numbers

    def presets(self):
        """[Preset] by name."""
        rows = self.db.execute("SELECT name, params FROM presets ORDER BY name COLLATE NOCASE")
        return [Preset(*row) for row in rows]

    def preset(self, name):
        """The parameters JSON of the preset `name`, or None."""
        row = self.db.execute("SELECT params FROM presets WHERE name = ?", (name,)).fetchone()
        return row[0] if row is not None else None

    def save_preset(self, name, params_json):
        with self.transaction():
            self.db.execute(
                "INSERT INTO presets (name, params, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET params = excluded.params",
                (name, params_json, time.time()),
            )

    def delete_preset(self, name):
        with self.transaction():
            self.db.execute("DELETE FROM presets WHERE name = ?", (name,))
