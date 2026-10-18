"""The library database's constants and records."""

from dataclasses import dataclass

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
class ExportItem:
    """A file to export: a photo's edit (`copy` 0 is its own) to `destination`, a full
    path. `volume_key` and `path` say where the photo is."""

    id: int
    photo_id: int
    copy: int
    destination: str
    state: int
    message: str | None
    volume_key: str
    path: str


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


# A tethered camera's frame: still to fetch, saved, or not saved (with why)
TETHER_PENDING, TETHER_SAVED, TETHER_FAILED = 0, 1, 2


@dataclass(frozen=True)
class TetherSession:
    id: int
    name: str
    volume_key: str
    folder: str
    keep: str
    collection_id: int | None
    started_at: float


@dataclass(frozen=True)
class TetherItem:
    id: int
    session_id: int
    camera: str
    camera_folder: str
    camera_name: str
    on_card: bool
    state: int
    name: str | None
    message: str | None


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


_TETHER_ITEM = (
    "SELECT id, session_id, camera, camera_folder, camera_name, on_card, state, name, message "
    "FROM tether_items"
)


def _tether_item(row):
    return TetherItem(*row[:5], bool(row[5]), *row[6:])
