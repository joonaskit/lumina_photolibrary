"""Events and On This Day: grouping by gaps in capture time, their names, the library's
times and the filter by time."""

from datetime import date

import pytest

from lumina.core import events
from lumina.core.db import Library
from lumina.core.dbtypes import Filter


def hours(*offsets, day="2026-03-04"):
    """Times `offsets` hours after midnight of `day`, as the library keeps them."""
    return [f"{day} {int(h):02d}:{int(h % 1 * 60):02d}:00" for h in offsets]


def test_photos_less_than_six_hours_apart_are_one_event():
    found = events.group(hours(1, 5, 10.9, 16.5))

    assert [e.count for e in found] == [4]
    assert (found[0].start.hour, found[0].end.hour) == (1, 16)


def test_a_gap_of_exactly_six_hours_starts_a_new_event():
    found = events.group(hours(1, 7))

    assert [e.count for e in found] == [1, 1]


def test_events_come_oldest_first_with_their_counts():
    times = hours(1, 2, 3) + hours(1, 2, day="2026-03-06") + hours(12, day="2026-04-01")

    assert [e.count for e in events.group(times)] == [3, 2, 1]


def test_photos_without_a_time_are_in_no_event():
    assert events.group([None, "", "not a time"]) == []
    assert [e.count for e in events.group([None, *hours(1, 2), "x"])] == [2]


def test_only_the_local_time_counts_not_the_offset():
    found = events.group(["2026-03-04 10:00:00+03:00", "2026-03-04 12:00:00-08:00"])

    assert [e.count for e in found] == [2]


def test_an_event_s_range_holds_times_with_sub_seconds():
    event = events.group(["2026-03-04 10:00:00.250", "2026-03-04 10:00:07.900"])[0]

    low, high = event.time_range()

    assert low <= "2026-03-04 10:00:00.250" and "2026-03-04 10:00:07.900" < high
    assert high <= "2026-03-04 10:00:09"


@pytest.mark.parametrize(
    ("first", "last", "name"),
    [
        (date(2026, 3, 4), date(2026, 3, 4), "4 Mar 2026"),
        (date(2026, 3, 4), date(2026, 3, 9), "4–9 Mar 2026"),
        (date(2026, 2, 28), date(2026, 3, 3), "28 Feb – 3 Mar 2026"),
        (date(2025, 12, 28), date(2026, 1, 3), "28 Dec 2025 – 3 Jan 2026"),
    ],
)
def test_an_event_is_named_by_its_dates(first, last, name):
    assert events.range_name(first, last) == name


def test_an_event_over_midnight_is_a_range_of_two_days():
    event = events.group(["2026-03-04 23:00:00", "2026-03-05 01:00:00"])[0]

    assert event.name() == "4–5 Mar 2026"


def test_on_this_day_has_the_date_in_each_earlier_year_newest_first():
    ranges = events.day_ranges(["2026", "2024", "2022", "2022"], date(2026, 3, 4))

    assert ranges == [
        ("2024-03-04 00:00:00", "2024-03-05 00:00:00"),
        ("2022-03-04 00:00:00", "2022-03-05 00:00:00"),
    ]


def test_the_leap_day_is_only_in_leap_years():
    ranges = events.day_ranges(["2023", "2022", "2020"], date(2028, 2, 29))

    assert [low[:4] for low, _high in ranges] == ["2020"]


# The library


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


def add(library, times):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    names = [f"p/{n}.jpg" for n in range(len(times))]
    library.write_scan(
        folder, volume, new=[(n, 1, 1, t) for n, t in zip(names, times, strict=True)]
    )
    return [library.photo_at("uuid:A", name) for name in names]


def test_a_stack_counts_once_at_its_top_photo(library):
    a, b, c, d = add(
        library,
        [
            "2026-03-04 10:00:00",
            "2026-03-04 20:00:00",
            "2026-03-04 20:01:00",
            "2026-03-04 21:00:00",
        ],
    )
    library.make_stack([b, c])

    times = library.event_times()

    assert times == ["2026-03-04 10:00:00", "2026-03-04 20:00:00", "2026-03-04 21:00:00"]
    assert [e.count for e in events.group(times)] == [1, 2]


def test_a_captured_time_is_used_before_the_file_time(library):
    (photo,) = add(library, ["2026-03-04 10:00:00"])
    library.db.execute("UPDATE photos SET captured = '2020-01-02 03:04:05' WHERE id = ?", (photo,))

    assert library.event_times() == ["2020-01-02 03:04:05"]


def test_the_filter_shows_only_the_photos_in_its_ranges(library):
    add(
        library,
        hours(1, 2, day="2026-03-04") + hours(3, day="2026-03-09") + hours(5, day="2025-03-04"),
    )
    event = events.group(library.event_times())[1]

    shown = library.photos(Filter(time_ranges=(event.time_range(),)))
    both = library.photos(
        Filter(time_ranges=(("2025-03-04 00:00:00", "2025-03-05 00:00:00"), event.time_range()))
    )

    assert [p.taken[:10] for p in shown] == ["2026-03-04", "2026-03-04"]
    assert len(both) == 3


def test_on_this_day_lists_only_the_years_that_have_photos_on_it(library):
    add(
        library,
        hours(5, day="2025-03-04") + hours(5, day="2023-03-05") + hours(5, day="2026-03-04"),
    )

    found = library.on_this_day(date(2026, 3, 4))

    assert found == [("2025-03-04 00:00:00", "2025-03-05 00:00:00")]
    assert library.on_this_day(date(2026, 7, 1)) == []
