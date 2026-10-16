"""Lens corrections: distortion, chromatic aberration and vignetting from a lens profile.

A `LensProfile` is the numbers lensfun measured for a lens at the focal length and
aperture of a photo (see lensdb.py, which finds them), kept in the edit so that the edit
renders the same whatever lensfun's data says later. The formulas are lensfun's:

- Radii are measured from the middle of the frame in units of half the shorter side,
  divided by the profile's `scale` (the crop factor of the sensor the lens was measured
  on over this camera's: a lens measured on a larger sensor is seen through a smaller
  part of its image circle here). Vignetting uses half the diagonal instead.
- Distortion maps a point of the corrected photo at radius Ru to the point of the photo as
  shot at Rd = Ru * k(Ru); k is a polynomial (ptlens, poly3 or poly5, see `_distortion`).
- Chromatic aberration: the red and blue channels are sampled at Ru * (b Ru^2 + c Ru + v)
  first, then as above; green is the reference.
- Vignetting: the photo as shot is 1 + k1 r^2 + k2 r^4 + k3 r^6 as bright as it should be
  at radius r; dividing by that corrects it.

Distortion and chromatic aberration are one resampling (OpenCV `remap`, bilinear), which
the straightening of the geometry step joins, sampling only the pixels the crop needs.
Vignetting multiplies the samples. The result is zoomed in just enough that no empty
border shows. Amounts (0 to 1) scale each correction: 0 leaves it out, 1 is the profile's.

The light that vignetting brings up past the white of the photo as shot is kept: a 16-bit
result is divided by `headroom` (the gain at the corners) so that it does not clip, and the
pipeline puts the factor back with the exposure.
"""

import math
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

# Rows resampled at a time: bounds the float maps
ROWS = 256
# The darkest vignetting is taken to be (a profile gone wrong could divide by nothing)
_MIN_DARKNESS = 0.2
# Most the corrected photo is zoomed in to hide its border
_MAX_ZOOM = 3.0
_EDGE_POINTS = 64
_DISTORTION_TERMS = {"ptlens": 3, "poly3": 1, "poly5": 2}


@dataclass(frozen=True)
class LensProfile:
    name: str
    # Crop factor of the sensor the lens was measured on over this camera's
    scale: float = 1.0
    # ("ptlens", a, b, c), ("poly3", k1) or ("poly5", k1, k2); or None
    distortion: tuple | None = None
    # (vr, vb, cr, cb, br, bb); or None
    tca: tuple | None = None
    # (k1, k2, k3); or None
    vignetting: tuple | None = None

    def to_dict(self):
        return {
            "name": self.name,
            "scale": float(self.scale),
            "distortion": None
            if self.distortion is None
            else {"model": self.distortion[0], "terms": [float(t) for t in self.distortion[1:]]},
            "tca": None if self.tca is None else [float(t) for t in self.tca],
            "vignetting": None if self.vignetting is None else [float(t) for t in self.vignetting],
        }

    @staticmethod
    def from_dict(data):
        """A profile from stored JSON, or None when it is not one (a damaged edit renders
        without it)."""
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            return None
        scale = _finite(data.get("scale"))
        if scale is None or not 0.2 <= scale <= 5.0:
            return None
        distortion = None
        stored = data.get("distortion")
        if isinstance(stored, dict) and stored.get("model") in _DISTORTION_TERMS:
            terms = _numbers(stored.get("terms"), _DISTORTION_TERMS[stored["model"]])
            distortion = (stored["model"], *terms) if terms is not None else None
        return LensProfile(
            name=data["name"],
            scale=scale,
            distortion=distortion,
            tca=_numbers(data.get("tca"), 6),
            vignetting=_numbers(data.get("vignetting"), 3),
        )


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _numbers(values, count):
    if not isinstance(values, list | tuple) or len(values) != count:
        return None
    numbers = tuple(_finite(v) for v in values)
    return None if None in numbers else numbers


@dataclass(frozen=True)
class Correction:
    """A profile and how much of each correction to make, 0 to 1."""

    profile: LensProfile
    distortion: float = 1.0
    vignetting: float = 1.0
    aberration: float = 1.0

    def has_distortion(self):
        return self.profile.distortion is not None and self.distortion > 0

    def has_aberration(self):
        return self.profile.tca is not None and self.aberration > 0

    def has_vignetting(self):
        return self.profile.vignetting is not None and self.vignetting > 0

    def is_identity(self):
        return not (self.has_distortion() or self.has_aberration() or self.has_vignetting())


# Distortion and aberration: where the corrected photo is sampled


