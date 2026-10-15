"""Colour grading: a tint and a brightness change for each of the shadows, the
midtones and the highlights, as three wheels.

Which range a pixel is in follows its (sRGB-encoded) luma: three weights that add up
to one, the shadows below the first edge, the highlights above the second and the
midtones between. Balance moves both edges down when positive, so more of the photo
counts as highlights, and up when negative; blending is how wide the transition
between ranges is, from nearly a step to the ranges overlapping.

A wheel's tint is a colour with no luma, in the direction of its hue, as strong as its
saturation says, added in proportion to the pixel's weight for that range. A pixel's
luma therefore does not change with the tint, and the tint is cut to what the pixel
can take, so a pixel near black or white takes less of it rather than clipping.
A wheel's luminance lightens (towards white) or darkens (towards black) its range by
up to half.
"""

import numpy as np

from lumina.core.edit import colour, params

# How long the tint of a wheel at 100 is, in encoded units (the distance from grey)
MAX_TINT = 0.25
# How far a wheel's luminance at 100 moves its range towards white or black
MAX_LIGHTEN = 0.5
# The edges of the shadows and the highlights, either side of the middle, with no balance
_SPREAD = 0.2
# How far balance moves them at its ends
_BALANCE = 0.25
# Half the width of the transition between ranges at blending 0 and the added width at 100
_TRANSITION = (0.04, 0.30)


def _hue_colour(hue):
    # The fully saturated colour of `hue` (degrees), as HSV gives it
    sector = (hue / 60.0) % 6.0
    return np.array(
        [
            np.clip(abs(sector - 3) - 1, 0, 1),
            np.clip(2 - abs(sector - 2), 0, 1),
            np.clip(2 - abs(sector - 4), 0, 1),
        ]
    )


def tint(wheel):
    """The colour a wheel adds at full weight: a vector with no luma, as a float32 row."""
    if wheel.saturation == 0:
        return np.zeros(3, dtype=np.float32)
    rgb = _hue_colour(wheel.hue)
    away = rgb - float(colour.LUMA @ rgb)
    away /= np.linalg.norm(away)
    return (away * MAX_TINT * wheel.saturation / 100.0).astype(np.float32)


def prepare(grading):
    """What `adjust_rows` needs of `grading` (params.Grading): per range its tint and
    its luminance amount, then balance and blending as the edges and transition."""
    ranges = [getattr(grading, name) for name in params.RANGES]
    pivot = 0.5 - _BALANCE * grading.balance / 100.0
    transition = _TRANSITION[0] + _TRANSITION[1] * grading.blending / 100.0
    return (
        [tint(wheel) for wheel in ranges],
        [wheel.luminance / 100.0 * MAX_LIGHTEN for wheel in ranges],
        (pivot - _SPREAD, pivot + _SPREAD, transition),
    )


def weights(luma, edges):
    """(shadows, midtones, highlights) weights of each pixel, adding up to one."""
    low, high, transition = edges
    shadows = 1.0 - colour.smoothstep(low - transition, low + transition, luma)
    highlights = colour.smoothstep(high - transition, high + transition, luma)
    midtones = np.maximum(1.0 - shadows - highlights, 0.0)
    total = shadows + midtones + highlights
    return shadows / total, midtones / total, highlights / total


def adjust_rows(encoded, prepared):
    """`encoded` (rows x W x 3 float32, sRGB-encoded) graded, in place."""
    tints, lightens, edges = prepared
    luma = encoded @ colour.LUMA
    ranged = weights(luma, edges)
    if any(t.any() for t in tints):
        away = sum(w[..., None] * t for w, t in zip(ranged, tints, strict=True) if t.any())
        # As much of the tint as the pixel can take
        eps = np.float32(1e-6)
        room = np.float32(1.0)
        for c in range(3):
            push, there = away[..., c], encoded[..., c]
            room = np.minimum(
                room,
                np.where(
                    push > eps,
                    (1.0 - there) / np.maximum(push, eps),
                    np.where(push < -eps, there / np.maximum(-push, eps), 2.0),
                ),
            )
        encoded += away * room[..., None]
    if any(lightens):
        amount = sum(w * a for w, a in zip(ranged, lightens, strict=True) if a)
        # Towards white above zero, towards black below
        encoded *= (1.0 - np.abs(amount))[..., None]
        encoded += np.maximum(amount, 0.0)[..., None]
