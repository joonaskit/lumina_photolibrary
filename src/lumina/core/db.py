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

from lumina.core import backup, keywords, metadata, xmp

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


class NoBackup(Exception):
    """The file needs a migration and could not be backed up first: it is left alone."""


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


def _migrate_8(db):
    db.executescript(
        """
        -- The photos an import job copies (core/importing.py), one row each, so a quit
        -- or a crash resumes where it was. Where it comes from (a volume, a card, and
        -- the path from its root) and goes to (a folder by volume and path from the
        -- root, the name wanted there, and a second folder for a backup copy, or NULL).
        -- `state`: 0 to copy, 1 copied, 2 failed; `path` is where the copy was put.
        -- The content ids of those copied are how a card is known to be imported before
        CREATE TABLE import_items (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            source_volume_id INTEGER NOT NULL REFERENCES volumes(id),
            source_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            content_id TEXT,
            volume_id INTEGER NOT NULL REFERENCES volumes(id),
            folder TEXT NOT NULL,
            name TEXT NOT NULL,
            backup_volume_id INTEGER REFERENCES volumes(id),
            backup_folder TEXT,
            state INTEGER NOT NULL DEFAULT 0,
            path TEXT,
            message TEXT
        );
        CREATE INDEX import_items_job ON import_items (job_id, state);
        CREATE INDEX import_items_content ON import_items (content_id, state);
        """
    )


def _migrate_9(db):
    db.executescript(
        """
        -- A keyword is a path of names from the top ("Places > Finland > Helsinki",
        -- core/keywords.py), compared without regard to case. The ones above a keyword
        -- have rows of their own, so the list is a tree; a photo is linked to the
        -- keywords it was given, not to those above them
        CREATE TABLE keywords (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE COLLATE NOCASE
        );
        -- Per photo, not per virtual copy, as flags are. Forgotten with the photo (a
        -- folder removed from the library), kept while its file or drive is missing
        CREATE TABLE photo_keywords (
            photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
            PRIMARY KEY (photo_id, keyword_id)
        );
        CREATE INDEX photo_keywords_keyword ON photo_keywords (keyword_id);
        -- Whether the keywords in the file (IPTC, XMP) have been taken in as a starting
        -- set. Once: a keyword taken off a photo is not put back by reading its file again
        ALTER TABLE photos ADD COLUMN keywords_read INTEGER NOT NULL DEFAULT 0;

        -- Manual collections hold photos; a smart one has `query`, a saved filter as
        -- JSON (core/smart.py), and holds none
        CREATE TABLE collections (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            query TEXT,
            created_at REAL NOT NULL
        );
        CREATE TABLE collection_photos (
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            added_at REAL NOT NULL,
            PRIMARY KEY (collection_id, photo_id)
        );
        CREATE INDEX collection_photos_photo ON collection_photos (photo_id);
        """
    )


def _migrate_10(db):
    db.executescript(
        """
        -- When a photo's flag, stars, keywords or edits last changed (time.time()), or
        -- NULL when they never have since it was added. What an XMP sidecar (core/xmp.py)
        -- is compared by
        ALTER TABLE photos ADD COLUMN changed_at REAL;
        -- The `changed_at` its sidecar holds, NULL when none was written or read; the
        -- sidecar is out of date while this is behind `changed_at`
        ALTER TABLE photos ADD COLUMN sidecar_at REAL;
        -- The sidecar file's time (ns) when it was last written or read here: a scan reads
        -- a sidecar again when the file's time is another
        ALTER TABLE photos ADD COLUMN sidecar_mtime_ns INTEGER;
        -- What was marked before this schema has no time of its own: the edit's, else now
        UPDATE photos SET changed_at = COALESCE(
            (SELECT MAX(updated_at) FROM edits WHERE photo_id = photos.id),
            CAST(strftime('%s', 'now') AS REAL)
        )
        WHERE flag <> 0 OR rating <> 0
            OR EXISTS (SELECT 1 FROM edits WHERE photo_id = photos.id)
            OR EXISTS (SELECT 1 FROM photo_keywords WHERE photo_id = photos.id);
        """
    )


