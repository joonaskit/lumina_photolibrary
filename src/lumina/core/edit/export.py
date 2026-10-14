"""Export: an edited photo rendered at full size (or smaller) to JPEG, PNG or TIFF.

The rules it keeps:
- The original is never written to: a destination that is the original is refused,
  even with Replace.
- An existing file is never overwritten unless the caller says Replace. This is
  checked when planning and again when writing: the image is written to a
  temporary file beside the destination, then put in place by creating the
  destination exclusively, so a file that appeared in the meantime is not
  clobbered. That needs no hard links, so it works on exFAT and FAT drives too.
- A cancel or a failure leaves no partial file behind.

Every file has an sRGB ICC profile. 16 bits are for PNG and TIFF; Pillow can't write
16-bit colour, so those two writers are here.
"""

import os
import struct
import tempfile
import zlib
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageCms

from lumina.core.edit import pipeline, source
from lumina.core.errors import PhotoError

FORMATS = {"jpeg": (8,), "png": (8, 16), "tiff": (8, 16)}
EXTENSIONS = {"jpeg": ".jpg", "png": ".png", "tiff": ".tif"}
_ROWS_PER_STRIP = 64


@dataclass(frozen=True)
class ExportOptions:
    format: str = "jpeg"
    bits: int = 8
    quality: int = 92
    # Longest side in pixels, or None for full size
    max_side: int | None = None

    def check(self):
        if self.format not in FORMATS:
            raise ValueError(f"unknown format {self.format!r}")
        if self.bits not in FORMATS[self.format]:
            raise ValueError(f"{self.format} can't hold {self.bits} bits")


class ExportExists(PhotoError):
    """The destination exists and Replace was not chosen."""


class ExportCancelled(Exception):
    pass


def suggested_name(source_path, options):
    stem = os.path.splitext(os.path.basename(source_path))[0]
    return stem + EXTENSIONS[options.format]


def export(
    source_path,
    params,
    destination,
    options=None,
    replace=False,
    should_stop=lambda: False,
    linear=None,
):
    """Render `source_path` with `params` and write it to `destination`. `linear` is the
    decoded source if the caller has it (the editor does). Returns the destination.

    Raises ExportExists, ExportCancelled, or PhotoError naming the file that failed."""
    options = options or ExportOptions()
    options.check()
    destination = os.path.abspath(destination)
    _refuse_original(source_path, destination)
    if os.path.lexists(destination) and not replace:
        raise ExportExists(destination, "already exists")

    def check_stop():
        if should_stop():
            raise ExportCancelled()

    check_stop()
    if linear is None:
        linear = source.load_linear(source_path)
    check_stop()
    if options.max_side:
        linear = source.fitted(linear, params, options.max_side)
    check_stop()
    pixels = pipeline.render(linear, params, bits=options.bits)
    check_stop()
    _write(pixels, destination, options, replace, source_path, check_stop)
    return destination


def _refuse_original(source_path, destination):
    try:
        same = os.path.samefile(source_path, destination)
    except OSError:
        same = os.path.realpath(source_path) == os.path.realpath(destination)
    if same:
        raise PhotoError(destination, "is the original photo, which is never written to")


def _write(pixels, destination, options, replace, source_path, check_stop):
    folder = os.path.dirname(destination)
    try:
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=".lumina-export-", suffix=".part")
    except OSError as error:
        raise PhotoError(destination, f"cannot write here ({error.strerror})") from error
    placed = False
    try:
        with os.fdopen(handle, "wb") as file:
            _WRITERS[(options.format, options.bits)](file, pixels, options)
            file.flush()
            os.fsync(file.fileno())
        check_stop()
        _place(temporary, destination, replace, source_path)
        placed = True
    except OSError as error:
        raise PhotoError(destination, f"cannot write ({error.strerror or error})") from error
    finally:
        if not placed:
            _remove(temporary)


def _place(temporary, destination, replace, source_path):
    if replace:
        # Checked again now: the original may have been put there since
        _refuse_original(source_path, destination)
        os.replace(temporary, destination)
        return
    # Claim the name: this fails if anything has it, however it got there
    try:
        claim = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as error:
        raise ExportExists(destination, "already exists") from error
    os.close(claim)
    try:
        # Over the empty file just made, which is ours
        os.replace(temporary, destination)
    except OSError:
        _remove(destination)
        raise


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


