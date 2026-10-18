"""Exporting many photos to a folder, as a job in the library's database.

One row a file (db.export_items), so a quit or a crash resumes where it was and a file
done is not made again. The rules it keeps:
- The originals are only read. A file is never written over unless the job was made with
  Replace, and that is decided when the file is written (`export.export` places it by an
  exclusive create), not only when the job is planned: a file that appears meanwhile is
  kept and named as a failure.
- A photo whose drive is not connected is waited for, not failed. A photo that cannot be
  exported is named, with why, and the rest go on.
- A cancel stops before the next file (or inside one, when the render can be stopped)
  and reports `cancelled`; a part file goes with it. `done` is reported only when every
  file was made or named as failed.
"""

import dataclasses
import json
import os

from lumina.core import db as dbm
from lumina.core import volumes
from lumina.core.edit import export as _export
from lumina.core.edit import params as _params
from lumina.core.errors import PhotoError
from lumina.core.library import NotConnected, _Job

EXPORT = "export"
EXPORTING = "exporting"

# An item's `state`
TO_DO, EXPORTED, NOT_EXPORTED = 0, 1, 2


def destinations(photos, folder, options):
    """The files to write: [(photo id, copy, destination)] for `photos`, [(photo id, copy,
    the photo's path)], named as `export.suggested_name` says. Two photos that would have
    one name get "-2", "-3" and so on; a name that is the photo's own, in its own folder,
    gets "-edit": an export never writes to the original."""
    taken, planned = set(), []
    for photo_id, copy, source in photos:
        name = _export.suggested_name(source, options, copy)
        stem, extension = os.path.splitext(name)

        def free(candidate, source=source):
            path = os.path.join(folder, candidate)
            return candidate.casefold() not in taken and not _same_file(path, source)

        candidate = name
        if not free(candidate):
            own = _same_file(os.path.join(folder, name), source)
            candidate = f"{stem}-edit{extension}" if own else f"{stem}-2{extension}"
            number = 2
            while not free(candidate):
                number += 1
                candidate = f"{stem}-{number}{extension}"
        taken.add(candidate.casefold())
        planned.append((photo_id, copy, os.path.join(folder, candidate)))
    return planned


def _same_file(path, other):
    if other is None:
        return False
    return os.path.realpath(path) == os.path.realpath(other)


def queue(library, entries, folder, options, replace=False, mounts=None):
    """Queue an export of `entries` [(photo id, copy)] to `folder` and return the job's
    id. `mounts` (volumes.read_mounts()) lets a photo's own folder be told from the
    destination."""
    options.check()
    rows = {row["id"]: row for row in library.photo_rows([photo for photo, _copy in entries])}
    photos = []
    for photo_id, copy in entries:
        row = rows.get(photo_id)
        if row is None:
            continue
        source = volumes.resolve(row["volume_key"], row["path"], mounts) if mounts else None
        photos.append((photo_id, copy, source or row["path"]))
    folder = os.path.abspath(os.path.expanduser(folder))
    return library.add_export_job(
        json.dumps(dataclasses.asdict(options)),
        replace,
        folder,
        destinations(photos, folder, options),
    )


def render(source, params, destination, options, replace, should_stop=lambda: False):
    """Render one photo and write it (a function of its own, so that it can run in a
    worker process, where the job's `should_stop` can't go)."""
    return _export.export(source, params, destination, options, replace, should_stop)


def run(library, job_id, mounts, should_stop=lambda: False, report=None, render=render):
    """Run export job `job_id` to its end and return its state. Raises NotConnected,
    leaving the job queued, when a photo's drive is not mounted. `render(source, params,
    destination, options, replace, should_stop)` writes one file (see `render`)."""
    report = report or (lambda phase, done, total, changed: None)
    job = _Job(library, job_id, report)
    options_json, replace, folder = library.export_job(job_id)
    options = _export.ExportOptions(**json.loads(options_json))
    items = library.export_items(job_id)
    waiting = [item for item in items if item.state == TO_DO]
    for item in waiting:
        if volumes.resolve(item.volume_key, item.path, mounts) is None:
            raise NotConnected(item.path)
    total = len(items)
    done = total - len(waiting)
    library.update_job(job_id, state=dbm.RUNNING, phase=EXPORTING, done=done, total=total)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        message = f"Cannot write to {folder}: {error.strerror or error}"
        library.add_failures(job_id, [(folder, message)])
        job.end(dbm.FAILED_JOB, message)
        return dbm.FAILED_JOB
    failed = sum(1 for item in items if item.state == NOT_EXPORTED)
    for item in waiting:
        if should_stop():
            job.end(dbm.CANCELLED, "Cancelled")
            return dbm.CANCELLED
        source = volumes.resolve(item.volume_key, item.path, mounts)
        try:
            _export_item(library, item, source, options, replace, should_stop, render)
        except _export.ExportCancelled:
            job.end(dbm.CANCELLED, "Cancelled")
            return dbm.CANCELLED
        except _Failed as error:
            library.set_export_item(item.id, NOT_EXPORTED, error.message)
            library.add_failures(job_id, [(error.path, error.message)])
            failed += 1
        else:
            library.set_export_item(item.id, EXPORTED)
        done += 1
        job.progress(EXPORTING, done, total, changed=True)
    job.progress(EXPORTING, done, total, force=True)
    job.end(dbm.DONE, _summary(total - failed, failed))
    return dbm.DONE


class _Failed(Exception):
    def __init__(self, path, message):
        super().__init__(message)
        self.path, self.message = path, message


def _export_item(library, item, source, options, replace, should_stop, render):
    stored = library.edit(item.photo_id, item.copy)
    try:
        params = _params.from_json(stored) if stored else _params.Params()
    except _params.NewerParams as error:
        raise _Failed(source, str(error)) from error
    try:
        render(source, params, item.destination, options, replace, should_stop)
    except _export.ExportExists as error:
        raise _Failed(error.path, "already exists, and Replace was not chosen") from error
    except PhotoError as error:
        _gone_or(source, error)
    except OSError as error:
        _gone_or(source, PhotoError(error.filename or source, error.strerror or str(error)))


def _gone_or(source, error):
    """The source gone since the job began is a drive unplugged: the job waits. Anything
    else is the file's own failure, named."""
    if not os.path.exists(source):
        raise NotConnected(source) from error
    raise _Failed(error.path, error.message) from error


def _summary(exported, failed):
    text = f"Exported {exported} file{'' if exported == 1 else 's'}"
    if failed:
        text += f", {failed} could not be exported"
    return text
