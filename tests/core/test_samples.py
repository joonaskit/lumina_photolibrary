"""Real camera files, when there are some: tests/samples/ or $LUMINA_SAMPLES.

They are not in git (a CR3 is 20 to 50 MB), so these tests run on the machines
that have them and are skipped elsewhere. Put a few files from each camera body
and phone in there, ideally with both orientations.
"""

import hashlib
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter

from lumina.core import images, raw
from lumina.core.edit import source

SAMPLES = Path(os.environ.get("LUMINA_SAMPLES") or Path(__file__).parent.parent / "samples")
FILES = sorted(p for p in SAMPLES.glob("*") if images.is_photo(p.name)) if SAMPLES.is_dir() else []
RAW_FILES = [p for p in FILES if raw.is_raw(p)]
HEIC_FILES = [p for p in FILES if p.suffix.lower() in (".heic", ".heif")]

needs_samples = pytest.mark.skipif(not FILES, reason=f"no samples in {SAMPLES}")
needs_raw = pytest.mark.skipif(not RAW_FILES, reason=f"no RAW samples in {SAMPLES}")
needs_heic = pytest.mark.skipif(not HEIC_FILES, reason=f"no HEIC samples in {SAMPLES}")


@needs_raw
@pytest.mark.parametrize("path", RAW_FILES, ids=lambda p: p.name)
def test_the_large_embedded_preview_is_used(path):
    # A CR3 has a full-size preview and a 160 x 120 one: the full-size one is wanted
    image = raw.load_preview(path)

    assert max(image.size) >= 1600, f"{path.name}: preview only {image.size}"


@needs_raw
@pytest.mark.parametrize("path", RAW_FILES, ids=lambda p: p.name)
def test_the_preview_has_the_shape_of_the_raw_image(path):
    # Upright: the preview is portrait exactly when the turned sensor image is
    import rawpy

    with rawpy.imread(str(path)) as file:
        width, height = file.sizes.width, file.sizes.height
        if file.sizes.flip in (5, 6):
            width, height = height, width
    image = raw.load_preview(path)

    assert (image.width > image.height) == (width > height)


@needs_samples
def test_samples_are_left_as_they_were():
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in FILES}

    for path in FILES:
        images.load_pixels(path, 320)

    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in FILES} == before


# The EXIF orientation of a photo, and LibRaw's `flip` for the same turn: exiftool and
# LibRaw both read it from the file, and what is shown is turned by one or the other
# (the preview by its own EXIF or LibRaw's flip, the decode by LibRaw's)
FLIP_OF_ORIENTATION = {1: 0, 3: 3, 8: 5, 6: 6}


def _flip(path):
    import rawpy

    with rawpy.imread(str(path)) as file:
        return file.sizes.flip


@needs_raw
@pytest.mark.parametrize("path", RAW_FILES, ids=lambda p: p.name)
def test_exiftool_and_libraw_agree_on_the_orientation(path, exiftool):
    # A camera body where they disagree would show its portrait photos on their side
    orientation = exiftool.read(path).orientation

    assert FLIP_OF_ORIENTATION[orientation or 1] == _flip(path), path.name


# EXIF orientations that turn the photo a quarter, so it is shown with its sides swapped
QUARTER_TURNS = {5, 6, 7, 8}


def _stored_size(path):
    # The size the camera wrote, before any turn: the EXIF's pixel dimensions (a phone
    # writes them as stored, while its opener turns the image by the file's own rotation)
    with Image.open(path) as image:
        exif = image.getexif().get_ifd(0x8769)
    return exif[0xA002], exif[0xA003]


@needs_heic
@pytest.mark.parametrize("path", HEIC_FILES, ids=lambda p: p.name)
def test_a_heic_is_shown_upright_as_its_exif_says(path, exiftool):
    # The opener turns it by the file's rotation, the EXIF says the same turn, and it is
    # turned once: a portrait photo is neither on its side nor turned back
    width, height = _stored_size(path)
    if (exiftool.read(path).orientation or 1) in QUARTER_TURNS:
        width, height = height, width

    pixels = images.load_pixels(path, 320)

    assert pixels.full == (width, height)
    assert (pixels.width > pixels.height) == (width > height)


@needs_heic
@pytest.mark.parametrize("path", HEIC_FILES, ids=lambda p: p.name)
def test_the_profile_of_a_heic_is_one_that_can_be_used(path):
    # A profile lcms cannot read leaves the photo as it was, silently: a phone's Display
    # P3 must be converted, which changes the pixels
    with Image.open(path) as image:
        image.thumbnail((160, 160))
        profile = image.info.get("icc_profile")
        rgb = image.convert("RGB")
    if not profile or b"sRGB" in profile:
        pytest.skip(f"{path.name} has no profile other than sRGB")

    assert images.to_srgb(rgb, profile).tobytes() != rgb.tobytes(), path.name


@needs_heic
@pytest.mark.parametrize("path", HEIC_FILES, ids=lambda p: p.name)
def test_the_grid_reads_a_heic_from_the_thumbnail_inside_it(path, drafts):
    # Fast, upright and in the same colours as the photo read in full and made as small:
    # softer, which the blur leaves out of the comparison
    small = images.load_pixels(path, 320)
    used = drafts[-1]
    with Image.open(path) as image:
        whole = images.upright_srgb(image)
    whole.thumbnail((320, 320), Image.Resampling.LANCZOS, reducing_gap=2.0)

    def blurred(image):
        return np.asarray(image.filter(ImageFilter.GaussianBlur(3)), dtype=float)

    assert used is not None, f"{path.name}: no thumbnail inside it was used"
    assert (small.width, small.height) == whole.size
    shown = Image.frombytes("RGB", (small.width, small.height), small.data)
    assert np.abs(blurred(shown) - blurred(whole)).mean() < 3, path.name


@needs_heic
@pytest.mark.parametrize("path", HEIC_FILES, ids=lambda p: p.name)
def test_a_heic_is_edited_at_its_full_size_upright(path):
    linear = source.load_linear(path)
    full = images.load_pixels(path, 320).full

    assert (linear.shape[1], linear.shape[0]) == full


@needs_heic
@pytest.mark.parametrize("path", HEIC_FILES, ids=lambda p: p.name)
def test_the_metadata_of_a_heic_is_read(path, exiftool):
    data = exiftool.read(path)

    assert data.captured is not None, path.name
    assert data.make and data.model, path.name
