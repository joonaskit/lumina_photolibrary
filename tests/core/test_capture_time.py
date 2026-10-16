"""The capture time read from a photo's first bytes, without exiftool."""

import struct
from datetime import datetime

from photos import make_jpeg, make_jpeg_with_exif
from rawfiles import make_dng

from lumina.core import capture_time

CANON_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")


def tiff(tags, endian="<"):
    """A TIFF of one IFD with these ASCII `tags` ({tag: text}), as a CR3's CMT boxes are."""
    entries, data = [], b""
    start = 8 + 2 + 12 * len(tags) + 4
    for tag, text in sorted(tags.items()):
        value = text.encode("ascii") + b"\x00"
        if len(value) <= 4:
            entries.append(struct.pack(endian + "HHI4s", tag, 2, len(value), value.ljust(4, b"\0")))
        else:
            entries.append(struct.pack(endian + "HHII", tag, 2, len(value), start + len(data)))
            data += value
    order = b"II" if endian == "<" else b"MM"
    head = order + struct.pack(endian + "HI", 42, 8)
    return head + struct.pack(endian + "H", len(tags)) + b"".join(entries) + b"\0\0\0\0" + data


def box(kind, body):
    return struct.pack(">I4s", 8 + len(body), kind) + body


def make_cr3(path, cmt2=None, cmt1=None):
    """A CR3 as Canon lays out its start: ftyp, then moov with Canon's uuid box holding
    the CMT boxes; then the image data, which is not read."""
    inside = b""
    if cmt1 is not None:
        inside += box(b"CMT1", tiff(cmt1, endian=">"))
    if cmt2 is not None:
        inside += box(b"CMT2", tiff(cmt2))
    canon = box(b"uuid", CANON_UUID + box(b"CNCV", b"CanonCR3_001/01.09.00/00.00.00") + inside)
    data = box(b"ftyp", b"crx \x00\x00\x00\x01crx isom") + box(b"moov", canon)
    path.write_bytes(data + box(b"mdat", b"\x00" * 4096))
    return path


def test_a_jpeg_tells_its_time_with_the_subseconds(tmp_path):
    path = make_jpeg_with_exif(tmp_path / "a.jpg")

    assert capture_time.read(path) == datetime(2024, 6, 1, 18, 30, 5, 250000)


def test_a_cr3_tells_its_time_from_its_exif_box(tmp_path):
    path = make_cr3(
        tmp_path / "IMG_0001.CR3",
        cmt2={0x9003: "2026:10:14 09:30:05", 0x9291: "43"},
        cmt1={0x0132: "2026:10:14 09:30:06"},
    )

    assert capture_time.read(path) == datetime(2026, 10, 14, 9, 30, 5, 430000)


def test_a_cr3_without_its_exif_box_tells_the_time_of_its_first_ifd(tmp_path):
    path = make_cr3(tmp_path / "IMG_0001.CR3", cmt1={0x0132: "2026:10:14 09:30:06"})

    assert capture_time.read(path) == datetime(2026, 10, 14, 9, 30, 6)


def test_a_tiff_based_raw_tells_its_time_from_its_exif_ifd(tmp_path):
    path = tmp_path / "IMG_0001.CR2"
    path.write_bytes(tiff({0x9003: "2026:10:14 09:30:05"}, endian=">"))

    assert capture_time.read(path) == datetime(2026, 10, 14, 9, 30, 5)


def test_nothing_is_told_without_a_time_or_with_a_clock_never_set(tmp_path):
    never_set = make_cr3(tmp_path / "never.CR3", cmt2={0x9003: "0000:00:00 00:00:00"})
    no_exif = make_jpeg(tmp_path / "plain.jpg")
    dng = make_dng(tmp_path / "plain.dng")

    assert capture_time.read(never_set) is None
    assert capture_time.read(no_exif) is None
    assert capture_time.read(dng) is None


def test_a_damaged_or_missing_file_is_not_an_error(tmp_path):
    whole = make_cr3(tmp_path / "IMG_0001.CR3", cmt2={0x9003: "2026:10:14 09:30:05"}).read_bytes()
    date = whole.index(b"2026:10:14")
    # Cut short anywhere before the date ends: in a box's header, in the TIFF, in the date
    for size in (3, 20, 60, 120, date, date + 10):
        cut = tmp_path / f"cut-{size}.CR3"
        cut.write_bytes(whole[:size])
        assert capture_time.read(cut) is None
    looping = tmp_path / "looping.CR3"
    # A box claiming no size, which would never move on
    looping.write_bytes(box(b"ftyp", b"crx ") + struct.pack(">I4s", 0, b"moov"))

    assert capture_time.read(looping) is None
    assert capture_time.read(tmp_path / "gone.CR3") is None
    assert capture_time.read(tmp_path) is None
