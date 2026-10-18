"""Tether: the part of `Library` for it (see db.py)."""

import time

from lumina.core.dbtypes import _TETHER_ITEM, COPIED, TETHER_PENDING, TetherSession, _tether_item


class TetherStore:
    # Shooting tethered

    def add_tether_session(self, name, volume_key, folder, keep, collection_id=None):
        """A camera session whose frames go in `folder` (from the root) of the volume
        `volume_key`. Returns its id."""
        volume = self.volume_id(volume_key)
        with self.transaction():
            return self.db.execute(
                "INSERT INTO tether_sessions (name, volume_id, folder, keep, collection_id, "
                "started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, volume, folder.strip("/"), keep, collection_id, time.time()),
            ).lastrowid

    def start_tether_session(self, name, volume_key, folder, keep):
        """A camera session as `add_tether_session`, in the manual collection `name`, made
        when there is none (none when a smart collection has the name), in one transaction.
        Returns (its TetherSession, the id of the collection made or None)."""
        with self.transaction():
            row = self.db.execute(
                "SELECT id, query FROM collections WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            made = None
            if row is None:
                collection = made = self.add_collection(name)
            else:
                collection = row[0] if row[1] is None else None
            session_id = self.add_tether_session(name, volume_key, folder, keep, collection)
            return self.tether_session(session_id), made

    def tether_session(self, session_id):
        row = self.db.execute(
            "SELECT s.id, s.name, v.key, s.folder, s.keep, s.collection_id, s.started_at "
            "FROM tether_sessions s JOIN volumes v ON v.id = s.volume_id WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        return TetherSession(*row) if row is not None else None

    def unfinished_tether_sessions(self):
        """The TetherSessions with frames still to fetch: a crash or an unplug cut them
        short."""
        rows = self.db.execute(
            "SELECT s.id, s.name, v.key, s.folder, s.keep, s.collection_id, s.started_at "
            "FROM tether_sessions s JOIN volumes v ON v.id = s.volume_id WHERE EXISTS "
            "(SELECT 1 FROM tether_items i WHERE i.session_id = s.id AND i.state = ?) "
            "ORDER BY s.id",
            (TETHER_PENDING,),
        )
        return [TetherSession(*row) for row in rows]

    def add_tether_item(self, session_id, camera, camera_folder, camera_name, on_card):
        """A frame the camera told of, before it is fetched. Returns its id."""
        with self.transaction():
            return self.db.execute(
                "INSERT INTO tether_items (session_id, camera, camera_folder, camera_name, "
                "on_card, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, camera, camera_folder, camera_name, int(on_card), time.time()),
            ).lastrowid

    def tether_item(self, item_id):
        row = self.db.execute(f"{_TETHER_ITEM} WHERE id = ?", (item_id,)).fetchone()
        return _tether_item(row) if row is not None else None

    def tether_items(self, session_id, state=None):
        """A session's TetherItems, in order; only those in `state`, with one."""
        where, args = (
            ("AND state = ?", (session_id, state)) if state is not None else ("", (session_id,))
        )
        rows = self.db.execute(f"{_TETHER_ITEM} WHERE session_id = ? {where} ORDER BY id", args)
        return [_tether_item(row) for row in rows]

    def pending_tether_items(self, camera):
        """The frames of the camera `camera` (its serial number) still to fetch, in order."""
        rows = self.db.execute(
            f"{_TETHER_ITEM} WHERE camera = ? AND state = ? ORDER BY id", (camera, TETHER_PENDING)
        )
        return [_tether_item(row) for row in rows]

    def set_tether_item(self, item_id, state, name=None, message=None):
        """A frame saved (TETHER_SAVED, as `name` in its session's folder), not saved
        (TETHER_FAILED, with why), or to fetch again (TETHER_PENDING)."""
        with self.transaction():
            self.db.execute(
                "UPDATE tether_items SET state = ?, name = ?, message = ? WHERE id = ?",
                (state, name, message, item_id),
            )

    def last_import(self):
        """The id of the newest import job that copied a photo, or None. Its photos are
        what `Filter(import_job=...)` shows; the ones already in the library."""
        row = self.db.execute(
            "SELECT j.id FROM jobs j WHERE j.kind = 'import' AND EXISTS "
            "(SELECT 1 FROM import_items i WHERE i.job_id = j.id AND i.state = ?) "
            "ORDER BY j.id DESC LIMIT 1",
            (COPIED,),
        ).fetchone()
        return row[0] if row is not None else None

    def imported(self, content_ids):
        """Those of `content_ids` that are in the library or were imported before: a photo
        imported and later removed from its folder is still not new."""
        ids = [cid for cid in set(content_ids) if cid]
        found = set()
        for start in range(0, len(ids), 500):
            part = ids[start : start + 500]
            marks = ", ".join("?" * len(part))
            rows = self.db.execute(
                f"SELECT content_id FROM photos WHERE content_id IN ({marks}) "
                f"UNION SELECT content_id FROM import_items WHERE content_id IN ({marks}) "
                "AND state = ?",
                (*part, *part, COPIED),
            )
            found.update(row[0] for row in rows)
        return found
