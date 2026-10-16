"""The original, decoded for editing: linear light, 16 bits, upright.

A RAW file is decoded once in full (rawpy: camera white balance, linear output in the
camera's own colours, no automatic brightening), which takes a second or two; the editor
keeps it while the photo is open and renders from it. With it comes its `CameraColour`,
which the edit's profile turns into linear sRGB (colour_profile.py): LibRaw's matrix gives
what LibRaw's sRGB decode gave, to the last bit of rounding. Other files are decoded from
sRGB to linear, with no `CameraColour`. While a control moves, renders use a proxy of about
2 MP, made once from the full decode.

Memory: a 32 MP photo is about 190 MB as 16 bits, 380 MB as floats. Only the 16-bit
image and the float proxy (about 24 MB) are kept; full renders work in tiles.
"""

import numpy as np
import rawpy
from PIL import Image

from lumina.core import images, linear_tiff, raw
from lumina.core.edit.colour_profile import CameraColour, libraw_rgb_cam
from lumina.core.edit.pipeline import srgb_decode
from lumina.core.errors import PhotoError

PROXY_PIXELS = 2_000_000
# Raw values this far under the black level (a share of the white level) are not noise
# or an exposure: they are garbage. A healthy file has none, a damaged one 1 to 38 %.
DAMAGE_MARGIN = 1 / 40
DAMAGE_SHARE = 0.001


def load_linear(path, half=False):
    """The photo at `path` as H x W x 3 linear light, upright: uint16, or float16 for a
    linear TIFF (a merge's), whose light past white is above 1. With `half`, a RAW is
    decoded at half size, four times faster, and a linear TIFF read at half size: for
    thumbnails and the viewer. Raises PhotoError naming it."""
    if raw.is_raw(path):
        return raw_linear(path, half)
    if linear_tiff.is_linear(path):
        return linear_tiff.read(path, step=2 if half else 1)
    return _image_linear(path)


def raw_linear(path, half=False, white_balance=None):
    """A RAW file decoded to uint16 linear light, upright: with the camera's white
    balance, or `white_balance` (multipliers, as `camera_white_balance` gives them)."""
    try:
        with rawpy.imread(str(path)) as file:
            if _damaged(file):
                raise PhotoError(
                    path,
                    "the raw data is damaged. The camera's JPEG preview is fine; "
                    "copy the file from the card again",
                )
            balance = {"use_camera_wb": True}
            if white_balance is not None:
                balance = {"use_camera_wb": False, "user_wb": list(white_balance)}
            # postprocess() turns the image by the file's flip itself
            return file.postprocess(
                gamma=(1, 1),
                no_auto_bright=True,
                output_bps=16,
                output_color=rawpy.ColorSpace.sRGB,
                half_size=half,
                **balance,
            )
    except (rawpy.LibRawError, OSError, ValueError) as error:
        raise PhotoError(path, f"cannot decode RAW ({error})") from error


def load_for_edit(path, half=False):
    """(the photo at `path` as `load_linear` has it, its `CameraColour`): for a RAW file
    the decode is in the camera's colours, for other files linear sRGB and None. Raises
    PhotoError naming it."""
    if raw.is_raw(path):
        return raw_camera(path, half)
    return load_linear(path, half), None


def raw_camera(path, half=False):
    """A RAW file decoded to uint16 linear light in the camera's colours, white-balanced as
    shot, upright, and its `CameraColour`."""
    try:
        with rawpy.imread(str(path)) as file:
            if _damaged(file):
                raise PhotoError(
                    path,
                    "the raw data is damaged. The camera's JPEG preview is fine; "
                    "copy the file from the card again",
                )
            colour = _camera_colour(file)
            image = file.postprocess(
                gamma=(1, 1),
                no_auto_bright=True,
                output_bps=16,
                output_color=rawpy.ColorSpace.raw,
                half_size=half,
                use_camera_wb=True,
            )
            return image, colour
    except (rawpy.LibRawError, OSError, ValueError) as error:
        raise PhotoError(path, f"cannot decode RAW ({error})") from error


