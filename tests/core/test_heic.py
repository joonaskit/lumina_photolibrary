"""HEIC and HEIF photos (phones), and photos with a colour profile of their own: shown
upright and in sRGB wherever they are read."""

import numpy as np
import pytest
from photos import (
    DISPLAY_P3_COLORANTS,
    SRGB_COLORANTS,
    camera_exif,
    icc_profile,
    make_heic,
    make_jpeg,
)
from PIL import Image

from lumina.core import images, scanner, thumbs
from lumina.core.edit import source
from lumina.core.edit.pipeline import srgb_decode, srgb_encode
from lumina.core.errors import PhotoError

P3 = icc_profile("Display P3", DISPLAY_P3_COLORANTS)
SRGB = icc_profile("sRGB IEC61966-2.1", SRGB_COLORANTS)


def near(pixel, expected, tolerance=6):
    return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(pixel, expected, strict=True))


def in_srgb(p3_colour):
    """What a Display P3 colour is in sRGB, from the colorants the profiles hold (not
    from lcms): linear, to XYZ by P3's matrix, back by sRGB's, encoded again."""
    p3 = np.array(DISPLAY_P3_COLORANTS).T
    srgb = np.array(SRGB_COLORANTS).T
    linear = srgb_decode(np.array(p3_colour) / 255.0)
    converted = np.linalg.inv(srgb) @ (p3 @ linear)
    return np.round(srgb_encode(np.clip(converted, 0, 1)) * 255)


def test_heic_and_heif_are_photos():
    assert images.HEIC
    for name in ("IMG_0001.HEIC", "a.heic", "b.heif", "c.HEIF"):
        assert images.is_photo(name)
    assert not images.is_photo("IMG_0001.MOV")


def test_the_scan_finds_heic_files(tmp_path):
    make_heic(tmp_path / "a.heic")
    make_heic(tmp_path / "b.heif")
    make_jpeg(tmp_path / "c.jpg")
    (tmp_path / "IMG_0001.MOV").write_bytes(b"not a photo")

    found = sorted(
        path.name for path in map(__import__("pathlib").Path, scanner.find_photos(tmp_path))
    )

    assert found == ["a.heic", "b.heif", "c.jpg"]


def test_a_heic_is_read_as_rgb_rows_at_the_size_asked(tmp_path):
    path = make_heic(tmp_path / "a.heic", size=(200, 100), colour=(30, 160, 60))

    pixels = images.load_pixels(path, 100)

    assert (pixels.width, pixels.height) == (100, 50)
    assert pixels.full == (200, 100)
    assert near(pixels.data[:3], (30, 160, 60))


@pytest.mark.parametrize(
    "orientation, shown", [(1, (96, 64)), (6, (64, 96)), (8, (64, 96)), (3, (96, 64))]
)
def test_a_heic_is_shown_upright_once(tmp_path, orientation, shown):
    # Turned by its opener and not again: a portrait photo is not turned on its side, or back
    path = make_heic(tmp_path / "a.heic", size=(96, 64), orientation=orientation)

    pixels = images.load_pixels(path, None)

    assert (pixels.width, pixels.height) == shown
    assert pixels.full == shown


def test_which_corner_is_which_after_the_turn(tmp_path):
    # The left half is red and the right blue; orientation 6 turns it a quarter clockwise,
    # so red is at the top
    image = Image.new("RGB", (64, 32), (220, 30, 30))
    image.paste((30, 30, 220), (32, 0, 64, 32))
    import pillow_heif

    pillow_heif.register_heif_opener()
    exif = Image.Exif()
    exif[0x0112] = 6
    image.save(tmp_path / "a.heic", "HEIF", exif=exif.tobytes(), quality=95)

    pixels = images.load_pixels(tmp_path / "a.heic", None)

    assert (pixels.width, pixels.height) == (32, 64)
    row = pixels.width * 3
    assert near(pixels.data[:3], (220, 30, 30))
    assert near(pixels.data[-row : -row + 3], (30, 30, 220))


def test_a_display_p3_photo_is_converted_to_srgb(tmp_path):
    path = make_heic(tmp_path / "p3.heic", colour=(200, 100, 100), icc=P3)

    pixels = images.load_pixels(path, None)

    expected = in_srgb((200, 100, 100))
    # Not as it was: P3's red is further out than sRGB's
    assert not near((200, 100, 100), expected, 8)
    assert near(pixels.data[:3], expected, 8)


def test_a_photo_without_a_profile_or_with_srgb_is_left_as_it_is(tmp_path):
    plain = make_heic(tmp_path / "plain.heic", colour=(200, 100, 100))
    srgb = make_heic(tmp_path / "srgb.heic", colour=(200, 100, 100), icc=SRGB)

    assert near(images.load_pixels(plain, None).data[:3], (200, 100, 100))
    assert near(images.load_pixels(srgb, None).data[:3], (200, 100, 100))


