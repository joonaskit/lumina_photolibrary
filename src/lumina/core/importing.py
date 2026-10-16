"""Importing: copying photos from a memory card, or any folder, into a library folder.

Two steps. Reading (`read_source`): every photo on the card, its size, content id and
the time it was taken, and whether the library has it already. Copying (`run`): a job
in the library's database, one row a photo (db.import_items), so a quit or a crash
resumes where it was.

The rules it keeps:
- The card is only read: nothing on it is written, renamed or deleted.
- A copy is made as a hidden part file beside where it goes, synced, read back and
  checked against what was read from the card, and only then put in place under the
  first free name by an exclusive create: a file that is there is never written over.
  One that is there with the same bytes is the copy: an import run again, or resumed
  after a crash just after a file was put in place, copies nothing twice.
- A backup copy, when asked for, is made the same way into a second folder.
- What could not be copied is named, file by file, in the job's failures; the rest
  goes on. A cancel stops before reporting `cancelled`; the part file goes.
"""

import hashlib
import os
import shutil
from dataclasses import dataclass, replace
from datetime import datetime

from lumina.core import capture_time, derived, metadata, scanner, volumes
from lumina.core import db as dbm
from lumina.core.errors import PhotoError
from lumina.core.fingerprint import content_id
from lumina.core.library import NotConnected, _Job

IMPORT = "import"
READING, COPYING = "reading", "copying"

# Where in the library folder a photo goes, by the day it was taken (strftime)
LAYOUTS = {
    "year-day": "%Y/%Y-%m-%d",
    "year-month": "%Y/%m",
    "day": "%Y-%m-%d",
    "none": "",
}
# What a copy is called: as on the card, or the time it was taken first
# (20261014-153012-IMG_0001.CR3), which sorts by time in any file manager
NAMINGS = ("original", "date")

_CHUNK = 1024 * 1024
_DETAILS_BATCH = 50


@dataclass(frozen=True)
class Candidate:
    """A photo on the card, as read."""

    path: str
    size: int
    content_id: str | None
    # When it was taken (the camera's local time), or the file's time without that
    taken: datetime
    # In the library already, or imported before
    imported: bool = False


@dataclass(frozen=True)
class Reading:
    candidates: tuple
    # (path, why) of the photos that could not be read
    failures: tuple


def find_cards(mounts):
    """The mount points of the mounted cards: filesystems with a DCIM folder at their top
    (where every camera puts its photos), in the order mounted."""
    found = []
    for mount in mounts:
        if mount.mount_point == "/" or mount.root != "/":
            continue
        if os.path.isdir(os.path.join(mount.mount_point, "DCIM")):
            found.append(mount.mount_point)
    return found


def read_source(
    library,
    root,
    should_stop=lambda: False,
    report=lambda done, total: None,
    exiftool=metadata.ExifTool,
):
    """Read the photos under `root` (a card or a folder) for an import: a Reading, or
    None when stopped. Only reads. The time a photo was taken is read from its first
    bytes (capture_time), from exiftool for a photo they don't tell it of, and from the
    file without exiftool."""
    failures = []
    found = list(
        scanner.find_photos(
            root, should_stop, on_error=lambda path, message: failures.append((path, message))
        )
    )
    if should_stop():
        return None
    report(0, len(found))
    read, undated = [], []
    for number, path in enumerate(found, 1):
        if should_stop():
            return None
        try:
            info = os.stat(path)
            cid = content_id(path)
        except PhotoError as error:
            failures.append((path, error.message))
            continue
        except OSError as error:
            failures.append((path, error.strerror or str(error)))
            continue
        candidate = Candidate(path, info.st_size, cid, capture_time.read(path))
        (read if candidate.taken is not None else undated).append(candidate)
        report(number, len(found))
    dated = _ask_exiftool(undated, exiftool, should_stop)
    if dated is None:
        return None
    read.extend(dated)
    read.sort(key=lambda c: (c.taken, c.path))
    known = library.imported(candidate.content_id for candidate in read)
    candidates = tuple(replace(c, imported=c.content_id in known) for c in read)
    return Reading(candidates, tuple(failures))


