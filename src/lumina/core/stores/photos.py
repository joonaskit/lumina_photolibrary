"""Photos: the part of `Library` for it (see db.py)."""

import time

from lumina.core.dbtypes import Choices, Filter, PhotoRow, _photo
from lumina.core.dbutil import _inside, _metadata_columns, _where


class PhotosStore:
    # Photos, for the scan

    def photo_states(self, folder_id):
        """{path: (id, size, mtime_ns, available)} of a folder's photos."""
        rows = self.db.execute(
            "SELECT path, id, size, mtime_ns, available FROM photos WHERE folder_id = ?",
            (folder_id,),
        )
        return {row[0]: (row[1], row[2], row[3], bool(row[4])) for row in rows}

    def photo_state(self, volume, path):
        """(id, size, mtime_ns, available) of the photo at `path` (from the root) of the
        volume with id `volume`, or None."""
        row = self.db.execute(
            "SELECT id, size, mtime_ns, available FROM photos WHERE volume_id = ? AND path = ?",
            (volume, path),
        ).fetchone()
        return (row[0], row[1], row[2], bool(row[3])) if row is not None else None

    def write_scan(self, folder_id, volume, new=(), changed=(), returned=()):
        """One batch of what a walk found, in one transaction. `new` and `changed` are
        (path, size, mtime_ns, file_time); `changed` and `returned` are by photo id
        in the first place: (id, size, mtime_ns, file_time) and ids. A new photo the
        library has by now (a tethered folder's, taken in while a scan walked) is left
        as it is."""
        now = time.time()
        with self.transaction():
            self.db.executemany(
                "INSERT INTO photos (folder_id, volume_id, path, size, mtime_ns, file_time, "
                "added_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (volume_id, path) DO NOTHING",
                [
                    (folder_id, volume, path, size, mtime, when, now)
                    for path, size, mtime, when in new
                ],
            )
            # The bytes changed: what was read from them is read again
            self.db.executemany(
                "UPDATE photos SET size = ?, mtime_ns = ?, file_time = ?, content_id = NULL, "
                "metadata_state = 0, metadata_error = NULL, available = 1, missing_since = NULL "
                "WHERE id = ?",
                [(size, mtime, when, photo) for photo, size, mtime, when in changed],
            )
            self.db.executemany(
                "UPDATE photos SET available = 1, missing_since = NULL WHERE id = ?",
                [(photo,) for photo in returned],
            )

    def mark_missing(self, ids):
        """These photos were not found: unavailable, with everything known of them kept."""
        now = time.time()
        with self.transaction():
            self.db.executemany(
                "UPDATE photos SET available = 0, missing_since = COALESCE(missing_since, ?) "
                "WHERE id = ?",
                [(now, photo) for photo in ids],
            )

    def pending_metadata(self, folder_id, limit):
        """[(id, path)] of available photos whose details are still to be read."""
        rows = self.db.execute(
            "SELECT id, path FROM photos WHERE folder_id = ? AND metadata_state = 0 "
            "AND available = 1 ORDER BY id LIMIT ?",
            (folder_id, limit),
        )
        return [tuple(row) for row in rows]

    def count_pending_metadata(self, folder_id):
        return self.db.execute(
            "SELECT COUNT(*) FROM photos WHERE folder_id = ? AND metadata_state = 0 "
            "AND available = 1",
            (folder_id,),
        ).fetchone()[0]

    def write_metadata(self, read=(), failed=()):
        """`read`: (id, content_id, Metadata); `failed`: (id, content_id or None, message)."""
        with self.transaction():
            self.db.executemany(
                "UPDATE photos SET metadata_state = 1, metadata_error = NULL, content_id = ?, "
                "captured = ?, captured_offset = ?, make = ?, model = ?, lens = ?, "
                "focal_length = ?, aperture = ?, exposure_time = ?, iso = ?, orientation = ?, "
                "latitude = ?, longitude = ?, file_type = ?, damage_warning = ? WHERE id = ?",
                [(cid, *_metadata_columns(data), photo) for photo, cid, data in read],
            )
            for photo, _cid, data in read:
                self._take_in_keywords(photo, data.keywords)
            self.db.executemany(
                "UPDATE photos SET metadata_state = 2, metadata_error = ?, "
                "content_id = COALESCE(?, content_id) WHERE id = ?",
                [(message, cid, photo) for photo, cid, message in failed],
            )

    # Photos, for the grid

    def offline_content_ids(self, connected_volume_keys):
        """The content ids of photos that can't be read now: marked unavailable, or on a
        drive that is not among `connected_volume_keys`."""
        keys = list(connected_volume_keys)
        marks = ", ".join("?" * len(keys))
        rows = self.db.execute(
            "SELECT DISTINCT p.content_id FROM photos p JOIN volumes v ON v.id = p.volume_id "
            "WHERE p.content_id IS NOT NULL "
            f"AND (p.available = 0 OR v.key NOT IN ({marks}))",
            keys,
        )
        return {row[0] for row in rows}

    def photos(self, shown=None):
        """The photos `shown` (a Filter) selects, oldest first."""
        where, args = _where(shown or Filter())
        rows = self.db.execute(
            "SELECT p.id, v.key, p.path, p.content_id, p.available, "
            "COALESCE(p.captured, p.file_time) AS taken, e.updated_at, p.flag, p.rating, "
            "p.damage_warning, "
            "(SELECT GROUP_CONCAT(k.keyword_id) FROM photo_keywords k WHERE k.photo_id = p.id), "
            "(SELECT COUNT(*) FROM edits c WHERE c.photo_id = p.id AND c.copy > 0), "
            "p.stack_id, p.stack_position, COALESCE(s.collapsed, 1), h.score, "
            # The newest, should a name have been made twice (as `derived_at` has it)
            "(SELECT d.kind FROM derived d WHERE d.volume_id = p.volume_id AND d.path = p.path "
            "ORDER BY d.id DESC LIMIT 1) "
            f"FROM photos p JOIN volumes v ON v.id = p.volume_id "
            "LEFT JOIN stacks s ON s.id = p.stack_id "
            "LEFT JOIN sharpness h ON h.content_id = p.content_id "
            "LEFT JOIN edits e ON e.photo_id = p.id AND e.copy = 0 "
            f"{where} ORDER BY taken, p.path",
            args,
        )
        return [
            PhotoRow(
                r[0],
                r[1],
                r[2],
                r[3],
                bool(r[4]),
                r[5],
                r[6],
                r[7],
                r[8],
                r[9],
                tuple(int(i) for i in r[10].split(",")) if r[10] else (),
                r[11],
                r[12],
                r[13],
                bool(r[14]),
                sharpness=r[15],
                derived=r[16],
            )
            for r in rows
        ]

    def raw_candidates(self, model):
        """[(volume key, path)] of the available photos of the camera `model`, oldest
        first: where its RAW files may be."""
        rows = self.db.execute(
            "SELECT v.key, p.path FROM photos p JOIN volumes v ON v.id = p.volume_id "
            "WHERE p.model = ? AND p.available = 1 ORDER BY COALESCE(p.captured, p.file_time), "
            "p.path",
            (model,),
        )
        return [(row[0], row[1]) for row in rows]

    def photo(self, photo_id):
        row = self.db.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return _photo(row) if row is not None else None

    def photo_at(self, key, path):
        """The id of the photo at `path` (from the root) of volume `key`, or None."""
        row = self.db.execute(
            "SELECT p.id FROM photos p JOIN volumes v ON v.id = p.volume_id "
            "WHERE v.key = ? AND p.path = ?",
            (key, path.strip("/")),
        ).fetchone()
        return row[0] if row is not None else None

    def folder_holding(self, key, path):
        """The id of the library folder that is `path` (from the root) of volume `key`,
        or has it inside, the deepest of them; None when none does."""
        path = path.strip("/")
        found = [
            folder
            for folder in self.folders()
            if folder.volume_key == key and (folder.path == path or _inside(path, folder.path))
        ]
        return max(found, key=lambda folder: len(folder.path)).id if found else None

    def choices(self, folder_id=None):
        """The Choices there are, to filter by: within a folder, or all."""
        where, args = ("WHERE folder_id = ?", (folder_id,)) if folder_id is not None else ("", ())
        years = [
            row[0]
            for row in self.db.execute(
                f"SELECT DISTINCT substr(COALESCE(captured, file_time), 1, 4) AS y FROM photos "
                f"{where} ORDER BY y DESC",
                args,
            )
        ]

        def distinct(column):
            joiner = "AND" if where else "WHERE"
            return [
                row[0]
                for row in self.db.execute(
                    f"SELECT DISTINCT {column} FROM photos {where} {joiner} {column} IS NOT NULL "
                    f"ORDER BY {column} COLLATE NOCASE",
                    args,
                )
            ]

        return Choices(years, distinct("model"), distinct("lens"))
