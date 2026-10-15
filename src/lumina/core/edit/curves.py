"""Tone curves: a monotone cubic (PCHIP) through the user's points, sampled into a LUT.

PCHIP (Fritsch and Carlson's piecewise cubic Hermite interpolation) never overshoots:
between two points the curve stays within their values, so a curve through rising
points rises everywhere and a point dragged near the top can't push the curve past
white. A natural or Catmull-Rom spline can do both.

Points and the curve are in [0, 1] both ways, on sRGB-encoded values (see pipeline).
"""

import numpy as np

# Entries in a LUT: 12 bits, so 8-bit and 16-bit output alike see no steps
LUT_SIZE = 4096

IDENTITY = ((0.0, 0.0), (1.0, 1.0))


def normalize(points):
    """The points as a sorted tuple of (x, y) pairs in [0, 1], with one point per x
    (the last given wins) and the two ends present. Fewer than two points: identity."""
    clean = {}
    for point in points or ():
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if np.isfinite(x) and np.isfinite(y):
            clean[round(min(max(x, 0.0), 1.0), 6)] = round(min(max(y, 0.0), 1.0), 6)
    if len(clean) < 2:
        return IDENTITY
    xs = sorted(clean)
    if xs[0] > 0.0:
        clean[0.0] = clean[xs[0]]
    if xs[-1] < 1.0:
        clean[1.0] = clean[xs[-1]]
    return tuple((x, clean[x]) for x in sorted(clean))


def is_identity(points):
    return normalize(points) == IDENTITY


def _slopes(x, y):
    """Fritsch-Carlson slopes at each point: zero at a local extremum, the weighted
    harmonic mean of the neighbouring secants elsewhere."""
    h = np.diff(x)
    delta = np.diff(y) / h
    slopes = np.zeros_like(y)
    if len(x) == 2:
        slopes[:] = delta[0]
        return slopes
    for k in range(1, len(x) - 1):
        if delta[k - 1] * delta[k] > 0:
            w1 = 2 * h[k] + h[k - 1]
            w2 = h[k] + 2 * h[k - 1]
            slopes[k] = (w1 + w2) / (w1 / delta[k - 1] + w2 / delta[k])
    slopes[0] = _end_slope(h[0], h[1], delta[0], delta[1])
    slopes[-1] = _end_slope(h[-1], h[-2], delta[-1], delta[-2])
    return slopes


def _end_slope(h0, h1, d0, d1):
    # The three-point end condition, kept shape-preserving
    slope = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
    if np.sign(slope) != np.sign(d0):
        return 0.0
    if np.sign(d0) != np.sign(d1) and abs(slope) > abs(3 * d0):
        return 3 * d0
    return slope


def evaluate(points, values):
    """The curve through `points` at `values` (an array in [0, 1])."""
    points = normalize(points)
    x = np.array([p[0] for p in points], dtype=np.float64)
    y = np.array([p[1] for p in points], dtype=np.float64)
    return np.clip(monotone(x, y, np.clip(values, 0.0, 1.0)), 0.0, 1.0)


def monotone(x, y, values):
    """The PCHIP through the points `x` (rising), `y` at `values` (within x's range),
    on any scale: for curves the pipeline builds itself."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    t = np.clip(np.asarray(values, dtype=np.float64), x[0], x[-1])
    slopes = _slopes(x, y)
    k = np.clip(np.searchsorted(x, t, side="right") - 1, 0, len(x) - 2)
    h = x[k + 1] - x[k]
    s = (t - x[k]) / h
    s2, s3 = s * s, s * s * s
    result = (
        (2 * s3 - 3 * s2 + 1) * y[k]
        + (s3 - 2 * s2 + s) * h * slopes[k]
        + (-2 * s3 + 3 * s2) * y[k + 1]
        + (s3 - s2) * h * slopes[k + 1]
    )
    return result


def lut(points, size=LUT_SIZE):
    """The curve sampled at `size` even steps from 0 to 1, as float32."""
    return evaluate(points, np.linspace(0.0, 1.0, size)).astype(np.float32)
