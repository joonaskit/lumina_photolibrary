"""Bursts: runs of photos taken a moment apart that look alike, to stack (#61).

A photo joins the run before it when it was taken at most BURST_GAP seconds after the
last photo of the run and its search vector is close to that photo's. A photo with no
vector yet, or none of a time, is left alone: it could not be told from a different
shot. Photos already in a stack are left alone too, so a stack made by hand stays as
it was made. Pure: nothing here reads the library or changes it.
"""

from datetime import datetime

import numpy as np

from lumina.core.db import PICKED, REJECTED

# Seconds between one frame and the next of a burst (a camera shoots 3 to 30 a second;
# this also takes a person pressing the button again)
BURST_GAP = 3.0
# Cosine of two frames' vectors at which they are the same shot (SigLIP 2, image to
# image); to be tuned on real libraries
SIMILARITY = 0.85


def find(rows, vectors, gap=BURST_GAP, similarity=SIMILARITY):
    """The bursts among `rows` (photos with id, content_id, taken, flag, rating and stack),
    as lists of ids, each with the best photo first and the others in time order.
    `vectors` is {content id: normalised vector}."""
    found = []
    run = []
    last = None
    for row in sorted(rows, key=lambda r: (r.taken, r.id)):
        when = _seconds(row.taken)
        vector = vectors.get(row.content_id)
        if row.stack is not None or when is None or vector is None:
            _keep(run, found)
            run, last = [], None
            continue
        if run and when - last[0] <= gap and float(np.dot(last[1], vector)) >= similarity:
            run.append(row)
        else:
            _keep(run, found)
            run = [row]
        last = (when, vector)
    _keep(run, found)
    return found


def best(run):
    """The photo to show on top: a thumbs up before no flag before a thumbs down, then
    the most stars, then the first taken."""
    order = {PICKED: 0, REJECTED: 2}
    return min(run, key=lambda r: (order.get(r.flag, 1), -r.rating, r.taken, r.id))


def _keep(run, found):
    if len(run) < 2:
        return
    top = best(run)
    found.append([top.id, *(row.id for row in run if row is not top)])


def _seconds(taken):
    try:
        return datetime.fromisoformat(taken).timestamp()
    except (TypeError, ValueError):
        return None
