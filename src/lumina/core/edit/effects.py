"""Vignette and grain: the last steps, on the cropped photo's sRGB-encoded values.

Both work from where a pixel is in the photo as rendered (after the crop), not from
where it was in the original, and both are made so that a small render looks like a
large one of the same photo: the vignette is in shares of the frame, the grain's size
in shares of its long side.

The vignette follows the frame's ellipse, so it is round on a square photo and wider
on a wide one. Below zero it multiplies the corners darker; above zero it blends them
towards white. Midpoint is where, along the way from the centre to a corner, it is half
there; feather how wide the way from nothing to everything is.

The grain is value noise, hashed from the position of each lattice point in the render
with a fixed seed: the same photo at the same size has the same grain every time, and
it does not depend on how the render is cut into tiles. It is the same on all three
channels (it is brightness grain, not colour speckle) and weaker in the deepest shadows
and the brightest highlights, and gone from pure black and white, where it would only be
clipped.
"""

from functools import lru_cache

import numpy as np

from lumina.core.edit import colour

# The vignette's midpoint control, 0 to 100, as the share of the way to a corner
# where it is half there, and its feather as half the width of the way from nothing
# to everything
_MIDPOINT = (0.2, 1.0)
_FEATHER = (0.05, 0.5)

# The grain's size control, 0 to 100, as the size of a grain in shares of the long side
# of the photo: from 1/2500 (3 px on 8000) to 1/250
_SIZE = (1 / 2500, 1 / 250)
# How strong the grain is at 100: its standard deviation, on encoded values
_STRENGTH = 0.08
# Grain keeps this much of its strength in the deepest shadows and brightest highlights,
# and fades out to nothing over the last _CLEAN of the range, so that pure black and
# pure white stay clean (grain there would only be clipped, and lift them)
_EDGES = 0.35
_CLEAN = 0.03
SEED = 0x2F6B1D93


# Vignette


def prepare_vignette(vignette):
    """What `vignette_rows` needs: the amount, and where the effect begins and ends,
    as shares of the way from the centre to a corner."""
    middle = _MIDPOINT[0] + (_MIDPOINT[1] - _MIDPOINT[0]) * vignette.midpoint / 100.0
    half = _FEATHER[0] + (_FEATHER[1] - _FEATHER[0]) * vignette.feather / 100.0
    return vignette.amount / 100.0, middle - half, middle + half


def vignette_rows(encoded, first_row, size, prepared, first_column=0):
    """`encoded` (rows x columns x 3 float32) from row `first_row` and column
    `first_column` of a photo `size` (height, width) with the vignette applied, in place."""
    amount, begin, end = prepared
    height, width = size
    ys = (np.arange(first_row, first_row + len(encoded), dtype=np.float32) + 0.5) / (height / 2) - 1
    columns = np.arange(first_column, first_column + encoded.shape[1], dtype=np.float32)
    xs = (columns + 0.5) / (width / 2) - 1
    # 0 at the centre, 1 at a corner
    distance = np.sqrt(xs[None, :] ** 2 + ys[:, None] ** 2) * np.float32(np.sqrt(0.5))
    weight = colour.smoothstep(begin, end, distance)
    if amount < 0:
        encoded *= (1.0 + amount * weight)[..., None]
    else:
        encoded += (amount * weight)[..., None] * (1.0 - encoded)


# Grain


def _smooth(t):
    return t * t * (3.0 - 2.0 * t)


def _lattice(ix, iy):
    """Noise in [-1, 1] at the lattice points `ix` (a row of them) by `iy` (a column):
    a hash of each point's place and the seed (murmur3's finaliser)."""
    h = ix.astype(np.uint32) * np.uint32(374761393)
    h = h + iy.astype(np.uint32) * np.uint32(668265263) + np.uint32(SEED)
    h ^= h >> np.uint32(16)
    h *= np.uint32(0x85EBCA6B)
    h ^= h >> np.uint32(13)
    h *= np.uint32(0xC2B2AE35)
    h ^= h >> np.uint32(16)
    return h.astype(np.float32) * np.float32(2.0 / 4294967295.0) - 1.0


@lru_cache(maxsize=32)
def _spread(cell):
    """How much interpolating between lattice points, `cell` pixels apart, takes from
    the noise's strength: the mean square of the weights, over where the pixels fall
    between lattice points, in each direction. Divided out, the grain is as strong at
    any size."""
    t = (np.arange(4096) / cell) % 1.0
    s = _smooth(t)
    return float(np.mean((1 - s) ** 2 + s**2))


def prepare_grain(grain, size):
    """What `grain_rows` needs for a photo `size` (height, width): the strength, the
    grain's size in pixels, and the factor that makes the interpolated noise as strong
    as the lattice's."""
    low, high = _SIZE
    cell = max(1.0, max(size) * (low + (high - low) * grain.size / 100.0))
    cell = round(cell, 3)
    # Uniform noise in [-1, 1] has a standard deviation of 1 / sqrt(3)
    unit = np.sqrt(3.0) / _spread(cell)
    return _STRENGTH * grain.amount / 100.0 * unit, cell


def grain_rows(encoded, first_row, size, prepared, first_column=0):
    """`encoded` (rows x columns x 3 float32) from row `first_row` and column
    `first_column` of a photo `size` (height, width) with the grain added, in place."""
    strength, cell = prepared
    rows, columns = encoded.shape[:2]
    y = np.arange(first_row, first_row + rows, dtype=np.float32) / np.float32(cell)
    x = np.arange(first_column, first_column + columns, dtype=np.float32) / np.float32(cell)
    iy, ix = y.astype(np.int64), x.astype(np.int64)
    fy, fx = _smooth(y - iy), _smooth(x - ix)
    # The lattice points the rows and columns lie between
    first, left = iy[0], ix[0]
    points = _lattice(np.arange(left, ix[-1] + 2)[None, :], np.arange(first, iy[-1] + 2)[:, None])
    above, below = points[iy - first], points[iy - first + 1]
    line = above + (below - above) * fy[:, None]
    noise = line[:, ix - left]
    noise += (line[:, ix - left + 1] - noise) * fx[None, :]
    luma = encoded @ colour.LUMA
    # Full strength in the middle, `_EDGES` of it towards black and white, none at them
    noise *= strength * (_EDGES + (1.0 - _EDGES) * (1.0 - (2.0 * luma - 1.0) ** 2))
    noise *= colour.smoothstep(0.0, _CLEAN, luma) * colour.smoothstep(0.0, _CLEAN, 1.0 - luma)
    encoded += noise[..., None]
    np.clip(encoded, 0.0, 1.0, out=encoded)
