"""Tiny DNG files made for the tests, so RAW handling is tested without real camera files.

LibRaw reads DNG through the same paths as CR3: a JPEG or bitmap preview from
`extract_thumb()`, the half-size decode where there is none, and the flip from the
file's orientation. Real CR3 files are tested too when there are some in
tests/samples/ (see test_samples.py).
"""

import io
import struct

import numpy as np
from PIL import Image

_BYTE, _ASCII, _SHORT, _LONG, _RATIONAL, _SRATIONAL = 1, 2, 3, 4, 5, 10
_FORMATS = {_BYTE: "B", _SHORT: "H", _LONG: "I"}

# The raw image, as stored: an RGGB mosaic, 12 bits
RAW_SIZE = (128, 96)
# Colours of the preview's two halves, left and right as stored
LEFT, RIGHT = (220, 30, 30), (30, 30, 220)


class _Tiff:
    """A little-endian TIFF, written as it goes: data first, then the IFD pointing at it."""

    def __init__(self):
        self.buf = bytearray(b"II*\x00\x00\x00\x00\x00")

    def blob(self, data):
        if len(self.buf) % 2:
            self.buf += b"\x00"
        offset = len(self.buf)
        self.buf += data
        return offset

    def ifd(self, entries):
        packed = []
        for tag, kind, values in sorted(entries):
            if kind == _ASCII:
                raw, count = values.encode() + b"\x00", len(values) + 1
            elif kind in (_RATIONAL, _SRATIONAL):
                code = "I" if kind == _RATIONAL else "i"
                raw = b"".join(struct.pack(f"<{code}{code}", n, d) for n, d in values)
                count = len(values)
            else:
                raw, count = struct.pack(f"<{len(values)}{_FORMATS[kind]}", *values), len(values)
            if len(raw) <= 4:
                inline = raw.ljust(4, b"\x00")
            else:
                inline = struct.pack("<I", self.blob(raw))
            packed.append(struct.pack("<HHI", tag, kind, count) + inline)
        return self.blob(struct.pack("<H", len(packed)) + b"".join(packed) + b"\x00" * 4)

    def finish(self, first_ifd):
        self.buf[4:8] = struct.pack("<I", first_ifd)
        return bytes(self.buf)


def _camera_tags(orientation, model="Test Camera"):
    identity = [(1, 1), (0, 1), (0, 1), (0, 1), (1, 1), (0, 1), (0, 1), (0, 1), (1, 1)]
    return [
        (271, _ASCII, "Lumina"),
        (272, _ASCII, model),
        (274, _SHORT, [orientation]),
        (50706, _BYTE, [1, 4, 0, 0]),
        (50707, _BYTE, [1, 1, 0, 0]),
        (50708, _ASCII, f"Lumina {model}"),
        (50721, _SRATIONAL, identity),
        (50728, _RATIONAL, [(1, 1), (1, 1), (1, 1)]),
        (50778, _SHORT, [21]),
    ]


def _raw_entries(tiff, subfile_type, black, level, mosaic=None, model="Test Camera"):
    if mosaic is None:
        width, height = RAW_SIZE
        # A flat grey mosaic is enough: the tests look at sizes and the preview's colours
        data = struct.pack(f"<{width * height}H", *([level] * (width * height)))
    else:
        height, width = mosaic.shape
        data = mosaic.astype("<u2").tobytes()
    return [
        (254, _LONG, [subfile_type]),
        (256, _LONG, [width]),
        (257, _LONG, [height]),
        (258, _SHORT, [16]),
        (259, _SHORT, [1]),
        (262, _SHORT, [32803]),
        (273, _LONG, [tiff.blob(data)]),
        (277, _SHORT, [1]),
        (278, _LONG, [height]),
        (279, _LONG, [len(data)]),
        (284, _SHORT, [1]),
        (33421, _SHORT, [2, 2]),
        (33422, _BYTE, [0, 1, 1, 2]),
        (50714, _LONG, [black]),
        (50717, _LONG, [4095]),
    ]


def preview_image(size):
    """The preview as stored: left half LEFT, right half RIGHT."""
    image = Image.new("RGB", size, RIGHT)
    image.paste(LEFT, (0, 0, size[0] // 2, size[1]))
    return image


def make_dng(
    path,
    preview=None,
    orientation=1,
    preview_size=(96, 64),
    black=0,
    level=1500,
    mosaic=None,
    model="Test Camera",
):
    """A DNG at `path`. `preview` is None, "jpeg" or "bitmap": with one, IFD 0 is the
    preview and the raw image is its sub-IFD, as cameras and converters write them.
    The raw values are all `level` over a black level of `black`; a `level` far under
    `black` is what damaged raw data looks like. `mosaic` (H x W integers up to 4095,
    an RGGB mosaic) gives the raw values instead."""
    tiff = _Tiff()
    if preview is None:
        entries = _raw_entries(tiff, 0, black, level, mosaic)
        first = tiff.ifd(entries + _camera_tags(orientation, model))
        path.write_bytes(tiff.finish(first))
        return path
    raw_ifd = tiff.ifd(_raw_entries(tiff, 0, black, level, mosaic))
    image = preview_image(preview_size)
    if preview == "jpeg":
        out = io.BytesIO()
        image.save(out, "JPEG", quality=95)
        data, compression, photometric = out.getvalue(), 7, 6
    else:
        data, compression, photometric = image.tobytes(), 1, 2
    width, height = preview_size
    entries = [
        (254, _LONG, [1]),
        (256, _LONG, [width]),
        (257, _LONG, [height]),
        (258, _SHORT, [8, 8, 8]),
        (259, _SHORT, [compression]),
        (262, _SHORT, [photometric]),
        (273, _LONG, [tiff.blob(data)]),
        (277, _SHORT, [3]),
        (278, _LONG, [height]),
        (279, _LONG, [len(data)]),
        (284, _SHORT, [1]),
        (330, _LONG, [raw_ifd]),
    ]
    first = tiff.ifd(entries + _camera_tags(orientation, model))
    path.write_bytes(tiff.finish(first))
    return path


def scene(width=192, height=128):
    """Light in a made-up scene, linear, white of the middle exposure at 1: a gradient
    from deep shadow to four times past white, with a square of detail to align on."""
    x = np.linspace(-4.0, 2.0, width, dtype=np.float64)
    light = np.tile(2.0**x, (height, 1))
    # Texture of a few pixels across, the same every time
    noise = np.random.default_rng(7).random((height // 4 + 2, width // 4 + 2))
    texture = np.kron(noise, np.ones((4, 4)))[:height, :width]
    light *= 0.6 + 0.8 * texture
    light[height // 3 : height // 3 + 24, width // 3 : width // 3 + 24] *= 3.0
    return light


def bracket_mosaic(light, stops=0.0, shift=(0, 0)):
    """`light` (from `scene`) as a camera at `stops` from the middle exposure records it:
    12 bits, white at 4095 and clipped there, moved by `shift` (x, y) whole pixels, as an
    RGGB mosaic of a grey scene."""
    moved = np.roll(light, shift=(shift[1], shift[0]), axis=(0, 1))
    return np.clip(np.round(moved * 2.0**stops * 4095), 0, 4095).astype(np.uint16)
