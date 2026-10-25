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

import contextlib
import os
import sqlite3

from lumina.core import backup
from lumina.core.dbtypes import *  # noqa: F403 (the records and constants, as `db.X`)
from lumina.core.dbutil import local_time  # noqa: F401
from lumina.core.stores.collections import CollectionsStore
from lumina.core.stores.derived import DerivedStore
from lumina.core.stores.edits import EditsStore
from lumina.core.stores.embeddings import EmbeddingsStore
from lumina.core.stores.exports import ExportsStore
from lumina.core.stores.folders import FoldersStore
from lumina.core.stores.imports import ImportsStore
from lumina.core.stores.jobs import JobsStore
from lumina.core.stores.keywords import KeywordsStore
from lumina.core.stores.marks import MarksStore
from lumina.core.stores.photos import PhotosStore
from lumina.core.stores.sharpness import SharpnessStore
from lumina.core.stores.sidecars import SidecarsStore
from lumina.core.stores.stacks import StacksStore
from lumina.core.stores.tether import TetherStore


def default_path():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "lumina", "library.sqlite3")


class NewerDatabase(Exception):
    """The file was written by a newer Lumina: it is left alone."""


class NoBackup(Exception):
    """The file needs a migration and could not be backed up first: it is left alone."""


def _script(db, sql):
    """Run the statements of `sql` one by one in the transaction open on `db`.
    `executescript` commits what is open before it runs and then autocommits every
    statement, so a migration that failed half way would stay half applied."""
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        db.execute(statement)


def _migrate_1(db):
    _script(
        db,
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
        """,
    )


def _migrate_2(db):
    _script(
        db,
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
        """,
    )


def _migrate_3(db):
    _script(
        db,
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
        """,
    )


def _migrate_4(db):
    _script(
        db,
        """
        -- Thumbs up (1) or down (-1), or none (0), for going through new photos. One a
        -- photo, not per copy; kept while the file is missing, as its edits are
        ALTER TABLE photos ADD COLUMN flag INTEGER NOT NULL DEFAULT 0;
        """,
    )


def _migrate_5(db):
    _script(
        db,
        """
        -- Stars, 1 to 5, or none (0). Separate from the flag; one a photo, not per copy,
        -- and kept while the file is missing, as the flag and the edits are
        ALTER TABLE photos ADD COLUMN rating INTEGER NOT NULL DEFAULT 0;
        """,
    )


def _migrate_6(db):
    _script(
        db,
        """
        -- exiftool's warning when it says the raw data may be damaged (metadata's
        -- damage_warning), or NULL. Read with the details; the CR3s read before it are
        -- read again by the next scan, so that those damaged already are found
        ALTER TABLE photos ADD COLUMN damage_warning TEXT;
        UPDATE photos SET metadata_state = 0 WHERE metadata_state = 1 AND file_type = 'CR3';
        """,
    )


def _migrate_7(db):
    _script(
        db,
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
        """,
    )


def _migrate_8(db):
    _script(
        db,
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
        """,
    )


def _migrate_9(db):
    _script(
        db,
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
        """,
    )


def _migrate_10(db):
    _script(
        db,
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
        """,
    )


def _migrate_11(db):
    _script(
        db,
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
        """,
    )


def _migrate_12(db):
    _script(
        db,
        """
        -- A session of shooting with a camera on USB (core/tether.py): the folder its
        -- frames go in, from the volume's root, where the camera keeps them ('card' or
        -- 'computer'), and the collection that gathers its photos
        CREATE TABLE tether_sessions (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            volume_id INTEGER NOT NULL REFERENCES volumes(id),
            folder TEXT NOT NULL,
            keep TEXT NOT NULL,
            collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
            started_at REAL NOT NULL
        );
        -- A frame the camera told of, noted before it is fetched: one a crash or an
        -- unplug cut short is fetched again from the card, or named as lost. `camera` is
        -- its serial number; `name` what the frame was saved as in the session's folder
        CREATE TABLE tether_items (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES tether_sessions(id) ON DELETE CASCADE,
            camera TEXT NOT NULL,
            camera_folder TEXT NOT NULL,
            camera_name TEXT NOT NULL,
            on_card INTEGER NOT NULL,
            state INTEGER NOT NULL DEFAULT 0,
            name TEXT,
            message TEXT,
            added_at REAL NOT NULL
        );
        CREATE INDEX tether_items_camera ON tether_items (camera, state);
        CREATE INDEX tether_items_session ON tether_items (session_id, state);
        """,
    )