def _ask_exiftool(undated, exiftool, should_stop):
    """The Candidates `undated` with the time exiftool reads, or the file's time without
    it. None when stopped."""
    if not undated:
        return []
    try:
        tool = exiftool()
    except metadata.MetadataUnavailable:
        tool = None
    dated = []
    try:
        for start in range(0, len(undated), _DETAILS_BATCH):
            if should_stop():
                return None
            batch = undated[start : start + _DETAILS_BATCH]
            details = {}
            if tool is not None:
                try:
                    details = tool.read_many([c.path for c in batch])
                except metadata.MetadataUnavailable:
                    tool.close()
                    tool = None
            for candidate in batch:
                data = details.get(candidate.path)
                taken = data.captured if isinstance(data, metadata.Metadata) else None
                if taken is None:
                    taken = _file_time(candidate.path)
                dated.append(replace(candidate, taken=taken.replace(tzinfo=None)))
    finally:
        if tool is not None:
            tool.close()
    return dated


def _file_time(path):
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime)
    except OSError:
        return datetime.now()


def subfolder(taken, layout):
    """The folder under the library folder that a photo taken at `taken` goes in, from
    the library folder: "2026/2026-10-14", or "" for none."""
    pattern = LAYOUTS[layout]
    return taken.strftime(pattern) if pattern else ""


def file_name(candidate, naming):
    """What the copy of `candidate` is called."""
    name = os.path.basename(candidate.path)
    if naming == "date":
        return f"{candidate.taken:%Y%m%d-%H%M%S}-{name}"
    return name


def queue(
    library, candidates, destination, mounts, layout="year-day", naming="original", backup=None
):
    """Queue an import of `candidates` into the folder `destination` (an absolute path in
    a library folder), laid out by `layout` and named by `naming`, with a copy of each in
    the folder `backup` (an absolute path) too, unless None. Returns the job's id.
    Raises ValueError saying why it cannot be done."""
    if layout not in LAYOUTS or naming not in NAMINGS:
        raise ValueError(f"Not a way to lay out or name photos: {layout}, {naming}")
    if not candidates:
        raise ValueError("No photos to import")
    where = volumes.locate(destination, mounts)
    if where is None:
        raise ValueError(f"No mounted filesystem holds {destination}")
    key, inside = where
    folder_id = library.folder_holding(key, inside)
    if folder_id is None:
        raise ValueError(f"{destination} is not in the library")
    backup_at = None
    if backup is not None:
        backup_at = volumes.locate(backup, mounts)
        if backup_at == where:
            raise ValueError("The backup copies need a folder of their own")
        if backup_at is None:
            raise ValueError(f"No mounted filesystem holds {backup}")
    items = []
    for candidate in candidates:
        source = volumes.locate(candidate.path, mounts)
        if source is None:
            raise ValueError(f"No mounted filesystem holds {candidate.path}")
        below = subfolder(candidate.taken, layout)
        items.append(
            dbm.ImportItem(
                source_key=source[0],
                source_path=source[1],
                size=candidate.size,
                content_id=candidate.content_id,
                key=key,
                folder=_join(inside, below),
                name=file_name(candidate, naming),
                backup_key=backup_at[0] if backup_at else None,
                backup_folder=_join(backup_at[1], below) if backup_at else None,
            )
        )
    return library.add_import_job(folder_id, items)


def run(library, job_id, mounts, should_stop=lambda: False, report=None):
    """Run import job `job_id` to its end and return its state. Raises NotConnected,
    leaving the job queued, when the card's or a destination's drive is not mounted."""
    report = report or (lambda phase, done, total, changed: None)
    job = _Job(library, job_id, report)
    items = library.import_items(job_id)
    waiting = [item for item in items if item.state == dbm.TO_COPY]
    for item in waiting:
        for key, path in _places(item):
            if volumes.resolve(key, path, mounts) is None:
                raise NotConnected(path)
    total = len(items)
    done = total - len(waiting)
    library.update_job(job_id, state=dbm.RUNNING, phase=COPYING, done=done, total=total)
    short = _short_of_space(waiting, mounts)
    if short is not None:
        job.end(dbm.FAILED_JOB, short)
        return dbm.FAILED_JOB
    failed = sum(1 for item in items if item.state == dbm.NOT_COPIED)
    for item in waiting:
        if should_stop():
            job.end(dbm.CANCELLED, "Cancelled")
            return dbm.CANCELLED
        source = volumes.resolve(item.source_key, item.source_path, mounts)
        try:
            path = _copy_item(item, source, mounts, job_id, should_stop)
        except (PhotoError, OSError) as error:
            if not isinstance(error, PhotoError):
                error = PhotoError(error.filename or source, error.strerror or str(error))
            library.set_import_item(item.id, dbm.NOT_COPIED, message=error.message)
            library.add_failures(job_id, [(error.path, error.message)])
            failed += 1
        else:
            if path is None:
                job.end(dbm.CANCELLED, "Cancelled")
                return dbm.CANCELLED
            library.set_import_item(item.id, dbm.COPIED, path=path)
        done += 1
        job.progress(COPYING, done, total, changed=True)
    job.progress(COPYING, done, total, force=True)
    job.end(dbm.DONE, _summary(total - failed, failed))
    return dbm.DONE


