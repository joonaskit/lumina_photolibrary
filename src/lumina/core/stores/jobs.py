"""Jobs: the part of `Library` for it (see db.py)."""

import time

from lumina.core.dbtypes import QUEUED, UNFINISHED, Job
from lumina.core.dbutil import _JOB_COLUMNS


class JobsStore:
    # Jobs

    def add_job(self, kind, folder_id):
        """Queue a job, or return the unfinished one of that kind for that folder."""
        now = time.time()
        with self.transaction():
            row = self.db.execute(
                "SELECT id FROM jobs WHERE kind = ? AND folder_id IS ? AND state IN (?, ?)",
                (kind, folder_id, *UNFINISHED),
            ).fetchone()
            if row is not None:
                return row[0]
            cursor = self.db.execute(
                "INSERT INTO jobs (kind, folder_id, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, folder_id, QUEUED, now, now),
            )
        return cursor.lastrowid

    def update_job(self, job_id, **fields):
        allowed = {"state", "phase", "done", "total", "message"}
        assert set(fields) <= allowed, fields
        sets = ", ".join(f"{name} = ?" for name in fields)
        with self.transaction():
            self.db.execute(
                f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), time.time(), job_id),
            )

    def add_failures(self, job_id, failures):
        """[(path, message)] a job could not do."""
        with self.transaction():
            self.db.executemany(
                "INSERT INTO job_failures (job_id, path, message) VALUES (?, ?, ?)",
                [(job_id, path, message) for path, message in failures],
            )

    def failures(self, job_id):
        rows = self.db.execute(
            "SELECT path, message FROM job_failures WHERE job_id = ? ORDER BY id", (job_id,)
        )
        return [tuple(row) for row in rows]

    def job(self, job_id):
        row = self.db.execute(f"{_JOB_COLUMNS} WHERE j.id = ?", (job_id,)).fetchone()
        return Job(*row) if row is not None else None

    def unfinished_jobs(self):
        """Jobs queued or interrupted while running (a quit or a crash), oldest first."""
        rows = self.db.execute(f"{_JOB_COLUMNS} WHERE j.state IN (?, ?) ORDER BY j.id", UNFINISHED)
        return [Job(*row) for row in rows]
