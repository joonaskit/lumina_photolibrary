"""Running a service's jobs from the library's `jobs` table: scans, indexing, imports,
merges and exports.

A JobRunner runs the jobs of its kinds one at a time, oldest first, on a lane of its own
(workers.py), each with a database connection of its own. A job is persisted, so one
queued or stopped by a quit is run again at the next start, from where it had got to.
One whose drive or card is not connected waits until the drives change. Progress and the
end come back to the main thread as a JobStatus. A job asked for again while it runs
runs once more after it.

The service describes its jobs; the runner does the rest:

- `prepare(job)`, on the main thread, gives the work: `work(library, job_id, mounts,
  should_stop, report)`, which runs on the lane and returns the job's end state
- `name(job)`: what the job is about, for the status bar
- `ended(job, status)`, on the main thread, after the status is told: what the job's
  end means for the service (a rescan, the grid told)

The phases named `timed` are given a time left (`JobStatus.eta`) from the rate so far.
"""

import time
from dataclasses import dataclass, replace

from lumina.core import db as dbm
from lumina.core.errors import NotConnected
from lumina.qt_models import workers

# A time left is told once a phase has run this long and done this many
_ETA_AFTER_SECONDS = 5.0
_ETA_AFTER_ITEMS = 3


@dataclass(frozen=True)
class JobStatus:
    # What the job is about: a folder's name, a merge's
    name: str
    state: str
    phase: str | None
    done: int
    total: int | None
    message: str | None
    failures: tuple
    # Jobs of the same service waiting after this one
    queued: int
    kind: str | None = None
    # Seconds left, as far as the rate so far tells; None when not known
    eta: float | None = None
    # The job's id in the `jobs` table
    job: int | None = None


def waiting_for(error):
    """The words for a job waiting for what `error` (NotConnected) names."""
    return f"Waiting for {error} to be connected"


class Estimate:
    """Seconds until a phase is done at the rate so far, or None: not before it has run
    `after_seconds` and done `after_items`, and not for a phase whose total is unknown."""

    def __init__(self, after_items=_ETA_AFTER_ITEMS, after_seconds=_ETA_AFTER_SECONDS):
        self.after_items = after_items
        self.after_seconds = after_seconds
        # (job id, phase, when it was first told, how many were done then)
        self._began = None

    def __call__(self, job_id, phase, done, total, now):
        if self._began is None or self._began[:2] != (job_id, phase):
            self._began = (job_id, phase, now, done)
            return None
        _job, _phase, began, first = self._began
        made = done - first
        if not total or made < self.after_items or now - began < self.after_seconds:
            return None
        return max(0, total - done) * (now - began) / made


