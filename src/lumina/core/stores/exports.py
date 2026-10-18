"""Exports: the part of `Library` for it (see db.py)."""

import time

from lumina.core.dbtypes import QUEUED, ExportItem


class ExportsStore:
    # Exports

    def add_export_job(self, options_json, replace, folder, items):
        """An export job, queued, writing `items` [(photo id, copy, destination)] to
        `folder` as `options_json` says; with `replace`, over files that exist. In one
        transaction. Returns the job's id."""
        now = time.time()
        with self.transaction():
            job = self.db.execute(
                "INSERT INTO jobs (kind, state, total, created_at, updated_at) "
                "VALUES ('export', ?, ?, ?, ?)",
                (QUEUED, len(items), now, now),
            ).lastrowid
            self.db.execute(
                "INSERT INTO export_jobs (job_id, options, replace, folder) VALUES (?, ?, ?, ?)",
                (job, options_json, int(bool(replace)), folder),
            )
            self.db.executemany(
                "INSERT INTO export_items (job_id, photo_id, copy, destination) "
                "VALUES (?, ?, ?, ?)",
                [(job, photo, copy, destination) for photo, copy, destination in items],
            )
        return job

    def export_job(self, job_id):
        """(options JSON, replace, folder) of an export job, or None."""
        row = self.db.execute(
            "SELECT options, replace, folder FROM export_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return (row[0], bool(row[1]), row[2]) if row is not None else None

    def export_items(self, job_id, state=None):
        """The ExportItems of export job `job_id`, in order; only those in `state`, with one."""
        where, args = ("AND i.state = ?", (job_id, state)) if state is not None else ("", (job_id,))
        rows = self.db.execute(
            "SELECT i.id, i.photo_id, i.copy, i.destination, i.state, i.message, v.key, p.path "
            "FROM export_items i JOIN photos p ON p.id = i.photo_id "
            f"JOIN volumes v ON v.id = p.volume_id WHERE i.job_id = ? {where} ORDER BY i.id",
            args,
        )
        return [ExportItem(*row) for row in rows]

    def set_export_item(self, item_id, state, message=None):
        with self.transaction():
            self.db.execute(
                "UPDATE export_items SET state = ?, message = ? WHERE id = ?",
                (state, message, item_id),
            )