def _distortion(profile, ru):
    """k = Rd / Ru for the corrected radius `ru` (in the profile's units)."""
    model, *t = profile.distortion
    if model == "ptlens":
        a, b, c = t
        return ((a * ru + b) * ru + c) * ru + (1.0 - a - b - c)
    if model == "poly3":
        return 1.0 - t[0] + t[0] * ru * ru
    ru2 = ru * ru
    return 1.0 + t[0] * ru2 + t[1] * ru2 * ru2


def _source(corr, width, height, zoom, x, y, channel):
    """Where in the photo as shot the pixels at (`x`, `y`) of the corrected photo (arrays of
    pixel positions) come from, for `channel` (0 red, 1 green, 2 blue); and that, zoomed in
    by `zoom`."""
    cx, cy = (width - 1) / 2, (height - 1) / 2
    norm = np.float32(min(width, height) / 2 / corr.profile.scale)
    dx = (x - np.float32(cx)) * np.float32(1 / zoom)
    dy = (y - np.float32(cy)) * np.float32(1 / zoom)
    if channel != 1 and corr.has_aberration():
        vr, vb, cr, cb, br, bb = corr.profile.tca
        v, c, b = (vr, cr, br) if channel == 0 else (vb, cb, bb)
        ru = np.hypot(dx, dy) / norm
        k = (np.float32(b) * ru + np.float32(c)) * ru + np.float32(v)
        k = np.clip(k, 0.5, 2.0)
        k = np.float32(1.0) + np.float32(corr.aberration) * (k - np.float32(1.0))
        dx, dy = dx * k, dy * k
    if corr.has_distortion():
        ru = np.hypot(dx, dy) / norm
        k = np.clip(_distortion(corr.profile, ru), 0.3, 3.0)
        k = np.float32(1.0) + np.float32(corr.distortion) * (k - np.float32(1.0))
        dx, dy = dx * k, dy * k
    return dx + np.float32(cx), dy + np.float32(cy)


@lru_cache(maxsize=64)
def autoscale(corr, width, height):
    """How much the corrected photo must be zoomed in (1 or more) so that its edges are
    sampled from inside the photo as shot: no empty border."""
    if not corr.has_distortion():
        return 1.0
    t = np.linspace(0.0, 1.0, _EDGE_POINTS)
    xs, ys = t * (width - 1), t * (height - 1)
    edge_x = np.concatenate([xs, xs, np.zeros_like(ys), np.full_like(ys, width - 1)])
    edge_y = np.concatenate([np.zeros_like(xs), np.full_like(xs, height - 1), ys, ys])
    edge_x, edge_y = edge_x.astype(np.float32), edge_y.astype(np.float32)

    def fits(zoom):
        sx, sy = _source(corr, width, height, zoom, edge_x, edge_y, 1)
        return (
            sx.min() >= -0.5
            and sy.min() >= -0.5
            and sx.max() <= width - 0.5
            and sy.max() <= height - 0.5
        )

    if fits(1.0):
        return 1.0
    low, high = 1.0, _MAX_ZOOM
    for _ in range(24):
        middle = (low + high) / 2
        low, high = (low, middle) if fits(middle) else (middle, high)
    return float(high)


# Vignetting


def _gain(corr, r):
    """The factor the photo as shot is multiplied by at `r`, the radius as a share of half
    the diagonal (after the profile's scale)."""
    k1, k2, k3 = corr.profile.vignetting
    r2 = r * r
    darkness = np.maximum(1.0 + r2 * (k1 + r2 * (k2 + r2 * k3)), _MIN_DARKNESS)
    return 1.0 + corr.vignetting * (1.0 / darkness - 1.0)


def headroom(corr, width, height):
    """How much brighter than white the corners can come out of the vignetting correction
    (1 for none): the factor a 16-bit result is divided by to keep it all."""
    if corr is None or not corr.has_vignetting():
        return 1.0
    r = np.linspace(0.0, corr.profile.scale, 513)
    peak = float(np.max(_gain(corr, r)))
    return peak if math.isfinite(peak) and peak > 1.0 else 1.0


# Resampling

# The maps are smooth, so they are worked out at every STEP-th pixel and interpolated
# between (bilinearly: well under a hundredth of a pixel off for any real lens), which
# is a tenth of the work of working out every pixel's
STEP = 16


class _Axis:
    """The positions 0 to n - 1 along one side of a block, the ones every STEP pixels
    (and the last) that a map is worked out at (`coarse`), and for every position the
    two of those it lies between and how far (`low`, `share`)."""

    def __init__(self, n):
        self.coarse = np.unique(np.append(np.arange(0, n, STEP), n - 1)).astype(np.float32)
        if len(self.coarse) == 1:
            self.coarse = np.zeros(2, dtype=np.float32)
        position = np.arange(n, dtype=np.float32)
        self.low = np.clip(
            np.searchsorted(self.coarse, position, "right") - 1, 0, len(self.coarse) - 2
        )
        span = self.coarse[self.low + 1] - self.coarse[self.low]
        self.share = np.divide(
            position - self.coarse[self.low], span, out=np.zeros_like(position), where=span > 0
        ).astype(np.float32)


