"""The library database: volumes, the folders in the library, their photos, and jobs.

SQLite, one file under `$XDG_DATA_HOME/lumina/`. The schema has a version
(`PRAGMA user_version`) and each change is a migration in `_MIGRATIONS`, run in
order when the file is opened; a file from a newer Lumina is refused, not changed.

A photo is a volume and its path from that filesystem's root (see volumes.py).
Records are never deleted because a file or drive went missing: the photo is
marked unavailable. Only removing a folder from the library forgets its photos.

A `Library` is for the thread that opened it. Open one per thread: SQLite in WAL
mode lets the UI read while a scan writes.
"""

import os
import sqlite3
import time
from dataclasses import dataclass

from lumina.core import metadata

# Steps of `metadata_state`
PENDING, READ, FAILED = 0, 1, 2
# A job's `state`
QUEUED, RUNNING, DONE, FAILED_JOB, CANCELLED = "queued", "running", "done", "failed", "cancelled"
UNFINISHED = (QUEUED, RUNNING)
# A photo's `flag`: thumbs up, none, thumbs down
PICKED, UNFLAGGED, REJECTED = 1, 0, -1
FLAGS = (PICKED, UNFLAGGED, REJECTED)
# A photo's `rating`: 0 for none, or 1 to 5 stars
RATINGS = (0, 1, 2, 3, 4, 5)


def default_path():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "lumina", "library.sqlite3")


class NewerDatabase(Exception):
    """The file was written by a newer Lumina: it is left alone."""


def _migrate_1(db):
    db.executescript(
        """
        CREATE TABLE volumes (
            id INTEGER PRIMARY KEY,
            -- "uuid:<UUID>" or "mount:<mount point>", see volumes.py
            key TEXT NOT NULL UNIQUE,
            label TEXT,
            -- Where it was mounted when last seen: for showing an unplugged drive
            last_mount TEXT
        );

        -- The folders the user added to the library
        CREATE TABLE folders (
            id INTEGER PRIMARY KEY,
            volume_id INTEGER NOT NULL REFERENCES volumes(id),
            -- From the filesystem's root, no leading "/"; "" is the root itself
            path TEXT NOT NULL,
            added_at REAL NOT NULL,
            scanned_at REAL,
            UNIQUE (volume_id, path)
        );

        CREATE TABLE photos (
            id INTEGER PRIMARY KEY,
            folder_id INTEGER NOT NULL REFERENCES folders(id),
            volume_id INTEGER NOT NULL REFERENCES volumes(id),
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            -- The file's time, local, for photos without a capture time
            file_time TEXT NOT NULL,
            content_id TEXT,
            available INTEGER NOT NULL DEFAULT 1,
            missing_since REAL,
            metadata_state INTEGER NOT NULL DEFAULT 0,
            metadata_error TEXT,
            -- Local wall-clock time as the camera had it, ISO, and its offset if known
            captured TEXT,
            captured_offset TEXT,
            make TEXT,
            model TEXT,
            lens TEXT,
            focal_length REAL,
            aperture REAL,
            exposure_time REAL,
            iso INTEGER,
            orientation INTEGER,
            latitude REAL,
            longitude REAL,
            file_type TEXT,
            added_at REAL NOT NULL,
            UNIQUE (volume_id, path)
        );
        CREATE INDEX photos_folder ON photos (folder_id, metadata_state);
        CREATE INDEX photos_time ON photos (COALESCE(captured, file_time));
        CREATE INDEX photos_model ON photos (model);
        CREATE INDEX photos_lens ON photos (lens);
        CREATE INDEX photos_content ON photos (content_id);

        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
            state TEXT NOT NULL,
            phase TEXT,
            done INTEGER NOT NULL DEFAULT 0,
            total INTEGER,
            message TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        -- What a job could not do, file by file
        CREATE TABLE job_failures (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            message TEXT NOT NULL
        );
        """
    )


