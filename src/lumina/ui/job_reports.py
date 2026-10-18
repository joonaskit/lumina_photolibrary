"""Lumina's jobs outside its window (#86): in the Quickshell Island while they run, with
Cancel, and a desktop notification when a long one ends, or one fails, while Lumina is
not the active app. The status bar still tells every job; this is a second view of the
same JobStatus, and Lumina works the same without an island or a notification service.

A job reaches the island only once it has run a few seconds, so the rescans the watcher
starts all the time stay in the status bar.
"""

import time
from dataclasses import dataclass, replace
from typing import Callable

from PySide6.QtCore import QObject, QTimer

from lumina import APP_ID
from lumina.core.dbtypes import CANCELLED, DONE, FAILED_JOB, QUEUED, UNFINISHED
from lumina.ui.job_status import (
    describe,
    describe_export,
    describe_import,
    describe_merge,
    left,
    merge_heading,
)
from lumina.ui.search import describe_indexing

# How long a job runs before the island shows it
SHOW_AFTER_MS = 3000
# How long a job runs before its end is worth a notification; a failure always is
NOTIFY_AFTER_SECONDS = 60.0
# The failures a notification names
_NAMED = 3
# What the island is told of a job running when Lumina quits
QUIT_FIELDS = {
    "state": "cancelled",
    "text": "Lumina quit; it carries on when Lumina starts again",
}


def counted(status):
    """How far a job is, with the time left, or None when its total is not known."""
    if not status.total:
        return None
    more = f"  ·  {status.queued} more waiting" if status.queued else ""
    done_of = f"{status.done:,} of {status.total:,}".replace(",", " ")
    return f"{done_of}{left(status)}{more}"


@dataclass(frozen=True)
class Source:
    """One service's jobs: `heading(status)`, a running job's title in the island;
    `words(status)`, the status bar's words, for the rest; `cancel()` what the status
    bar's Cancel does; `detail(status)` a running job's second line, or None for its
    words."""

    heading: Callable
    words: Callable
    cancel: Callable
    detail: Callable = counted


@dataclass
class _Job:
    source: Source
    started: float
    status: object
    shown: bool = False
    timer: QTimer | None = None


class JobReports(QObject):
    def __init__(
        self,
        activities,
        notifier,
        is_active,
        parent=None,
        clock=time.monotonic,
        show_after_ms=SHOW_AFTER_MS,
        notify_after=NOTIFY_AFTER_SECONDS,
    ):
        """`activities` is the island's (`integration.island.Activities`), `notifier`
        the desktop's (`integration.notifications.Notifier`), `is_active()` whether
        Lumina is the app the user is in."""
        super().__init__(parent)
        self._activities = activities
        self._notifier = notifier
        self._is_active = is_active
        self._clock = clock
        self._show_after = show_after_ms
        self._notify_after = notify_after
        # Activity id -> the job, while it runs
        self._jobs = {}
        activities.action_invoked.connect(self._on_action)

    def watch(self, signal, source):
        """Report the JobStatus `signal` tells, of `source`'s jobs."""
        signal.connect(lambda status: self.tell(source, status))

    def close(self):
        """For the app's exit: a job running in the island is stopped by the quit."""
        for job in self._jobs.values():
            if job.timer is not None:
                job.timer.stop()
        self._jobs.clear()
        self._activities.close(QUIT_FIELDS)

    def tell(self, source, status):
        key = f"{status.kind or 'job'}-{status.job}"
        job = self._jobs.get(key)
        if status.state in UNFINISHED:
            self._running(key, job, source, status)
        else:
            self._ended(key, job, source, status)

    def _running(self, key, job, source, status):
        waiting = status.state == QUEUED and status.message
        if job is None:
            if waiting:
                # Not started: its drive or card is not there. The status bar says so
                return
            job = self._jobs[key] = _Job(source, self._clock(), status)
        job.status = status
        if job.shown:
            self._activities.set(key, self._fields(source, status))
            return
        ran_ms = (self._clock() - job.started) * 1000
        if ran_ms >= self._show_after:
            self._show(key)
        elif job.timer is None:
            job.timer = QTimer(self)
            job.timer.setSingleShot(True)
            job.timer.timeout.connect(lambda: self._show(key))
            job.timer.start(max(1, round(self._show_after - ran_ms)))

    def _show(self, key):
        job = self._jobs.get(key)
        if job is None or job.shown:
            return
        if job.timer is not None:
            job.timer.stop()
            job.timer = None
        job.shown = True
        self._activities.set(key, self._fields(job.source, job.status))

    def _ended(self, key, job, source, status):
        job = self._jobs.pop(key, None)
        if job is not None and job.timer is not None:
            job.timer.stop()
        failed = status.state == FAILED_JOB
        if failed or (job is not None and job.shown):
            self._activities.set(key, self._fields(source, status))
        ran = self._clock() - job.started if job is not None else 0.0
        if status.state == CANCELLED or self._is_active():
            return
        if failed or ran >= self._notify_after:
            self._notifier.notify(source.words(status), self._failures(status), urgent=failed)

    def _fields(self, source, status):
        """The island's fields for `status`: all of them, so that what changed is told."""
        fields = {"icon": APP_ID}
        if status.state in UNFINISHED:
            share = status.done / status.total if status.total else None
            if status.state == QUEUED and status.message:
                text = status.message
            else:
                text = source.detail(status) or source.words(status)
            return fields | {
                "state": "running",
                "title": source.heading(status),
                "text": text,
                # Negative is indeterminate. 1.0 is not "done": only the state says that
                "progress": -1.0 if share is None else min(1.0, share),
                "actions": [("cancel", "Cancel")],
            }
        if status.state == DONE:
            fields |= {"state": "done", "progress": 1.0}
        elif status.state == CANCELLED:
            fields |= {"state": "cancelled"}
        else:
            # What failed and why, by name; no Cancel left on it
            error = status.message or self._failures(status) or "It stopped with an error"
            fields |= {"state": "failed", "error": error, "actions": []}
            status = replace(status, message=None) if status.message else status
        return fields | {"title": source.words(status), "text": ""}

    @staticmethod
    def _failures(status):
        """The files that failed, by name, the first few."""
        lines = [f"{path}: {message}" for path, message in status.failures[:_NAMED]]
        if len(status.failures) > _NAMED:
            lines.append(f"and {len(status.failures) - _NAMED} more")
        return "\n".join(lines)

    def _on_action(self, key, action):
        job = self._jobs.get(key)
        if job is not None and action == "cancel":
            # A request: the activity runs on until the job says how it ended
            job.source.cancel()


def watch_services(reports, library, search, merge, importer, exporter):
    """Report the jobs of each service: the scans, indexing, merges, imports, exports."""
    reports.watch(
        library.job_changed,
        Source(lambda status: f"Scanning {status.name}", describe, library.cancel),
    )
    reports.watch(
        search.indexing_changed,
        Source(lambda _status: "Indexing for search", describe_indexing, search.cancel_indexing),
    )
    # A merge's done and total are of its phase, which its words tell
    reports.watch(
        merge.job_changed,
        Source(merge_heading, describe_merge, merge.cancel, detail=lambda _status: None),
    )
    reports.watch(
        importer.job_changed,
        Source(lambda status: f"Importing into {status.name}", describe_import, importer.cancel),
    )
    reports.watch(
        exporter.job_changed,
        Source(lambda status: f"Exporting to {status.name}", describe_export, exporter.cancel),
    )
