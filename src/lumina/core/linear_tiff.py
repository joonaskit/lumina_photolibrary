"""Linear light in a TIFF: RGB as 16-bit floats ("half"), uncompressed, in strips.

What a merge writes (core/hdr.py): light past white is kept, which 16-bit integers
cannot do, with 11 bits of precision at every brightness, up to 65504 times white.
Other editors (darktable, GIMP, Photoshop) open it too. The values are linear light
in sRGB primaries with 1.0 as white of the frame the merge took its exposure from.

The file is laid out before any pixel is known (`Layout`): a header with the tags,
then the rows one after another, so rows can be written in any order, by offset, as
a job makes them (`write_rows`), and a job resumed after a crash writes the rest.

Reading (`read`) takes any TIFF of float RGB with no compression, in strips, either
byte order: the files written here and most others like them. `is_linear` tells
those apart from the TIFFs Pillow reads, by their header alone.
"""

import os
import struct
from dataclasses import dataclass

import numpy as np

from lumina.core.errors import PhotoError

_ASCII, _SHORT, _LONG, _RATIONAL = 2, 3, 4, 5
# SampleFormat: IEEE floating point
_FLOAT = 3
_ORIENTATION = 274
# Pixels held in one strip of the files written here
_ROWS_PER_STRIP = 16


@dataclass(frozen=True)
class Tags:
    """What the file says of itself, for the library's details (exiftool reads them)."""

    description: str | None = None
    make: str | None = None
    model: str | None = None
    lens: str | None = None
    # "YYYY:MM:DD HH:MM:SS", as EXIF has it, and the fraction of the second ("120")
    captured: str | None = None
    subseconds: str | None = None
    software: str = "Lumina"


@dataclass(frozen=True)
class Layout:
    """Where everything goes in a file of `width` x `height`: `header` is the bytes up to
    the first row, and row r starts at `data_at` + r x `row_bytes`."""

    width: int
    height: int
    header: bytes

    @property
    def data_at(self):
        return len(self.header)

    @property
    def row_bytes(self):
        return self.width * 6

    @property
    def size(self):
        return self.data_at + self.height * self.row_bytes


def layout(width, height, tags=None, profile=None):
    """The Layout of a linear TIFF of `width` x `height` with `tags` (Tags) and an ICC
    `profile` (bytes) if any."""
    tags = tags or Tags()
    if width < 1 or height < 1:
        raise ValueError(f"no pixels: {width} x {height}")
    strips = range(0, height, _ROWS_PER_STRIP)
    row_bytes = width * 6
    counts = [min(_ROWS_PER_STRIP, height - start) * row_bytes for start in strips]
    exif = [(36867, _ASCII, tags.captured)] if tags.captured else []
    if tags.captured and tags.subseconds:
        exif.append((37521, _ASCII, tags.subseconds))
    if tags.lens:
        exif.append((42036, _ASCII, tags.lens))
    main = [
        (256, _LONG, [width]),
        (257, _LONG, [height]),
        (258, _SHORT, [16, 16, 16]),
        (259, _SHORT, [1]),
        (262, _SHORT, [2]),
        (_ORIENTATION, _SHORT, [1]),
        (277, _SHORT, [3]),
        (278, _LONG, [_ROWS_PER_STRIP]),
        (284, _SHORT, [1]),
        (339, _SHORT, [_FLOAT] * 3),
    ]
    for tag, value in ((270, tags.description), (271, tags.make), (272, tags.model)):
        if value:
            main.append((tag, _ASCII, value))
    main.append((305, _ASCII, tags.software))
    if tags.captured:
        main.append((306, _ASCII, tags.captured))
    if profile:
        main.append((34675, 7, profile))
    # Twice: the first time with made-up offsets, to learn where the rows start
    data_at = 0
    for _ in range(2):
        writer = _Writer()
        exif_at = writer.ifd(exif) if exif else None
        offsets = [data_at + sum(counts[:i]) for i in range(len(counts))]
        entries = [
            *main,
            (273, _LONG, offsets),
            (279, _LONG, counts),
            *([(34665, _LONG, [exif_at])] if exif_at else []),
        ]
        first = writer.ifd(entries)
        header = writer.finish(first)
        data_at = len(header)
    return Layout(width, height, header)


class _Writer:
    """A little-endian TIFF header: values first, then the IFD pointing at them."""

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
        for tag, kind, values in sorted(entries, key=lambda entry: entry[0]):
            if kind == _ASCII:
                raw = values.encode("utf-8", "replace") + b"\x00"
                count = len(raw)
            elif kind == 7:
                raw, count = bytes(values), len(values)
            else:
                code = "H" if kind == _SHORT else "I"
                raw, count = struct.pack(f"<{len(values)}{code}", *values), len(values)
            inline = raw.ljust(4, b"\x00") if len(raw) <= 4 else struct.pack("<I", self.blob(raw))
            packed.append(struct.pack("<HHI", tag, kind, count) + inline)
        at = self.blob(struct.pack("<H", len(packed)) + b"".join(packed) + b"\x00" * 4)
        return at

    def finish(self, first_ifd):
        if len(self.buf) % 2:
            self.buf += b"\x00"
        self.buf[4:8] = struct.pack("<I", first_ifd)
        return bytes(self.buf)


def start(fd, file_layout):
    """Write the header of `file_layout` into the open file `fd` and give the file its
    whole size, so rows can be written in any order."""
    os.pwrite(fd, file_layout.header, 0)
    os.ftruncate(fd, file_layout.size)


