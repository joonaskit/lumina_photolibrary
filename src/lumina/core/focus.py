"""Scoring for sharpness: the job that gives every photo its score (core/sharpness.py).

As the indexing for search (core/indexing.py) does, it works from what is left to do:
the photos with no score, so the job resumes after a quit or a crash without keeping
any state of its own, and a scan that adds photos only adds work. One score per content
id. Only photos on connected drives are read; the others wait.

The scoring itself is `score(paths)`, given by the caller: in the app worker processes.
It returns a float or a PhotoError for each path. When a batch fails as a whole (a decoder
crashed the worker) its photos are tried one at a time, so the one to blame is named. A
photo that is not there when it fails (its drive was unplugged meanwhile) is not marked:
it waits, as a photo on an unplugged drive does.
"""

import os

from lumina.core import db as dbm
from lumina.core import volumes
from lumina.core.errors import PhotoError
from lumina.core.library import _Job

SHARPNESS = "sharpness"
SCORING = "scoring"
_BATCH = 16


def run(
    library,
    job_id,
    mounts,
    score,
    should_stop=lambda: False,
    report=lambda phase, done, total, changed: None,
    batch=_BATCH,
):
    """Run scoring job `job_id` to its end and return its state."""
    job = _Job(library, job_id, report)
    library.update_job(job_id, state=dbm.RUNNING, phase=SCORING, done=0, total=None, message=None)
    keys = volumes.mounted_ids(mounts)
    total = library.count_pending_sharpness(keys)
    done = failures = 0
    # Passed over (not reachable now): left for the next run, not asked for again in this
    passed = set()
    job.progress(SCORING, 0, total, force=True)
    while True:
        if should_stop():
            job.end(dbm.CANCELLED, "Cancelled while scoring sharpness")
            return dbm.CANCELLED
        pending = library.pending_sharpness(keys, batch, skip=passed)
        if not pending:
            break
        paths = {}
        for cid, key, relative in pending:
            where = volumes.resolve(key, relative, mounts)
            if where is None:
                passed.add(cid)
            else:
                paths[cid] = where
        scores, failed = [], []
        for (cid, path), result in zip(
            paths.items(), _score(score, list(paths.values())), strict=True
        ):
            if isinstance(result, PhotoError) and not os.path.exists(path):
                # Gone while the job ran (a drive unplugged): not the photo's fault
                passed.add(cid)
            elif isinstance(result, PhotoError):
                failed.append((cid, path, result.message))
            else:
                scores.append((cid, result))
        library.write_sharpness(scores, [(cid, message) for cid, _p, message in failed])
        library.add_failures(job_id, [(path, message) for _cid, path, message in failed])
        failures += len(failed)
        done += len(pending)
        job.progress(SCORING, done, total, changed=True)
    job.progress(SCORING, done, total, force=True)
    job.end(dbm.DONE, _summary(done - len(passed) - failures, failures, len(passed)))
    return dbm.DONE


def _score(score, paths):
    if not paths:
        return []
    try:
        return score(paths)
    except Exception:
        pass
    results = []
    for path in paths:
        try:
            results.extend(score([path]))
        except Exception as error:
            results.append(
                PhotoError(path, f"Could not be scored for sharpness: {_describe(error)}")
            )
    return results


def _describe(error):
    return str(error) or type(error).__name__


def _summary(scored, failures, skipped):
    parts = [f"{scored} photo" + ("" if scored == 1 else "s") + " scored for sharpness"]
    if failures:
        parts.append(f"{failures} could not be read")
    if skipped:
        parts.append(f"{skipped} not reachable")
    return ", ".join(parts)
