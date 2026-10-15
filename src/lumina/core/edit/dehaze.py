"""Dehaze: less haze (above zero) or more (below).

Haze lays a veil of the colour of the air over the photo, more of it where the scene is
farther away. Its mark is that, in a patch of a clear photo, one channel of some pixel
is near black (the "dark channel"); where the haze is thick it is not. That gives the
air's colour (from the brightest of the haze) and, for each place, how much of the scene
gets through (its transmission). The scene is then recovered as (pixel - air) / how
much gets through + air.

The air's colour and the transmission are worked out once for the whole photo, on a
small copy of it (the haze varies slowly, so little is lost), and the transmission is
refined with a guided filter so it follows the photo's edges roughly. Each tile then
only reads its part of that map, scaled up: no rows beyond the tile are needed, and the
result does not depend on how the photo is cut into tiles.

Below zero the photo is mixed towards the air's colour, the same everywhere: there is no
scene depth to follow.
"""

from dataclasses import dataclass

import cv2
import numpy as np

# The long side of the copy the haze is measured on
ANALYSIS_SIDE = 512
# Size of the patch the dark channel is the minimum over, and of the guided filter, on that copy
_PATCH = 7
_GUIDE_RADIUS = 24
_GUIDE_EPS = 1e-3
# The share of the brightest dark-channel pixels the air's colour is read from
_AIR_SHARE = 0.001
# How much of the haze a control at 100 takes out, and the least that gets through
_REMOVAL = 0.9
_FLOOR = 0.15
# How far a control at -100 mixes the photo towards the air
_ADDED = 0.45


@dataclass(frozen=True)
class Analysis:
    # The air's colour, and for dehazing the transmission on the small copy (None when
    # adding haze), the full size (height, width) it stands for
    light: np.ndarray
    transmission: np.ndarray | None
    size: tuple


def _guided_filter(guide, source, radius, eps):
    box = (2 * radius + 1, 2 * radius + 1)

    def mean(a):
        return cv2.boxFilter(a, -1, box, borderType=cv2.BORDER_REFLECT)

    mean_guide, mean_source = mean(guide), mean(source)
    covariance = mean(guide * source) - mean_guide * mean_source
    variance = mean(guide * guide) - mean_guide * mean_guide
    slope = covariance / (variance + eps)
    offset = mean_source - slope * mean_guide
    return mean(slope) * guide + mean(offset)


def analyse(stage, amount):
    """The air's colour and, for an `amount` above zero, the transmission, of a photo
    `stage` (H x W x 3 uint16, sRGB-encoded)."""
    height, width = stage.shape[:2]
    factor = max(1.0, max(height, width) / ANALYSIS_SIDE)
    small_size = (max(1, round(width / factor)), max(1, round(height / factor)))
    small = cv2.resize(stage, small_size, interpolation=cv2.INTER_AREA).astype(np.float32)
    small *= np.float32(1.0 / 65535.0)
    kernel = np.ones((_PATCH, _PATCH), dtype=np.uint8)
    dark = cv2.erode(small.min(axis=2), kernel)
    count = max(1, int(_AIR_SHARE * dark.size))
    brightest = np.argpartition(dark.ravel(), -count)[-count:]
    # The air is the haziest colour there is: not darker than a dull grey
    light = np.clip(small.reshape(-1, 3)[brightest].mean(axis=0), 0.4, 1.0)
    if amount <= 0:
        return Analysis(light.astype(np.float32), None, (height, width))
    unhazed = small / light
    rough = 1.0 - cv2.erode(unhazed.min(axis=2), kernel)
    guide = (small @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)).astype(np.float32)
    radius = min(_GUIDE_RADIUS, max(1, min(small.shape[:2]) // 4))
    refined = np.clip(_guided_filter(guide, rough.astype(np.float32), radius, _GUIDE_EPS), 0, 1)
    # The share of the haze a control at this amount takes out
    transmission = 1.0 - _REMOVAL * amount / 100.0 * (1.0 - refined)
    return Analysis(
        light.astype(np.float32),
        np.maximum(transmission, _FLOOR).astype(np.float32),
        (height, width),
    )


def _transmission_rows(analysis, first_row, rows):
    """The transmission for `rows` rows of the photo from `first_row`, at full width."""
    height, width = analysis.size
    small = analysis.transmission
    sx, sy = small.shape[1] / width, small.shape[0] / height
    # Output pixel (x, y) reads the small map at ((x + 0.5) * sx - 0.5, ...)
    matrix = np.array(
        [[sx, 0.0, 0.5 * sx - 0.5], [0.0, sy, sy * first_row + 0.5 * sy - 0.5]],
        dtype=np.float64,
    )
    return cv2.warpAffine(
        small,
        matrix,
        (width, rows),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )


def apply_rows(x, first_row, analysis, amount):
    """`x` (rows x W x 3 float32, sRGB-encoded) from row `first_row` with the haze taken
    out (`amount` above zero) or put in (below), in place. `analysis` is from `analyse`."""
    if amount > 0:
        transmission = _transmission_rows(analysis, first_row, len(x))
        x -= analysis.light
        x /= transmission[..., None]
        x += analysis.light
    else:
        mix = np.float32(-amount / 100.0 * _ADDED)
        x *= 1.0 - mix
        x += analysis.light * mix
    np.clip(x, 0.0, 1.0, out=x)
