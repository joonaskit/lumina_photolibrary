"""Pixels of a photo at a given size, for thumbnails and the viewer.

Runs in worker processes, so what it returns is plain data that pickles: rows of
8-bit RGB. Originals are opened for reading only.
"""

import io
from dataclasses import dataclass

from PIL import Image, ImageCms, ImageOps

from lumina.core import raw
from lumina.core.errors import PhotoError

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
try:
    import pillow_heif
except ImportError:
    # Without it HEIC files are not photos here: they would be listed and not shown
    HEIC = False
else:
    # In every process that opens a photo: this module is imported in each
    pillow_heif.register_heif_opener()
    IMAGE_EXTENSIONS |= {".heic", ".heif"}
    HEIC = True
IMAGE_EXTENSIONS = frozenset(IMAGE_EXTENSIONS)
PHOTO_EXTENSIONS = IMAGE_EXTENSIONS | raw.RAW_EXTENSIONS

_SRGB = ImageCms.createProfile("sRGB")


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


def to_srgb(image, icc_profile):
    """`image` (RGB) in sRGB, from the ICC profile it was made in: a phone's Display P3, a
    camera's Adobe RGB. Without a profile, or with sRGB's, it is as it was: sRGB is
    what a photo without one is taken to be. A profile that cannot be used leaves it as
    it was too."""
    if not icc_profile:
        return image
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
        if "srgb" in ImageCms.getProfileDescription(source).lower():
            return image
        return ImageCms.profileToProfile(
            image,
            source,
            _SRGB,
            renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
            outputMode="RGB",
        )
    except (ImageCms.PyCMSError, OSError, ValueError):
        return image


def upright_srgb(image):
    """An opened image as an upright RGB image in sRGB: turned by its EXIF orientation
    (a HEIC's is applied by its opener already), then converted from its profile."""
    profile = image.info.get("icc_profile")
    return to_srgb(ImageOps.exif_transpose(image).convert("RGB"), profile)


def _draft_box(image, max_side):
    """What to ask `draft` for, which is most of the time saved at a small size. A JPEG
    is decoded at a reduced scale covering the square. A HEIC is read from a thumbnail
    inside it (an iPhone's is 416 px, in 20 ms where the photo takes 0.5 to 1 s) when
    one covers the photo at `max_side`, as stored; not the square, which a 416 x 312
    thumbnail does not cover. Only one that is upright and in the photo's profile like
    the photo is taken."""
    if image.format != "HEIF":
        return max_side, max_side
    width, height = image.size
    scale = max_side / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _load_image(path, max_side):
    try:
        with Image.open(path) as image:
            orientation = image.getexif().get(raw.EXIF_ORIENTATION, 1)
            full = raw.upright_size(image.size, orientation)
            if max_side:
                image.draft("RGB", _draft_box(image, max_side))
            return upright_srgb(image), full
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise PhotoError(path, f"cannot read image ({error})") from error
