import hashlib
import os

import pytest
from rawfiles import LEFT, RAW_SIZE, RIGHT, make_dng

from lumina.core import images, raw
from lumina.core.errors import PhotoError


def _close(colour, expected, tolerance=40):
    return all(abs(a - b) <= tolerance for a, b in zip(colour, expected, strict=True))


@pytest.mark.parametrize("kind", ["jpeg", "bitmap"])
def test_the_embedded_preview_is_used(tmp_path, kind):
    path = make_dng(tmp_path / "a.dng", preview=kind, preview_size=(96, 64))

    image = raw.load_preview(path)

    assert image.size == (96, 64)
    assert _close(image.getpixel((0, 32)), LEFT)
    assert _close(image.getpixel((95, 32)), RIGHT)


@pytest.mark.parametrize("kind", ["jpeg", "bitmap"])
def test_the_preview_is_turned_upright_by_the_files_orientation(tmp_path, kind):
    # Stored landscape with the left half red; a quarter turn clockwise puts red on top
    path = make_dng(tmp_path / "turned.dng", preview=kind, orientation=6, preview_size=(96, 64))

    image = raw.load_preview(path)

    assert image.size == (64, 96)
    assert _close(image.getpixel((32, 0)), LEFT)
    assert _close(image.getpixel((32, 95)), RIGHT)


def test_a_raw_file_without_a_preview_is_decoded_at_half_size(tmp_path):
    width, height = RAW_SIZE

    assert raw.load_preview(make_dng(tmp_path / "a.dng")).size == (width // 2, height // 2)
    turned = raw.load_preview(make_dng(tmp_path / "b.dng", orientation=6))
    assert turned.size == (height // 2, width // 2)


def test_raw_files_go_through_load_pixels(tmp_path):
    path = make_dng(tmp_path / "a.dng", preview="jpeg", preview_size=(400, 200))

    pixels = images.load_pixels(path, 100)

    assert (pixels.width, pixels.height) == (100, 50)


def test_a_truncated_raw_file_is_an_error_naming_it(tmp_path):
    whole = make_dng(tmp_path / "whole.dng", preview="jpeg").read_bytes()
    path = tmp_path / "cut.dng"
    path.write_bytes(whole[: len(whole) // 3])

    with pytest.raises(PhotoError) as raised:
        raw.load_preview(path)

    assert raised.value.path == str(path)


def test_a_missing_raw_file_is_an_error_naming_it(tmp_path):
    with pytest.raises(PhotoError) as raised:
        raw.load_preview(tmp_path / "gone.CR3")

    assert raised.value.path == str(tmp_path / "gone.CR3")


def test_reading_a_raw_file_leaves_it_as_it_was(tmp_path):
    path = make_dng(tmp_path / "original.dng", preview="jpeg", orientation=6)
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), os.stat(path).st_mtime_ns)

    raw.load_preview(path, 64)
    raw.load_preview(make_dng(tmp_path / "plain.dng"))

    assert (hashlib.sha256(path.read_bytes()).hexdigest(), os.stat(path).st_mtime_ns) == before
