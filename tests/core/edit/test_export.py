import hashlib
import os
import struct
import zlib

import numpy as np
import pytest
from photos import make_jpeg
from PIL import Image
from rawfiles import make_dng

from lumina.core.edit import export as exporting
from lumina.core.edit.export import ExportCancelled, ExportExists, export
from lumina.core.edit.export_options import ExportOptions, suggested_name
from lumina.core.edit.params import Crop, Params
from lumina.core.errors import PhotoError


@pytest.fixture
def photo(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    return make_jpeg(folder / "a.jpg", size=(64, 48), colour=(200, 100, 50))


@pytest.fixture
def out(tmp_path):
    folder = tmp_path / "export"
    folder.mkdir()
    return folder


def read_png16(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position, chunks = 8, {}
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        chunks.setdefault(kind, []).append(data[position + 8 : position + 8 + length])
        position += 12 + length
    width, height, depth, colour = struct.unpack(">IIBB", chunks[b"IHDR"][0][:10])
    raw = zlib.decompress(b"".join(chunks[b"IDAT"]))
    rows = np.frombuffer(raw, np.uint8).reshape(height, 1 + width * 6)
    assert (rows[:, 0] == 0).all()
    pixels = rows[:, 1:].copy().view(">u2").reshape(height, width, 3)
    return (depth, colour), pixels.astype(np.uint16), chunks


def read_tiff16(path):
    data = path.read_bytes()
    assert data[:4] == b"II*\x00"
    (ifd,) = struct.unpack("<I", data[4:8])
    (count,) = struct.unpack("<H", data[ifd : ifd + 2])
    tags = {}
    for n in range(count):
        tag, kind, number, value = struct.unpack(
            "<HHII", data[ifd + 2 + 12 * n : ifd + 14 + 12 * n]
        )
        tags[tag] = (kind, number, value)

    def values(tag):
        kind, number, value = tags[tag]
        if number == 1:
            return [value]
        return list(struct.unpack(f"<{number}I", data[value : value + 4 * number]))

    width, height = tags[256][2], tags[257][2]
    bits = struct.unpack("<3H", data[tags[258][2] : tags[258][2] + 6])
    strips = b"".join(data[o : o + c] for o, c in zip(values(273), values(279), strict=True))
    pixels = np.frombuffer(strips, "<u2").reshape(height, width, 3)
    kind, number, at = tags[34675]
    return bits, pixels, data[at : at + number]


@pytest.mark.parametrize(("fmt", "bits"), [("jpeg", 8), ("png", 8), ("tiff", 8)])
def test_8_bit_formats_are_written_with_an_srgb_profile(photo, out, fmt, bits):
    options = ExportOptions(fmt, bits)
    destination = out / suggested_name(photo, options)

    export(photo, Params(), destination, options)

    with Image.open(destination) as image:
        assert image.size == (64, 48)
        assert image.info["icc_profile"] == exporting.srgb_profile()
        pixel = image.convert("RGB").getpixel((32, 24))
    assert all(abs(a - b) <= 4 for a, b in zip(pixel, (200, 100, 50), strict=True))


def test_16_bit_png_holds_the_exact_render(photo, out):
    destination = out / "a.png"

    export(photo, Params(exposure=0.5), destination, ExportOptions("png", 16))

    (depth, colour), pixels, chunks = read_png16(destination)
    assert (depth, colour) == (16, 2)
    from lumina.core.edit import pipeline, source

    expected = pipeline.render(source.load_linear(photo), Params(exposure=0.5), bits=16)
    assert np.array_equal(pixels, expected)
    assert zlib.decompress(chunks[b"iCCP"][0].split(b"\x00", 1)[1][1:]) == exporting.srgb_profile()


def test_16_bit_tiff_holds_the_exact_render(photo, out, monkeypatch):
    # Several strips, as a large photo has
    monkeypatch.setattr(exporting, "_ROWS_PER_STRIP", 10)
    destination = out / "a.tif"

    export(photo, Params(), destination, ExportOptions("tiff", 16))

    bits, pixels, profile = read_tiff16(destination)
    from lumina.core.edit import pipeline, source

    assert bits == (16, 16, 16)
    assert np.array_equal(pixels, pipeline.render(source.load_linear(photo), Params(), bits=16))
    assert profile == exporting.srgb_profile()


def test_16_bit_files_are_read_by_exiftool(photo, out, exiftool):
    for fmt in ("png", "tiff"):
        destination = out / f"a.{fmt}"
        export(photo, Params(), destination, ExportOptions(fmt, 16))
        assert exiftool.read(destination).file_type in ("PNG", "TIFF")


def test_the_edit_and_size_are_applied(photo, out):
    destination = out / "small.jpg"

    export(
        photo, Params(rotate=1, crop=Crop(0, 0, 1, 0.5)), destination, ExportOptions(max_side=16)
    )

    with Image.open(destination) as image:
        # Turned: 48 x 64, cropped to the top half: 48 x 32, its longest side made 16
        assert image.width == 16 and image.height in (10, 11)


def test_raw_files_are_exported(tmp_path, out):
    path = make_dng(tmp_path / "a.dng")

    export(path, Params(), out / "a.jpg")

    with Image.open(out / "a.jpg") as image:
        assert image.size == (128, 96)


def test_an_existing_file_is_not_overwritten(photo, out):
    destination = out / "a.jpg"
    destination.write_bytes(b"someone else's file")

    with pytest.raises(ExportExists) as raised:
        export(photo, Params(), destination)

    assert raised.value.path == str(destination)
    assert destination.read_bytes() == b"someone else's file"
    assert os.listdir(out) == ["a.jpg"]


def test_a_file_that_appears_while_exporting_is_not_overwritten(photo, out, monkeypatch):
    destination = out / "a.jpg"
    real = exporting._WRITERS[("jpeg", 8)]

    def write_and_race(file, pixels, options):
        real(file, pixels, options)
        # Another program saves to the same name while the export is writing
        destination.write_bytes(b"arrived meanwhile")

    monkeypatch.setitem(exporting._WRITERS, ("jpeg", 8), write_and_race)

    with pytest.raises(ExportExists):
        export(photo, Params(), destination)

    assert destination.read_bytes() == b"arrived meanwhile"
    assert os.listdir(out) == ["a.jpg"]


def test_replace_overwrites_when_chosen(photo, out):
    destination = out / "a.jpg"
    destination.write_bytes(b"old export")

    export(photo, Params(), destination, replace=True)

    with Image.open(destination) as image:
        assert image.size == (64, 48)


def test_the_original_is_never_written_even_with_replace(photo):
    before = (hashlib.sha256(photo.read_bytes()).hexdigest(), photo.stat().st_mtime_ns)

    for replace in (False, True):
        with pytest.raises(PhotoError) as raised:
            export(photo, Params(exposure=2), photo, replace=replace)
        assert "original" in raised.value.message

    assert (hashlib.sha256(photo.read_bytes()).hexdigest(), photo.stat().st_mtime_ns) == before
    assert sorted(os.listdir(photo.parent)) == ["a.jpg"]


def test_the_original_through_a_link_is_refused_too(photo, out):
    link = out / "link.jpg"
    link.symlink_to(photo)

    with pytest.raises(PhotoError):
        export(photo, Params(), link, replace=True)

    assert link.is_symlink()


def test_cancel_leaves_nothing_behind(photo, out):
    asked = []

    def stop_late():
        asked.append(1)
        return len(asked) >= 4

    with pytest.raises(ExportCancelled):
        export(photo, Params(), out / "a.jpg", should_stop=stop_late)

    assert os.listdir(out) == []


def test_a_failed_write_names_the_file_and_leaves_nothing(photo, out, monkeypatch):
    def full_disk(file, pixels, options):
        file.write(b"part of it")
        raise OSError(28, "No space left on device")

    monkeypatch.setitem(exporting._WRITERS, ("jpeg", 8), full_disk)

    with pytest.raises(PhotoError) as raised:
        export(photo, Params(), out / "a.jpg")

    assert raised.value.path == str(out / "a.jpg")
    assert "No space left" in raised.value.message
    assert os.listdir(out) == []


def test_a_folder_that_cannot_be_written_is_named(photo, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(PhotoError) as raised:
            export(photo, Params(), locked / "a.jpg")
    finally:
        locked.chmod(0o700)

    assert raised.value.path == str(locked / "a.jpg")


def test_an_option_a_format_cant_hold_is_refused(photo, out):
    with pytest.raises(ValueError):
        export(photo, Params(), out / "a.jpg", ExportOptions("jpeg", 16))


def test_a_tiff_too_large_for_its_format_is_refused_naming_the_file(photo, out, monkeypatch):
    # A 700 MP panorama: tested with the size, not with the pixels
    big = np.zeros((2, 2, 3), np.float32)
    monkeypatch.setattr(
        "lumina.core.edit.source.load_for_edit", lambda path: (big, None), raising=True
    )
    monkeypatch.setattr(
        "lumina.core.edit.pipeline.output_size", lambda width, height, params: (30000, 30000)
    )
    destination = str(out / "huge.tiff")

    with pytest.raises(PhotoError) as raised:
        export(str(photo), Params(), destination, ExportOptions("tiff", 16))

    assert raised.value.path == destination
    assert "too large for a TIFF" in raised.value.message
    assert not os.path.exists(destination)
    # PNG has no such limit; 8 bits at that size fits
    exporting._check_size(destination, ExportOptions("png", 16), 30000, 30000, Params())
    exporting._check_size(destination, ExportOptions("tiff", 8), 30000, 30000, Params())
