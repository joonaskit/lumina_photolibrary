"""Scanning a library folder: the job that keeps the database in step with the disk.

Two steps. Finding: walk the folder, add new photos, note changed ones (size or
time), and mark those no longer there unavailable. Details: read each new or
changed photo's content id and metadata, in batches through one exiftool.

The rules it keeps:
- Files are only read. A photo that is not found is marked unavailable, with all
  that is known of it kept; it is available again when it is back.
- Nothing is marked missing unless the walk saw everything: not after a cancel,
  and not below a folder that could not be read.
- Every failure names its file, in the job's failures.
- The job is `done` only when both steps finished; a cancel stops the work, then
  reports `cancelled`.
- A large scan can be gentle: from GENTLE_FROM photos it rests after each batch for as
  long as the batch took, and exiftool runs at the lowest priority, so the desktop
  stays responsive through a first import of a library of hundreds of thousands.
- Everything is written as it goes, so a scan stopped by a quit or a crash is run
  again from the start and skips what it did: unchanged photos and read details.
"""

import os
import time

from lumina.core import db as dbm
from lumina.core import metadata, scanner, volumes
from lumina.core.errors import PhotoError
from lumina.core.fingerprint import content_id

SCAN = "scan"
_WRITE_EVERY = 500
_DETAILS_BATCH = 50
# How often progress is written to the database, in seconds
_SAVE_EVERY = 1.0
# A gentle scan rests from this many photos on: a small one is over before it is noticed
GENTLE_FROM = 2000
# It rests this many times as long as the work before the rest took, but never longer
# than _REST_MOST seconds
_REST_RATIO = 1.0
_REST_MOST = 5.0
_REST_STEP = 0.1

FINDING, DETAILS = "finding", "details"


class NotConnected(Exception):
    """The folder's volume is not mounted: the job waits for it."""


def rest(seconds, should_stop):
    """Wait, a tenth of a second at a time, so that a cancel is not waited for."""
    end = time.monotonic() + seconds
    while not should_stop() and (left := end - time.monotonic()) > 0:
        time.sleep(min(_REST_STEP, left))


class _Job:
    def __init__(self, library, job_id, report):
        self.library = library
        self.id = job_id
        self._report = report
        self._saved = 0.0

    def progress(self, phase, done, total, changed=False, force=False):
        now = time.monotonic()
        if force or now - self._saved >= _SAVE_EVERY:
            self.library.update_job(self.id, phase=phase, done=done, total=total)
            self._saved = now
        self._report(phase, done, total, changed)

    def end(self, state, message):
        self.library.update_job(self.id, state=state, message=message)


def scan(
    library,
    job_id,
    folder_id,
    mounts,
    should_stop=lambda: False,
    report=lambda phase, done, total, changed: None,
    exiftool=metadata.ExifTool,
    gentle=False,
    rest=rest,
):
    """Run scan job `job_id` for folder `folder_id` to its end and return its state.
    `mounts` are the system's mounts (volumes.read_mounts()); `exiftool` makes an
    ExifTool. With `gentle`, a large scan rests between batches (`rest(seconds,
    should_stop)`) and reads at a low priority. Raises NotConnected, leaving the job
    queued, when the folder's volume is not mounted."""
    folder = library.folder(folder_id)
    if folder is None:
        library.update_job(job_id, state=dbm.FAILED_JOB, message="The folder is not in the library")
        return dbm.FAILED_JOB
    base = volumes.resolve(folder.volume_key, folder.path, mounts)
    if base is None:
        raise NotConnected(folder.label or folder.last_mount or folder.path)
    job = _Job(library, job_id, report)
    library.update_job(job_id, state=dbm.RUNNING, phase=FINDING, done=0, total=None, message=None)
    volume = library.volume_id(folder.volume_key, mount=_mount_point(base, mounts))

    if not os.path.isdir(base):
        # The volume is there and the folder is not: its photos are missing, not gone
        library.mark_missing(state[0] for state in library.photo_states(folder_id).values())
        library.add_failures(job_id, [(base, "Folder not found")])
        job.end(dbm.FAILED_JOB, f"Folder not found: {base}")
        return dbm.FAILED_JOB

    found = _find(library, job, folder, volume, base, should_stop, gentle, rest)
    if found is None:
        job.end(dbm.CANCELLED, "Cancelled while finding photos")
        return dbm.CANCELLED
    counts, failures = found

    details = _details(library, job, folder, base, should_stop, exiftool, gentle, rest)
    if details is None:
        job.end(dbm.CANCELLED, "Cancelled while reading details")
        return dbm.CANCELLED
    state, note, unread = details
    library.mark_scanned(folder_id)
    job.end(state, _summary(library, folder_id, counts, failures + unread, note))
    return state