def _spread(values, rows, columns):
    """`values` (worked out at `rows.coarse` x `columns.coarse`) at every position."""
    across = (
        values[:, columns.low] * (1 - columns.share) + values[:, columns.low + 1] * columns.share
    )
    return (
        across[rows.low] * (1 - rows.share)[:, None] + across[rows.low + 1] * rows.share[:, None]
    ).astype(np.float32)


def resample(image, corr, box, turn=None):
    """The `box` (top, bottom, left, right) of `image` (H x W x 3: uint16, or floats)
    with the corrections made, as uint16 or float32 (16 bits keep their meaning, divided
    by `headroom`). `turn` is (degrees clockwise, scale) to turn the corrected photo
    about its centre, as the geometry step's straightening does, or a 3 x 3 matrix
    (perspective.source_matrix) taking the places of the result to the corrected photo's."""
    height, width = image.shape[:2]
    top, bottom, left, right = box
    zoom = autoscale(corr, width, height)
    peak = headroom(corr, width, height)
    if image.dtype not in (np.uint16, np.float32):
        image = image.astype(np.float32)
    source = np.ascontiguousarray(image)
    separate = corr.has_aberration()
    planes = [np.ascontiguousarray(source[..., c]) for c in range(3)] if separate else None
    out = np.empty((bottom - top, right - left, 3), dtype=source.dtype)
    columns = _Axis(right - left)
    xs = columns.coarse + np.float32(left)
    cx, cy = np.float32((width - 1) / 2), np.float32((height - 1) / 2)
    matrix = turn if isinstance(turn, np.ndarray) else None
    if turn is not None and matrix is None:
        angle = math.radians(turn[0])
        cos, sin = np.float32(math.cos(angle) / turn[1]), np.float32(math.sin(angle) / turn[1])
    for start in range(top, bottom, ROWS):
        stop = min(start + ROWS, bottom)
        rows = _Axis(stop - start)
        ys = rows.coarse + np.float32(start)
        shape = (len(ys), len(xs))
        x, y = np.broadcast_to(xs[None, :], shape), np.broadcast_to(ys[:, None], shape)
        if matrix is not None:
            x, y = _projected(matrix, xs, ys)
        elif turn is not None:
            # The turn undone (y points down, so this turns the photo clockwise)
            x, y = (
                cos * (xs[None, :] - cx) + sin * (ys[:, None] - cy) + cx,
                -sin * (xs[None, :] - cx) + cos * (ys[:, None] - cy) + cy,
            )
        gx, gy = _source(corr, width, height, zoom, x, y, 1)
        if separate:
            channels = []
            for c in range(3):
                mx, my = (gx, gy) if c == 1 else _source(corr, width, height, zoom, x, y, c)
                channels.append(
                    _remap(planes[c], _spread(mx, rows, columns), _spread(my, rows, columns))
                )
            block = np.stack(channels, axis=-1)
        else:
            block = _remap(source, _spread(gx, rows, columns), _spread(gy, rows, columns))
        if corr.has_vignetting():
            gain = _spread(_gain(corr, _radius(corr, width, height, gx, gy)) / peak, rows, columns)
            block = _vignetted(block, gain)
        out[start - top : stop - top] = block
    return out


def _projected(matrix, xs, ys):
    """Where the places (`xs`, `ys`: a row and a column of positions) of a block go by the
    3 x 3 `matrix`, as float32 grids."""
    gx, gy = np.meshgrid(xs.astype(np.float64), ys.astype(np.float64))
    w = matrix[2, 0] * gx + matrix[2, 1] * gy + matrix[2, 2]
    x = (matrix[0, 0] * gx + matrix[0, 1] * gy + matrix[0, 2]) / w
    y = (matrix[1, 0] * gx + matrix[1, 1] * gy + matrix[1, 2]) / w
    return x.astype(np.float32), y.astype(np.float32)


def _remap(plane, mx, my):
    return cv2.remap(plane, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _radius(corr, width, height, gx, gy):
    """Radius, as a share of half the diagonal (after the profile's scale), of the places
    (`gx`, `gy`) in the photo as shot."""
    half_diagonal = math.hypot(width, height) / 2
    return np.hypot(gx - (width - 1) / 2, gy - (height - 1) / 2) * np.float32(
        corr.profile.scale / half_diagonal
    )


def _vignetted(block, gain):
    """`block` multiplied by `gain` (per pixel)."""
    result = block.astype(np.float32)
    result *= gain[..., None]
    if block.dtype == np.uint16:
        result += np.float32(0.5)
        np.clip(result, 0.0, 65535.0, out=result)
        return result.astype(np.uint16)
    return result
