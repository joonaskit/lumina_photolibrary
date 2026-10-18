"""Stacks: the part of `Library` for it (see db.py)."""

import os


class StacksStore:
    # Stacks

    def _stack_of(self, photo_id):
        row = self.db.execute(
            "SELECT s.key, p.stack_position FROM photos p JOIN stacks s ON s.id = p.stack_id "
            "WHERE p.id = ?",
            (photo_id,),
        ).fetchone()
        return (row[0], row[1]) if row is not None else None

    def _tidy_stacks(self):
        """A stack with one photo, or none, is no stack. In the caller's transaction."""
        lone = [
            row[0]
            for row in self.db.execute(
                "SELECT s.id FROM stacks s "
                "WHERE (SELECT COUNT(*) FROM photos p WHERE p.stack_id = s.id) < 2"
            )
        ]
        if not lone:
            return
        marks = ", ".join("?" * len(lone))
        # Their one photo's sidecar still names the stack: it is out of date
        left = [
            row[0]
            for row in self.db.execute(f"SELECT id FROM photos WHERE stack_id IN ({marks})", lone)
        ]
        self.db.execute(
            f"UPDATE photos SET stack_id = NULL, stack_position = 0 WHERE stack_id IN ({marks})",
            lone,
        )
        self.db.execute(f"DELETE FROM stacks WHERE id IN ({marks})", lone)
        self._touch(left)

    def _stack_members(self, stack_ids):
        stack_ids = list(stack_ids)
        marks = ", ".join("?" * len(stack_ids))
        rows = self.db.execute(
            f"SELECT id FROM photos WHERE stack_id IN ({marks})", stack_ids
        ).fetchall()
        return [row[0] for row in rows]

    def _stacks_of(self, photo_ids):
        ids = list(photo_ids)
        found = set()
        for start in range(0, len(ids), 500):
            part = ids[start : start + 500]
            marks = ", ".join("?" * len(part))
            rows = self.db.execute(
                f"SELECT DISTINCT stack_id FROM photos WHERE id IN ({marks}) "
                "AND stack_id IS NOT NULL",
                part,
            )
            found.update(row[0] for row in rows)
        return found

    def make_stack(self, photo_ids):
        """Stack these photos, the first on top and in the order given, collapsed. A photo
        that was in a stack leaves it (what is left of that one is a stack still, or none
        when fewer than two remain). Fewer than two photos make none. Returns the stack's
        id, or None."""
        ids = list(dict.fromkeys(photo_ids))
        if len(ids) < 2:
            return None
        with self.transaction():
            old = self._stacks_of(ids)
            affected = set(ids) | (set(self._stack_members(old)) if old else set())
            cursor = self.db.execute(
                "INSERT INTO stacks (key, collapsed) VALUES (?, 1)", (os.urandom(8).hex(),)
            )
            stack = cursor.lastrowid
            self.db.executemany(
                "UPDATE photos SET stack_id = ?, stack_position = ? WHERE id = ?",
                [(stack, place, photo) for place, photo in enumerate(ids)],
            )
            self._tidy_stacks()
            self._touch(affected)
        return stack

    def unstack(self, photo_ids):
        """Dissolve the stacks these photos are in: every member is on its own again."""
        with self.transaction():
            stacks = self._stacks_of(photo_ids)
            if not stacks:
                return
            members = self._stack_members(stacks)
            marks = ", ".join("?" * len(stacks))
            self.db.execute(
                f"UPDATE photos SET stack_id = NULL, stack_position = 0 "
                f"WHERE stack_id IN ({marks})",
                list(stacks),
            )
            self.db.execute(f"DELETE FROM stacks WHERE id IN ({marks})", list(stacks))
            self._touch(members)

    def leave_stack(self, photo_ids):
        """Take these photos out of their stacks. The others stay stacked, in their order
        (the next one is the top when the top left), unless fewer than two are left."""
        ids = list(dict.fromkeys(photo_ids))
        with self.transaction():
            leaving = [
                row[0]
                for start in range(0, len(ids), 500)
                for row in self.db.execute(
                    "SELECT id FROM photos WHERE stack_id IS NOT NULL AND id IN "
                    f"({', '.join('?' * len(ids[start : start + 500]))})",
                    ids[start : start + 500],
                )
            ]
            self.db.executemany(
                "UPDATE photos SET stack_id = NULL, stack_position = 0 WHERE id = ?",
                [(photo,) for photo in leaving],
            )
            self._tidy_stacks()
            self._touch(leaving)

    def set_stack_top(self, photo_id):
        """Make this photo the top of its stack; the others keep their order."""
        with self.transaction():
            row = self.db.execute(
                "SELECT stack_id FROM photos WHERE id = ?", (photo_id,)
            ).fetchone()
            if row is None or row[0] is None:
                return
            order = [
                r[0]
                for r in self.db.execute(
                    "SELECT id FROM photos WHERE stack_id = ? AND id != ? "
                    "ORDER BY stack_position, id",
                    (row[0], photo_id),
                )
            ]
            self.db.executemany(
                "UPDATE photos SET stack_position = ? WHERE id = ?",
                [(place, photo) for place, photo in enumerate([photo_id, *order])],
            )
            self._touch([photo_id, *order])

    def set_stacks_collapsed(self, stack_ids, collapsed):
        """Collapse or expand stacks. How the library is shown, not something to keep with
        the photos: their sidecars are not rewritten."""
        stack_ids = list(stack_ids)
        if not stack_ids:
            return
        marks = ", ".join("?" * len(stack_ids))
        with self.transaction():
            self.db.execute(
                f"UPDATE stacks SET collapsed = ? WHERE id IN ({marks})",
                [int(collapsed), *stack_ids],
            )

    def stack_members(self, stack_id):
        """The ids of a stack's photos, the top first."""
        rows = self.db.execute(
            "SELECT id FROM photos WHERE stack_id = ? ORDER BY stack_position, id", (stack_id,)
        )
        return [row[0] for row in rows]