def _migrate_11(db):
    db.executescript(
        """
        -- A stack of photos (core/db.py `make_stack`): shown as one cell while collapsed.
        -- `key` is how the XMP sidecars name it (a sidecar holds one photo, so a stack
        -- is put together again from the members' keys). The top is the member with the
        -- lowest position. Per library like flags; kept while a file or drive is missing
        CREATE TABLE stacks (
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL UNIQUE,
            collapsed INTEGER NOT NULL DEFAULT 1
        );
        ALTER TABLE photos ADD COLUMN stack_id INTEGER REFERENCES stacks(id) ON DELETE SET NULL;
        ALTER TABLE photos ADD COLUMN stack_position INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX photos_stack ON photos (stack_id);
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
    _migrate_8,
    _migrate_9,
    _migrate_10,
    _migrate_11,
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
    # The manual collection whose photos to show
    collection_id: int | None = None
    # The import job whose copied photos to show (`Library.last_import`)
    import_job: int | None = None


@dataclass(frozen=True)
class Collection:
    id: int
    name: str
    # A smart collection's saved filter as JSON (core/smart.py); None for a manual one
    query: str | None
    # How many photos a manual collection holds; None for a smart one
    count: int | None = None


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
    # The ids of the keywords the photo was given (not of those above them)
    keywords: tuple[int, ...] = ()
    # How many virtual copies the photo has (edits other than its own)
    copies: int = 0
    # The stack the photo is in (its id), its place there (the top is the lowest) and
    # whether the stack is collapsed; None for no stack
    stack: int | None = None
    stack_position: int = 0
    stack_collapsed: bool = True
    # A grid cell of a virtual copy of the photo (its number), shown open; 0 for the photo
    copy: int = 0


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


# An import item's `state`
TO_COPY, COPIED, NOT_COPIED = 0, 1, 2


@dataclass(frozen=True)
class ImportItem:
    """A photo to copy into the library: from where (a volume key and the path from its
    root), to which folder (the same) under which name, and to a backup folder too, or
    not (None)."""

    source_key: str
    source_path: str
    size: int
    content_id: str | None
    key: str
    folder: str
    name: str
    backup_key: str | None = None
    backup_folder: str | None = None
    # Set when read back from the database
    id: int | None = None
    state: int = TO_COPY
    path: str | None = None


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
        if 0 < version < SCHEMA_VERSION:
            # A copy as it was, kept apart from the daily ones: a migration that goes
            # wrong loses nothing. Without it, it is not changed
            try:
                backup.back_up(
                    self.path,
                    backup.default_folder(self.path),
                    backup.BEFORE_MIGRATION,
                    schema=version,
                )
            except backup.BackupError as error:
                self.db.close()
                raise NoBackup(
                    f"{self.path} needs updating for this Lumina, and could not be backed up "
                    f"first, so it was left as it is. {error}"
                ) from error
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
        with self.db:
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
            "COALESCE(p.captured, p.file_time) AS taken, e.params, p.flag, p.rating, "
            "p.damage_warning, "
            "(SELECT GROUP_CONCAT(k.keyword_id) FROM photo_keywords k WHERE k.photo_id = p.id), "
            "(SELECT COUNT(*) FROM edits c WHERE c.photo_id = p.id AND c.copy > 0), "
            "p.stack_id, p.stack_position, COALESCE(s.collapsed, 1) "
            f"FROM photos p JOIN volumes v ON v.id = p.volume_id "
            "LEFT JOIN stacks s ON s.id = p.stack_id "
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
            )
            for r in rows
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
            self._touch(values)

    def _touch(self, photo_ids):
        """Note that these photos' flag, stars, keywords or edits changed now: their
        sidecars are out of date. In the caller's transaction."""
        now = time.time()
        self.db.executemany(
            "UPDATE photos SET changed_at = ? WHERE id = ?", [(now, photo) for photo in photo_ids]
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

    # Keywords

    def keyword_paths(self):
        """{id: path} of every keyword."""
        return {row[0]: row[1] for row in self.db.execute("SELECT id, path FROM keywords")}

    def keyword_counts(self):
        """{id: photos given the keyword or one below it} (one given two keywords in a
        branch counts once there); a keyword nothing is given is left out."""
        # Those below are the paths from "<path> > " up to "<path> >!": the next character
        # after the separator's last, so the range holds nothing else (and the index serves it)
        return {
            row[0]: row[1]
            for row in self.db.execute(
                "SELECT a.id, COUNT(DISTINCT m.photo_id) FROM keywords a "
                "JOIN keywords b ON b.path = a.path OR "
                "(b.path >= a.path || ?1 AND b.path < a.path || ?2) "
                "JOIN photo_keywords m ON m.keyword_id = b.id GROUP BY a.id",
                (keywords.SEPARATOR, keywords.SEPARATOR[:-1] + "!"),
            )
        }

    def keyword_list(self):
        """[(id, path, count)] of every keyword, by path; `count` is the photos given it
        or one below it."""
        counts = self.keyword_counts()
        return sorted(
            (
                (keyword, path, counts.get(keyword, 0))
                for keyword, path in self.keyword_paths().items()
            ),
            key=lambda item: [name.casefold() for name in keywords.levels(item[1])],
        )

    def ensure_keywords(self, paths):
        """{path: id} of the keywords `paths` (parsed by `keywords.parse`), made when they
        are new, with the levels above them. The path is as first written."""
        with self.db:
            return {path: self._keyword(path) for path in paths}

    def _keyword(self, path):
        # Each level is spelt as the keyword that is there already spells it, so that what
        # is below a keyword always starts with its path
        names = keywords.levels(path)
        found = None
        for number in range(1, len(names) + 1):
            above = keywords.SEPARATOR.join([*([found] if found else []), names[number - 1]])
            self.db.execute("INSERT OR IGNORE INTO keywords (path) VALUES (?)", (above,))
            found = self.db.execute(
                "SELECT path FROM keywords WHERE path = ?", (above,)
            ).fetchone()[0]
        return self.db.execute("SELECT id FROM keywords WHERE path = ?", (found,)).fetchone()[0]

    def photo_keywords(self, ids):
        """{photo id: ids of its keywords, sorted} of those of `ids` in the library."""
        ids = list(ids)
        found = {photo: () for photo in self._marks("id", ids)}
        for start in range(0, len(ids), 500):
            part = ids[start : start + 500]
            marks = ", ".join("?" * len(part))
            rows = self.db.execute(
                "SELECT photo_id, keyword_id FROM photo_keywords "
                f"WHERE photo_id IN ({marks}) ORDER BY keyword_id",
                part,
            )
            for photo, keyword in rows:
                found[photo] = (*found[photo], keyword)
        return found

    def set_photo_keywords(self, values):
        """{photo id: ids of the keywords it has}, replacing what each had, in one
        transaction."""
        with self.db:
            for photo, ids in values.items():
                self.db.execute("DELETE FROM photo_keywords WHERE photo_id = ?", (photo,))
                self.db.executemany(
                    "INSERT OR IGNORE INTO photo_keywords (photo_id, keyword_id) "
                    "SELECT ?, id FROM keywords WHERE id = ?",
                    [(photo, keyword) for keyword in ids],
                )
            self._touch(values)

    def rename_keyword(self, path, new_path):
        """Make keyword `path` and those below it `new_path` and below. Returns False when
        `path` is not a keyword; a ValueError when `new_path` is another keyword's. Photos
        keep their keywords. The levels above `new_path` are made."""
        with self.db:
            found = self._keywords_within(path)
            if not found:
                return False
            taken = {other.casefold() for _id, other in self._keywords_within(new_path)}
            moved = {old.casefold() for _id, old in found}
            if taken - moved:
                raise ValueError(f"There is a keyword {new_path} already")
            self._touch_keyword_photos(keyword for keyword, _old in found)
            for keyword, old in found:
                renamed = new_path + old[len(path) :]
                self.db.execute("UPDATE keywords SET path = ? WHERE id = ?", (renamed, keyword))
            for above in keywords.ancestors(new_path):
                self.db.execute("INSERT OR IGNORE INTO keywords (path) VALUES (?)", (above,))
        return True

    def delete_keyword(self, path):
        """Forget keyword `path` and those below it, taking them off every photo. Returns
        how many keywords went."""
        with self.db:
            found = self._keywords_within(path)
            self._touch_keyword_photos(i for i, _p in found)
            self.db.executemany("DELETE FROM keywords WHERE id = ?", [(i,) for i, _p in found])
        return len(found)

    def _touch_keyword_photos(self, keyword_ids):
        ids = list(keyword_ids)
        marks = ", ".join("?" * len(ids))
        photos = [
            row[0]
            for row in self.db.execute(
                f"SELECT DISTINCT photo_id FROM photo_keywords WHERE keyword_id IN ({marks})", ids
            )
        ]
        self._touch(photos)

    def _keywords_within(self, path):
        return [
            (row[0], row[1])
            for row in self.db.execute("SELECT id, path FROM keywords")
            if keywords.within(row[1], path)
        ]

    def keyword_ids_within(self, path):
        """The ids of keyword `path` and those below it."""
        return {keyword for keyword, _path in self._keywords_within(path)}

    def _take_in_keywords(self, photo, found):
        # The first time its file is read only
        row = self.db.execute("SELECT keywords_read FROM photos WHERE id = ?", (photo,)).fetchone()
        if row is None or row[0]:
            return
        self.db.execute("UPDATE photos SET keywords_read = 1 WHERE id = ?", (photo,))
        for path in found:
            self.db.execute(
                "INSERT OR IGNORE INTO photo_keywords (photo_id, keyword_id) VALUES (?, ?)",
                (photo, self._keyword(path)),
            )

    # Collections

    def collections(self):
        """Every collection, by name, manual ones with their photo counts."""
        rows = self.db.execute(
            "SELECT c.id, c.name, c.query, "
            "(SELECT COUNT(*) FROM collection_photos m WHERE m.collection_id = c.id) "
            "FROM collections c ORDER BY c.name COLLATE NOCASE"
        )
        return [Collection(r[0], r[1], r[2], None if r[2] is not None else r[3]) for r in rows]

    def add_collection(self, name, query=None):
        """A new collection, smart when it has a `query` (JSON). Returns its id; a
        ValueError when the name is empty or another collection has it."""
        name = " ".join(name.split())
        if not name:
            raise ValueError("A collection needs a name")
        try:
            with self.db:
                cursor = self.db.execute(
                    "INSERT INTO collections (name, query, created_at) VALUES (?, ?, ?)",
                    (name, query, time.time()),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"There is a collection called {name} already") from error
        return cursor.lastrowid

    def rename_collection(self, collection_id, name):
        name = " ".join(name.split())
        if not name:
            raise ValueError("A collection needs a name")
        try:
            with self.db:
                self.db.execute(
                    "UPDATE collections SET name = ? WHERE id = ?", (name, collection_id)
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"There is a collection called {name} already") from error

    def set_collection_query(self, collection_id, query):
        """Change a smart collection's saved filter (JSON)."""
        with self.db:
            self.db.execute(
                "UPDATE collections SET query = ? WHERE id = ? AND query IS NOT NULL",
                (query, collection_id),
            )

    def delete_collection(self, collection_id):
        """Forget a collection; its photos stay in the library."""
        with self.db:
            self.db.execute("DELETE FROM collections WHERE id = ?", (collection_id,))

    def add_to_collection(self, collection_id, photo_ids):
        """Put photos in a manual collection (those in it already stay). Returns how many
        were added."""
        now = time.time()
        with self.db:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO collection_photos (collection_id, photo_id, added_at) "
                "SELECT c.id, p.id, ? FROM collections c, photos p "
                "WHERE c.id = ? AND c.query IS NULL AND p.id = ?",
                [(now, collection_id, photo) for photo in photo_ids],
            )
            return self.db.total_changes - before

    def remove_from_collection(self, collection_id, photo_ids):
        with self.db:
            self.db.executemany(
                "DELETE FROM collection_photos WHERE collection_id = ? AND photo_id = ?",
                [(collection_id, photo) for photo in photo_ids],
            )

    def collection_photo_ids(self, collection_id):
        return {
            row[0]
            for row in self.db.execute(
                "SELECT photo_id FROM collection_photos WHERE collection_id = ?",
                (collection_id,),
            )
        }

    # Sidecars

    def sidecar_pending(self, after=0, limit=100):
        """[(id, volume key, path, available, changed_at)] of photos whose sidecar is out
        of date (never written, or behind their last change), by id, those after id `after`."""
        rows = self.db.execute(
            "SELECT p.id, v.key, p.path, p.available, p.changed_at FROM photos p "
            "JOIN volumes v ON v.id = p.volume_id WHERE p.id > ? AND p.changed_at IS NOT NULL "
            "AND (p.sidecar_at IS NULL OR p.sidecar_at < p.changed_at) ORDER BY p.id LIMIT ?",
            (after, limit),
        )
        return [(r[0], r[1], r[2], bool(r[3]), r[4]) for r in rows]

    def count_sidecars_pending(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM photos WHERE changed_at IS NOT NULL "
            "AND (sidecar_at IS NULL OR sidecar_at < changed_at)"
        ).fetchone()[0]

    def sidecar_state(self, photo_id):
        """What a photo's sidecar holds (xmp.SidecarState), or None for no such photo."""
        row = self.db.execute(
            "SELECT flag, rating, changed_at FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        if row is None:
            return None
        paths = self.keyword_paths()
        found = self.photo_keywords([photo_id])[photo_id]
        edits = self.db.execute(
            "SELECT copy, params FROM edits WHERE photo_id = ? ORDER BY copy", (photo_id,)
        )
        return xmp.SidecarState(
            updated=row[2] or 0.0,
            flag=row[0],
            rating=row[1],
            keywords=tuple(sorted((paths[k] for k in found if k in paths), key=str.casefold)),
            edits=tuple((r[0], r[1]) for r in edits),
            stack=self._stack_of(photo_id),
        )

    def sidecar_written(self, photo_id, changed_at, mtime_ns):
        """A sidecar was written holding the state as of `changed_at`; its file's time."""
        with self.db:
            self.db.execute(
                "UPDATE photos SET sidecar_at = ?, sidecar_mtime_ns = ? WHERE id = ?",
                (changed_at, mtime_ns, photo_id),
            )

    def sidecar_candidates(self, folder_id):
        """[(id, path, sidecar_mtime_ns)] of the available photos of a folder: where to
        look for a sidecar that changed."""
        rows = self.db.execute(
            "SELECT id, path, sidecar_mtime_ns FROM photos WHERE folder_id = ? AND available = 1",
            (folder_id,),
        )
        return [(r[0], r[1], r[2]) for r in rows]

    def forget_sidecar(self, photo_id):
        """Its sidecar is gone: a photo with something to keep gets a new one."""
        with self.db:
            self.db.execute(
                "UPDATE photos SET sidecar_at = NULL, sidecar_mtime_ns = NULL WHERE id = ?",
                (photo_id,),
            )

    def note_sidecar(self, photo_id, mtime_ns):
        """A sidecar file that is not read (another program's) is as it was at `mtime_ns`."""
        with self.db:
            self.db.execute(
                "UPDATE photos SET sidecar_mtime_ns = ? WHERE id = ?", (mtime_ns, photo_id)
            )

    def apply_sidecar(self, photo_id, state, mtime_ns):
        """Take a sidecar's state (xmp.SidecarState) in when it is newer than the library's
        own change of the photo; else the library's stays, and is written to the sidecar
        again. Returns whether the sidecar was used."""
        with self.db:
            row = self.db.execute(
                "SELECT changed_at FROM photos WHERE id = ?", (photo_id,)
            ).fetchone()
            if row is None:
                return False
            used = state.updated > (row[0] or 0.0)
            if used:
                self._take_sidecar(photo_id, state)
            self.db.execute(
                "UPDATE photos SET sidecar_at = ?, sidecar_mtime_ns = ? WHERE id = ?",
                (state.updated, mtime_ns, photo_id),
            )
        return used

    def _take_sidecar(self, photo_id, state):
        flag = state.flag if state.flag in FLAGS else UNFLAGGED
        rating = state.rating if state.rating in RATINGS else 0
        self.db.execute(
            "UPDATE photos SET flag = ?, rating = ?, changed_at = ?, keywords_read = 1 "
            "WHERE id = ?",
            (flag, rating, state.updated, photo_id),
        )
        self.db.execute("DELETE FROM photo_keywords WHERE photo_id = ?", (photo_id,))
        for path in state.keywords:
            self.db.execute(
                "INSERT OR IGNORE INTO photo_keywords (photo_id, keyword_id) VALUES (?, ?)",
                (photo_id, self._keyword(path)),
            )
        self.db.execute("DELETE FROM edits WHERE photo_id = ?", (photo_id,))
        self.db.executemany(
            "INSERT OR REPLACE INTO edits (photo_id, copy, params, updated_at) VALUES (?, ?, ?, ?)",
            [(photo_id, copy, params, state.updated) for copy, params in state.edits],
        )
        if state.stack is None:
            self.db.execute(
                "UPDATE photos SET stack_id = NULL, stack_position = 0 WHERE id = ?", (photo_id,)
            )
        else:
            key, position = state.stack
            self.db.execute("INSERT OR IGNORE INTO stacks (key) VALUES (?)", (key,))
            self.db.execute(
                "UPDATE photos SET stack_id = (SELECT id FROM stacks WHERE key = ?), "
                "stack_position = ? WHERE id = ?",
                (key, position, photo_id),
            )
        # Not tidied: the stack's other photos' sidecars are read one by one, and a stack
        # of one photo meanwhile is a stack to be (the grid shows it as no stack)

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
        with self.db:
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
        with self.db:
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
        with self.db:
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
        with self.db:
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
        with self.db:
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
            self._touch([photo_id])

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
            self._touch(edits)

    def delete_edit(self, photo_id, copy=0):
        """Back to the original. The photo's own edit and its copies are separate."""
        with self.db:
            self.db.execute("DELETE FROM edits WHERE photo_id = ? AND copy = ?", (photo_id, copy))
            self._touch([photo_id])

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
            self._touch([photo_id])
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

    # Imports

    def add_import_job(self, folder_id, items):
        """An import job that copies `items` (ImportItem, in order) into the library
        folder `folder_id` (whose scan then finds them), queued: in one transaction.
        Returns the job's id."""
        now = time.time()
        # Before the transaction: volume_id() commits one of its own
        keys = {item.source_key for item in items} | {item.key for item in items}
        keys |= {item.backup_key for item in items if item.backup_key}
        ids = {key: self.volume_id(key) for key in keys}
        with self.db:
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
        with self.db:
            self.db.execute(
                "UPDATE import_items SET state = ?, path = ?, message = ? WHERE id = ?",
                (state, path.strip("/") if path else None, message, item_id),
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
    if shown.collection_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM collection_photos m WHERE m.photo_id = p.id "
            "AND m.collection_id = ?)"
        )
        args.append(shown.collection_id)
    if shown.import_job is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM import_items i WHERE i.job_id = ? AND i.state = ? "
            "AND i.volume_id = p.volume_id AND i.path = p.path)"
        )
        args.extend([shown.import_job, COPIED])
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
