"""Merging photos for the UI: HDR merges of brackets (core/hdr.py) and panoramas
(core/panorama.py), as jobs.

One merge or panorama at a time, on a thread of its own (workers.MERGE, by the JobRunner of
qt_models/jobs.py), its steps in a worker process of its own (workers.MERGING), so
neither a scan nor the grid's thumbnails wait for it, nor it for them. A job queued or
stopped by a quit is run again at the next start, from the frames it had decoded; one
whose drive is not connected waits for it. When a merge is done its folder is scanned, which
puts the new photo in the grid.
"""

import json
import os
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import QObject, Signal

from lumina.core import db as dbm
from lumina.core import hdr, panorama, volumes
from lumina.qt_models import workers
from lumina.qt_models.jobs import JobRunner
from lumina.qt_models.panorama_preview import PanoramaPreview

DEGHOSTING = tuple(hdr.DEGHOST)
PROJECTIONS = panorama.PROJECTIONS
# What runs each kind of job
_RUN = {dbm.HDR: hdr.run, dbm.PANORAMA: panorama.run}
# How far around a photo a bracket is looked for, in seconds
_NEAR = 60


@dataclass(frozen=True)
class Frame:
    """A photo as the merge dialog lists it."""

    id: int
    name: str
    # "1/60 s · f/8 · ISO 100", or "" when not known
    exposure: str


class MergeService(QObject):
    # A JobStatus (its kind HDR or PANORAMA), while one runs and when it ends
    job_changed = Signal(object)

    def __init__(self, library, work_root=None, parent=None):
        """`library` is the LibraryService: its database, drives and scans."""
        super().__init__(parent)
        self.library = library
        self.work_root = work_root or hdr.default_work_root()
        self.jobs = JobRunner(
            self,
            library,
            set(_RUN),
            workers.MERGE,
            prepare=self._prepare,
            name=self._name,
            changed=self.job_changed.emit,
            ended=self._ended,
            failed=_failed,
        )
        library.mounts_changed.connect(self.jobs.wake)

    def start(self):
        """Resume the merges a quit or a crash stopped."""
        self.jobs.start()

    def close(self):
        """For the app's exit: a merge in progress stops and is queued for the next start,
        its decoded frames kept."""
        self.jobs.close()

    # Choosing

    def bracket(self, photo_ids):
        """The photos to merge for the ones chosen: those, or with one chosen, the bracket
        it was shot in (photos moments apart at different exposures), if it is in one."""
        db = self.library.db
        if len(photo_ids) != 1:
            return list(photo_ids)
        [row] = db.photo_rows(photo_ids) or [None]
        if row is None:
            return list(photo_ids)
        near = db.photos_near(row.folder_id, row.taken, _NEAR)
        return hdr.bracket_around(near, row.id) or list(photo_ids)

    def frames(self, photo_ids):
        """[Frame] of the photos, in the order a merge takes them: by exposure."""
        rows = self.library.db.photo_rows(photo_ids)
        rows.sort(key=lambda row: hdr.exposure_value(row) or 0.0)
        return [Frame(row.id, os.path.basename(row.path), _exposure(row)) for row in rows]

    def check(self, photo_ids):
        """Why the photos cannot be merged, or None when they can."""
        rows = self.library.db.photo_rows(photo_ids)
        try:
            hdr.check_frames(rows)
        except ValueError as error:
            return str(error)
        missing = next((row for row in rows if not row.available), None)
        if missing is not None:
            return f"{os.path.basename(missing.path)} is missing"
        return None

    def merge_hdr(self, photo_ids, deghost="off", align=True):
        """Queue an HDR merge of the photos. Raises ValueError saying why they cannot be."""
        hdr.queue(self.library.db, photo_ids, deghost, align)
        self.jobs.next()

    def panorama_frames(self, photo_ids):
        """[Frame] of the photos, in the order a panorama takes them: as taken."""
        rows = self.panorama_rows(photo_ids)
        return [Frame(row.id, os.path.basename(row.path), _exposure(row)) for row in rows]

    def check_panorama(self, photo_ids):
        """Why the photos cannot be stitched into a panorama, or None when they can."""
        rows = self.library.db.photo_rows(photo_ids)
        try:
            panorama.check_frames(rows)
        except ValueError as error:
            return str(error)
        missing = next((row for row in rows if not row.available), None)
        if missing is not None:
            return f"{os.path.basename(missing.path)} is missing"
        return None

    def preview_panorama(self, photo_ids):
        """A PanoramaPreview of the photos, to start when the dialog is shown and to close
        when it is gone; None when a photo's drive is not connected."""
        paths = []
        for row in self.panorama_rows(photo_ids):
            where = volumes.resolve(row.volume_key, row.path, self.library.mounts)
            if where is None:
                return None
            paths.append(where)
        return PanoramaPreview(paths, os.path.join(self.work_root, "preview"), parent=self)

    def panorama_rows(self, photo_ids):
        """The photos' rows, in the order a panorama takes them: as taken."""
        rows = self.library.db.photo_rows(photo_ids)
        rows.sort(key=lambda row: (row.taken or "", row.path))
        return rows

    def stitch_panorama(self, photo_ids, projection=panorama.CYLINDRICAL, crop=True):
        """Queue a panorama of the photos. Raises ValueError saying why they cannot be."""
        panorama.queue(self.library.db, photo_ids, projection, crop)
        self.jobs.next()

    # Jobs

    def is_busy(self):
        return self.jobs.is_busy()

    def cancel(self):
        """Stop the running merge and drop those waiting."""
        self.jobs.cancel()

    def _prepare(self, job):
        return partial(
            _merge, job.kind, self.work_root, call=_call_in_process, keep_work=self.jobs.quitting
        )

    def _ended(self, job, status):
        if status.state == dbm.DONE and job.folder_id is not None:
            # The new photo goes in the grid with the folder's next scan
            self.library.rescan(job.folder_id)

    def _name(self, job):
        derived = self.library.db.derived(job.derived_id) if job.derived_id else None
        if derived is None:
            return "photos"
        return json.loads(derived.settings).get("name", "photos")


def _failed(job, error):
    doing = "stitch" if job.kind == dbm.PANORAMA else "merge"
    return f"Could not {doing}: {error}"


def _exposure(row):
    parts = []
    time = row.exposure_time
    if time:
        parts.append(f"1/{round(1 / time)} s" if time < 0.5 else f"{time:g} s")
    if row.aperture:
        parts.append(f"f/{row.aperture:g}")
    if row.iso:
        parts.append(f"ISO {row.iso}")
    return " · ".join(parts)


def _call_in_process(fn, *args):
    return workers.call(workers.MERGING, fn, *args)


def _merge(kind, work_root, library, job_id, mounts, should_stop, report, call, keep_work):
    """On the merge thread."""
    return _RUN[kind](
        library,
        job_id,
        mounts,
        call=call,
        should_stop=should_stop,
        report=report,
        work_root=work_root,
        keep_work=keep_work,
    )
