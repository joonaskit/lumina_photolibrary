"""Imports: the part of `Library` for it (see db.py)."""

import time

from lumina.core.dbtypes import QUEUED, ImportItem


class ImportsStore:
    # Imports

    def add_import_job(self, folder_id, items):
        """An import job that copies `items` (ImportItem, in order) into the library
        folder `folder_id` (whose scan then finds them), queued: in one transaction.
        Returns the job's id."""
        now = time.time()
        keys = {item.source_key for item in items} | {item.key for item in items}
        keys |= {item.backup_key for item in items if item.backup_key}
        ids = {key: self.volume_id(key) for key in keys}
        with self.transaction():
            job = self.db.execute(
                "INSERT INTO jobs (kind, folder_id, state, total, created_at, updated_at) "
                "VALUES ('import', ?, ?, ?, ?, ?)",
                (folder_id, QUEUED, len(items), now, now),
            ).lastrowid
            self.db.executemany(
                "INSERT INTO import_items (job_id, source_volume_id, source_path, size, "
                "content_id, volume_id, folder, name, backup_volume_id, backup_folder) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        job,
                        ids[item.source_key],
                        item.source_path.strip("/"),
                        item.size,
                        item.content_id,
                        ids[item.key],
                        item.folder.strip("/"),
                        item.name,
                        ids.get(item.backup_key),
                        item.backup_folder.strip("/") if item.backup_key else None,
                    )
                    for item in items
                ],
            )
        return job

    def import_items(self, job_id, state=None):
        """The ImportItems of import job `job_id`, in order; only those in `state`, with one."""
        where, args = ("AND i.state = ?", (job_id, state)) if state is not None else ("", (job_id,))
        rows = self.db.execute(
            "SELECT s.key, i.source_path, i.size, i.content_id, v.key, i.folder, i.name, "
            "b.key, i.backup_folder, i.id, i.state, i.path FROM import_items i "
            "JOIN volumes s ON s.id = i.source_volume_id JOIN volumes v ON v.id = i.volume_id "
            f"LEFT JOIN volumes b ON b.id = i.backup_volume_id WHERE i.job_id = ? {where} "
            "ORDER BY i.id",
            args,
        )
        return [ImportItem(*row) for row in rows]

    def set_import_item(self, item_id, state, path=None, message=None):
        """An item copied (COPIED, to `path` from its volume's root) or not (NOT_COPIED,
        with why)."""
        with self.transaction():
            self.db.execute(
                "UPDATE import_items SET state = ?, path = ?, message = ? WHERE id = ?",
                (state, path.strip("/") if path else None, message, item_id),
            )
