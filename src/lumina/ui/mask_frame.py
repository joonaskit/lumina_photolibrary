"""A mask's handles on the photo: where they are, which one a click takes, what dragging
one does to the mask, and how the mask and its tint are drawn.

Everything is in the photo as shown (its `rect` in the view, zoomed or not) and in shares
of it, as a mask's shape is (see core/edit/masks.py): a radius is a share of the longer
side. Drawn with Qt gradients, which follow the same formulas, so the view needs nothing
from the render."""

import math
from dataclasses import replace

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
    QTransform,
)

from lumina.core.edit.params import ANGLE, POSITION, RADIUS
from lumina.integration import style

# The handles' radius and how near the pointer must be to take one, unscaled
HANDLE = 6
GRAB = 12
# How strong the tint is where the mask is full
_TINT = 0.4
# Stops of the tint's fall from full to gone: smoothstep is sampled at this many points
_STOPS = 12
# The narrowest a radial mask's edge is drawn, as the render's (masks.py)
_MIN_FEATHER = 0.005


def _clamped(value, bounds):
    return min(max(value, bounds[0]), bounds[1])


def _smooth(t):
    t = min(max(t, 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def points(mask, rect):
    """The handles of `mask` on a photo drawn in `rect`: name -> QPointF. A linear mask
    has "start" (the effect full) and "end" (gone); a radial one its "centre" and the
    ends of its two radii, "rx" and "ry"."""
    width, height = rect.width(), rect.height()
    if mask.kind == "linear":
        x0, y0, x1, y1 = mask.shape
        return {
            "start": QPointF(rect.left() + x0 * width, rect.top() + y0 * height),
            "end": QPointF(rect.left() + x1 * width, rect.top() + y1 * height),
        }
    x, y, rx, ry, angle = mask.shape
    long_side = max(width, height)
    centre = QPointF(rect.left() + x * width, rect.top() + y * height)
    cos, sin = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return {
        "centre": centre,
        "rx": centre + QPointF(cos, sin) * (rx * long_side),
        "ry": centre + QPointF(-sin, cos) * (ry * long_side),
    }


def hit(mask, position, rect, grab=None):
    """What a click at `position` takes of `mask`: a handle's name, "move" for the rest of
    the mask's body (a linear mask's band between its lines, a radial mask's inside), or
    None."""
    grab = style.px(GRAB) if grab is None else grab
    handles = points(mask, rect)
    for name, point in handles.items():
        if abs(point.x() - position.x()) <= grab and abs(point.y() - position.y()) <= grab:
            return name
    if mask.kind == "linear":
        a, b = handles["start"], handles["end"]
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = dx * dx + dy * dy
        if length < 1e-9:
            return None
        along = ((position.x() - a.x()) * dx + (position.y() - a.y()) * dy) / length
        return "move" if 0.0 <= along <= 1.0 else None
    u, v = _in_axes(mask, position, rect)
    return "move" if u * u + v * v <= 1.0 else None


def _in_axes(mask, position, rect):
    """`position` in a radial mask's own axes, in units of its radii."""
    x, y, rx, ry, angle = mask.shape
    long_side = max(rect.width(), rect.height())
    dx = position.x() - (rect.left() + x * rect.width())
    dy = position.y() - (rect.top() + y * rect.height())
    cos, sin = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return (dx * cos + dy * sin) / (rx * long_side), (dy * cos - dx * sin) / (ry * long_side)


def dragged(mask, part, start, position, rect):
    """`mask` (as it was when the drag began at `start`) with `part` dragged to `position`."""
    width, height = rect.width(), rect.height()
    if width <= 0 or height <= 0:
        return mask
    dx, dy = (position.x() - start.x()) / width, (position.y() - start.y()) / height
    px, py = (position.x() - rect.left()) / width, (position.y() - rect.top()) / height
    px, py = _clamped(px, POSITION), _clamped(py, POSITION)
    if mask.kind == "linear":
        x0, y0, x1, y1 = mask.shape
        if part == "start":
            return replace(mask, shape=(px, py, x1, y1))
        if part == "end":
            return replace(mask, shape=(x0, y0, px, py))
        # Both ends together, kept as far apart as they were
        dx = _clamped(x0 + dx, POSITION) - x0
        dx = _clamped(x1 + dx, POSITION) - x1
        dy = _clamped(y0 + dy, POSITION) - y0
        dy = _clamped(y1 + dy, POSITION) - y1
        return replace(mask, shape=(x0 + dx, y0 + dy, x1 + dx, y1 + dy))
    x, y, rx, ry, angle = mask.shape
    long_side = max(width, height)
    if part in ("centre", "move"):
        return replace(
            mask,
            shape=(
                _clamped(x + dx, POSITION),
                _clamped(y + dy, POSITION),
                rx,
                ry,
                angle,
            ),
        )
    centre_x, centre_y = rect.left() + x * width, rect.top() + y * height
    vx, vy = position.x() - centre_x, position.y() - centre_y
    reach = _clamped(math.hypot(vx, vy) / long_side, RADIUS)
    if part == "rx":
        # The handle on the first axis sets the radius and turns the ellipse with it
        if math.hypot(vx, vy) < 1e-6:
            return mask
        turned = _clamped(math.degrees(math.atan2(vy, vx)), ANGLE)
        return replace(mask, shape=(x, y, reach, ry, turned))
    return replace(mask, shape=(x, y, rx, reach, angle))


# Drawing


def _tint(alpha=1.0):
    colour = QColor(style.color("accent"))
    colour.setAlphaF(_TINT * alpha)
    return colour


def paint(painter, mask, rect, overlay):
    """The mask's handles over the photo drawn in `rect`, and with `overlay` its weight
    as a tint."""
    painter.save()
    painter.setClipRect(rect)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if overlay:
        _paint_tint(painter, mask, rect)
    _paint_guides(painter, mask, rect)
    painter.restore()


def _paint_tint(painter, mask, rect):
    handles = points(mask, rect)
    if mask.kind == "linear":
        gradient = QLinearGradient(handles["start"], handles["end"])
        for index in range(_STOPS + 1):
            t = index / _STOPS
            weight = 1.0 - _smooth(t)
            gradient.setColorAt(t, _tint(1.0 - weight if mask.invert else weight))
        painter.fillRect(rect, QBrush(gradient))
        return
    x, y, rx, ry, angle = mask.shape
    long_side = max(rect.width(), rect.height())
    soft = max(mask.feather / 100.0, _MIN_FEATHER)
    reach = 1.0 + soft
    painter.save()
    transform = QTransform()
    transform.translate(handles["centre"].x(), handles["centre"].y())
    transform.rotate(angle)
    transform.scale(rx * long_side, ry * long_side)
    painter.setTransform(transform, True)
    gradient = QRadialGradient(0.0, 0.0, reach)
    for index in range(_STOPS + 1):
        t = index / _STOPS
        distance = t * reach
        weight = 1.0 - _smooth((distance - (1.0 - soft)) / (2.0 * soft))
        gradient.setColorAt(t, _tint(1.0 - weight if mask.invert else weight))
    # Beyond the last stop the gradient holds its colour, as the render holds its weight
    outside, _ok = painter.worldTransform().inverted()
    painter.fillRect(outside.mapRect(rect), QBrush(gradient))
    painter.restore()


def _outlined(painter, draw):
    # A line in the text colour over a wider one in the background's, to be seen on any photo
    halo = QPen(style.color("background"), 3)
    line = QPen(style.color("textPrimary"), 1)
    for pen in (halo, line):
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        draw()


def _paint_guides(painter, mask, rect):
    handles = points(mask, rect)
    radius = style.px(HANDLE)
    if mask.kind == "linear":
        a, b = handles["start"], handles["end"]
        direction = QPointF(b.x() - a.x(), b.y() - a.y())
        length = math.hypot(direction.x(), direction.y())
        if length > 1e-6:
            across = (
                QPointF(-direction.y(), direction.x()) / length * (rect.width() + rect.height())
            )
            full = QLineF(a - across, a + across)
            gone = QLineF(b - across, b + across)
            _outlined(painter, lambda: painter.drawLine(full))
            painter.setPen(QPen(style.color("textPrimary"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(gone)
        _outlined(painter, lambda: painter.drawLine(a, b))
    else:
        x, y, rx, ry, angle = mask.shape
        long_side = max(rect.width(), rect.height())

        def ellipse():
            painter.save()
            painter.translate(handles["centre"])
            painter.rotate(angle)
            painter.drawEllipse(QPointF(0, 0), rx * long_side, ry * long_side)
            painter.restore()

        _outlined(painter, ellipse)
    for point in handles.values():
        _outlined(painter, lambda point=point: painter.drawEllipse(point, radius, radius))
    painter.setBrush(Qt.BrushStyle.NoBrush)


def visible(rect):
    """Whether there is a photo to draw masks on."""
    return isinstance(rect, QRectF) and rect.width() > 0 and rect.height() > 0