def _migrate_2(db):
    db.executescript(
        """
        -- An edit is its parameters as JSON (core/edit/params.py), rendered from the
        -- original. Copy 0 is the photo's edit; more are virtual copies
        CREATE TABLE edits (
            id INTEGER PRIMARY KEY,
            photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            copy INTEGER NOT NULL DEFAULT 0,
            params TEXT NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (photo_id, copy)
        );

        -- Saved parts of an edit, applied over another
        CREATE TABLE presets (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            params TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )


def _migrate_3(db):
    db.executescript(
        """
        -- A photo's vector for search (core/embed.py), by content id: a moved, renamed
        -- or copied photo has the same one, and a missing one keeps it. A vector of
        -- NULL is a photo that could not be read, with the reason in `error`; it is
        -- tried again when its bytes change, as that is a new content id
        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY,
            content_id TEXT NOT NULL,
            model TEXT NOT NULL,
            vector BLOB,
            error TEXT,
            UNIQUE (content_id, model)
        );
        """
    )


def _migrate_4(db):
    db.executescript(
        """
        -- Thumbs up (1) or down (-1), or none (0), for going through new photos. One a
        -- photo, not per copy; kept while the file is missing, as its edits are
        ALTER TABLE photos ADD COLUMN flag INTEGER NOT NULL DEFAULT 0;
        """
    )


def _migrate_5(db):
    db.executescript(
        """
        -- Stars, 1 to 5, or none (0). Separate from the flag; one a photo, not per copy,
        -- and kept while the file is missing, as the flag and the edits are
        ALTER TABLE photos ADD COLUMN rating INTEGER NOT NULL DEFAULT 0;
        """
    )


def _migrate_6(db):
    db.executescript(
        """
        -- exiftool's warning when it says the raw data may be damaged (metadata's
        -- damage_warning), or NULL. Read with the details; the CR3s read before it are
        -- read again by the next scan, so that those damaged already are found
        ALTER TABLE photos ADD COLUMN damage_warning TEXT;
        UPDATE photos SET metadata_state = 0 WHERE metadata_state = 1 AND file_type = 'CR3';
        """
    )


def _migrate_7(db):
    db.executescript(
        """
        -- A photo made from others: an HDR merge (core/hdr.py), later a panorama. The
        -- settings it was made with, as JSON, and where the result is once written
        -- (a volume and the path from its root, as a photo's; NULL until then). Kept
        -- when its sources go missing or leave the library: they are only named here
        CREATE TABLE derived (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            settings TEXT NOT NULL,
            volume_id INTEGER REFERENCES volumes(id),
            path TEXT,
            created_at REAL NOT NULL
        );
        CREATE INDEX derived_result ON derived (volume_id, path);

        -- What it was made from, in order, by place and content id, not by photo row: a
        -- source whose folder is removed from the library is still named
        CREATE TABLE derived_sources (
            derived_id INTEGER NOT NULL REFERENCES derived(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            volume_id INTEGER NOT NULL REFERENCES volumes(id),
            path TEXT NOT NULL,
            content_id TEXT,
            PRIMARY KEY (derived_id, position)
        );

        -- The derived photo a merge job makes
        ALTER TABLE jobs ADD COLUMN derived_id INTEGER REFERENCES derived(id) ON DELETE CASCADE;
        """
    )


# Index i is the migration to version i + 1. Append only: never change one released
_MIGRATIONS = [
    _migrate_1,
    _migrate_2,
    _migrate_3,
    _migrate_4,
    _migrate_5,
    _migrate_6,
    _migrate_7,
]
SCHEMA_VERSION = len(_MIGRATIONS)


@dataclass(frozen=True)
class Folder:
    id: int
    volume_key: str
    path: str
    label: str | None
    last_mount: str | None
    scanned_at: float | None


@dataclass(frozen=True)
class Filter:
    """What the grid shows. None means any."""

    folder_id: int | None = None
    year: str | None = None
    model: str | None = None
    lens: str | None = None


@dataclass(frozen=True)
class PhotoRow:
    id: int
    volume_key: str
    path: str
    content_id: str | None
    available: bool
    # COALESCE(captured, file_time)
    taken: str
    # The photo's own edit as parameters JSON, or None
    edit: str | None = None
    flag: int = UNFLAGGED
    rating: int = 0
    # exiftool's warning that the raw data may be damaged, or None
    damage_warning: str | None = None


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    folder_id: int | None
    state: str
    phase: str | None
    done: int
    total: int | None
    message: str | None
    failures: int
    # The derived photo a merge job makes, or None
    derived_id: int | None = None


@dataclass(frozen=True)
class Source:
    """A photo a derived one is made from, where it was when it was chosen."""

    volume_key: str
    path: str
    content_id: str | None = None


@dataclass(frozen=True)
class Derived:
    id: int
    kind: str
    # JSON
    settings: str
    sources: tuple
    # Where the result is, once written: a volume key and the path from its root
    volume_key: str | None
    path: str | None


class Library:
    def __init__(self, path=None):
        self.path = os.fspath(path or default_path())
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        # Before anything else: switching to WAL rewrites the file's header, and a file
        # from a newer Lumina is to be left exactly as it is
        self._check_version()
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 10000")
        self._migrate()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _check_version(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.db.close()
            raise NewerDatabase(
                f"{self.path} is from a newer Lumina (schema {version}, this one knows "
                f"{SCHEMA_VERSION})"
            )

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for number in range(version, SCHEMA_VERSION):
            with self.db:
                _MIGRATIONS[number](self.db)
                self.db.execute(f"PRAGMA user_version = {number + 1}")

    # Volumes and folders

    def volume_id(self, key, label=None, mount=None):
        """The id of the volume `key`, made if new; its label and mount kept up to date."""
        with self.db:
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
        for folder in self.folders():
            if folder.volume_key != key:
                continue
            if folder.path == path or _inside(path, folder.path):
                return folder.id
        with self.db:
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
                    self.db.execute("DELETE FROM jobs WHERE folder_id = ?", (folder.id,))
                    self.db.execute("DELETE FROM folders WHERE id = ?", (folder.id,))
        return new

    def remove_folder(self, folder_id):
        """Forget a folder and its photos. The files are not touched."""
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE folder_id = ?", (folder_id,))
            self.db.execute("DELETE FROM photos WHERE folder_id = ?", (folder_id,))
            self.db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
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
        with self.db:
            self.db.execute(
                "UPDATE folders SET scanned_at = ? WHERE id = ?", (time.time(), folder_id)
            )

    # Photos, for the scan

    def photo_states(self, folder_id):
        """{path: (id, size, mtime_ns, available)} of a folder's photos."""
        rows = self.db.execute(
            "SELECT path, id, size, mtime_ns, available FROM photos WHERE folder_id = ?",
            (folder_id,),
        )
        return {row[0]: (row[1], row[2], row[3], bool(row[4])) for row in rows}

    def write_scan(self, folder_id, volume, new=(), changed=(), returned=()):
        """One batch of what a walk found, in one transaction. `new` and `changed` are
        (path, size, mtime_ns, file_time); `changed` and `returned` are by photo id
        in the first place: (id, size, mtime_ns, file_time) and ids."""
        now = time.time()
        with self.db:
            self.db.executemany(
                "INSERT INTO photos (folder_id, volume_id, path, size, mtime_ns, file_time, "
                "added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        with self.db:
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
        with self.db:
            self.db.executemany(
                "UPDATE photos SET metadata_state = 1, metadata_error = NULL, content_id = ?, "
                "captured = ?, captured_offset = ?, make = ?, model = ?, lens = ?, "
                "focal_length = ?, aperture = ?, exposure_time = ?, iso = ?, orientation = ?, "
                "latitude = ?, longitude = ?, file_type = ?, damage_warning = ? WHERE id = ?",
                [(cid, *_metadata_columns(data), photo) for photo, cid, data in read],
            )
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
            "COALESCE(p.captured, p.file_time) AS taken, e.params, p.flag, p.rating, "
            "p.damage_warning "
            f"FROM photos p JOIN volumes v ON v.id = p.volume_id "
            "LEFT JOIN edits e ON e.photo_id = p.id AND e.copy = 0 "
            f"{where} ORDER BY taken, p.path",
            args,
        )
        return [
            PhotoRow(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6], r[7], r[8], r[9]) for r in rows
        ]

    def photo(self, photo_id):
        row = self.db.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return dict(row) if row is not None else None

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
        """The years, cameras and lenses there are, to filter by: within a folder, or all."""
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

        return years, distinct("model"), distinct("lens")

    # Flags and ratings

    def set_flags(self, flags):
        """{photo id: PICKED, UNFLAGGED or REJECTED}, in one transaction."""
        self._set_marks("flag", FLAGS, flags)

    def flags(self, ids):
        """{photo id: flag} of those of `ids` in the library."""
        return self._marks("flag", ids)

    def set_ratings(self, ratings):
        """{photo id: 0 for none, or 1 to 5 stars}, in one transaction."""
        self._set_marks("rating", RATINGS, ratings)

    def ratings(self, ids):
        """{photo id: rating} of those of `ids` in the library."""
        return self._marks("rating", ids)

    def _set_marks(self, column, allowed, values):
        for value in values.values():
            if value not in allowed:
                raise ValueError(f"Not a {column}: {value!r}")
        with self.db:
            self.db.executemany(
                f"UPDATE photos SET {column} = ? WHERE id = ?",
                [(value, photo) for photo, value in values.items()],
            )

    def _marks(self, column, ids):
        ids = list(ids)
        found = {}
        # In slices: SQLite has a limit on the number of parameters
        for start in range(0, len(ids), 500):
            part = ids[start : start + 500]
            marks = ", ".join("?" * len(part))
            rows = self.db.execute(f"SELECT id, {column} FROM photos WHERE id IN ({marks})", part)
            found.update((row[0], row[1]) for row in rows)
        return found

    # Edits

    def edit(self, photo_id, copy=0):
        """The stored parameters JSON of a photo's edit, or None for no edit."""
        row = self.db.execute(
            "SELECT params FROM edits WHERE photo_id = ? AND copy = ?", (photo_id, copy)
        ).fetchone()
        return row[0] if row is not None else None

    def save_edit(self, photo_id, params_json, copy=0):
        with self.db:
            self.db.execute(
                "INSERT INTO edits (photo_id, copy, params, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (photo_id, copy) DO UPDATE SET "
                "params = excluded.params, updated_at = excluded.updated_at",
                (photo_id, copy, params_json, time.time()),
            )

    def save_edits(self, edits):
        """{photo id: parameters JSON, or None for no edit} as the photos' own edits, in
        one transaction: all of them or none."""
        now = time.time()
        with self.db:
            for photo_id, params_json in edits.items():
                if params_json is None:
                    self.db.execute(
                        "DELETE FROM edits WHERE photo_id = ? AND copy = 0", (photo_id,)
                    )
                    continue
                self.db.execute(
                    "INSERT INTO edits (photo_id, copy, params, updated_at) VALUES (?, 0, ?, ?) "
                    "ON CONFLICT (photo_id, copy) DO UPDATE SET "
                    "params = excluded.params, updated_at = excluded.updated_at",
                    (photo_id, params_json, now),
                )

    def delete_edit(self, photo_id, copy=0):
        """Back to the original. The photo's own edit and its copies are separate."""
        with self.db:
            self.db.execute("DELETE FROM edits WHERE photo_id = ? AND copy = ?", (photo_id, copy))

    def copies(self, photo_id):
        """The numbers of a photo's edits: 0 for its own, then its virtual copies."""
        rows = self.db.execute(
            "SELECT copy FROM edits WHERE photo_id = ? ORDER BY copy", (photo_id,)
        )
        return [row[0] for row in rows]

    def new_copy(self, photo_id, params_json):
        """A virtual copy of a photo with these parameters. Returns its number."""
        with self.db:
            row = self.db.execute(
                "SELECT COALESCE(MAX(copy), 0) + 1 FROM edits WHERE photo_id = ?", (photo_id,)
            ).fetchone()
            self.db.execute(
                "INSERT INTO edits (photo_id, copy, params, updated_at) VALUES (?, ?, ?, ?)",
                (photo_id, row[0], params_json, time.time()),
            )
        return row[0]

    def presets(self):
        """[(name, parameters JSON)] by name."""
        rows = self.db.execute("SELECT name, params FROM presets ORDER BY name COLLATE NOCASE")
        return [tuple(row) for row in rows]

    def save_preset(self, name, params_json):
        with self.db:
            self.db.execute(
                "INSERT INTO presets (name, params, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET params = excluded.params",
                (name, params_json, time.time()),
            )

    def delete_preset(self, name):
        with self.db:
            self.db.execute("DELETE FROM presets WHERE name = ?", (name,))

    # Embeddings

    def pending_embeddings(self, model, volume_keys, limit, skip=0):
        """[(content_id, volume_key, path)] of photos with no vector for `model` yet,
        one per content id, on the volumes `volume_keys` (the connected ones), newest
        first: recent photos are searchable soonest. `skip` leaves out the first ones,
        those a job passed over."""
        where, args = _pending_embeddings(model, volume_keys)
        rows = self.db.execute(
            "SELECT p.content_id, v.key, p.path, MAX(COALESCE(p.captured, p.file_time)) AS t "
            f"{where} GROUP BY p.content_id ORDER BY t DESC, p.content_id LIMIT ? OFFSET ?",
            (*args, limit, skip),
        )
        return [tuple(row[:3]) for row in rows]

    def count_pending_embeddings(self, model, volume_keys):
        where, args = _pending_embeddings(model, volume_keys)
        return self.db.execute(f"SELECT COUNT(DISTINCT p.content_id) {where}", args).fetchone()[0]

    def write_embeddings(self, model, vectors=(), failed=()):
        """`vectors`: (content_id, float16 blob); `failed`: (content_id, message)."""
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO embeddings (content_id, model, vector, error) "
                "VALUES (?, ?, ?, NULL)",
                [(cid, model, blob) for cid, blob in vectors],
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO embeddings (content_id, model, vector, error) "
                "VALUES (?, ?, NULL, ?)",
                [(cid, model, message) for cid, message in failed],
            )

    def embeddings(self, model, after=0):
        """[(row id, content_id, float16 blob)] of `model`'s vectors written after row
        `after`, in order: a search index reads only what is new."""
        rows = self.db.execute(
            "SELECT id, content_id, vector FROM embeddings "
            "WHERE model = ? AND id > ? AND vector IS NOT NULL ORDER BY id",
            (model, after),
        )
        return [tuple(row) for row in rows]

    def embedding(self, content_id, model):
        row = self.db.execute(
            "SELECT vector FROM embeddings WHERE content_id = ? AND model = ?",
            (content_id, model),
        ).fetchone()
        return row[0] if row is not None else None

    # Jobs

    def add_job(self, kind, folder_id):
        """Queue a job, or return the unfinished one of that kind for that folder."""
        row = self.db.execute(
            "SELECT id FROM jobs WHERE kind = ? AND folder_id IS ? AND state IN (?, ?)",
            (kind, folder_id, *UNFINISHED),
        ).fetchone()
        if row is not None:
            return row[0]
        now = time.time()
        with self.db:
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
        with self.db:
            self.db.execute(
                f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), time.time(), job_id),
            )

    def add_failures(self, job_id, failures):
        """[(path, message)] a job could not do."""
        with self.db:
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
        row = self.db.execute(
            "SELECT j.id, j.kind, j.folder_id, j.state, j.phase, j.done, j.total, j.message, "
            "(SELECT COUNT(*) FROM job_failures WHERE job_id = j.id), j.derived_id "
            "FROM jobs j WHERE j.id = ?",
            (job_id,),
        ).fetchone()
        return Job(*row) if row is not None else None

    def unfinished_jobs(self):
        """Jobs queued or interrupted while running (a quit or a crash), oldest first."""
        rows = self.db.execute(
            "SELECT id FROM jobs WHERE state IN (?, ?) ORDER BY id", UNFINISHED
        ).fetchall()
        return [self.job(row[0]) for row in rows]

    # Derived photos

    def add_derived_job(self, kind, settings_json, sources, folder_id):
        """A photo to be made from `sources` (Source, in order) with `settings_json`, and
        the job of kind `kind` that makes it in the library folder `folder_id`, queued:
        both in one transaction. Returns the job's id."""
        now = time.time()
        # Before the transaction: volume_id() commits one of its own
        volumes = [self.volume_id(source.volume_key) for source in sources]
        with self.db:
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
        with self.db:
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
        rows = self.db.execute(
            "SELECT p.*, v.key AS volume_key, COALESCE(p.captured, p.file_time) AS taken "
            "FROM photos p JOIN volumes v ON v.id = p.volume_id WHERE p.folder_id = ? "
            "AND abs(julianday(COALESCE(p.captured, p.file_time)) - julianday(?)) * 86400 <= ? "
            "ORDER BY taken, p.path",
            (folder_id, taken, seconds),
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


def _inside(path, folder):
    """Whether `path` is strictly inside `folder`, both from a filesystem's root."""
    return folder == "" and path != "" or path.startswith(folder + "/")


def _where(shown):
    clauses, args = [], []
    if shown.folder_id is not None:
        clauses.append("p.folder_id = ?")
        args.append(shown.folder_id)
    if shown.year is not None:
        clauses.append("substr(COALESCE(p.captured, p.file_time), 1, 4) = ?")
        args.append(shown.year)
    if shown.model is not None:
        clauses.append("p.model = ?")
        args.append(shown.model)
    if shown.lens is not None:
        clauses.append("p.lens = ?")
        args.append(shown.lens)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", args


def _pending_embeddings(model, volume_keys):
    keys = list(volume_keys)
    marks = ", ".join("?" * len(keys))
    return (
        "FROM photos p JOIN volumes v ON v.id = p.volume_id "
        f"WHERE p.available = 1 AND p.content_id IS NOT NULL AND v.key IN ({marks}) "
        "AND NOT EXISTS (SELECT 1 FROM embeddings e "
        "WHERE e.content_id = p.content_id AND e.model = ?)",
        (*keys, model),
    )


def _metadata_columns(data):
    captured = offset = None
    if data.captured is not None:
        if data.captured.utcoffset() is not None:
            offset = _format_offset(data.captured.utcoffset())
        captured = data.captured.replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
    return (
        captured,
        offset,
        data.make,
        data.model,
        data.lens,
        data.focal_length,
        data.aperture,
        data.exposure_time,
        data.iso,
        data.orientation,
        data.latitude,
        data.longitude,
        data.file_type,
        metadata.damage_warning(data),
    )


def _format_offset(delta):
    minutes = round(delta.total_seconds() / 60)
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def local_time(mtime_ns):
    """A file time as local wall-clock ISO text, as `captured` is."""
    seconds, nanos = divmod(mtime_ns, 1_000_000_000)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
    return f"{stamp}.{nanos // 1000:06d}"