class JobRunner:
    def __init__(
        self,
        owner,
        library,
        kinds,
        lane,
        *,
        prepare,
        name,
        changed,
        ended=None,
        progress=None,
        waits=None,
        failed=None,
        ready=None,
        timed=(),
        eta_after=_ETA_AFTER_ITEMS,
        clock=time.monotonic,
    ):
        """Run the jobs of `kinds` on `lane`, for the QObject `owner` (nothing is told
        once it is gone). `library` is the LibraryService: its database, path and drives.
        `changed(status)` is told the JobStatus as a job starts, runs and ends.

        Optional: `progress(job, status, changed)` instead of `changed` while it runs
        (`changed` is whether it wrote something the grid shows); `waits(job, error)`
        when its drive is not connected, giving the words to tell or None; `failed(job,
        error)` the message stored for a job that raised; `ready(job)` whether a job
        queued can start now; `timed`, the phases given a time left once `eta_after`
        are done in them; `clock`, for the tests."""
        self.owner = owner
        self.library = library
        self.kinds = frozenset(kinds)
        self.lane = lane
        self._prepare = prepare
        self._name = name
        self._changed = changed
        self._ended = ended
        self._progress = progress
        self._waits = waits
        self._failed = failed or (lambda _job, error: f"{type(error).__name__}: {error}")
        self._ready = ready or (lambda _job: True)
        self._timed = frozenset(timed)
        self._estimate = Estimate(eta_after)
        self._clock = clock
        self.quitting = workers.Token()
        # The job running now, and the token that stops it
        self.running = None
        self._stop = None
        # Ids of jobs waiting for a drive, until the drives change
        self._waiting = set()
        # (kind, folder id) asked for again while that job runs
        self._again = set()
        # Kept for the tests, which wait for a job to end
        self.future = None

    def start(self):
        """Resume the jobs a quit or a crash stopped."""
        self.next()

    def close(self):
        """For the app's exit: a job in progress stops and is queued for the next start."""
        self.quitting.cancel()

    def add(self, kind, folder_id=None):
        """Queue a job of `kind` for `folder_id` and start it when its turn comes. When
        that job is running it runs again after."""
        running = self.running
        if running is not None and (running.kind, running.folder_id) == (kind, folder_id):
            self._again.add((kind, folder_id))
        else:
            self.library.db.add_job(kind, folder_id)
        self.next()

    def is_busy(self):
        return self.running is not None

    def jobs(self):
        """The unfinished jobs of its kinds, oldest first."""
        return [job for job in self.library.db.unfinished_jobs() if job.kind in self.kinds]

    def cancel(self):
        """Stop the running job and drop those waiting."""
        self._again.clear()
        for job in self.jobs():
            if self.running is None or job.id != self.running.id:
                self.library.db.update_job(job.id, state=dbm.CANCELLED, message="Cancelled")
        self.stop()

    def stop(self):
        """Stop the running job; the others stay queued."""
        if self._stop is not None:
            self._stop.cancel()

    def forget(self, folder_id):
        """Not again for `folder_id`, which is going."""
        self._again = {key for key in self._again if key[1] != folder_id}

    def wake(self, tell=True):
        """The drives changed: the jobs waiting for one may start. Without `tell`, the
        one started is not told until it reports progress."""
        self._waiting.clear()
        self.next(tell)

    def next(self, tell=True):
        """Start the oldest job that can start, unless one runs."""
        if self.running is not None or self.quitting.cancelled():
            return
        for job in self.jobs():
            if job.id not in self._waiting and self._ready(job):
                self._run(job, tell)
                return

    def status(self, job):
        db = self.library.db
        found = db.job(job.id)
        failures = tuple(db.failures(job.id)[:20]) if found.failures else ()
        queued = sum(1 for other in self.jobs() if other.id != job.id)
        return JobStatus(
            self._name(job),
            found.state,
            found.phase,
            found.done,
            found.total,
            found.message,
            failures,
            queued,
            job.kind,
            job=job.id,
        )

    def _run(self, job, tell):
        stop = workers.Token(self.quitting)
        self._stop = stop
        self.running = job
        work = self._prepare(job)
        if tell:
            self._changed(self.status(job))
        quitting = self.quitting
        owner = self.owner
        self.future = workers.submit(
            _run,
            work,
            self.library.path,
            job.id,
            self.library.mounts,
            stop,
            quitting,
            lambda *args: workers.call_on_main(owner, self._on_progress, job, *args),
            on_done=lambda _state: self._on_ended(job),
            on_error=lambda error: self._on_failed(job, error),
            owner=owner,
            lane=self.lane,
        )

    def _on_progress(self, job, phase, done, total, changed):
        if self.quitting.cancelled() or self.running is None or self.running.id != job.id:
            return
        # The phase the job reported, not the one the database has by the time this is
        # delivered: a quick phase would otherwise never be told
        eta = None
        if phase in self._timed:
            eta = self._estimate(job.id, phase, done, total, self._clock())
        status = replace(
            self.status(job), state=dbm.RUNNING, phase=phase, done=done, total=total, eta=eta
        )
        if self._progress is not None:
            self._progress(job, status, changed)
        else:
            self._changed(status)

    def _on_ended(self, job):
        self.running = self._stop = None
        if self.quitting.cancelled():
            return
        again = (job.kind, job.folder_id)
        if again in self._again:
            self._again.discard(again)
            self.library.db.add_job(*again)
        status = self.status(job)
        self._changed(status)
        if self._ended is not None:
            self._ended(job, status)
        self.next()

    def _on_failed(self, job, error):
        if isinstance(error, NotConnected):
            # Unplugged as it started: it waits for the drive
            self.running = self._stop = None
            self._waiting.add(job.id)
            words = self._waits(job, error) if self._waits is not None else waiting_for(error)
            if words is not None and not self.quitting.cancelled():
                status = JobStatus(self._name(job), dbm.QUEUED, None, 0, None, words, (), 0)
                self._changed(replace(status, kind=job.kind, job=job.id))
            self.next()
            return
        if not self.quitting.cancelled():
            self.library.db.update_job(
                job.id, state=dbm.FAILED_JOB, message=self._failed(job, error)
            )
        self._on_ended(job)


def _run(work, path, job_id, mounts, stop, quitting, report):
    """On the job's lane, with a connection of its own."""
    with dbm.Library(path) as library:
        state = work(library, job_id, mounts, stop, report)
        if state == dbm.CANCELLED and quitting.cancelled():
            # Stopped by a quit, not by the user: run again at the next start
            library.update_job(job_id, state=dbm.QUEUED, message=None)
        return state
