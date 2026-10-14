"""Indexing for search: the job that gives every photo its vector (core/embed.py).

What is left to do is simply the photos with no vector for the model, so the job
resumes after a quit or a crash without keeping any state of its own, and a scan
that adds photos only adds work. One vector per content id: copies of a photo are
encoded once. Only photos on connected drives are read; the others wait.

The encoding itself is `encode(paths)`, given by the caller: in the app a worker
process of its own, at low priority. It returns a float16 blob or a PhotoError for
each path. When a batch fails as a whole (a decoder crashed the worker) its photos
are tried one at a time, so the one to blame is named; when every one of them fails
too, the encoder itself is broken and the job stops. A photo that is not there when it
fails (its drive was unplugged meanwhile) is not marked: it waits, as a photo on an
unplugged drive does.
"""

import os

from lumina.core import db as dbm
from lumina.core import embed, volumes
from lumina.core.errors import PhotoError
from lumina.core.library import _Job

EMBED = "embed"
ENCODING = "encoding"
_BATCH = 16


def index(
    library,
    job_id,
    mounts,
    encode,
    model=embed.MODEL.name,
    should_stop=lambda: False,
    report=lambda phase, done, total, changed: None,
    batch=_BATCH,
):
    """Run indexing job `job_id` to its end and return its state."""
    job = _Job(library, job_id, report)
    library.update_job(job_id, state=dbm.RUNNING, phase=ENCODING, done=0, total=None, message=None)
    keys = volumes.mounted_ids(mounts)
    total = library.count_pending_embeddings(model, keys)
    done = skipped = failures = 0
    job.progress(ENCODING, 0, total, force=True)
    while True:
        if should_stop():
            job.end(dbm.CANCELLED, "Cancelled while indexing for search")
            return dbm.CANCELLED
        pending = library.pending_embeddings(model, keys, batch, skip=skipped)
        if not pending:
            break
        paths = {}
        for cid, key, relative in pending:
            where = volumes.resolve(key, relative, mounts)
            if where is None:
                # On a connected drive, but not where any mount shows it: left for later
                skipped += 1
            else:
                paths[cid] = where
        try:
            results = _encode(encode, list(paths.values()))
        except _EncoderBroken as error:
            message = f"Indexing stopped: {error}"
            job.end(dbm.FAILED_JOB, message)
            return dbm.FAILED_JOB
        vectors, failed = [], []
        for (cid, path), result in zip(paths.items(), results, strict=True):
            if isinstance(result, PhotoError) and not os.path.exists(path):
                # Gone while the job ran (a drive unplugged): not the photo's fault
                skipped += 1
            elif isinstance(result, PhotoError):
                failed.append((cid, path, result.message))
            else:
                vectors.append((cid, result))
        library.write_embeddings(model, vectors, [(cid, message) for cid, _p, message in failed])
        library.add_failures(job_id, [(path, message) for _cid, path, message in failed])
        failures += len(failed)
        done += len(pending)
        job.progress(ENCODING, done, total, changed=True)
    job.progress(ENCODING, done, total, force=True)
    job.end(dbm.DONE, _summary(done - skipped - failures, failures, skipped))
    return dbm.DONE


class _EncoderBroken(Exception):
    pass


def _encode(encode, paths):
    if not paths:
        return []
    try:
        return encode(paths)
    except Exception as error:
        first = error
    results, raised = [], 0
    for path in paths:
        try:
            results.extend(encode([path]))
        except Exception as error:
            raised += 1
            results.append(PhotoError(path, f"Could not be read for search: {_describe(error)}"))
    # One photo can be to blame; a whole batch of them is the encoder
    if len(paths) > 1 and raised == len(paths):
        raise _EncoderBroken(_describe(first)) from first
    return results


def _describe(error):
    return str(error) or type(error).__name__


def _summary(indexed, failures, skipped):
    parts = [f"{indexed} photo" + ("" if indexed == 1 else "s") + " indexed for search"]
    if failures:
        parts.append(f"{failures} could not be read")
    if skipped:
        parts.append(f"{skipped} not reachable")
    return ", ".join(parts)
