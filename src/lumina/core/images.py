"""Pixels of a photo at a given size, for thumbnails and the viewer.

Runs in worker processes, so what it returns is plain data that pickles: rows of
8-bit RGB. Originals are opened for reading only.
"""

from dataclasses import dataclass

from PIL import Image, ImageOps

from lumina.core import raw
from lumina.core.errors import PhotoError

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"})
PHOTO_EXTENSIONS = IMAGE_EXTENSIONS | raw.RAW_EXTENSIONS


@dataclass(frozen=True)
class Pixels:
    width: int
    height: int
    # RGB, three bytes a pixel, rows without padding
    data: bytes
    # (width, height) of the photo read in full, upright, when known: the viewer's 100 %
    full: tuple | None = None


def is_photo(path):
    return str(path).lower().endswith(tuple(PHOTO_EXTENSIONS))


def load_pixels(path, max_side):
    """The photo at `path`, upright, no larger than `max_side` on its longer side, or in
    full with `max_side` None. For a RAW file, its embedded preview. Raises PhotoError
    naming the file."""
    if raw.is_raw(path):
        image, full = raw.load_preview_sized(path, max_side)
    else:
        image, full = _load_image(path, max_side)
    if max_side and max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS, reducing_gap=2.0)
    return Pixels(image.width, image.height, image.tobytes(), tuple(full))


def _load_image(path, max_side):
    try:
        with Image.open(path) as image:
            orientation = image.getexif().get(raw.EXIF_ORIENTATION, 1)
            full = raw.upright_size(image.size, orientation)
            if max_side:
                # For a JPEG: decode at a reduced scale, which is most of the time saved
                image.draft("RGB", (max_side, max_side))
            return ImageOps.exif_transpose(image).convert("RGB"), full
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise PhotoError(path, f"cannot read image ({error})") from error
