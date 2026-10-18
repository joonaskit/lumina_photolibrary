"""Folders: the part of `Library` for it (see db.py)."""

import time

from lumina.core.dbtypes import Folder
from lumina.core.dbutil import _inside


class FoldersStore:
    # Volumes and folders

    def volume_id(self, key, label=None, mount=None):
        """The id of the volume `key`, made if new; its label and mount kept up to date."""
        with self.transaction():
            self.db.execute(
                "INSERT INTO volumes (key, label, last_mount) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET "
                "label = COALESCE(excluded.label, label), "
                "last_mount = COALESCE(excluded.last_mount, last_mount)",
                (key, label, mount),
            )
        return self.db.execute("SELECT id FROM volumes WHERE key = ?", (key,)).fetchone()[0]

    def folders(self):
        rows = self.db.execute(
            "SELECT f.id, v.key, f.path, v.label, v.last_mount, f.scanned_at "
            "FROM folders f JOIN volumes v ON v.id = f.volume_id ORDER BY f.path, f.id"
        )
        return [Folder(*row) for row in rows]

    def folder(self, folder_id):
        return next((f for f in self.folders() if f.id == folder_id), None)

    def add_folder(self, key, path, label=None, mount=None):
        """Add the folder `path` of volume `key` and return its id. A folder already in
        the library, as one or inside one, is not added again: the id of the folder
        that has it is returned. Folders inside the new one are merged into it: their
        photos become its photos."""
        path = path.strip("/")
        volume = self.volume_id(key, label, mount)
        with self.transaction():
            for folder in self.folders():
                if folder.volume_key == key and (folder.path == path or _inside(path, folder.path)):
                    return folder.id
            cursor = self.db.execute(
                "INSERT INTO folders (volume_id, path, added_at) VALUES (?, ?, ?)",
                (volume, path, time.time()),
            )
            new = cursor.lastrowid
            for folder in self.folders():
                if folder.volume_key == key and folder.id != new and _inside(folder.path, path):
                    self.db.execute(
                        "UPDATE photos SET folder_id = ? WHERE folder_id = ?", (new, folder.id)
                    )
                    # Its scans are the new folder's scan; other jobs (a merge, an
                    # import) still put their photos there, now in the new folder
                    self.db.execute(
                        "UPDATE jobs SET folder_id = ? WHERE folder_id = ? AND kind != 'scan'",
                        (new, folder.id),
                    )
                    self.db.execute("DELETE FROM jobs WHERE folder_id = ?", (folder.id,))
                    self.db.execute("DELETE FROM folders WHERE id = ?", (folder.id,))
        return new

    def remove_folder(self, folder_id):
        """Forget a folder and its photos. The files are not touched."""
        with self.transaction():
            self.db.execute("DELETE FROM jobs WHERE folder_id = ?", (folder_id,))
            self.db.execute("DELETE FROM photos WHERE folder_id = ?", (folder_id,))
            self.db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            self._tidy_stacks()
            # Vectors of photos no longer in the library, copies elsewhere kept
            self.db.execute(
                "DELETE FROM embeddings WHERE content_id NOT IN "
                "(SELECT content_id FROM photos WHERE content_id IS NOT NULL)"
            )
            # Merges that were to be made there and now never will be. Those made stay:
            # they say what a file was made of, should the folder come back
            self.db.execute(
                "DELETE FROM derived WHERE path IS NULL "
                "AND id NOT IN (SELECT derived_id FROM jobs WHERE derived_id IS NOT NULL)"
            )

    def mark_scanned(self, folder_id):
        with self.transaction():
            self.db.execute(
                "UPDATE folders SET scanned_at = ? WHERE id = ?", (time.time(), folder_id)
            )
