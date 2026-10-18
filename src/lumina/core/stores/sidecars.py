"""Sidecars: the part of `Library` for it (see db.py)."""

from lumina.core import xmp
from lumina.core.dbtypes import FLAGS, RATINGS, UNFLAGGED


class SidecarsStore:
    # Sidecars

    def sidecar_pending(self, after=0, limit=100):
        """[(id, volume key, path, available, changed_at)] of photos whose sidecar is out
        of date (never written, or behind their last change), by id, those after id `after`."""
        rows = self.db.execute(
            "SELECT p.id, v.key, p.path, p.available, p.changed_at FROM photos p "
            "JOIN volumes v ON v.id = p.volume_id WHERE p.id > ? AND p.changed_at IS NOT NULL "
            "AND (p.sidecar_at IS NULL OR p.sidecar_at < p.changed_at) ORDER BY p.id LIMIT ?",
            (after, limit),
        )
        return [(r[0], r[1], r[2], bool(r[3]), r[4]) for r in rows]

    def count_sidecars_pending(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM photos WHERE changed_at IS NOT NULL "
            "AND (sidecar_at IS NULL OR sidecar_at < changed_at)"
        ).fetchone()[0]

    def sidecar_state(self, photo_id):
        """What a photo's sidecar holds (xmp.SidecarState), or None for no such photo."""
        row = self.db.execute(
            "SELECT flag, rating, changed_at FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        if row is None:
            return None
        paths = self.keyword_paths()
        found = self.photo_keywords([photo_id])[photo_id]
        edits = self.db.execute(
            "SELECT copy, params FROM edits WHERE photo_id = ? ORDER BY copy", (photo_id,)
        )
        return xmp.SidecarState(
            updated=row[2] or 0.0,
            flag=row[0],
            rating=row[1],
            keywords=tuple(sorted((paths[k] for k in found if k in paths), key=str.casefold)),
            edits=tuple((r[0], r[1]) for r in edits),
            stack=self._stack_of(photo_id),
        )

    def sidecar_written(self, photo_id, changed_at, mtime_ns):
        """A sidecar was written holding the state as of `changed_at`; its file's time."""
        with self.transaction():
            self.db.execute(
                "UPDATE photos SET sidecar_at = ?, sidecar_mtime_ns = ? WHERE id = ?",
                (changed_at, mtime_ns, photo_id),
            )

    def sidecar_candidates(self, folder_id):
        """[(id, path, sidecar_mtime_ns)] of the available photos of a folder: where to
        look for a sidecar that changed."""
        rows = self.db.execute(
            "SELECT id, path, sidecar_mtime_ns FROM photos WHERE folder_id = ? AND available = 1",
            (folder_id,),
        )
        return [(r[0], r[1], r[2]) for r in rows]

    def forget_sidecar(self, photo_id):
        """Its sidecar is gone: a photo with something to keep gets a new one."""
        with self.transaction():
            self.db.execute(
                "UPDATE photos SET sidecar_at = NULL, sidecar_mtime_ns = NULL WHERE id = ?",
                (photo_id,),
            )

    def note_sidecar(self, photo_id, mtime_ns):
        """A sidecar file that is not read (another program's) is as it was at `mtime_ns`."""
        with self.transaction():
            self.db.execute(
                "UPDATE photos SET sidecar_mtime_ns = ? WHERE id = ?", (mtime_ns, photo_id)
            )

    def apply_sidecar(self, photo_id, state, mtime_ns):
        """Take a sidecar's state (xmp.SidecarState) in when it is newer than the library's
        own change of the photo; else the library's stays, and is written to the sidecar
        again. Returns whether the sidecar was used.

        Both sides changing since they last agreed is a conflict, and the newer whole
        state wins (compared by time, from whichever machine wrote it). The edits it
        replaces are not lost then: they stay as virtual copies after the sidecar's."""
        with self.transaction():
            row = self.db.execute(
                "SELECT changed_at, sidecar_at FROM photos WHERE id = ?", (photo_id,)
            ).fetchone()
            if row is None:
                return False
            used = state.updated > (row[0] or 0.0)
            if used:
                conflict = row[0] is not None and row[0] > (row[1] or 0.0)
                self._take_sidecar(photo_id, state, keep_replaced=conflict)
            self.db.execute(
                "UPDATE photos SET sidecar_at = ?, sidecar_mtime_ns = ? WHERE id = ?",
                (state.updated, mtime_ns, photo_id),
            )
        return used

    def _take_sidecar(self, photo_id, state, keep_replaced=False):
        flag = state.flag if state.flag in FLAGS else UNFLAGGED
        rating = state.rating if state.rating in RATINGS else 0
        self.db.execute(
            "UPDATE photos SET flag = ?, rating = ?, changed_at = ?, keywords_read = 1 "
            "WHERE id = ?",
            (flag, rating, state.updated, photo_id),
        )
        self.db.execute("DELETE FROM photo_keywords WHERE photo_id = ?", (photo_id,))
        for path in state.keywords:
            self.db.execute(
                "INSERT OR IGNORE INTO photo_keywords (photo_id, keyword_id) VALUES (?, ?)",
                (photo_id, self._keyword(path)),
            )
        replaced = self.db.execute(
            "SELECT copy, params FROM edits WHERE photo_id = ? ORDER BY copy", (photo_id,)
        ).fetchall()
        self.db.execute("DELETE FROM edits WHERE photo_id = ?", (photo_id,))
        self.db.executemany(
            "INSERT OR REPLACE INTO edits (photo_id, copy, params, updated_at) VALUES (?, ?, ?, ?)",
            [(photo_id, copy, params, state.updated) for copy, params in state.edits],
        )
        if keep_replaced:
            taken = set(state.edits)
            number = max((copy for copy, _params in state.edits), default=0)
            for copy, params in replaced:
                if (copy, params) not in taken:
                    number += 1
                    self.db.execute(
                        "INSERT INTO edits (photo_id, copy, params, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (photo_id, number, params, state.updated),
                    )
        if state.stack is None:
            self.db.execute(
                "UPDATE photos SET stack_id = NULL, stack_position = 0 WHERE id = ?", (photo_id,)
            )
        else:
            key, position = state.stack
            self.db.execute("INSERT OR IGNORE INTO stacks (key) VALUES (?)", (key,))
            self.db.execute(
                "UPDATE photos SET stack_id = (SELECT id FROM stacks WHERE key = ?), "
                "stack_position = ? WHERE id = ?",
                (key, position, photo_id),
            )
        # Not tidied: the stack's other photos' sidecars are read one by one, and a stack
        # of one photo meanwhile is a stack to be (the grid shows it as no stack)
