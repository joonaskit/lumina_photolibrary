"""Where a local adjustment applies: a mask's weight at each pixel, from 0 to 1.

A mask is a shape in the cropped and straightened photo, in shares of its size, so the
proxy and the full-size render agree (see params.Mask). Its weight at a pixel is a
formula of the pixel's position, evaluated for the rows and columns asked for and never
for the whole photo, so a full-resolution render holds no plane of weights.

- linear: a line from (x0, y0), where the effect is full, to (x1, y1), where it is gone,
  smooth between them; full on the first point's side of it and gone beyond the second.
- radial: an ellipse at (x, y) with radii rx and ry, as shares of the photo's longer side
  (so equal radii are a circle whatever the photo's shape), turned `angle` degrees
  clockwise on the screen. Full inside, gone outside, the edge softened `feather` percent
  to either side of it.

An inverted mask is one minus that.
"""

import numpy as np

from lumina.core.edit.colour import smoothstep

# The narrowest the edge of a radial mask gets: a hard edge, but not a step in one pixel
_MIN_FEATHER = 0.005


def weights(mask, row, column, rows, columns, size):
    """The weight of `mask` for `rows` x `columns` pixels of the photo of `size` (height,
    width), the first at (`row`, `column`): floats of that shape, in [0, 1]."""
    height, width = size
    # Pixel centres, in pixels from the photo's top left
    ys = (np.arange(row, row + rows, dtype=np.float32) + np.float32(0.5))[:, None]
    xs = (np.arange(column, column + columns, dtype=np.float32) + np.float32(0.5))[None, :]
    if mask.kind == "linear":
        weight = _linear(mask.shape, xs, ys, width, height)
    else:
        weight = _radial(mask.shape, mask.feather, xs, ys, width, height)
    if mask.invert:
        weight = np.float32(1.0) - weight
    return weight


def _linear(shape, xs, ys, width, height):
    x0, y0, x1, y1 = shape
    x0, x1 = x0 * width, x1 * width
    y0, y1 = y0 * height, y1 * height
    dx, dy = x1 - x0, y1 - y0
    # How far along the line each pixel is: 0 at the start, 1 at the end
    length = max(dx * dx + dy * dy, 1.0)
    along = ((xs - np.float32(x0)) * np.float32(dx) + (ys - np.float32(y0)) * np.float32(dy)) * (
        np.float32(1.0 / length)
    )
    return (np.float32(1.0) - smoothstep(0.0, 1.0, along)).astype(np.float32, copy=False)


def _radial(shape, feather, xs, ys, width, height):
    x, y, rx, ry, angle = shape
    long_side = max(width, height)
    soft = max(feather / 100.0, _MIN_FEATHER)
    cos, sin = np.cos(np.radians(angle)), np.sin(np.radians(angle))
    # The pixel in the ellipse's own axes (y points down, so a positive angle is clockwise)
    dx = xs - np.float32(x * width)
    dy = ys - np.float32(y * height)
    u = (dx * np.float32(cos) + dy * np.float32(sin)) * np.float32(1.0 / (rx * long_side))
    v = (dy * np.float32(cos) - dx * np.float32(sin)) * np.float32(1.0 / (ry * long_side))
    distance = np.sqrt(u * u + v * v)
    return (np.float32(1.0) - smoothstep(1.0 - soft, 1.0 + soft, distance)).astype(
        np.float32, copy=False
    )
