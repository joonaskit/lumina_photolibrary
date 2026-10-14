"""The original, decoded for editing: linear light, 16 bits, upright.

A RAW file is decoded once in full (rawpy: camera white balance, linear output in
sRGB primaries, no automatic brightening), which takes a second or two; the
editor keeps it while the photo is open and renders from it. Other files are
decoded from sRGB to linear. While a control moves, renders use a proxy of about
2 MP, made once from the full decode.

Memory: a 32 MP photo is about 190 MB as 16 bits, 380 MB as floats. Only the 16-bit
image and the float proxy (about 24 MB) are kept; full renders work in tiles.
"""

import numpy as np
import rawpy
from PIL import Image, ImageOps

from lumina.core import raw
from lumina.core.edit.pipeline import srgb_decode
from lumina.core.errors import PhotoError

PROXY_PIXELS = 2_000_000


def load_linear(path):
    """The photo at `path` as H x W x 3 uint16 linear light, upright. Raises PhotoError
    naming it."""
    if raw.is_raw(path):
        return _raw_linear(path)
    return _image_linear(path)


def _raw_linear(path):
    try:
        with rawpy.imread(str(path)) as file:
            # postprocess() turns the image by the file's flip itself
            return file.postprocess(
                gamma=(1, 1),
                no_auto_bright=True,
                output_bps=16,
                use_camera_wb=True,
                output_color=rawpy.ColorSpace.sRGB,
            )
    except (rawpy.LibRawError, OSError, ValueError) as error:
        raise PhotoError(path, f"cannot decode RAW ({error})") from error


def _image_linear(path):
    try:
        with Image.open(path) as image:
            rgb = np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise PhotoError(path, f"cannot read image ({error})") from error
    # 256 entries: decoding through a table is exact and fast
    table = np.round(srgb_decode(np.arange(256) / 255.0) * 65535).astype(np.uint16)
    return table[rgb]


def resize(linear, width, height, quality=True):
    """`linear` (uint16 or float32) at `width` x `height` as float32 in [0, 1], resampled
    in linear light, which is what keeps fine detail's brightness right. `quality`
    uses Lanczos (for export); without it an area average (fast, for the proxy)."""
    scale = 65535.0 if linear.dtype == np.uint16 else 1.0
    method = Image.Resampling.LANCZOS if quality else Image.Resampling.BOX
    channels = []
    for c in range(3):
        plane = Image.fromarray(np.ascontiguousarray(linear[..., c], dtype=np.float32), "F")
        channels.append(np.asarray(plane.resize((width, height), method), dtype=np.float32))
    result = np.stack(channels, axis=-1)
    if scale != 1.0:
        result /= np.float32(scale)
    return np.clip(result, 0.0, 1.0, out=result)


def fit(width, height, pixels=None, side=None):
    """`width` x `height` made smaller to at most `pixels` or a longest `side`, keeping
    its shape. Never larger."""
    factor = 1.0
    if pixels and width * height > pixels:
        factor = min(factor, (pixels / (width * height)) ** 0.5)
    if side and max(width, height) > side:
        factor = min(factor, side / max(width, height))
    return max(1, round(width * factor)), max(1, round(height * factor))


def make_proxy(linear, pixels=PROXY_PIXELS):
    """A float32 copy of about `pixels`, for renders while a control moves."""
    height, width = linear.shape[:2]
    target = fit(width, height, pixels=pixels)
    if target == (width, height):
        scale = 65535.0 if linear.dtype == np.uint16 else 1.0
        return linear.astype(np.float32) / np.float32(scale)
    return resize(linear, *target, quality=False)


class EditSource:
    """One photo opened for editing: decoded once, with its proxy."""

    def __init__(self, path):
        self.path = path
        self._full = None
        self._proxy = None

    @property
    def full(self):
        if self._full is None:
            self._full = load_linear(self.path)
        return self._full

    @property
    def proxy(self):
        if self._proxy is None:
            self._proxy = make_proxy(self.full)
        return self._proxy

    def size(self):
        height, width = self.full.shape[:2]
        return width, height
