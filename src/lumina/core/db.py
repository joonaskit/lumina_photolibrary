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

# Steps of `metadata_state`
PENDING, READ, FAILED = 0, 1, 2
# A job's `state`
QUEUED, RUNNING, DONE, FAILED_JOB, CANCELLED = "queued", "running", "done", "failed", "cancelled"
UNFINISHED = (QUEUED, RUNNING)


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


# Index i is the migration to version i + 1. Append only: never change one released
_MIGRATIONS = [_migrate_1]
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
                "latitude = ?, longitude = ?, file_type = ? WHERE id = ?",
                [(cid, *_metadata_columns(data), photo) for photo, cid, data in read],
            )
            self.db.executemany(
                "UPDATE photos SET metadata_state = 2, metadata_error = ?, "
                "content_id = COALESCE(?, content_id) WHERE id = ?",
                [(message, cid, photo) for photo, cid, message in failed],
            )

    # Photos, for the grid

    def photos(self, shown=None):
        """The photos `shown` (a Filter) selects, oldest first."""
        where, args = _where(shown or Filter())
        rows = self.db.execute(
            "SELECT p.id, v.key, p.path, p.content_id, p.available, "
            "COALESCE(p.captured, p.file_time) AS taken "
            f"FROM photos p JOIN volumes v ON v.id = p.volume_id {where} "
            "ORDER BY taken, p.path",
            args,
        )
        return [PhotoRow(r[0], r[1], r[2], r[3], bool(r[4]), r[5]) for r in rows]

    def photo(self, photo_id):
        row = self.db.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return dict(row) if row is not None else None

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
            "(SELECT COUNT(*) FROM job_failures WHERE job_id = j.id) "
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
