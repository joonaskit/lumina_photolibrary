"""Constants and helpers the window and its parts share."""

from PySide6.QtCore import QMargins, QPoint, QSize

from lumina.integration import style
from lumina.qt_models.library_model import PICKED, REJECTED

APP_NAME = "Lumina"
# Size of a thumbnail in the grid, unscaled, by the size chosen. Large stays within the
# thumbnails' own 320 pixels (qt_models/thumbnails.py), so it is sharp on a 1x screen
GRID_ICONS = {"small": 112, "medium": 168, "large": 280}
# A cell is its thumbnail with room around it and the name under it
GRID_CELL_EXTRA = QSize(28, 46)
# The largest image the viewer asks for: a 4K screen's width
VIEWER_MAX_SIDE = 3840

# Space between neighbouring cells, so their hover and selection backgrounds stay apart
GRID_GAP = 8
# How long after the grid stopped scrolling the thumbnails not wanted are dropped
_TRIM_MS = 150
# How much of a dimmed thumbs-down photo shows
DIMMED_OPACITY = 0.35

FLAG_NAMES = {PICKED: "Thumbs up", REJECTED: "Thumbs down"}

_EMPTY, _GRID, _VIEWER, _DEVELOP, _LIVE = range(5)
# What the shortcuts' overlay says of each page
_VIEW_NAMES = {
    _EMPTY: "Library",
    _GRID: "Library",
    _VIEWER: "Viewer",
    _DEVELOP: "Develop",
    _LIVE: "Live View",
}
# Why an action on photos can't be used
_NO_PHOTO = "No photo chosen"


def describe_photo(photo):
    """When, with what and how a photo was taken, in one line: "2022-05-09 21:03 ·
    Canon EOS 90D · EF-S55-250mm f/4-5.6 IS STM · 250 mm · 1/250 s · f/5.6 · ISO 400"."""
    if not photo:
        return ""
    parts = []
    taken = photo.captured or photo.file_time
    if taken:
        parts.append(taken[:16])
    for value in (photo.model, photo.lens):
        if value:
            parts.append(value)
    if photo.focal_length:
        parts.append(f"{photo.focal_length:g} mm")
    exposure = photo.exposure_time
    if exposure:
        parts.append(f"1/{round(1 / exposure)} s" if exposure < 1 else f"{exposure:g} s")
    if photo.aperture:
        parts.append(f"f/{photo.aperture:g}")
    if photo.iso:
        parts.append(f"ISO {photo.iso}")
    return "  ·  ".join(parts)


def _margins(gap):
    # Half the gap on each side of a cell
    return QMargins(gap // 2, gap // 2, gap - gap // 2, gap - gap // 2)


def _inset():
    # Inside the first cell, past the grid's own margin
    return QPoint(style.gap(GRID_GAP * 2), style.gap(GRID_GAP * 2))
