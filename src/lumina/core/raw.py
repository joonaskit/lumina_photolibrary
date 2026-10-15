"""RAW files through rawpy (LibRaw). Opened for reading only.

For browsing, the camera's embedded preview is used instead of a full decode: a CR3
has a full-size JPEG in it (`PRVW`), which LibRaw picks over the small `THMB` one.
"""

import io

import rawpy
from PIL import Image, ImageOps

from lumina.core.errors import PhotoError

RAW_EXTENSIONS = frozenset(
    {".cr3", ".cr2", ".crw", ".nef", ".nrw", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef"}
)

EXIF_ORIENTATION = 0x0112

# LibRaw's `sizes.flip` as a turn of the image: 3 is half a turn, 5 a quarter turn
# counter-clockwise and 6 a quarter turn clockwise. Pillow turns counter-clockwise.
_FLIP_TURNS = {
    3: Image.Transpose.ROTATE_180,
    5: Image.Transpose.ROTATE_90,
    6: Image.Transpose.ROTATE_270,
}


def is_raw(path):
    return str(path).lower().endswith(tuple(RAW_EXTENSIONS))


def apply_flip(image, flip):
    """`image` turned upright by LibRaw's `flip` value."""
    turn = _FLIP_TURNS.get(flip)
    return image.transpose(turn) if turn is not None else image


def load_preview(path, max_side=None):
    """The embedded preview of a RAW file as an upright RGB `PIL.Image`.

    The preview is JPEG on most cameras and a bitmap on some; a file with neither is
    decoded at half size. With `max_side`, a JPEG preview is decoded at the smallest
    scale that is still at least that large, which is much faster. Raises PhotoError."""
    return load_preview_sized(path, max_side)[0]


def load_preview_sized(path, max_side=None):
    """`load_preview`, and the (width, height) of the preview upright at full size:
    what the viewer shows at 100 %."""
    try:
        with rawpy.imread(str(path)) as raw:
            flip = raw.sizes.flip
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                thumb = None
            if thumb is None:
                # postprocess() turns the image by the file's flip itself
                image = Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True))
                return image, image.size
            if thumb.format == rawpy.ThumbFormat.BITMAP:
                image = apply_flip(Image.fromarray(thumb.data).convert("RGB"), flip)
                return image, image.size
            return _jpeg_preview(thumb.data, flip, max_side)
    except (rawpy.LibRawError, OSError, ValueError) as error:
        raise PhotoError(path, f"cannot read RAW preview ({error})") from error


def _jpeg_preview(data, flip, max_side):
    image = Image.open(io.BytesIO(data))
    # Before the draft, which makes it smaller
    full = image.size
    if max_side:
        image.draft("RGB", (max_side, max_side))
    # Most cameras leave the orientation out of the preview and LibRaw has it; when the
    # preview has one of its own, that one is used, never both
    orientation = image.getexif().get(EXIF_ORIENTATION, 1)
    if orientation != 1:
        return ImageOps.exif_transpose(image).convert("RGB"), upright_size(full, orientation)
    turned = flip in (5, 6)
    return apply_flip(image.convert("RGB"), flip), full[::-1] if turned else full


def upright_size(size, orientation):
    """(width, height) of an image of `size` turned by its EXIF `orientation`."""
    return tuple(size[::-1]) if orientation in (5, 6, 7, 8) else tuple(size)