def _find(library, job, folder, volume, base, should_stop, gentle=False, rest=rest):
    """Walk the folder. Returns ({"new", "changed", "missing"}: count, failures) or None
    when cancelled."""
    known = library.photo_states(folder.id)
    seen, unreadable, failures = set(), [], []
    new, changed, returned = [], [], []
    counts = {"new": 0, "changed": 0, "missing": 0}

    def unreadable_folder(path, message):
        unreadable.append(_relative(folder.path, base, path))
        failures.append((path, f"Cannot read folder: {message}"))

    def flush():
        if new or changed or returned:
            library.write_scan(folder.id, volume, new, changed, returned)
            counts["new"] += len(new)
            counts["changed"] += len(changed)
            new.clear(), changed.clear(), returned.clear()
            return True
        return False

    worked = time.monotonic()
    for path in scanner.find_photos(base, should_stop, on_error=unreadable_folder):
        relative = _relative(folder.path, base, path)
        try:
            relative.encode("utf-8")
            info = os.stat(path)
        except UnicodeEncodeError:
            failures.append((path, "The name is not valid UTF-8"))
            continue
        except OSError as error:
            failures.append((path, error.strerror or str(error)))
            continue
        seen.add(relative)
        row = (relative, info.st_size, info.st_mtime_ns, dbm.local_time(info.st_mtime_ns))
        state = known.get(relative)
        if state is None:
            new.append(row)
        elif (state[1], state[2]) != (info.st_size, info.st_mtime_ns):
            changed.append((state[0], *row[1:]))
        elif not state[3]:
            returned.append(state[0])
        if len(new) + len(changed) + len(returned) >= _WRITE_EVERY:
            job.progress(FINDING, len(seen), None, changed=flush())
            if gentle and len(seen) >= GENTLE_FROM:
                rest(min(_REST_MOST, (time.monotonic() - worked) * _REST_RATIO), should_stop)
            worked = time.monotonic()
    if should_stop():
        flush()
        return None
    job.progress(FINDING, len(seen), None, changed=flush(), force=True)

    gone = [
        state[0]
        for relative, state in known.items()
        if state[3] and relative not in seen and not _under_any(relative, unreadable)
    ]
    library.mark_missing(gone)
    counts["missing"] = len(gone)
    library.add_failures(job.id, failures)
    return counts, len(failures)


def _details(library, job, folder, base, should_stop, exiftool, gentle=False, rest=rest):
    """Read content ids and metadata. Returns (state, note, failures) or None when
    cancelled."""
    total = library.count_pending_metadata(folder.id)
    done = 0
    job.progress(DETAILS, 0, total, force=True)
    if total == 0:
        return dbm.DONE, None, 0
    try:
        tool = exiftool()
    except metadata.MetadataUnavailable as error:
        # Photos are listed without their details; the next scan reads them
        return dbm.DONE, f"Details not read: {error}", 0
    resting = gentle and total >= GENTLE_FROM
    if resting:
        tool.set_gentle(True)
    failures = 0
    try:
        while True:
            if should_stop():
                return None
            began = time.monotonic()
            batch = library.pending_metadata(folder.id, _DETAILS_BATCH)
            if not batch:
                break
            read, failed = [], []
            paths = {photo: _absolute(folder.path, base, relative) for photo, relative in batch}
            ids = {}
            for photo, path in paths.items():
                try:
                    ids[photo] = content_id(path)
                except PhotoError as error:
                    failed.append((photo, None, error.message))
            try:
                results = tool.read_many([paths[photo] for photo in ids])
            except metadata.MetadataUnavailable as error:
                library.write_metadata(failed=failed)
                return dbm.FAILED_JOB, f"Reading details stopped: {error}", failures + len(failed)
            for photo, cid in ids.items():
                result = results[paths[photo]]
                if isinstance(result, PhotoError):
                    failed.append((photo, cid, result.message))
                else:
                    read.append((photo, cid, result))
            library.write_metadata(read, failed)
            library.add_failures(
                job.id, [(paths[photo], message) for photo, _cid, message in failed]
            )
            failures += len(failed)
            done += len(batch)
            job.progress(DETAILS, done, total, changed=True)
            if resting:
                rest(min(_REST_MOST, (time.monotonic() - began) * _REST_RATIO), should_stop)
    finally:
        tool.close()
    job.progress(DETAILS, done, total, force=True)
    return dbm.DONE, None, failures


def _relative(folder_path, base, path):
    below = os.path.relpath(path, base)
    below = "" if below == "." else below
    return "/".join(part for part in (folder_path, below) if part)


def _absolute(folder_path, base, relative):
    below = relative[len(folder_path) :].lstrip("/") if folder_path else relative
    return os.path.join(base, below)


def _under_any(relative, folders):
    return any(relative == f or relative.startswith(f + "/") for f in folders)


def _mount_point(path, mounts):
    mount = volumes.mount_of(path, mounts)
    return mount.mount_point if mount is not None else None


def _summary(library, folder_id, counts, failures, note):
    total = len(library.photo_states(folder_id))
    parts = [f"{total} photo" + ("" if total == 1 else "s")]
    for key, word in (("new", "new"), ("changed", "changed"), ("missing", "missing")):
        if counts[key]:
            parts.append(f"{counts[key]} {word}")
    if failures:
        parts.append(f"{failures} could not be read")
    if note:
        parts.append(note)
    return ", ".join(parts)
