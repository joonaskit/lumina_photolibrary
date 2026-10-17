"""When a photo was taken, read from its first bytes: fast enough for a memory card.

exiftool reads a CR3 to its end (about a second a photo from a card in a USB reader),
and the files' times on a card are not the capture time (the camera writes them in
another zone, seconds later). The capture time is near the start of a JPEG, a CR3 and
the TIFF-based RAW files, in EXIF: this reads only that. Anything else, or anything
not as expected, is None, and the caller asks exiftool.

The time is the camera's local wall-clock time, as `captured` is kept: naive, with the
sub-seconds when the photo has them. Opened for reading only.
"""

import struct
from datetime import datetime

# Boxes of an ISO base media file (CR3): Canon's metadata is in a uuid box in moov
_CANON_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
_EXIF_IFD = 0x8769
_DATE_TIME = 0x0132
_DATE_TIME_ORIGINAL = 0x9003
_CREATE_DATE = 0x9004
_SUB_SEC_ORIGINAL = 0x9291
_SUB_SEC_CREATE = 0x9292
# Limits, so a damaged file can't keep it reading
_MOST_ENTRIES = 1000
_MOST_BOXES = 64
_JPEG_SEGMENTS = 32


def read(path):
    """When the photo at `path` was taken, or None when its first bytes don't say."""
    try:
        with open(path, "rb") as file:
            return read_file(file)
    except OSError:
        return None


def read_file(file):
    """When the photo in `file` (anything with seek and read, such as a camera's file read
    in parts) was taken, or None."""
    try:
        start = file.read(16)
        file.seek(0)
        if start[:2] == b"\xff\xd8":
            return _jpeg(file)
        if start[:4] in (b"II*\x00", b"MM\x00*"):
            return _tiff(file, 0)
        if start[4:8] == b"ftyp":
            return _cr3(file)
    except (OSError, ValueError, struct.error):
        return None
    return None


def _jpeg(file):
    position = 2
    for _segment in range(_JPEG_SEGMENTS):
        file.seek(position)
        marker, size = struct.unpack(">HH", file.read(4))
        if marker in (0xFFDA, 0xFFD9) or marker >> 8 != 0xFF:
            return None
        if marker == 0xFFE1 and file.read(6) == b"Exif\x00\x00":
            return _tiff(file, position + 10)
        position += 2 + size
    return None


def _cr3(file):
    """The capture time in a CR3's CMT2 box (the Exif IFD as a TIFF of its own), else
    in CMT1 (IFD0's DateTime)."""
    moov = _box(file, 0, None, b"moov")
    if moov is None:
        return None
    uuid = _box(file, *moov, b"uuid", _CANON_UUID)
    if uuid is None:
        return None
    for name in (b"CMT2", b"CMT1"):
        box = _box(file, *uuid, name)
        if box is not None:
            taken = _tiff(file, box[0])
            if taken is not None:
                return taken
    return None


def _box(file, start, end, name, uuid=None):
    """(where its contents start, where it ends) of the first box `name` (with the
    `uuid`, for a uuid box) from `start` to `end`, or None."""
    position = start
    for _box_number in range(_MOST_BOXES):
        if end is not None and position + 8 > end:
            return None
        file.seek(position)
        header = file.read(8)
        if len(header) < 8:
            return None
        size, kind = struct.unpack(">I4s", header)
        inside = position + 8
        if size == 1:
            size = struct.unpack(">Q", file.read(8))[0]
            inside += 8
        if size < 8:
            return None
        if kind == name:
            if uuid is None:
                return inside, position + size
            if file.read(16) == uuid:
                return inside + 16, position + size
        position += size
    return None


def _tiff(file, base):
    """The capture time in the TIFF at `base`: its first IFD, then its Exif IFD."""
    file.seek(base)
    order = file.read(2)
    if order not in (b"II", b"MM"):
        return None
    endian = "<" if order == b"II" else ">"
    _magic, first = struct.unpack(endian + "HI", file.read(6))
    tags = _ifd(file, base, first, endian)
    exif = tags.get(_EXIF_IFD)
    if exif is not None:
        tags = {**tags, **_ifd(file, base, exif, endian)}
    for when, subseconds in (
        (_DATE_TIME_ORIGINAL, _SUB_SEC_ORIGINAL),
        (_CREATE_DATE, _SUB_SEC_CREATE),
        (_DATE_TIME, None),
    ):
        taken = _parse(tags.get(when), tags.get(subseconds))
        if taken is not None:
            return taken
    return None


def _ifd(file, base, offset, endian):
    """{tag: value} of the IFD at `offset`: text for the dates, a number for the Exif
    IFD's place. Only the tags wanted are read."""
    wanted = {_EXIF_IFD, _DATE_TIME, _DATE_TIME_ORIGINAL, _CREATE_DATE}
    wanted |= {_SUB_SEC_ORIGINAL, _SUB_SEC_CREATE}
    file.seek(base + offset)
    (count,) = struct.unpack(endian + "H", file.read(2))
    if count > _MOST_ENTRIES:
        return {}
    entries = file.read(12 * count)
    found = {}
    for number in range(count):
        tag, kind, length, value = struct.unpack(
            endian + "HHI4s", entries[12 * number : 12 * number + 12]
        )
        if tag not in wanted:
            continue
        if tag == _EXIF_IFD:
            found[tag] = struct.unpack(endian + "I", value)[0]
        elif kind == 2 and 0 < length <= 64:
            if length <= 4:
                text = value[:length]
            else:
                file.seek(base + struct.unpack(endian + "I", value)[0])
                text = file.read(length)
            found[tag] = text.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    return found


def _parse(text, subseconds):
    # "2024:06:01 18:30:05" and "25" -> 2024-06-01 18:30:05.250000
    if not text:
        return None
    try:
        taken = datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        # Also the all-zero date of a camera whose clock was never set
        return None
    if subseconds and subseconds.isdigit():
        taken = taken.replace(microsecond=int(subseconds[:6].ljust(6, "0")))
    return taken
