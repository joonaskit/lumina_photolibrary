"""Photos made from others: what the jobs that make them share. HDR merges (core/hdr.py)
and panoramas (core/panorama.py).

A job (`run`) finds its sources where they are now, works in a folder of its own
(`default_work_root`), and writes its result as a hidden part file beside the first
source (`start_result`), which is put in place under the first free name by an
exclusive create (`place`): a file that is there is never written over. A cancel or
failure removes the part; the originals are only read.
"""

import json
import os
import shutil

from lumina.core import db as dbm
from lumina.core import linear_tiff, volumes
from lumina.core.errors import PhotoError
from lumina.core.library import NotConnected, _Job

DECODING = "decoding"


def default_work_root():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "lumina", "merge")


def run(library, job_id, mounts, make, finished, report, work_root, keep_work):
    """Run job `job_id`, which makes a derived photo, to its end and return its state.

    `make(job, paths, settings, work, part)` does the work: the sources' paths now, the
    derived photo's settings (a dict), the work folder and the part file to write; it
    returns (state, the result's path or a message, PhotoError or None).
    `finished(count, name)` is the job's message when done. A job stopped while
    `keep_work()` is true (a quit) keeps its work folder for the next start; otherwise
    the folder goes when the job ends. Raises NotConnected, leaving the job queued,
    when a source's drive is not mounted."""
    job = _Job(library, job_id, report)
    record = library.job(job_id)
    derived = library.derived(record.derived_id) if record and record.derived_id else None
    if derived is None:
        job.end(dbm.FAILED_JOB, "Nothing to make: the job lost its photos")
        return dbm.FAILED_JOB
    settings = json.loads(derived.settings)
    paths = []
    for item in derived.sources:
        where = volumes.resolve(item.volume_key, item.path, mounts)
        if where is None:
            raise NotConnected(item.path)
        paths.append(where)
    work = os.path.join(work_root or default_work_root(), str(job_id))
    folder = os.path.dirname(paths[settings.get("beside", 0)])
    part = os.path.join(folder, f".lumina-merge-{job_id}.part")
    library.update_job(job_id, state=dbm.RUNNING, phase=DECODING, done=0, total=len(paths))
    state, message, failed = make(job, paths, settings, work, part)
    if failed is not None:
        library.add_failures(job_id, [(failed.path, failed.message)])
    if state != dbm.DONE:
        remove(part)
        if not (state == dbm.CANCELLED and keep_work()):
            shutil.rmtree(work, ignore_errors=True)
        job.end(state, message)
        return state
    shutil.rmtree(work, ignore_errors=True)
    where = volumes.locate(message, mounts)
    if where is not None:
        library.set_derived_result(derived.id, *where)
    job.end(dbm.DONE, finished(len(paths), os.path.basename(message)))
    return dbm.DONE


def check_sources(paths):
    """Raise PhotoError naming the first of `paths` that is not there."""
    for path in paths:
        if not os.path.isfile(path):
            raise PhotoError(path, "is missing: the photo is not where it was")


def failure(error, part, doing):
    """(state, message, PhotoError) of a job that `error` (PhotoError or OSError) ended
    while `doing` ("merge", "stitch")."""
    if not isinstance(error, PhotoError):
        error = PhotoError(error.filename or part, error.strerror or str(error))
    return dbm.FAILED_JOB, f"Could not {doing}: {error.message}", error


# The result file


def start_result(part, width, height, tags):
    """The result file `part`, laid out for `width` x `height` with `tags` (a dict of
    linear_tiff.Tags), ready for its rows."""
    file_layout = linear_tiff.layout(width, height, linear_tiff.Tags(**tags))
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        linear_tiff.start(fd, file_layout)
    finally:
        os.close(fd)


def write_rows(part, width, height, tags, first, rows):
    """`rows` (floats) at row `first` of the result file `part` (laid out by
    `start_result` with the same size and tags)."""
    file_layout = linear_tiff.layout(width, height, linear_tiff.Tags(**tags))
    fd = os.open(part, os.O_WRONLY)
    try:
        linear_tiff.write_rows(fd, file_layout, first, rows)
    finally:
        os.close(fd)


def finish_result(part):
    """The result file's bytes on the disk, before it is put in place."""
    fd = os.open(part, os.O_WRONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def place(part, folder, stem, suffix=".tif", same=None):
    """`part` put in place as `stem` + `suffix` in `folder`, or -2, -3 and so on: the
    first name no file has. A name is claimed by creating it exclusively, so a file that
    appears meanwhile is never written over. Returns the path.

    With `same(path)`, a file already there for which it is true is taken as the result:
    `part` is removed and that file's path returned."""
    for number in range(1, 10000):
        name = f"{stem}{suffix}" if number == 1 else f"{stem}-{number}{suffix}"
        destination = os.path.join(folder, name)
        try:
            claim = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            if same is not None and same(destination):
                remove(part)
                return destination
            continue
        os.close(claim)
        try:
            # Over the empty file just made, which is ours
            os.replace(part, destination)
        except OSError:
            remove(destination)
            raise
        return destination
    raise PhotoError(os.path.join(folder, stem + suffix), "every name for it is taken")


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# What the result says of itself


def tags_of(row):
    """The linear_tiff.Tags (as a dict) a result takes from the photo `row`: its camera,
    lens and time."""
    return {
        "make": row.get("make"),
        "model": row.get("model"),
        "lens": row.get("lens"),
        "captured": exif_time(row.get("captured")),
        "subseconds": subseconds(row.get("captured")),
    }


def subseconds(captured):
    # "2026-10-16 14:03:05.120000" -> "12"
    if not captured or "." not in captured:
        return None
    return captured.split(".", 1)[1].rstrip("0") or None


def exif_time(captured):
    # "2026-10-16 14:03:05.120000" -> "2026:10:16 14:03:05"
    if not captured:
        return None
    return captured[:19].replace("-", ":", 2)


def check_camera(rows):
    """Raise ValueError naming the first of the photos `rows` from another camera than
    the first one's."""
    first = rows[0]
    for row in rows[1:]:
        if (row.get("make"), row.get("model")) != (first.get("make"), first.get("model")):
            raise ValueError(
                f"{os.path.basename(row['path'])} is from another camera "
                f"({row.get('model') or 'unknown'}, not {first.get('model') or 'unknown'})"
            )


def rows_to_queue(library, photo_ids):
    """The photos `photo_ids` as dicts, in that order. Raises ValueError when one is no
    longer in the library or is missing."""
    rows = library.photo_rows(photo_ids)
    if len(rows) != len(set(photo_ids)):
        raise ValueError("Some of the photos are no longer in the library")
    for row in rows:
        if not row["available"]:
            raise ValueError(f"{os.path.basename(row['path'])} is missing")
    return rows
