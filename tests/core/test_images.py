import hashlib
import os

import pytest
from photos import make_jpeg, make_png
from PIL import Image

from lumina.core import images, raw
from lumina.core.errors import PhotoError


def test_a_jpeg_is_read_as_rgb_rows(tmp_path):
    path = make_jpeg(tmp_path / "a.jpg", size=(64, 32))

    pixels = images.load_pixels(path, 256)

    assert (pixels.width, pixels.height) == (64, 32)
    assert len(pixels.data) == 64 * 32 * 3


def test_a_large_photo_is_made_smaller_keeping_its_shape(tmp_path):
    path = make_jpeg(tmp_path / "big.jpg", size=(1200, 600))

    pixels = images.load_pixels(path, 300)

    assert (pixels.width, pixels.height) == (300, 150)


def test_the_exif_orientation_is_applied(tmp_path):
    # Stored landscape, shown portrait: a quarter turn
    path = make_jpeg(tmp_path / "turned.jpg", size=(64, 32), orientation=6)

    pixels = images.load_pixels(path, 256)

    assert (pixels.width, pixels.height) == (32, 64)


def test_png_is_read(tmp_path):
    pixels = images.load_pixels(make_png(tmp_path / "a.png"), 256)

    assert (pixels.width, pixels.height) == (40, 30)


def test_a_broken_file_is_an_error_naming_it(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"\xff\xd8 not really a jpeg")

    with pytest.raises(PhotoError) as raised:
        images.load_pixels(path, 256)

    assert raised.value.path == str(path)
    assert str(path) in str(raised.value)


def test_a_broken_raw_file_is_an_error_naming_it(tmp_path):
    path = tmp_path / "broken.CR3"
    path.write_bytes(b"\x00" * 1024)

    with pytest.raises(PhotoError) as raised:
        images.load_pixels(path, 256)

    assert raised.value.path == str(path)


def test_the_error_survives_the_trip_from_a_worker_process(tmp_path):
    import pickle

    error = pickle.loads(pickle.dumps(PhotoError(tmp_path / "x.jpg", "cannot read")))

    assert (error.path, error.message) == (str(tmp_path / "x.jpg"), "cannot read")


def test_reading_leaves_the_original_as_it_was(tmp_path):
    path = make_jpeg(tmp_path / "original.jpg", size=(800, 400), orientation=6)
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), os.stat(path).st_mtime_ns)
    os.chmod(path, 0o444)

    images.load_pixels(path, 128)

    after = (hashlib.sha256(path.read_bytes()).hexdigest(), os.stat(path).st_mtime_ns)
    assert after == before


@pytest.mark.parametrize(
    ("flip", "size"),
    [(0, (60, 20)), (3, (60, 20)), (5, (20, 60)), (6, (20, 60))],
)
def test_libraw_flip_turns_the_preview_upright(flip, size):
    assert raw.apply_flip(Image.new("RGB", (60, 20)), flip).size == size


def test_flip_turns_the_right_way():
    # A red pixel at the top left, turned a quarter clockwise (flip 6), is at the top right
    image = Image.new("RGB", (2, 1), (0, 0, 0))
    image.putpixel((0, 0), (255, 0, 0))

    turned = raw.apply_flip(image, 6)

    assert turned.getpixel((0, 0)) == (255, 0, 0)
    assert turned.size == (1, 2)


def test_raw_and_image_names_are_recognised():
    assert images.is_photo("IMG_0001.CR3")
    assert images.is_photo("a.JPEG")
    assert raw.is_raw("x.cr3") and not raw.is_raw("x.jpg")
    assert not images.is_photo("notes.txt")


def test_in_full_a_photo_keeps_its_size_and_says_it(tmp_path):
    path = make_jpeg(tmp_path / "big.jpg", size=(1200, 600))

    whole = images.load_pixels(path, None)
    small = images.load_pixels(path, 300)

    assert (whole.width, whole.height) == (1200, 600)
    # What 100 % is, also when read smaller
    assert whole.full == small.full == (1200, 600)


def test_the_full_size_is_upright(tmp_path):
    path = make_jpeg(tmp_path / "turned.jpg", size=(640, 320), orientation=6)

    assert images.load_pixels(path, 100).full == (320, 640)
    assert images.load_pixels(path, None).full == (320, 640)
