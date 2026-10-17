"""Live view of a tethered camera (#59), without Qt: what a frame's exposure looks like
(its histogram and how much of it is clipped), and where a preset's crop falls on the
camera's frame.

The frames are the camera's own JPEGs (about 960×640 on the author's 90D), decoded
here in the session's process (core/tether.py) at a quarter of their pixels, which is
plenty for a histogram.
"""

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

# A frame's histogram is read from this many pixels on its longer side, at most
_LOOK_SIDE = 480


@dataclass(frozen=True)
class Look:
    """A frame's exposure: a histogram of 256 counts for red, green, blue and their
    luminance, and the share of the pixels at the ends (0 and 255) of each colour."""

    red: tuple
    green: tuple
    blue: tuple
    luminance: tuple
    # Shares (0 to 1) of the pixels at 255, and at 0, by colour: (red, green, blue)
    highlights: tuple
    shadows: tuple

    @property
    def clipped_highlights(self):
        return max(self.highlights)

    @property
    def clipped_shadows(self):
        return max(self.shadows)


def examine(jpeg):
    """The Look of the JPEG `jpeg` (bytes). Raises ValueError when it can't be read."""
    try:
        image = Image.open(io.BytesIO(jpeg))
        # Decoded at a half or a quarter of its size by the JPEG decoder itself: fast
        image.draft("RGB", (_LOOK_SIDE, _LOOK_SIDE))
        image = image.convert("RGB")
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError(f"The live view frame can't be read: {error}") from error
    counts = image.histogram()
    red, green, blue = (tuple(counts[i * 256 : (i + 1) * 256]) for i in range(3))
    luminance = tuple(image.convert("L").histogram())
    total = max(1, image.width * image.height)
    return Look(
        red,
        green,
        blue,
        luminance,
        tuple(channel[255] / total for channel in (red, green, blue)),
        tuple(channel[0] / total for channel in (red, green, blue)),
    )


def decode(jpeg):
    """The pixels of the JPEG `jpeg`: a (height, width, 3) uint8 array. Raises ValueError
    when it can't be read."""
    try:
        image = Image.open(io.BytesIO(jpeg))
        return np.asarray(image.convert("RGB"))
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError(f"The live view frame can't be read: {error}") from error


# The clipping warning's colours, over the frame: red where a colour is at its brightest,
# blue where all three are at their darkest
_HIGHLIGHT = (255, 0, 0, 255)
_SHADOW = (0, 96, 255, 255)


def clipping(pixels):
    """The clipping warning for the frame `pixels` (as decode gives): a (height, width,
    4) RGBA array, clear but where it is clipped."""
    height, width = pixels.shape[:2]
    out = np.zeros((height, width, 4), np.uint8)
    out[(pixels >= 255).any(axis=2)] = _HIGHLIGHT
    out[(pixels <= 0).all(axis=2)] = _SHADOW
    return out


def crop_guide(crop, rotate=0, flip_horizontal=False, flip_vertical=False):
    """Where an edit's crop (left, top, right, bottom as shares of the photo after its
    quarter turns `rotate` clockwise and mirroring) falls on the camera's frame, as
    shares of the frame: (left, top, right, bottom). None for no crop."""
    left, top, right, bottom = crop
    if (left, top, right, bottom) == (0, 0, 1, 1):
        return None
    # Mirroring undone, then the turns: a point (u, v) of a photo turned a quarter
    # clockwise was (v, 1 - u) before
    if flip_vertical:
        top, bottom = 1 - bottom, 1 - top
    if flip_horizontal:
        left, right = 1 - right, 1 - left
    for _ in range(rotate % 4):
        left, top, right, bottom = top, 1 - right, bottom, 1 - left
    return (left, top, right, bottom)


def magnified(point, zoom):
    """The part of the frame a camera magnified `zoom` times around `point` (x, y as
    shares of the whole frame) shows, as shares of the frame: (left, top, right, bottom),
    kept inside it."""
    if zoom <= 1:
        return (0.0, 0.0, 1.0, 1.0)
    size = 1 / zoom
    x = min(max(point[0], size / 2), 1 - size / 2)
    y = min(max(point[1], size / 2), 1 - size / 2)
    return (x - size / 2, y - size / 2, x + size / 2, y + size / 2)