def _places(item):
    yield item.source_key, item.source_path
    yield item.key, item.folder
    if item.backup_key:
        yield item.backup_key, item.backup_folder


def _copy_item(item, source, mounts, job_id, should_stop):
    """Copy one item to its folder, and to its backup folder. Returns the copy's path
    from its volume's root, or None when stopped."""
    stem, suffix = os.path.splitext(item.name)
    copies = [(item.key, item.folder)]
    if item.backup_key:
        copies.append((item.backup_key, item.backup_folder))
    result = None
    for key, folder in copies:
        where = volumes.resolve(key, folder, mounts)
        os.makedirs(where, exist_ok=True)
        part = os.path.join(where, f".lumina-import-{job_id}-{item.id}.part")
        try:
            digest = copy_file(source, part, should_stop)
            if digest is None:
                derived.remove(part)
                return None
            placed = derived.place(
                part, where, stem, suffix, same=lambda path, d=digest: _same(path, item.size, d)
            )
        except BaseException:
            derived.remove(part)
            raise
        if result is None:
            result = volumes.locate(placed, mounts)[1]
    return result


def copy_file(source, part, should_stop=lambda: False):
    """Copy the file `source` to `part` (made or emptied), synced, and read back to check
    it: the copy's hash, or None when stopped. Raises PhotoError naming the file when
    the copy is not what was read. `part`'s times are `source`'s."""
    digest = hashlib.blake2b()
    with open(source, "rb") as reading:
        info = os.fstat(reading.fileno())
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            while chunk := reading.read(_CHUNK):
                if should_stop():
                    return None
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
    os.utime(part, ns=(info.st_atime_ns, info.st_mtime_ns))
    wanted = digest.hexdigest()
    if _hash(part, drop_cache=True) != wanted:
        raise PhotoError(source, "the copy is not the same as the photo on the card")
    return wanted


def _hash(path, drop_cache=False):
    digest = hashlib.blake2b()
    with open(path, "rb") as file:
        if drop_cache and hasattr(os, "posix_fadvise"):
            # Read what is on the disk, not what is still in memory from writing it
            os.posix_fadvise(file.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _same(path, size, digest):
    """Whether the file at `path` is `size` bytes hashing to `digest`."""
    try:
        return os.path.getsize(path) == size and _hash(path) == digest
    except OSError:
        return False


def _short_of_space(items, mounts):
    """Why the items do not fit where they go, or None when they do."""
    # Per drive: the bytes going there, and a folder on it
    needed = {}
    for item in items:
        for key, folder in list(_places(item))[1:]:
            size, _folder = needed.get(key, (0, folder))
            needed[key] = (size + item.size, folder)
    for key, (size, folder) in needed.items():
        where = _existing(volumes.resolve(key, folder, mounts))
        free = shutil.disk_usage(where).free
        if size > free:
            return (
                f"Not enough space in {where}: {_gigabytes(size)} needed, {_gigabytes(free)} free"
            )
    return None


def _existing(path):
    while not os.path.isdir(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


def _gigabytes(size):
    return f"{size / 1e9:.1f} GB"


def _join(folder, below):
    return "/".join(part for part in (folder.strip("/"), below) if part)


def _summary(copied, failed):
    text = f"Imported {copied} photo" + ("" if copied == 1 else "s")
    if failed:
        text += f", {failed} could not be copied"
    return text