def test_a_jpeg_with_a_profile_is_converted_too(tmp_path):
    path = tmp_path / "p3.jpg"
    Image.new("RGB", (32, 32), (200, 100, 100)).save(path, "JPEG", quality=100, icc_profile=P3)

    pixels = images.load_pixels(path, None)

    assert near(pixels.data[:3], in_srgb((200, 100, 100)), 8)


def test_a_profile_that_cannot_be_used_leaves_the_photo_as_it_is():
    image = Image.new("RGB", (4, 4), (10, 20, 30))

    assert images.to_srgb(image, b"not a profile") is image
    assert images.to_srgb(image, None) is image
    assert images.to_srgb(image, P3) is not image


def test_the_profile_is_applied_after_the_turn(tmp_path):
    path = tmp_path / "p3.jpg"
    image = Image.new("RGB", (48, 32), (200, 100, 100))
    exif = Image.Exif()
    exif[0x0112] = 6
    image.save(path, "JPEG", quality=100, icc_profile=P3, exif=exif.tobytes())

    pixels = images.load_pixels(path, None)

    assert (pixels.width, pixels.height) == (32, 48)
    assert near(pixels.data[:3], in_srgb((200, 100, 100)), 8)


def test_a_heic_is_edited_from_a_linear_decode_in_srgb_and_upright(tmp_path):
    path = make_heic(
        tmp_path / "p3.heic", size=(96, 64), colour=(200, 100, 100), orientation=6, icc=P3
    )

    linear = source.load_linear(path)

    assert linear.shape == (96, 64, 3) and linear.dtype == np.uint16
    expected = srgb_decode(in_srgb((200, 100, 100)) / 255.0) * 65535
    assert np.allclose(linear[10, 10], expected, atol=0.03 * 65535)


def test_a_heic_gets_a_thumbnail_that_is_cached(tmp_path):
    path = make_heic(tmp_path / "a.heic", size=(400, 300))

    made = thumbs.load_thumbnail(path, 160, tmp_path / "cache")
    again = thumbs.load_thumbnail(path, 160, tmp_path / "cache")

    assert (made.width, made.height) == (160, 120)
    assert (again.width, again.height) == (160, 120)


@pytest.mark.parametrize(
    "orientation, shown", [(1, (320, 240)), (6, (240, 320)), (8, (240, 320)), (3, (320, 240))]
)
def test_a_small_heic_is_read_from_the_thumbnail_inside_it(tmp_path, drafts, orientation, shown):
    # An iPhone's is 416 x 312, which covers the grid's 320 x 240 but not 320 x 320; it
    # is turned as the photo is
    path = make_heic(
        tmp_path / "a.heic",
        size=(1600, 1200),
        colour=(30, 160, 60),
        orientation=orientation,
        icc=P3,
        thumbnail=416,
    )

    pixels = images.load_pixels(path, 320)

    assert drafts[-1] is not None
    assert (pixels.width, pixels.height) == shown
    # The viewer's 100 % is still the photo's
    assert pixels.full == (shown[0] * 5, shown[1] * 5)
    assert near(pixels.data[:3], in_srgb((30, 160, 60)))


def test_a_heic_without_a_large_enough_thumbnail_is_decoded_in_full(tmp_path, drafts):
    path = make_heic(tmp_path / "a.heic", size=(1600, 1200), colour=(30, 160, 60), thumbnail=200)

    pixels = images.load_pixels(path, 320)

    assert drafts[-1] is None
    assert (pixels.width, pixels.height) == (320, 240)
    assert near(pixels.data[:3], (30, 160, 60))


def test_a_broken_heic_is_an_error_naming_it(tmp_path):
    path = tmp_path / "broken.heic"
    path.write_bytes(b"\0\0\0\x18ftypheic" + b"not really")

    with pytest.raises(PhotoError, match="broken.heic"):
        images.load_pixels(path, 100)


def test_reading_a_heic_leaves_it_as_it_was(tmp_path):
    path = make_heic(tmp_path / "a.heic")
    before = path.read_bytes()

    images.load_pixels(path, 100)
    source.load_linear(path)

    assert path.read_bytes() == before


def test_the_metadata_of_a_heic_is_read(exiftool, tmp_path):
    path = make_heic(tmp_path / "a.heic", exif=camera_exif(orientation=1))

    data = exiftool.read(path)

    assert data.model == "Canon EOS R6m2"
    assert data.lens == "RF24-105mm F4 L IS USM"
    assert (data.captured.year, data.captured.month, data.captured.day) == (2024, 6, 1)
    assert data.iso == 400
