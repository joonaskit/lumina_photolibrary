"""Events and On This Day in the sidebar: listed by year, chosen like a folder, and
filtering the grid by time."""

from datetime import datetime, timedelta

from conftest import TIMEOUT
from test_flags import open_window

from lumina.core.events import Event
from lumina.ui.sidebar import PATH_ROLE, RANGES_ROLE


def event(start, hours=1, count=12):
    return Event(start, start + timedelta(hours=hours), count)


def items(window):
    tree = window.sidebar.event_tree
    found = []
    for n in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(n)
        found.append(
            (
                top.data(0, PATH_ROLE) or top.text(0),
                [top.child(c).text(0) for c in range(top.childCount())],
            )
        )
    return found


def test_events_are_listed_by_year_newest_first_with_their_counts(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    older, february, july = (
        event(datetime(2024, 3, 4, 10)),
        event(datetime(2025, 2, 1)),
        event(datetime(2025, 7, 1, 9), 30),
    )

    window.browser.on_events_changed([older, february, july], [])

    # Named in the user's language, so the names are the events' own
    assert items(window) == [
        ("2025", [f"\u2003{july.name()}  (12)", f"\u2003{february.name()}  (12)"]),
        ("2024", [f"\u2003{older.name()}  (12)"]),
    ]
    assert window.sidebar.event_tree.isVisibleTo(window)


def test_the_section_is_hidden_without_events_or_days(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")

    window.browser.on_events_changed([], [])

    assert not window.sidebar.event_tree.isVisibleTo(window)


def test_on_this_day_is_listed_first_when_a_day_has_photos(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")

    window.browser.on_events_changed([event(datetime(2024, 3, 4, 10))], [("a", "b")])

    assert [top for top, _ in items(window)] == ["On This Day", "2024"]


def test_choosing_an_event_shows_the_photos_taken_in_it_and_all_photos_ends_it(
    qtbot, service, drive
):
    window = open_window(qtbot, service, drive / "Photos")
    # The test library's photos were all taken on 9 May 2022 at 21:03
    window.browser.on_events_changed(
        [event(datetime(2020, 1, 1)), event(datetime(2022, 5, 9, 19), 4)], []
    )
    tree = window.sidebar.event_tree
    this_year = tree.topLevelItem(0)
    this_year.setExpanded(True)

    tree.setCurrentItem(this_year.child(0))
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert window.model.rowCount() == 3
    assert window.sidebar.current_time_ranges() == tuple(this_year.child(0).data(0, RANGES_ROLE))
    assert window.sidebar.list.currentItem() is None

    window.sidebar.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert window.sidebar.current_time_ranges() == ()
    assert tree.currentItem() is None


def test_an_event_without_the_photos_shows_none(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    window.browser.on_events_changed([event(datetime(2020, 1, 1))], [])
    tree = window.sidebar.event_tree

    tree.topLevelItem(0).setExpanded(True)
    tree.setCurrentItem(tree.topLevelItem(0).child(0))
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert window.model.rowCount() == 0
    assert window.title.text() == event(datetime(2020, 1, 1)).name()


def test_the_event_chosen_that_is_gone_falls_back_to_all_photos(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    window.browser.on_events_changed([event(datetime(2020, 1, 1))], [])
    tree = window.sidebar.event_tree
    tree.topLevelItem(0).setExpanded(True)
    tree.setCurrentItem(tree.topLevelItem(0).child(0))
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    window.browser.on_events_changed([], [])
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert window.sidebar.current_time_ranges() == ()
    assert window.model.rowCount() == 3


def test_the_service_works_out_the_events_after_a_scan(qtbot, service, drive):
    heard = []
    service.events_changed.connect(lambda found, days: heard.append((found, days)))

    open_window(qtbot, service, drive / "Photos")
    qtbot.waitUntil(lambda: bool(heard), timeout=TIMEOUT)

    found, days = heard[-1]
    # Three photos are fewer than an event needs to be listed, and none is from an earlier year
    assert found == []
    assert days == []


def test_only_the_current_year_is_open_at_first_and_the_ones_opened_stay_open(
    qtbot, service, drive
):
    window = open_window(qtbot, service, drive / "Photos")
    tree = window.sidebar.event_tree
    now = datetime.now()
    found = [event(datetime(2020, 1, 1)), event(now.replace(month=1, day=1))]

    window.browser.on_events_changed(found, [])

    assert [tree.topLevelItem(n).isExpanded() for n in range(2)] == [True, False]

    tree.topLevelItem(1).setExpanded(True)
    tree.topLevelItem(0).setExpanded(False)
    window.browser.on_events_changed(found, [])

    assert [tree.topLevelItem(n).isExpanded() for n in range(2)] == [False, True]