def _camera_colour(file):
    """What LibRaw knows of an open file's colours (see CameraColour): Adobe's matrix for
    the camera if it has one (as for CR3), else the file's own (a DNG's)."""
    cam_xyz = np.array(file.rgb_xyz_matrix, dtype=np.float64)[:3]
    if np.abs(cam_xyz).sum() > 0:
        rgb_cam = libraw_rgb_cam(cam_xyz)
    else:
        cam_xyz = None
        own = np.array(file.color_matrix, dtype=np.float64)[:, :3]
        rgb_cam = own if np.abs(own).sum() > 0 else np.eye(3)
    multipliers = np.array(file.camera_whitebalance[:3], dtype=np.float64)
    if (multipliers <= 0).any():
        multipliers = np.array(file.daylight_whitebalance[:3], dtype=np.float64)
    if (multipliers <= 0).any():
        multipliers = np.ones(3)
    # The camera's colours of white: what the multipliers make equal
    neutral = (1.0 / multipliers) / (1.0 / multipliers).max()
    return CameraColour(
        tuple(map(tuple, rgb_cam)),
        None if cam_xyz is None else tuple(map(tuple, cam_xyz)),
        tuple(neutral),
    )


def camera_white_balance(path):
    """The white balance the camera chose for the RAW file at `path`, as four
    multipliers, or None when the file has none. Raises PhotoError naming it."""
    try:
        with rawpy.imread(str(path)) as file:
            multipliers = [float(m) for m in file.camera_whitebalance]
    except (rawpy.LibRawError, OSError, ValueError) as error:
        raise PhotoError(path, f"cannot decode RAW ({error})") from error
    if not any(multipliers[:3]):
        return None
    if multipliers[3] == 0:
        # Three colours: the second green is the first one's
        multipliers[3] = multipliers[1]
    return multipliers


def _damaged(file):
    """Whether the raw values of `file` (an open rawpy file) are damaged: some are far
    below the black level, which no exposure produces. Reads, never repairs."""
    values = file.raw_image_visible
    floor = min(file.black_level_per_channel) - DAMAGE_MARGIN * file.white_level
    return np.count_nonzero(values < floor) > DAMAGE_SHARE * values.size


def _image_linear(path):
    try:
        with Image.open(path) as image:
            rgb = np.asarray(images.upright_srgb(image))
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise PhotoError(path, f"cannot read image ({error})") from error
    # 256 entries: decoding through a table is exact and fast
    table = np.round(srgb_decode(np.arange(256) / 255.0) * 65535).astype(np.uint16)
    return table[rgb]


def resize(linear, width, height, quality=True):
    """`linear` (uint16, or floats) at `width` x `height` as float32, resampled in linear
    light, which is what keeps fine detail's brightness right: in [0, 1] from uint16;
    floats keep their light past white (a merge's). `quality` uses Lanczos (for export);
    without it an area average (fast, for the proxy)."""
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
    # Lanczos rings below black at a hard edge
    return np.maximum(result, 0.0, out=result)


def fit(width, height, pixels=None, side=None):
    """`width` x `height` made smaller to at most `pixels` or a longest `side`, keeping
    its shape. Never larger."""
    factor = 1.0
    if pixels and width * height > pixels:
        factor = min(factor, (pixels / (width * height)) ** 0.5)
    if side and max(width, height) > side:
        factor = min(factor, side / max(width, height))
    return max(1, round(width * factor)), max(1, round(height * factor))


def fitted(linear, params, max_side):
    """`linear` made smaller so that what `params` renders of it is at most `max_side`
    on its longest side, after the turns and the crop. Unchanged if it is already."""
    from lumina.core.edit import pipeline

    height, width = linear.shape[:2]
    longest = max(pipeline.output_size(width, height, params))
    if longest <= max_side:
        return linear
    factor = max_side / longest
    size = (max(1, round(width * factor)), max(1, round(height * factor)))
    return resize(linear, *size, quality=True)


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
        self._camera = None

    @property
    def full(self):
        if self._full is None:
            self._full, self._camera = load_for_edit(self.path)
        return self._full

    @property
    def camera(self):
        """The RAW file's `CameraColour`, or None for a photo in sRGB."""
        _decoded = self.full
        return self._camera

    @property
    def proxy(self):
        if self._proxy is None:
            self._proxy = make_proxy(self.full)
        return self._proxy

    def make_ready(self):
        """Decode and make the proxy now."""
        return self.proxy is not None

    def size(self):
        height, width = self.full.shape[:2]
        return width, height