def write_rows(fd, file_layout, first_row, rows):
    """`rows` (n x width x 3, floats; written as 16-bit) at row `first_row` of the open
    file `fd`."""
    data = np.ascontiguousarray(rows, dtype="<f2")
    if data.shape[1:] != (file_layout.width, 3):
        raise ValueError(f"rows of {data.shape[1:]}, the file has {file_layout.width} x 3")
    if first_row + len(data) > file_layout.height:
        raise ValueError("rows past the end of the image")
    at = file_layout.data_at + first_row * file_layout.row_bytes
    view = memoryview(data.reshape(-1).view(np.uint8))
    while view:
        written = os.pwrite(fd, view, at)
        view, at = view[written:], at + written


# Reading


@dataclass(frozen=True)
class _Header:
    width: int
    height: int
    dtype: np.dtype
    strips: tuple
    counts: tuple
    rows_per_strip: int
    orientation: int


def is_linear(path):
    """Whether `path` is a TIFF of float RGB that `read` takes (Pillow does not)."""
    if not str(path).lower().endswith((".tif", ".tiff")):
        return False
    try:
        return _header(path) is not None
    except (OSError, ValueError, struct.error):
        return False


def size(path):
    """(width, height) of the linear TIFF at `path`, upright. Raises PhotoError."""
    header = _checked_header(path)
    turned = header.orientation in (5, 6, 7, 8)
    return (header.height, header.width) if turned else (header.width, header.height)


def read(path, step=1):
    """The linear TIFF at `path` as H x W x 3 float16, upright; with `step` > 1 every
    step-th row and column of it (a smaller copy, quickly). Raises PhotoError naming
    the file."""
    header = _checked_header(path)
    rows = []
    try:
        with open(path, "rb") as file:
            for index, (offset, count) in enumerate(zip(header.strips, header.counts, strict=True)):
                first = index * header.rows_per_strip
                high = min(header.rows_per_strip, header.height - first)
                if high <= 0:
                    break
                # Only the strips that hold a row taken
                wanted = [r for r in range(first, first + high) if r % step == 0]
                if not wanted:
                    continue
                file.seek(offset)
                data = file.read(count)
                if len(data) < high * header.width * 3 * header.dtype.itemsize:
                    raise PhotoError(path, "the image is cut short")
                strip = np.frombuffer(data, header.dtype, high * header.width * 3)
                strip = strip.reshape(high, header.width, 3)
                rows.append(strip[[r - first for r in wanted], ::step].astype(np.float16))
    except OSError as error:
        raise PhotoError(path, f"cannot read image ({error.strerror or error})") from error
    image = np.concatenate(rows) if rows else np.zeros((0, 0, 3), np.float16)
    image = np.nan_to_num(image, nan=0.0, posinf=65504.0, neginf=0.0)
    np.maximum(image, 0, out=image)
    return _upright(image, header.orientation)


def _upright(image, orientation):
    # Quarter turns; mirrored orientations are taken as their turn alone
    turns = {3: 2, 4: 2, 6: -1, 5: -1, 8: 1, 7: 1}.get(orientation, 0)
    return np.ascontiguousarray(np.rot90(image, k=turns)) if turns else image


def _checked_header(path):
    try:
        header = _header(path)
    except (OSError, ValueError, struct.error) as error:
        raise PhotoError(path, f"cannot read image ({error})") from error
    if header is None:
        raise PhotoError(path, "not a TIFF of linear floating-point RGB")
    return header


def _header(path):
    """The first image of the TIFF at `path`, or None when it is not float RGB in strips
    with no compression."""
    with open(path, "rb") as file:
        start = file.read(8)
        if start[:4] == b"II*\x00":
            order = "<"
        elif start[:4] == b"MM\x00*":
            order = ">"
        else:
            return None
        (first,) = struct.unpack(order + "I", start[4:8])
        file.seek(first)
        (count,) = struct.unpack(order + "H", file.read(2))
        raw = file.read(count * 12)
        tags = {}
        for i in range(count):
            tag, kind, n, value = struct.unpack(order + "HHI4s", raw[i * 12 : i * 12 + 12])
            tags[tag] = _values(file, order, kind, n, value)
    fmt = tags.get(339, [1])
    bits = tags.get(258, [1])
    if tags.get(277, [1])[0] != 3 or tags.get(262, [0])[0] != 2:
        return None
    if any(f != _FLOAT for f in fmt) or len(set(bits)) != 1 or bits[0] not in (16, 32):
        return None
    if tags.get(259, [1])[0] != 1 or tags.get(284, [1])[0] != 1 or 273 not in tags:
        return None
    width, height = tags[256][0], tags[257][0]
    dtype = np.dtype(f"{order}f{bits[0] // 8}")
    strips, counts = tuple(tags[273]), tuple(tags.get(279, ()))
    if len(counts) != len(strips):
        return None
    return _Header(
        width,
        height,
        dtype,
        strips,
        counts,
        tags.get(278, [height])[0],
        tags.get(_ORIENTATION, [1])[0],
    )


def _values(file, order, kind, count, inline):
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 7: 1, 16: 8}
    if kind not in (3, 4):
        # Only numbers are needed: the strips, sizes and formats
        return None
    width = sizes[kind] * count
    if width <= 4:
        data = inline[:width]
    else:
        (offset,) = struct.unpack(order + "I", inline)
        here = file.tell()
        file.seek(offset)
        data = file.read(width)
        file.seek(here)
    code = "H" if kind == 3 else "I"
    return list(struct.unpack(f"{order}{count}{code}", data))
