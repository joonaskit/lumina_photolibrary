"""Events: runs of photos with no long gap in capture time, and the days of earlier years
that On This Day looks at.

An event is computed from the times, never stored, so it is always in step with the
library. The time is the camera's local wall-clock time (core/capture_time.py): its offset
is ignored, so a flight across time zones can move a photo by hours.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

# A gap this long or longer between two photos starts a new event
GAP = timedelta(hours=6)
# The sidebar lists events of at least this many photos
LISTED_FROM = 10

_TEXT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    count: int

    def time_range(self):
        """(low, high) as text, for the filter: from the first photo's second up to the
        second after the last one's, so that times with sub-seconds fall inside."""
        low = self.start.replace(microsecond=0)
        high = self.end.replace(microsecond=0) + timedelta(seconds=1)
        return low.strftime(_TEXT), high.strftime(_TEXT)

    def name(self):
        return range_name(self.start.date(), self.end.date())


def _parse(taken):
    try:
        return datetime.fromisoformat(taken).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def group(times, gap=GAP):
    """The events of `times` (ISO text, in time order), oldest first. A time that can't
    be read, or none, is in no event."""
    events = []
    first = last = None
    count = 0
    for when in filter(None, map(_parse, times)):
        if last is not None and when - last >= gap:
            events.append(Event(first, last, count))
            first, count = None, 0
        if first is None:
            first = when
        last = when
        count += 1
    if first is not None:
        events.append(Event(first, last, count))
    return events


def range_name(first, last):
    """ "4 Mar 2026", "4–9 Mar 2026", "28 Feb – 3 Mar 2026" or "28 Dec 2025 – 3 Jan 2026"."""
    if first == last:
        return f"{first.day} {first:%b %Y}"
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}–{last.day} {last:%b %Y}"
    if first.year == last.year:
        return f"{first.day} {first:%b} – {last.day} {last:%b %Y}"
    return f"{first.day} {first:%b %Y} – {last.day} {last:%b %Y}"


def day_ranges(years, today):
    """The (low, high) text ranges of `today`'s month and day in each of `years` before
    today's year, newest year first. 29 February is only in leap years."""
    ranges = []
    for year in sorted({int(y) for y in years}, reverse=True):
        if year >= today.year:
            continue
        try:
            day = date(year, today.month, today.day)
        except ValueError:
            continue
        ranges.append((day.strftime(_TEXT), (day + timedelta(days=1)).strftime(_TEXT)))
    return ranges
