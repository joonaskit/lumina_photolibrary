"""The job in the status bar, in words."""

import pytest

from lumina.core.dbtypes import COPYING, DETAILS, EXPORTING, FAILED_JOB, FINDING, RUNNING
from lumina.qt_models.jobs import JobStatus
from lumina.ui import job_status
from lumina.ui.search import describe_indexing


def status(**fields):
    base = dict(
        name="Photos",
        state=RUNNING,
        phase=DETAILS,
        done=500,
        total=12000,
        message=None,
        failures=(),
        queued=0,
    )
    return JobStatus(**{**base, **fields})


def test_the_details_say_how_far_and_how_long_is_left():
    text = job_status.describe(status(eta=25 * 60))

    assert text == "Reading details in Photos: 500 of 12000  ·  about 25 minutes left"
    assert job_status.describe(status()) == "Reading details in Photos: 500 of 12000"
    assert job_status.describe(status(eta=3600, queued=2)).endswith(
        "about 1 h left  ·  2 more waiting"
    )


@pytest.mark.parametrize(
    "seconds, words",
    [
        (10, "a minute"),
        (150, "2 minutes"),
        (25 * 60, "25 minutes"),
        (59 * 60, "59 minutes"),
        (3600, "1 h"),
        (2 * 3600 + 10 * 60, "2 h 10 min"),
        (5 * 3600 + 31 * 60, "5 h 30 min"),
        (11 * 3600 + 20 * 60, "11 h"),
        (30 * 3600, "30 h"),
    ],
)
def test_a_time_left_in_words(seconds, words):
    assert job_status.time_left(seconds) == words


def test_the_walk_has_no_time_left_to_tell():
    text = job_status.describe(status(phase=FINDING, eta=100))

    assert text == "Scanning Photos: 500 photos found"


def test_imports_exports_and_indexing_tell_the_time_left_too():
    eta = 25 * 60

    assert job_status.describe_import(status(phase=COPYING, done=5, total=40, eta=eta)) == (
        "Importing into Photos: 5 of 40  ·  about 25 minutes left"
    )
    assert job_status.describe_export(status(phase=EXPORTING, done=5, total=40, eta=eta)) == (
        "Exporting to Photos: 5 of 40  ·  about 25 minutes left"
    )
    assert describe_indexing(status(phase="encoding", done=1500, total=12000, eta=eta)) == (
        "Indexing for search: 1 500 of 12 000  ·  about 25 minutes left"
    )
    assert describe_indexing(status(state=FAILED_JOB)) == "Could not index for search"
