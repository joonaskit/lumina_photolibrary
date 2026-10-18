"""Keywords: the part of `Library` for it (see db.py)."""

from lumina.core import keywords


class KeywordsStore:
    # Keywords

    def keyword_paths(self):
        """{id: path} of every keyword."""
        return {row[0]: row[1] for row in self.db.execute("SELECT id, path FROM keywords")}

    def keyword_counts(self):
        """{id: photos given the keyword or one below it} (one given two keywords in a
        branch counts once there); a keyword nothing is given is left out."""
        # Those below are the paths from "<path> > " up to "<path> >!": the next character
        # after the separator's last, so the range holds nothing else (and the index serves it)
        return {
            row[0]: row[1]
            for row in self.db.execute(
                "SELECT a.id, COUNT(DISTINCT m.photo_id) FROM keywords a "
                "JOIN keywords b ON b.path = a.path OR "
                "(b.path >= a.path || ?1 AND b.path < a.path || ?2) "
                "JOIN photo_keywords m ON m.keyword_id = b.id GROUP BY a.id",
                (keywords.SEPARATOR, keywords.SEPARATOR[:-1] + "!"),
            )
        }

    def keyword_list(self):
        """[(id, path, count)] of every keyword, by path; `count` is the photos given it
        or one below it."""
        counts = self.keyword_counts()
        return sorted(
            (
                (keyword, path, counts.get(keyword, 0))
                for keyword, path in self.keyword_paths().items()
            ),
            key=lambda item: [name.casefold() for name in keywords.levels(item[1])],
        )

    def ensure_keywords(self, paths):
        """{path: id} of the keywords `paths` (parsed by `keywords.parse`), made when they
        are new, with the levels above them. The path is as first written."""
        with self.transaction():
            return {path: self._keyword(path) for path in paths}

    def _keyword(self, path):
        # Each level is spelt as the keyword that is there already spells it, so that what
        # is below a keyword always starts with its path
        names = keywords.levels(path)
        found = None
        for number in range(1, len(names) + 1):
            above = keywords.SEPARATOR.join([*([found] if found else []), names[number - 1]])
            self.db.execute("INSERT OR IGNORE INTO keywords (path) VALUES (?)", (above,))
            found = self.db.execute(
                "SELECT path FROM keywords WHERE path = ?", (above,)
            ).fetchone()[0]
        return self.db.execute("SELECT id FROM keywords WHERE path = ?", (found,)).fetchone()[0]

    def photo_keywords(self, ids):
        """{photo id: ids of its keywords, sorted} of those of `ids` in the library."""
        ids = list(ids)
        found = {photo: () for photo in self._marks("id", ids)}
        for start in range(0, len(ids), 500):
            part = ids[start : start + 500]
            marks = ", ".join("?" * len(part))
            rows = self.db.execute(
                "SELECT photo_id, keyword_id FROM photo_keywords "
                f"WHERE photo_id IN ({marks}) ORDER BY keyword_id",
                part,
            )
            for photo, keyword in rows:
                found[photo] = (*found[photo], keyword)
        return found

    def set_photo_keywords(self, values):
        """{photo id: ids of the keywords it has}, replacing what each had, in one
        transaction."""
        with self.transaction():
            for photo, ids in values.items():
                self.db.execute("DELETE FROM photo_keywords WHERE photo_id = ?", (photo,))
                self.db.executemany(
                    "INSERT OR IGNORE INTO photo_keywords (photo_id, keyword_id) "
                    "SELECT ?, id FROM keywords WHERE id = ?",
                    [(photo, keyword) for keyword in ids],
                )
            self._touch(values)

    def rename_keyword(self, path, new_path):
        """Make keyword `path` and those below it `new_path` and below. Returns False when
        `path` is not a keyword; a ValueError when `new_path` is another keyword's. Photos
        keep their keywords. The levels above `new_path` are made."""
        with self.transaction():
            found = self._keywords_within(path)
            if not found:
                return False
            taken = {other.casefold() for _id, other in self._keywords_within(new_path)}
            moved = {old.casefold() for _id, old in found}
            if taken - moved:
                raise ValueError(f"There is a keyword {new_path} already")
            self._touch_keyword_photos(keyword for keyword, _old in found)
            for keyword, old in found:
                renamed = new_path + old[len(path) :]
                self.db.execute("UPDATE keywords SET path = ? WHERE id = ?", (renamed, keyword))
            for above in keywords.ancestors(new_path):
                self.db.execute("INSERT OR IGNORE INTO keywords (path) VALUES (?)", (above,))
        return True

    def delete_keyword(self, path):
        """Forget keyword `path` and those below it, taking them off every photo. Returns
        how many keywords went."""
        with self.transaction():
            found = self._keywords_within(path)
            self._touch_keyword_photos(i for i, _p in found)
            self.db.executemany("DELETE FROM keywords WHERE id = ?", [(i,) for i, _p in found])
        return len(found)

    def _touch_keyword_photos(self, keyword_ids):
        ids = list(keyword_ids)
        marks = ", ".join("?" * len(ids))
        photos = [
            row[0]
            for row in self.db.execute(
                f"SELECT DISTINCT photo_id FROM photo_keywords WHERE keyword_id IN ({marks})", ids
            )
        ]
        self._touch(photos)

    def _keywords_within(self, path):
        return [
            (row[0], row[1])
            for row in self.db.execute("SELECT id, path FROM keywords")
            if keywords.within(row[1], path)
        ]

    def keyword_ids_within(self, path):
        """The ids of keyword `path` and those below it."""
        return {keyword for keyword, _path in self._keywords_within(path)}

    def _take_in_keywords(self, photo, found):
        # The first time its file is read only
        row = self.db.execute("SELECT keywords_read FROM photos WHERE id = ?", (photo,)).fetchone()
        if row is None or row[0]:
            return
        self.db.execute("UPDATE photos SET keywords_read = 1 WHERE id = ?", (photo,))
        for path in found:
            self.db.execute(
                "INSERT OR IGNORE INTO photo_keywords (photo_id, keyword_id) VALUES (?, ?)",
                (photo, self._keyword(path)),
            )
