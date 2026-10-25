"""Scoring photos for sharpness for the UI: the job of core/focus.py, kept running.

It runs whenever there may be photos without a score: at the start, after every scan and
when a drive comes. It runs on a thread of its own (by the JobRunner of qt_models/jobs.py)
and scores in a worker process of its own at low priority, apart from the scans and the
indexing for search. A quit in the middle leaves it queued for the next start. The grid
reads the scores again when the job ends.
"""

from functools import partial

from PySide6.QtCore import QObject, Signal

from lumina.core import focus, sharpness
from lumina.qt_models import workers
from lumina.qt_models.jobs import JobRunner


class SharpnessService(QObject):
    # A JobStatus, as the job starts, runs and ends
    job_changed = Signal(object)

    def __init__(self, library, score=None, parent=None):
        """`library` is the LibraryService. For the tests: `score(paths)` replaces the
        worker process."""
        super().__init__(parent)
        self.library = library
        self._score = score or _score_in_process
        self.jobs = JobRunner(
            self,
            library,
            {focus.SHARPNESS},
            workers.SHARPNESS,
            prepare=lambda _job: partial(_run, self._score),
            name=lambda _job: "",
            changed=self.job_changed.emit,
            timed={focus.SCORING},
            ended=lambda _job, _status: library.photos_changed.emit(),
        )
        library.job_ended.connect(lambda _status: self.score())
        library.mounts_changed.connect(self.score)

    def start(self):
        self.score()

    def close(self):
        """For the app's exit: a job in progress stops and is queued for the next start."""
        self.jobs.close()

    def score(self):
        """Give every photo without a score one. Run again when it ends if it is running
        now: what a scan added since it started is not in its count."""
        self.jobs.add(focus.SHARPNESS)

    def cancel(self):
        """Stop it until the next start or scan."""
        self.jobs.cancel()

    def is_scoring(self):
        return self.jobs.is_busy()


def _score_in_process(paths):
    return workers.call(workers.SCORING, sharpness.score_files, paths, priority=workers.LATER)


def _run(score, library, job_id, mounts, should_stop, report):
    """On the sharpness thread."""
    return focus.run(library, job_id, mounts, score, should_stop=should_stop, report=report)
