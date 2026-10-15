"""Local contrast: texture and clarity, and the steps that need a look at the pixels
around each one (see also dehaze.py).

Both work on the luminance of the sRGB-encoded photo. A Gaussian blur of it says what
the neighbourhood is like; the difference between a pixel and that is its detail at the
blur's size. Clarity adds more of that detail (or less, below zero, which softens) at a
larger size, texture at a smaller one. The change is added to all three channels, so
a pixel's colour stays what it was while its brightness moves.

Sizes are in shares of the photo's long side, so a small render is the same as a large
one: a texture of 0.12 % is 7 px on 6000 and 2 px on 1700. Detail is limited as it is
added (a soft limiter, not a cut), so a hard edge does not grow a bright halo of its
own, and clarity leaves the shadows and highlights more alone than the midtones.

A blur of a large sigma is made on a smaller copy (a power of two smaller, so that its
sigma there is a few pixels) and scaled up again: a hundredth of the work, and the same
to the eye. A tile must then start at a multiple of that factor, to be cut the same way as
the whole photo.

A step that looks at neighbours needs rows beyond a tile: `layout` says how many, and
how a tile must be aligned, and the tile's caller supplies them (see pipeline.render).
"""

import math

import cv2
import numpy as np

from lumina.core.edit import colour

# Gaussian sigma, in shares of the photo's long side
CLARITY_SIGMA = 0.015
TEXTURE_SIGMA = 0.0012
# Never under this many pixels: below it a blur does nothing
_MIN_SIGMA = 0.7
# How much detail a control at 100 adds, as a multiple of the detail
_STRENGTH = 1.0
# The most detail a pixel can gain or lose at 100, as the soft limiter's ceiling
_CEILING = {"clarity": 0.07, "texture": 0.10}
# The least a blur's sigma is on the smaller copy it is made on, in pixels
_SMALLEST_SIGMA = 3.0
# How much of clarity the shadows and highlights keep: 1 at middle grey, this at ends
_CLARITY_EDGES = 0.3


def prepare(presence, size):
    """What `halo` and `apply_rows` need for `presence` (params.Presence) on a photo
    `size` (height, width) as rendered: (sigma, amount, ceiling) for clarity and texture,
    None for one that is at zero."""
    long_side = max(size)
    steps = []
    for name, share in (("clarity", CLARITY_SIGMA), ("texture", TEXTURE_SIGMA)):
        amount = getattr(presence, name) / 100.0 * _STRENGTH
        sigma = max(_MIN_SIGMA, share * long_side)
        steps.append((name, sigma, amount, _CEILING[name], _factor(sigma)) if amount else None)
    return tuple(steps)


def _factor(sigma):
    """How many times smaller the copy a blur of `sigma` is made on: a power of two that
    leaves it at least _SMALLEST_SIGMA there, 1 for a small sigma."""
    factor = 1
    while sigma / (factor * 2) >= _SMALLEST_SIGMA:
        factor *= 2
    return factor


def layout(prepared):
    """(halo, align) for the steps: how many rows beyond a tile they need, and the
    multiple of rows a tile and its halo must start at. The halo is what the largest
    blur reaches (OpenCV's kernel is 4 sigma either side), a pixel more, to a multiple
    of the align."""
    steps = [step for step in prepared if step]
    align = max((step[4] for step in steps), default=1)
    reach = max((math.ceil(4.0 * step[1]) for step in steps), default=0) + 1
    return -(-reach // align) * align, align


def _blur(plane, sigma, factor):
    if factor == 1:
        return cv2.GaussianBlur(plane, (0, 0), sigma, borderType=cv2.BORDER_REFLECT_101)
    height, width = plane.shape
    small = cv2.resize(
        plane,
        (max(1, round(width / factor)), max(1, round(height / factor))),
        interpolation=cv2.INTER_AREA,
    )
    small = cv2.GaussianBlur(small, (0, 0), sigma / factor, borderType=cv2.BORDER_REFLECT_101)
    return cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)


def apply_rows(x, prepared):
    """`x` (rows x W x 3 float32, sRGB-encoded) with clarity and texture applied, in
    place. The rows must include the halo's worth beyond the ones that are kept."""
    luma = x @ colour.LUMA
    change = np.zeros_like(luma)
    for step in prepared:
        if step is None:
            continue
        name, sigma, amount, ceiling, factor = step
        detail = luma - _blur(luma, sigma, factor)
        # A soft limiter: nearly the detail itself when small, never past the ceiling
        detail /= 1.0 + np.abs(detail) / ceiling
        if name == "clarity":
            detail *= 1.0 - (1.0 - _CLARITY_EDGES) * (2.0 * luma - 1.0) ** 2
        change += amount * detail
    x += change[..., None]
    np.clip(x, 0.0, 1.0, out=x)
