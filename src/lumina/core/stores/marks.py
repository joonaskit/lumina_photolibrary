"""Marks: the part of `Library` for it (see db.py)."""

import time

from lumina.core.dbtypes import FLAGS, RATINGS


class MarksStore:
    # Flags and ratings

    def set_flags(self, flags):
        """{photo id: PICKED, UNFLAGGED or REJECTED}, in one transaction."""
        self._set_marks("flag", FLAGS, flags)

    def flags(self, ids):
        """{photo id: flag} of those of `ids` in the library."""
        return self._marks("flag", ids)

    def set_ratings(self, ratings):
        """{photo id: 0 for none, or 1 to 5 stars}, in one transaction."""
        self._set_marks("rating", RATINGS, ratings)

    def ratings(self, ids):
        """{photo id: rating} of those of `ids` in the library."""
        return self._marks("rating", ids)

    def _set_marks(self, column, allowed, values):
        for value in values.values():
            if value not in allowed:
                raise ValueError(f"Not a {column}: {value!r}")
        with self.transaction():
            self.db.executemany(
                f"UPDATE photos SET {column} = ? WHERE id = ?",
                [(value, photo) for photo, value in values.items()],
            )
            self._touch(values)

    def _touch(self, photo_ids):
        """Note that these photos' flag, stars, keywords or edits changed now: their
        sidecars are out of date. In the caller's transaction."""
        now = time.time()
        self.db.executemany(
            "UPDATE photos SET changed_at = ? WHERE id = ?", [(now, photo) for photo in photo_ids]
        )

    def _marks(self, column, ids):
        ids = list(ids)
        found = {}
        # In slices: SQLite has a limit on the number of parameters
        for start in range(0, len(ids), 500):
            part = ids[start : start + 500]
            marks = ", ".join("?" * len(part))
            rows = self.db.execute(f"SELECT id, {column} FROM photos WHERE id IN ({marks})", part)
            found.update((row[0], row[1]) for row in rows)
        return found