@lru_cache(maxsize=1)
def srgb_profile():
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


# Writers


def _jpeg(file, pixels, options):
    Image.fromarray(pixels, "RGB").save(
        file,
        "JPEG",
        quality=options.quality,
        # Full colour resolution at high quality, as photo editors do
        subsampling=0 if options.quality >= 90 else 2,
        optimize=True,
        icc_profile=srgb_profile(),
    )


def _png8(file, pixels, options):
    Image.fromarray(pixels, "RGB").save(file, "PNG", icc_profile=srgb_profile())


def _tiff8(file, pixels, options):
    Image.fromarray(pixels, "RGB").save(
        file, "TIFF", compression="tiff_deflate", icc_profile=srgb_profile()
    )


def _png16(file, pixels, _options):
    height, width = pixels.shape[:2]

    def chunk(kind, data):
        file.write(struct.pack(">I", len(data)) + kind + data)
        file.write(struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    file.write(b"\x89PNG\r\n\x1a\n")
    chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 16, 2, 0, 0, 0))
    chunk(b"iCCP", b"sRGB\x00\x00" + zlib.compress(srgb_profile()))
    # Streamed: rows in blocks, each row its filter byte (0, none) then big-endian samples
    compressor = zlib.compressobj(6)
    for start in range(0, height, _ROWS_PER_STRIP):
        samples = pixels[start : start + _ROWS_PER_STRIP].astype(">u2")
        rows = samples.reshape(len(samples), width * 3).view(np.uint8)
        block = np.hstack([np.zeros((len(rows), 1), np.uint8), rows])
        data = compressor.compress(block.tobytes())
        if data:
            chunk(b"IDAT", data)
    chunk(b"IDAT", compressor.flush())
    chunk(b"IEND", b"")


def _tiff16(file, pixels, _options):
    """Uncompressed, little-endian, chunky RGB, strips of _ROWS_PER_STRIP rows."""
    height, width = pixels.shape[:2]
    profile = srgb_profile()
    strips = list(range(0, height, _ROWS_PER_STRIP))
    counts = [min(_ROWS_PER_STRIP, height - s) * width * 6 for s in strips]
    entries = 11
    ifd_size = 2 + entries * 12 + 4
    # Layout: header, IFD, its out-of-line values, then the strips
    extra_start = 8 + ifd_size
    bits_at = extra_start
    offsets_at = bits_at + 6
    counts_at = offsets_at + 4 * len(strips)
    profile_at = counts_at + 4 * len(strips)
    data_at = profile_at + len(profile) + (len(profile) % 2)
    offsets = []
    position = data_at
    for count in counts:
        offsets.append(position)
        position += count

    def entry(tag, kind, count, value):
        return struct.pack("<HHII", tag, kind, count, value)

    many = len(strips) > 1
    ifd = b"".join(
        [
            struct.pack("<H", entries),
            entry(256, 4, 1, width),
            entry(257, 4, 1, height),
            entry(258, 3, 3, bits_at),
            entry(259, 3, 1, 1),
            entry(262, 3, 1, 2),
            entry(273, 4, len(strips), offsets_at if many else offsets[0]),
            entry(277, 3, 1, 3),
            entry(278, 4, 1, _ROWS_PER_STRIP),
            entry(279, 4, len(strips), counts_at if many else counts[0]),
            entry(284, 3, 1, 1),
            entry(34675, 7, len(profile), profile_at),
            struct.pack("<I", 0),
        ]
    )
    file.write(b"II*\x00" + struct.pack("<I", 8) + ifd)
    file.write(struct.pack("<3H", 16, 16, 16))
    file.write(struct.pack(f"<{len(strips)}I", *offsets))
    file.write(struct.pack(f"<{len(strips)}I", *counts))
    file.write(profile + b"\x00" * (len(profile) % 2))
    for start in strips:
        file.write(pixels[start : start + _ROWS_PER_STRIP].astype("<u2").tobytes())


_WRITERS = {
    ("jpeg", 8): _jpeg,
    ("png", 8): _png8,
    ("png", 16): _png16,
    ("tiff", 8): _tiff8,
    ("tiff", 16): _tiff16,
}
