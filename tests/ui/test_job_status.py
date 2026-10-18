"""The job in the status bar, in words."""

import pytest

from lumina.core.dbtypes import DETAILS, FINDING, RUNNING
from lumina.qt_models import library
from lumina.ui import job_status


def status(**fields):
    base = dict(
        folder_name="Photos",
        state=RUNNING,
        phase=DETAILS,
        done=500,
        total=12000,
        message=None,
        failures=(),
        queued=0,
    )
    return library.JobStatus(**{**base, **fields})


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
