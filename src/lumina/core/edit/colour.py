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

import numpy as np

# Rec. 709 / sRGB luminance weights
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# Rows done at a time: small enough that the temporaries stay in the CPU cache, which
# is most of the speed of a step that does little arithmetic per value
_ROWS = 32
# How much of vibrance a skin tone keeps at the middle of the skin hues
_SKIN_KEEP = 0.4
# Share of red over blue taken by green, for the hues counted as skin: from a red
# orange (hue 10 degrees) to an orange yellow (45 degrees), with soft edges
_SKIN_EDGES = (0.05, 0.17, 0.75, 0.9)


def _smoothstep(low, high, x):
    t = np.clip((x - low) / (high - low), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _skin(r, g, b):
    """1 for the hues of skin, 0 for the others, soft between."""
    t = (g - b) / np.maximum(r - b, np.float32(1e-6))
    low, rise, fall, high = _SKIN_EDGES
    weight = _smoothstep(low, rise, t) * (1.0 - _smoothstep(fall, high, t))
    return np.where((r >= g) & (g >= b), weight, np.float32(0.0))


def _adjust_rows(encoded, vibrance, saturation):
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


def adjust(encoded, vibrance, saturation):
    """`encoded` (H x W x 3 float32, sRGB-encoded in [0, 1]) with vibrance and
    saturation (-100 to 100) applied, in place. Colours stay in range, give or take
    float rounding, which the caller's rounding to integers absorbs."""
    if not vibrance and not saturation:
        return
    for start in range(0, len(encoded), _ROWS):
        _adjust_rows(encoded[start : start + _ROWS], vibrance, saturation)
