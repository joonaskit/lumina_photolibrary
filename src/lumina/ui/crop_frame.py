"""The crop frame's geometry: dragging its edges and corners, free or held to an aspect
ratio, and fitting it to one. In shares of the photo as shown (after any turning), so
`aspect` is the photo's width over its height and a `ratio` is a frame's width over its
height in pixels."""

from lumina.core.edit.params import Crop

# The smallest crop, as a share of the photo
MIN_CROP = 0.02


def dragged(crop, part, dx, dy):
    """`crop` with `part` (an edge, a corner, or "move") dragged by dx, dy shares."""
    left, top, right, bottom = crop.left, crop.top, crop.right, crop.bottom
    if part == "move":
        dx = min(max(dx, -left), 1 - right)
        dy = min(max(dy, -top), 1 - bottom)
        return Crop(left + dx, top + dy, right + dx, bottom + dy)
    if "l" in part:
        left = min(max(left + dx, 0.0), right - MIN_CROP)
    if "r" in part:
        right = max(min(right + dx, 1.0), left + MIN_CROP)
    if "t" in part:
        top = min(max(top + dy, 0.0), bottom - MIN_CROP)
    if "b" in part:
        bottom = max(min(bottom + dy, 1.0), top + MIN_CROP)
    return Crop(left, top, right, bottom)


def reshaped(crop, ratio, aspect):
    """`crop` as a frame of `ratio`: the same area and centre, as far as the photo
    has room, shifted to lie inside it when it would not."""
    shape = ratio / aspect  # the frame's width share over its height share
    area = (crop.right - crop.left) * (crop.bottom - crop.top)
    width = (area * shape) ** 0.5
    height = width / shape
    fit = min(1.0, 1.0 / width, 1.0 / height)
    width, height = width * fit, height * fit
    x, y = (crop.left + crop.right) / 2, (crop.top + crop.bottom) / 2
    left, right = _within(x - width / 2, width)
    top, bottom = _within(y - height / 2, height)
    return Crop(left, top, right, bottom)


def _within(start, size):
    # A span of `size` from `start`, moved to lie inside 0 to 1
    start = min(max(start, 0.0), 1.0 - size)
    return start, start + size


def dragged_to_ratio(crop, part, dx, dy, ratio, aspect):
    """`crop` with `part` dragged by dx, dy shares and kept at `ratio`.

    A corner keeps the opposite corner still and follows the pointer along the frame's
    diagonal; an edge keeps the opposite edge still and makes the frame wider or taller
    about its centre. Moving the frame leaves its shape alone."""
    if part == "move":
        return dragged(crop, part, dx, dy)
    shape = ratio / aspect
    free = dragged(crop, part, dx, dy)
    width, height = free.right - free.left, free.bottom - free.top
    centre_x, centre_y = (crop.left + crop.right) / 2, (crop.top + crop.bottom) / 2
    if part in ("l", "r"):
        # As wide as dragged, as tall as that makes it, about the same middle
        width = min(width, shape)
        height = width / shape
        top, bottom = _within(centre_y - height / 2, height)
        if part == "l":
            return Crop(crop.right - width, top, crop.right, bottom)
        return Crop(crop.left, top, crop.left + width, bottom)
    if part in ("t", "b"):
        height = min(height, 1.0 / shape)
        width = height * shape
        left, right = _within(centre_x - width / 2, width)
        if part == "t":
            return Crop(left, crop.bottom - height, right, crop.bottom)
        return Crop(left, crop.top, right, crop.top + height)
    # A corner: the larger of the two sizes the pointer asks for, so that the frame
    # reaches it, but no larger than the photo has room for beyond the opposite corner
    width = max(width, height * shape)
    anchor_x = crop.right if "l" in part else crop.left
    anchor_y = crop.bottom if "t" in part else crop.top
    room_x = anchor_x if "l" in part else 1.0 - anchor_x
    room_y = anchor_y if "t" in part else 1.0 - anchor_y
    width = min(width, room_x, room_y * shape)
    height = width / shape
    left = anchor_x - width if "l" in part else anchor_x
    top = anchor_y - height if "t" in part else anchor_y
    return Crop(left, top, left + width, top + height)