def _migrate_13(db):
    _script(
        db,
        """
        -- The photos of a folder in a stretch of time (the bracket search in a folder of
        -- hundreds of thousands) without reading all of the folder
        CREATE INDEX photos_folder_time ON photos (folder_id, COALESCE(captured, file_time));
        """,
    )


def _migrate_14(db):
    _script(
        db,
        """
        -- An export of photos to a folder (core/exporting.py): a job, how to write the
        -- files, and a row a file, so a quit or a crash resumes where it was. The
        -- destinations are anywhere the user chose, not in a library volume
        CREATE TABLE export_jobs (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
            options TEXT NOT NULL,
            replace INTEGER NOT NULL DEFAULT 0,
            folder TEXT NOT NULL
        );
        CREATE TABLE export_items (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            copy INTEGER NOT NULL DEFAULT 0,
            destination TEXT NOT NULL,
            state INTEGER NOT NULL DEFAULT 0,
            message TEXT
        );
        CREATE INDEX export_items_job ON export_items (job_id, state);
        """,
    )


def _migrate_15(db):
    _script(
        db,
        """
        -- How sharp a photo is (core/sharpness.py), by content id as the vectors are: a
        -- moved, renamed or copied photo has the same score. A score of NULL is a photo
        -- that could not be read, with the reason in `error`; a photo with no row is not
        -- scored yet
        CREATE TABLE sharpness (
            content_id TEXT PRIMARY KEY,
            score REAL,
            error TEXT
        );
        """,
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
    _migrate_12,
    _migrate_13,
    _migrate_14,
    _migrate_15,
]
SCHEMA_VERSION = len(_MIGRATIONS)


class Library(
    FoldersStore,
    PhotosStore,
    MarksStore,
    KeywordsStore,
    CollectionsStore,
    SidecarsStore,
    StacksStore,
    EditsStore,
    EmbeddingsStore,
    SharpnessStore,
    JobsStore,
    ImportsStore,
    ExportsStore,
    TetherStore,
    DerivedStore,
):
    """The connection, the schema and the transaction; the rest is in `core/stores/`, one
    class per area, all working on this one connection."""

    def __init__(self, path=None):
        self.path = os.fspath(path or default_path())
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self._depth = 0
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
            # One transaction for the migration and its version: DDL is transactional
            # in SQLite, so a failure rolls both back and the next start tries again
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                # Another connection opening the file at the same time may have done it
                if self.db.execute("PRAGMA user_version").fetchone()[0] != number:
                    continue
                _MIGRATIONS[number](self.db)
                self.db.execute(f"PRAGMA user_version = {number + 1}")

    @contextlib.contextmanager
    def transaction(self):
        """The one way to write. The outermost block takes the write lock at its start
        (so a write that first reads whether it is needed can't race another connection:
        the window, a worker, the tether process) and commits or rolls back at its end.
        A block inside another is a savepoint: it rolls back alone when it fails, and
        commits nothing until the outer block does."""
        if self._depth:
            name = f"lumina_{self._depth}"
            self.db.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield
            except BaseException:
                self.db.execute(f"ROLLBACK TO {name}")
                self.db.execute(f"RELEASE {name}")
                raise
            else:
                self.db.execute(f"RELEASE {name}")
            finally:
                self._depth -= 1
            return
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()
        finally:
            self._depth = 0
