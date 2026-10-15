"""Sharpening: an unsharp mask on the luminance, with a limit on halos and a mask for
edges.

The detail at a pixel is the difference between its luminance and a Gaussian blur of it,
`radius` pixels wide. Adding `amount` times that to the pixel, to all three channels so
that its colour stays, makes edges steeper: the unsharp mask.

- Detail sets how much the detail is held down where it is strong, with a soft limiter:
  at 0 an edge gets only a thin line of extra contrast, at 100 the limiter is out of the
  way and fine detail is brought out as much as the amount asks, halos and all.
- Masking keeps the sharpening off what is not an edge: it is weighed by how steep the
  luminance is around the pixel, from nothing below a threshold to all of it above one.
  Noise and flat areas are steepest in the grain, not at the edges, so at 100 only the
  strongest edges are sharpened.

The radius is in pixels of the full-size photo. A render made smaller (the screen, a
thumbnail) uses the radius it would have at its size, and the steepness of an edge is
taken at full size too, so what a mask lets through does not depend on the render's
size. Below a third of a pixel a blur does nothing, and nothing is done.
"""

import math

import cv2
import numpy as np

from lumina.core.edit import colour

# Where the detail limiter's ceiling is at detail 0, and how far detail 100 raises it
_CEILING = (0.02, 0.30)
# The steepness threshold at masking 100, in luminance per full-size pixel; below it
# (halved) nothing is sharpened, above it (and a half more) all
_MASK_STEEPNESS = 0.10
_MASK_FLOOR = 0.002
# Smaller radii than this, in pixels of the render, do nothing
_MIN_SIGMA = 0.33


def prepare(sharpening, scale):
    """What `reach` and `apply_rows` need for `sharpening` (params.Sharpening) at
    `scale` (the render's size as a share of full size), or None for nothing."""
    sigma = sharpening.radius * scale
    if sharpening.amount == 0 or sigma < _MIN_SIGMA:
        return None
    low, high = _CEILING
    ceiling = low + (high - low) * sharpening.detail / 100.0
    threshold = _MASK_FLOOR + _MASK_STEEPNESS * sharpening.masking / 100.0
    return (
        sharpening.amount / 100.0,
        sigma,
        ceiling,
        threshold if sharpening.masking else None,
        scale,
    )


def reach(prepared):
    """How many rows beyond a tile it needs: its blur's, and the edge finder's."""
    _amount, sigma, _ceiling, _threshold, _scale = prepared
    return math.ceil(4.0 * sigma) + 3


def apply_rows(x, prepared):
    """`x` (rows x W x 3 float32, sRGB-encoded) sharpened, in place."""
    amount, sigma, ceiling, threshold, scale = prepared
    luma = x @ colour.LUMA
    detail = luma - cv2.GaussianBlur(luma, (0, 0), sigma, borderType=cv2.BORDER_REFLECT_101)
    detail /= 1.0 + np.abs(detail) / ceiling
    if threshold is not None:
        detail *= _edges(luma, scale, threshold)
    x += (amount * detail)[..., None]
    np.clip(x, 0.0, 1.0, out=x)


def _edges(luma, scale, threshold):
    """How much of an edge each pixel is, 0 to 1: the steepness of the luminance around
    it, at full size, between half the threshold and one and a half."""
    return colour.smoothstep(0.5 * threshold, 1.5 * threshold, _steepness(luma, scale))


def _steepness(luma, scale):
    """How steep the luminance is around each pixel, per pixel of the full-size photo."""
    smooth = cv2.GaussianBlur(luma, (0, 0), 0.8, borderType=cv2.BORDER_REFLECT_101)
    across = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3, borderType=cv2.BORDER_REFLECT_101)
    down = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3, borderType=cv2.BORDER_REFLECT_101)
    # Sobel's weights add up to 8 for a ramp of one per pixel; per full-size pixel
    return np.hypot(across, down) * np.float32(scale / 8.0)
