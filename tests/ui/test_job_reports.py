"""The jobs outside the window (#86): which reach the island and how they look there,
Cancel from the island, and which end in a notification."""

from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from lumina.core.dbtypes import CANCELLED, DETAILS, DONE, FAILED_JOB, QUEUED, RUNNING
from lumina.qt_models.jobs import JobStatus
from lumina.ui import job_status
from lumina.ui.job_reports import QUIT_FIELDS, JobReports, Source


class FakeActivities(QObject):
    action_invoked = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.shown = {}
        self.closed = None

    def set(self, activity_id, fields):
        self.shown[activity_id] = fields

    def close(self, fields):
        self.closed = fields


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, summary, body="", urgent=False):
        self.sent.append((summary, body, urgent))


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Service(QObject):
    job_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


def status(state=RUNNING, done=50, job=4, **fields):
    base = JobStatus("Holiday", state, DETAILS, done, 200, None, (), 0, "scan", job=job)
    return replace(base, **fields)


def made(active=False, show_after_ms=3000):
    activities, notifier, clock, service = FakeActivities(), FakeNotifier(), Clock(), Service()
    state = {"active": active}
    reports = JobReports(
        activities,
        notifier,
        lambda: state["active"],
        clock=clock,
        show_after_ms=show_after_ms,
    )
    source = Source(lambda s: f"Scanning {s.name}", job_status.describe, service.cancel)
    reports.watch(service.job_changed, source)
    return reports, activities, notifier, clock, service, state


def test_a_quick_job_stays_in_the_status_bar(qapp):
    _reports, activities, notifier, clock, service, _state = made()

    service.job_changed.emit(status())
    clock.now += 2
    service.job_changed.emit(status(DONE, done=200))

    assert activities.shown == {}
    assert notifier.sent == []


def test_a_job_that_runs_on_reaches_the_island_with_how_far_it_is(qtbot):
    _reports, activities, _notifier, _clock, service, _state = made(show_after_ms=50)

    service.job_changed.emit(status())
    # No progress told meanwhile: it is shown all the same, once it has run long enough
    qtbot.waitUntil(lambda: "scan-4" in activities.shown)
    assert activities.shown["scan-4"] == {
        "icon": "lumina",
        "state": "running",
        "title": "Scanning Holiday",
        "text": "50 of 200",
        "progress": 0.25,
        "actions": [("cancel", "Cancel")],
    }


def test_the_time_left_and_the_end_are_told(qtbot):
    _reports, activities, _notifier, clock, service, _state = made(show_after_ms=0)
    service.job_changed.emit(status())
    qtbot.waitUntil(lambda: "scan-4" in activities.shown)

    service.job_changed.emit(replace(status(done=1500), total=12000, eta=1500.0, queued=1))
    assert activities.shown["scan-4"]["text"] == (
        "1 500 of 12 000  ·  about 25 minutes left  ·  1 more waiting"
    )

    clock.now += 10
    service.job_changed.emit(status(DONE, done=200))
    assert activities.shown["scan-4"] == {
        "icon": "lumina",
        "state": "done",
        "progress": 1.0,
        "title": "Scanned Holiday",
        "text": "",
    }


def test_a_failure_reaches_the_island_naming_the_file_even_when_quick(qapp):
    _reports, activities, notifier, _clock, service, _state = made()
    failures = (("/photos/a.cr3", "exiftool stopped"),)

    service.job_changed.emit(status())
    service.job_changed.emit(status(FAILED_JOB, failures=failures))

    fields = activities.shown["scan-4"]
    assert fields["state"] == "failed"
    assert fields["title"] == "Could not scan Holiday"
    assert fields["error"] == "/photos/a.cr3: exiftool stopped"
    # No Cancel left on it
    assert fields["actions"] == []
    assert notifier.sent == [("Could not scan Holiday", "/photos/a.cr3: exiftool stopped", True)]


def test_a_long_job_ending_unseen_sends_one_notification(qapp):
    _reports, _activities, notifier, clock, service, _state = made()
    failures = tuple((f"/photos/{n}.cr3", "bad") for n in range(5))

    service.job_changed.emit(status())
    clock.now += 120
    service.job_changed.emit(status(DONE, done=200, failures=failures))

    assert notifier.sent == [
        (
            "Scanned Holiday",
            "/photos/0.cr3: bad\n/photos/1.cr3: bad\n/photos/2.cr3: bad\nand 2 more",
            False,
        )
    ]


def test_no_notification_while_lumina_is_looked_at_or_when_cancelled(qapp):
    _reports, _activities, notifier, clock, service, state = made(active=True)

    service.job_changed.emit(status())
    clock.now += 120
    service.job_changed.emit(status(DONE, done=200))
    service.job_changed.emit(status(job=5))
    service.job_changed.emit(status(FAILED_JOB, job=5))
    state["active"] = False
    service.job_changed.emit(status(job=6))
    clock.now += 120
    service.job_changed.emit(status(CANCELLED, job=6))

    assert notifier.sent == []


def test_cancel_in_the_island_cancels_the_job_and_only_ours(qtbot):
    _reports, activities, _notifier, _clock, service, _state = made(show_after_ms=0)
    service.job_changed.emit(status())
    qtbot.waitUntil(lambda: "scan-4" in activities.shown)

    activities.action_invoked.emit("scan-9", "cancel")
    assert service.cancelled == 0
    activities.action_invoked.emit("scan-4", "cancel")
    assert service.cancelled == 1
    # Still running in the island until the job says how it ended
    assert activities.shown["scan-4"]["state"] == "running"


def test_a_job_waiting_for_its_drive_is_not_brought_to_the_island(qtbot):
    _reports, activities, _notifier, clock, service, _state = made(show_after_ms=0)

    service.job_changed.emit(
        replace(status(QUEUED, done=0), message="Waiting for Card to be connected")
    )
    clock.now += 10
    qtbot.wait(20)

    assert activities.shown == {}


def test_at_the_exit_the_island_is_told_the_jobs_carry_on(qapp):
    reports, activities, *_ = made()

    reports.close()

    assert activities.closed == QUIT_FIELDS
