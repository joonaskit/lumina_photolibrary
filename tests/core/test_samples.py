"""Real camera files, when there are some: tests/samples/ or $LUMINA_SAMPLES.

They are not in git (a CR3 is 20 to 50 MB), so these tests run on the machines
that have them and are skipped elsewhere. Put a few files from each camera body
in there, ideally with both orientations.
"""

import hashlib
import os
from pathlib import Path

import pytest

from lumina.core import images, raw

SAMPLES = Path(os.environ.get("LUMINA_SAMPLES") or Path(__file__).parent.parent / "samples")
FILES = sorted(p for p in SAMPLES.glob("*") if images.is_photo(p.name)) if SAMPLES.is_dir() else []
RAW_FILES = [p for p in FILES if raw.is_raw(p)]

needs_raw = pytest.mark.skipif(not RAW_FILES, reason=f"no RAW samples in {SAMPLES}")


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


@needs_raw
def test_samples_are_left_as_they_were():
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in RAW_FILES}

    for path in RAW_FILES:
        images.load_pixels(path, 320)

    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in RAW_FILES} == before
