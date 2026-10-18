"""Derived: the part of `Library` for it (see db.py)."""

import time
from datetime import datetime, timedelta

from lumina.core.dbtypes import QUEUED, Derived, Source


class DerivedStore:
    # Derived photos

    def add_derived_job(self, kind, settings_json, sources, folder_id):
        """A photo to be made from `sources` (Source, in order) with `settings_json`, and
        the job of kind `kind` that makes it in the library folder `folder_id`, queued:
        both in one transaction. Returns the job's id."""
        now = time.time()
        volumes = [self.volume_id(source.volume_key) for source in sources]
        with self.transaction():
            derived = self.db.execute(
                "INSERT INTO derived (kind, settings, created_at) VALUES (?, ?, ?)",
                (kind, settings_json, now),
            ).lastrowid
            for position, (source, volume) in enumerate(zip(sources, volumes, strict=True)):
                self.db.execute(
                    "INSERT INTO derived_sources (derived_id, position, volume_id, path, "
                    "content_id) VALUES (?, ?, ?, ?, ?)",
                    (derived, position, volume, source.path.strip("/"), source.content_id),
                )
            job = self.db.execute(
                "INSERT INTO jobs (kind, folder_id, state, created_at, updated_at, derived_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kind, folder_id, QUEUED, now, now, derived),
            ).lastrowid
        return job

    def derived(self, derived_id):
        row = self.db.execute(
            "SELECT d.id, d.kind, d.settings, v.key, d.path FROM derived d "
            "LEFT JOIN volumes v ON v.id = d.volume_id WHERE d.id = ?",
            (derived_id,),
        ).fetchone()
        if row is None:
            return None
        sources = self.db.execute(
            "SELECT v.key, s.path, s.content_id FROM derived_sources s "
            "JOIN volumes v ON v.id = s.volume_id WHERE s.derived_id = ? ORDER BY s.position",
            (derived_id,),
        )
        return Derived(row[0], row[1], row[2], tuple(Source(*s) for s in sources), row[3], row[4])

    def set_derived_result(self, derived_id, volume_key, path):
        """The derived photo was written: it is the file at `path` of volume `volume_key`."""
        volume = self.volume_id(volume_key)
        with self.transaction():
            self.db.execute(
                "UPDATE derived SET volume_id = ?, path = ? WHERE id = ?",
                (volume, path.strip("/"), derived_id),
            )

    def derived_at(self, volume_key, path):
        """The Derived that the file at `path` of volume `volume_key` is, or None: the
        newest, should a name have been made twice."""
        row = self.db.execute(
            "SELECT d.id FROM derived d JOIN volumes v ON v.id = d.volume_id "
            "WHERE v.key = ? AND d.path = ? ORDER BY d.id DESC",
            (volume_key, path.strip("/")),
        ).fetchone()
        return self.derived(row[0]) if row is not None else None

    def photos_near(self, folder_id, taken, seconds):
        """Photos of the folder taken within `seconds` of `taken` (ISO text), as dicts with
        their details, in time order: where a bracket is looked for."""
        # A range on the indexed time, as text, a little wider than asked (the times are
        # text of different lengths); the exact test is made on what it finds
        centre = datetime.fromisoformat(taken)
        reach = timedelta(seconds=seconds + 2)
        low, high = ((centre + sign * reach).strftime("%Y-%m-%d %H:%M:%S") for sign in (-1, 1))
        rows = self.db.execute(
            "SELECT p.*, v.key AS volume_key, COALESCE(p.captured, p.file_time) AS taken "
            "FROM photos p JOIN volumes v ON v.id = p.volume_id WHERE p.folder_id = ? "
            "AND COALESCE(p.captured, p.file_time) BETWEEN ? AND ? "
            "AND abs(julianday(COALESCE(p.captured, p.file_time)) - julianday(?)) * 86400 <= ? "
            "ORDER BY taken, p.path",
            (folder_id, low, high, taken, seconds),
        )
        return [dict(row) for row in rows]

    def photo_rows(self, ids):
        """The photos `ids` as dicts with their details and volume key, in that order;
        those not in the library left out."""
        found = {}
        ids = list(ids)
        for start in range(0, len(ids), 500):
            part = ids[start : start + 500]
            marks = ", ".join("?" * len(part))
            rows = self.db.execute(
                "SELECT p.*, v.key AS volume_key, COALESCE(p.captured, p.file_time) AS taken "
                f"FROM photos p JOIN volumes v ON v.id = p.volume_id WHERE p.id IN ({marks})",
                part,
            )
            found.update((row["id"], dict(row)) for row in rows)
        return [found[i] for i in ids if i in found]
