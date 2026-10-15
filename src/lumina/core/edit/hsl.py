"""The HSL panel: hue, saturation and luminance for eight bands of colour.

A pixel's hue (on its sRGB-encoded values) says how much each band's controls apply:
the eight bands sit at fixed hues round the circle and the controls are blended
between neighbouring centres with a smooth step, so every hue is covered by the two
bands it lies between and a band reaches no further than its neighbours' centres. A
colour no adjusted band reaches is left as it is. How much of that applies also
follows the pixel's chroma: it fades in from nothing at grey, where hue means nothing
and noise would turn into colour.

- Hue turns the colour about the grey axis, with luma kept, by up to MAX_SHIFT degrees.
- Saturation scales the colour's distance from grey by 1 + amount.
- Luminance blends towards white (positive) or black (negative) by up to half.

The colour is never pushed out of range: if turning or boosting would take a channel
past the edge, the colour's distance from grey is cut to fit, which keeps its hue and
luma. A tables lookup per half degree of hue stands for the controls, so the work per
pixel is the hue, the lookups and a few multiplications.
"""

from functools import lru_cache

import numpy as np

from lumina.core.edit import colour, params

# Where the bands sit on the hue circle, in degrees, in the order of params.BANDS
CENTRES = (0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 270.0, 300.0)
# How far a hue control at 100 turns a colour, in degrees
MAX_SHIFT = 30.0
# How far a luminance control at 100 moves a colour towards white or black
MAX_LIGHTEN = 0.5
# Table entries per degree of hue. The tables start at hue -60 (red's neighbour on the
# magenta side) and have one entry more than a turn and a sixth, see `hue_index`
_PER_DEGREE = 2
_SIZE = 360 * _PER_DEGREE
# A colour whose chroma (brightest channel less darkest) is below the first is not
# touched; above the second, fully
_CHROMA_FADE = (0.0, 0.06)


def _blend(values):
    """The table of `values` (one per band) over the hue circle: each stands at its
    centre and they are smoothly stepped between."""
    centres = np.array((*CENTRES, 360.0))
    values = np.array((*values, values[0]), dtype=np.float64)
    hue = (np.arange(_SIZE + 1) / _PER_DEGREE - 60.0) % 360.0
    upper = np.clip(np.searchsorted(centres, hue, side="right"), 1, len(centres) - 1)
    low, high = centres[upper - 1], centres[upper]
    t = colour.smoothstep(0.0, 1.0, (hue - low) / (high - low))
    return values[upper - 1] * (1 - t) + values[upper] * t


@lru_cache(maxsize=8)
def tables(hsl):
    """What the bands of `hsl` (params.Hsl) make of each hue: the angle to turn by, in
    radians, the saturation amount and the luminance amount, as float32 tables with
    one entry per half degree, or None for a table that is all zero."""
    bands = [getattr(hsl, name) for name in params.BANDS]
    out = []
    for field, scale in (
        ("hue", np.radians(MAX_SHIFT) / 100.0),
        ("saturation", 1 / 100.0),
        ("luminance", MAX_LIGHTEN / 100.0),
    ):
        values = [getattr(band, field) for band in bands]
        out.append(_blend(values).astype(np.float32) * np.float32(scale) if any(values) else None)
    return tuple(out)


def hue_index(r, g, b, high, low):
    """The table entry of each pixel's hue: its sector, in sixths of the circle from
    -1 (the magenta side of red) to 5, scaled to entries. The sector is read off
    the order of the channels, as HSV does, but without wrapping negative ones round,
    which the tables start early for."""
    spread = np.maximum(high - low, np.float32(1e-6))
    numerator = np.where(
        high == r, g - b, np.where(high == g, (b - r) + 2.0 * spread, (r - g) + 4.0 * spread)
    )
    numerator /= spread
    numerator += 1.0
    numerator *= np.float32(_SIZE / 6)
    return numerator.astype(np.int32)


def adjust_rows(encoded, tables_):
    """`encoded` (rows x W x 3 float32, sRGB-encoded) with the HSL bands applied, in
    place. `tables_` is `tables(hsl)`."""
    turn, saturate, lighten = tables_
    # One plane per channel, each contiguous: far faster to work on than the interleaved
    r, g, b = np.ascontiguousarray(np.moveaxis(encoded, -1, 0))
    high = np.maximum(np.maximum(r, g), b)
    low = np.minimum(np.minimum(r, g), b)
    index = hue_index(r, g, b, high, low)
    fade_low, fade_high = _CHROMA_FADE
    reach = colour.smoothstep(fade_low, fade_high, high - low)
    luma = encoded @ colour.LUMA
    away = encoded - luma[..., None]
    if turn is not None:
        angle = turn[index] * reach
        away = np.cos(angle)[..., None] * away + np.sin(angle)[..., None] * (away @ colour.QUARTER)
    factor = 1.0 + saturate[index] * reach if saturate is not None else np.float32(1.0)
    if turn is not None or saturate is not None:
        # Whatever the colour is turned or scaled to, it stays in range
        factor = np.minimum(factor, colour.room(away, luma))
    away *= factor[..., None] if np.ndim(factor) else factor
    away += luma[..., None]
    if lighten is not None:
        # Towards white above zero, towards black below
        amount = lighten[index] * reach
        away *= (1.0 - np.abs(amount))[..., None]
        away += np.maximum(amount, 0.0)[..., None]
    encoded[:] = away
