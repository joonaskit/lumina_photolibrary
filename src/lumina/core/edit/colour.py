"""Vibrance and saturation: per-pixel colour controls, after the tone step.

Both scale each pixel's colour away from (or towards) its own grey, on sRGB-encoded
values, where the steps feel even. The grey is the pixel's luma, so brightness stays
where it was and a grey pixel stays grey whatever the controls say.

Saturation scales every colour by the same factor: 1 + amount, so -100 is
monochrome and +100 doubles every colour's distance from grey.

Vibrance scales by a factor that depends on the pixel: more for muted colours, less
for strong ones, and less again for skin tones (hues from red-orange to orange-yellow,
by the ordering of the channels: red over green over blue). Below zero it is the same
weight the other way round: muted colours are pulled to grey first, strong ones and
skin stay.

A boost never pushes a channel out of range: the factor is cut to what the pixel can
take, which keeps its hue and luma and leaves colours that are already at the edge of
the range where they are, rather than clipping one channel and shifting the hue.
"""

import cv2
import numpy as np

# Rec. 709 / sRGB luminance weights
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# Rows done at a time: small enough that the temporaries stay in the CPU cache, which
# is most of the speed of a step that does little arithmetic per value
ROWS = 32


def shrunk(plane, factor):
    """`plane` (H x W, or H x W x channels) averaged over blocks of `factor` x `factor`
    pixels, a smaller copy to filter on. A size that is not a multiple of `factor` is
    filled out with its own reflection first, so that every block is whole and the copy
    is the same whichever part of the photo it is made of, as long as the part starts at a
    multiple of `factor`."""
    height, width = plane.shape[:2]
    down, right = -height % factor, -width % factor
    if down or right:
        plane = cv2.copyMakeBorder(plane, 0, down, 0, right, cv2.BORDER_REFLECT_101)
    return cv2.resize(
        plane, ((width + right) // factor, (height + down) // factor), interpolation=cv2.INTER_AREA
    )


def grown(small, factor, size):
    """`small` (from `shrunk`) scaled up again to `size` (height, width)."""
    height, width = size
    big = cv2.resize(
        small, (small.shape[1] * factor, small.shape[0] * factor), interpolation=cv2.INTER_LINEAR
    )
    return big[:height, :width]


def _plane():
    """A quarter turn in the plane of colours with no luma (the `d` that is a colour
    less its luma: LUMA @ d is 0), as a matrix to apply to rows. Turning by an angle is
    cos * d + sin * (d @ QUARTER). The turn goes the way hue grows: red to yellow."""
    w = LUMA.astype(np.float64)
    first, _second, third = (np.eye(3)[i] - w[i] for i in range(3))
    q1 = first / np.linalg.norm(first)
    other = third - (third @ q1) * q1
    q2 = other / np.linalg.norm(other)
    quarter = np.outer(q2, q1) - np.outer(q1, q2)  # acts on column vectors
    # Red moves towards yellow when its green rises
    if (quarter @ first)[1] < 0:
        quarter = -quarter
    return quarter.T.astype(np.float32)


QUARTER = _plane()

# How much of vibrance a skin tone keeps at the middle of the skin hues
_SKIN_KEEP = 0.4
# Share of red over blue taken by green, for the hues counted as skin: from a red
# orange (hue 10 degrees) to an orange yellow (45 degrees), with soft edges
_SKIN_EDGES = (0.05, 0.17, 0.75, 0.9)


def smoothstep(low, high, x):
    t = np.clip((x - low) / (high - low), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _skin(r, g, b):
    """1 for the hues of skin, 0 for the others, soft between."""
    t = (g - b) / np.maximum(r - b, np.float32(1e-6))
    low, rise, fall, high = _SKIN_EDGES
    weight = smoothstep(low, rise, t) * (1.0 - smoothstep(fall, high, t))
    return np.where((r >= g) & (g >= b), weight, np.float32(0.0))


def room(away, luma):
    """The largest factor the colours `away` (their distances from grey, rows x W x 3)
    can be scaled by about their `luma` and stay in [0, 1]."""
    top = np.maximum(np.maximum(away[..., 0], away[..., 1]), away[..., 2])
    bottom = -np.minimum(np.minimum(away[..., 0], away[..., 1]), away[..., 2])
    eps = np.float32(1e-6)
    return np.minimum((1.0 - luma) / np.maximum(top, eps), luma / np.maximum(bottom, eps))


def adjust_rows(encoded, vibrance, saturation):
    r, g, b = encoded[..., 0], encoded[..., 1], encoded[..., 2]
    luma = encoded @ LUMA
    high = np.maximum(np.maximum(r, g), b)
    low = np.minimum(np.minimum(r, g), b)
    factor = np.float32(1.0 + saturation / 100.0)
    if vibrance:
        weight = (1.0 - (high - low)) ** 2 * (1.0 - (1.0 - _SKIN_KEEP) * _skin(r, g, b))
        factor = factor * (1.0 + np.float32(vibrance / 100.0) * weight)
    if vibrance > 0 or saturation > 0:
        # Never more than the pixel can take (its brightest channel up to 1, its
        # darkest down to 0), and never less than it is
        eps = np.float32(1e-6)
        room = np.minimum(
            (1.0 - luma) / np.maximum(high - luma, eps), luma / np.maximum(luma - low, eps)
        )
        factor = np.minimum(factor, np.maximum(room, 1.0))
    encoded *= factor[..., None] if np.ndim(factor) else factor
    encoded += (luma * (1.0 - factor))[..., None]
