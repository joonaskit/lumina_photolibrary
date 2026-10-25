"""Every job that runs or waits, in one list (#132): who is in it, Cancel, failures kept until
dismissed, and what the status bar's button says."""

from dataclasses import replace

from PySide6.QtCore import QObject, Signal
from test_job_reports import FakeActivities, FakeNotifier, Service, status

from lumina.core.dbtypes import CANCELLED, DONE, FAILED_JOB, QUEUED
from lumina.ui import job_status
from lumina.ui.job_list import JobsButton
from lumina.ui.job_reports import JobReports, Source


class Scoring(QObject):
    job_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


def made():
    reports = JobReports(FakeActivities(), FakeNotifier(), lambda: True)
    scans, scoring = Service(), Scoring()
    reports.watch(
        scans.job_changed,
        Source(lambda s: f"Scanning {s.name}", job_status.describe, scans.cancel),
    )
    reports.watch(
        scoring.job_changed,
        Source(lambda _s: "Scoring sharpness", lambda _s: "Scoring", scoring.cancel, alone=True),
    )
    return reports.jobs, scans, scoring


def scoring_status(state=job_status.RUNNING, job=7, **fields):
    base = replace(status(state, done=10, job=job), kind="sharpness", name="", total=40)
    return replace(base, **fields)


def titles(jobs):
    return [entry.key for entry in jobs.entries()]


def test_jobs_of_different_services_are_listed_together_running_before_waiting(qapp):
    jobs, scans, scoring = made()

    scans.job_changed.emit(status(QUEUED, job=1, message="Waiting for Holiday to be connected"))
    scoring.job_changed.emit(scoring_status())
    scans.job_changed.emit(status(job=2))

    assert titles(jobs) == ["sharpness-7", "scan-2", "scan-1"]
    assert jobs.entries()[2].waiting()


def test_a_job_that_ends_cleanly_or_is_cancelled_leaves_the_list(qapp):
    jobs, scans, _scoring = made()
    scans.job_changed.emit(status(job=1))
    scans.job_changed.emit(status(job=2))

    scans.job_changed.emit(status(DONE, job=1))
    scans.job_changed.emit(status(CANCELLED, job=2, failures=(("a.jpg", "x"),)))

    assert titles(jobs) == []


def test_a_job_that_ended_with_failures_stays_until_it_is_dismissed(qapp):
    jobs, scans, _scoring = made()
    failures = (("/p/a.jpg", "broken"), ("/p/b.jpg", "broken"))
    scans.job_changed.emit(status(FAILED_JOB, job=1, message="Stopped", failures=failures))
    scans.job_changed.emit(status(DONE, job=2, failures=failures[:1]))
    scans.job_changed.emit(status(job=3))

    assert titles(jobs) == ["scan-3", "scan-1", "scan-2"]
    jobs.dismiss("scan-3")
    assert "scan-3" in titles(jobs)
    jobs.dismiss("scan-1")
    assert titles(jobs) == ["scan-3", "scan-2"]
    jobs.clear()
    assert titles(jobs) == ["scan-3"]


def test_cancel_stops_the_service_of_that_job_only(qapp):
    jobs, scans, scoring = made()
    scans.job_changed.emit(status(job=1))
    scoring.job_changed.emit(scoring_status())

    jobs.cancel("sharpness-7")

    assert (scoring.cancelled, scans.cancelled) == (1, 0)


def test_the_button_says_what_has_no_widget_of_its_own_and_how_many_jobs_run(qtbot):
    jobs, scans, scoring = made()
    button = JobsButton(jobs)
    qtbot.addWidget(button)
    assert button.isHidden()

    scans.job_changed.emit(status(job=1))
    assert button.isHidden()
    scoring.job_changed.emit(scoring_status())
    assert button.text() == "2 jobs"
    scans.job_changed.emit(status(DONE, job=1))
    assert button.text() == "Scoring sharpness…"
    scoring.job_changed.emit(scoring_status(DONE))
    assert button.isHidden()
    scans.job_changed.emit(status(FAILED_JOB, job=3, failures=(("/p/a.jpg", "broken"),)))
    assert button.text() == "1 failed"


def test_the_list_shows_each_job_with_its_cancel_and_names_the_files_that_failed(qtbot):
    jobs, scans, scoring = made()
    button = JobsButton(jobs)
    qtbot.addWidget(button)
    scoring.job_changed.emit(scoring_status())
    scans.job_changed.emit(status(FAILED_JOB, job=3, failures=(("/p/a.jpg", "broken"),)))

    button.panel.open()

    rows = button.panel._rows
    assert set(rows) == {"sharpness-7", "scan-3"}
    assert rows["sharpness-7"].title.text() == "Scoring sharpness"
    assert rows["sharpness-7"].action.text() == "Cancel"
    assert "/p/a.jpg: broken" in rows["scan-3"].files.text()
    assert rows["scan-3"].action.text() == "Dismiss"
    rows["sharpness-7"].action.click()
    assert scoring.cancelled == 1
    rows["scan-3"].action.click()
    assert set(button.panel._rows) == {"sharpness-7"}
