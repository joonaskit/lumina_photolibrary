"""Noise reduction: of the brightness noise and of the colour noise, apart.

The photo is split into its luminance and two colour differences (red and blue less the
luminance); each is made smoother where it is only noise and left alone where it is an
edge, and put together again.

- Brightness noise goes through a bilateral filter on the luminance: a pixel's new value
  is the average of those near it that are about as bright, so noise within a flat area
  is averaged away and an edge, with a jump across it, is not blurred. How much counts as
  "about as bright" grows with the amount. Detail then puts back a share of what was taken
  out, so fine texture that the filter flattened comes back, with some of the noise.
- Colour noise is blotchy, so it is smoothed over a wide area, which a copy a quarter
  the size does cheaply (as image codecs treat colour): the colour differences are made
  smaller, put through a bilateral filter there, scaled up again, and a pixel takes the
  result only where it is near its own value, so a colour edge, which the scaling up
  softens, is not bled across.

Sizes are in pixels of the full-size photo, scaled to the render: noise is a matter of the
grain at full size and a render a quarter of it has averaged most of it away, so below a
third of full size nothing is done. A tile must start at a multiple of the copy's factor
(`align`), to be cut as the whole photo is.
"""

import math

import cv2
import numpy as np

from lumina.core.edit import colour

# Below this share of the full size a render has no noise left to take out
_MIN_SCALE = 0.3
# The bilateral filter's range, in encoded luminance: at amount 0 and added at 100
_RANGE = (0.02, 0.10)
# Its spatial sigma, in full-size pixels
_SPACE = 2.0
# How much of what luminance noise reduction took out detail 100 puts back
_DETAIL_BACK = 0.6
# The copy colour noise is reduced on is this many times smaller; its bilateral filter's
# spatial sigma there at amount 100, in pixels of the full-size photo, and its range
# (encoded units) at amount 0 and added at 100
_COLOUR_FACTOR = 4
_COLOUR_SIGMA = 12.0
_COLOUR_RANGE = (0.04, 0.12)
# A pixel takes the smoothed colour where it is within this of it; beyond, less and less
_COLOUR_GUARD = 0.08


def prepare(noise, scale):
    """What `reach` and `apply_rows` need for `noise` (params.NoiseReduction) at `scale`
    (the render's size as a share of full size), or None for nothing."""
    if (noise.luminance == 0 and noise.colour == 0) or scale < _MIN_SCALE:
        return None
    brightness = None
    if noise.luminance:
        amount = noise.luminance / 100.0
        low, high = _RANGE
        sigma = max(0.7, _SPACE * scale)
        brightness = (amount, low + (high - low) * amount, sigma, noise.detail / 100.0)
    chroma = None
    if noise.colour:
        amount = noise.colour / 100.0
        low, high = _COLOUR_RANGE
        # Spatial sigma on the small copy, at least a pixel
        sigma = max(1.0, _COLOUR_SIGMA * amount * scale / _COLOUR_FACTOR)
        chroma = (amount, sigma, low + (high - low) * amount)
    return brightness, chroma


def align(prepared):
    """The multiple of rows a tile must start at: the copy colour noise is reduced on is
    cut at it."""
    return _COLOUR_FACTOR if prepared[1] is not None else 1


def reach(prepared):
    """How many rows beyond a tile it needs."""
    brightness, chroma = prepared
    rows = 0
    if brightness is not None:
        rows = max(rows, 2 * math.ceil(1.5 * brightness[2]) + 1)
    if chroma is not None:
        # A filter of this many pixels on the small copy, and one more of it for the scaling
        rows = max(rows, (2 * math.ceil(1.5 * chroma[1]) + 2) * _COLOUR_FACTOR)
    return rows


def apply_rows(x, prepared):
    """`x` (rows x W x 3 float32, sRGB-encoded) with the noise reduced, in place."""
    brightness, chroma = prepared
    luma = x @ colour.LUMA
    change = np.zeros_like(luma)
    if brightness is not None:
        amount, colour_range, sigma, detail = brightness
        size = 2 * math.ceil(1.5 * sigma) + 1
        smooth = cv2.bilateralFilter(luma, size, colour_range, sigma)
        # Some of what was taken out comes back, as detail asks
        smooth += (luma - smooth) * np.float32(detail * _DETAIL_BACK)
        change = (smooth - luma) * np.float32(amount)
    if chroma is not None:
        red, blue = _smoothed_colour(x[..., 0] - luma, x[..., 2] - luma, chroma)
        # The channels again, with the luminance the pixel had: green from the weights
        w_r, w_g, w_b = colour.LUMA
        green = -(w_r * red + w_b * blue) / w_g
        x[..., 0] = luma + red
        x[..., 1] = luma + green
        x[..., 2] = luma + blue
    if brightness is not None:
        x += change[..., None]
    np.clip(x, 0.0, 1.0, out=x)


def _smoothed_colour(red, blue, chroma):
    """The colour differences `red` and `blue` (rows x W) with their noise reduced."""
    amount, sigma, colour_range = chroma
    height, width = red.shape
    pair = np.dstack([red, blue, np.zeros_like(red)])
    small = cv2.resize(
        pair,
        (max(1, round(width / _COLOUR_FACTOR)), max(1, round(height / _COLOUR_FACTOR))),
        interpolation=cv2.INTER_AREA,
    )
    size = 2 * math.ceil(1.5 * sigma) + 1
    small = cv2.bilateralFilter(small, size, colour_range, sigma)
    smooth = cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)
    result = []
    for index, plane in enumerate((red, blue)):
        change = smooth[..., index] - plane
        # 1 / (1 + (change / guard) ** 4), by squaring twice (a power is far slower)
        weight = change * np.float32(1.0 / _COLOUR_GUARD)
        weight *= weight
        weight *= weight
        weight += 1.0
        change *= np.float32(amount)
        change /= weight
        result.append(plane + change)
    return result
