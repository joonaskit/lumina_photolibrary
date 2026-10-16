import os
from dataclasses import replace

import numpy as np
import pytest
from photos import make_jpeg
from PIL import Image

from lumina.core import images, linear_tiff
from lumina.core.edit import pipeline, preview, source
from lumina.core.edit.params import Params
from lumina.core.errors import PhotoError


def written(path, image, tags=None, order=None):
    """`image` (H x W x 3 floats) written to `path` as a linear TIFF, its rows in bands
    of 5 in `order` (all bands, any order)."""
    height, width = image.shape[:2]
    layout = linear_tiff.layout(width, height, tags)
    bands = list(range(0, height, 5))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        linear_tiff.start(fd, layout)
        for first in order(bands) if order else bands:
            linear_tiff.write_rows(fd, layout, first, image[first : first + 5])
    finally:
        os.close(fd)
    return path


def light(width=37, height=23):
    rng = np.random.default_rng(3)
    # Deep shadow to well past white
    return (rng.random((height, width, 3)) * 2.0 ** rng.uniform(-8, 4, (height, width, 1))).astype(
        np.float32
    )


def test_light_past_white_is_kept_to_a_half_float(tmp_path):
    image = light()
    path = written(tmp_path / "merge.tif", image)

    back = linear_tiff.read(path)

    assert back.dtype == np.float16 and back.shape == image.shape
    assert back.max() > 4
    np.testing.assert_allclose(back.astype(np.float32), image, rtol=1e-3, atol=1e-7)


def test_rows_can_be_written_in_any_order(tmp_path):
    image = light()
    path = written(tmp_path / "merge.tif", image, order=lambda bands: bands[::-1])

    back = linear_tiff.read(path).astype(np.float32)
    np.testing.assert_allclose(back, image, rtol=1e-3, atol=1e-7)
    assert os.path.getsize(path) == linear_tiff.layout(37, 23).size


def test_a_smaller_copy_takes_every_nth_row_and_column(tmp_path):
    image = light()
    path = written(tmp_path / "merge.tif", image)

    small = linear_tiff.read(path, step=3)

    np.testing.assert_allclose(small.astype(np.float32), image[::3, ::3], rtol=1e-3, atol=1e-7)
    assert linear_tiff.size(path) == (37, 23)


def test_only_float_tiffs_are_linear(tmp_path):
    path = written(tmp_path / "merge.tif", light())
    Image.new("RGB", (8, 8)).save(tmp_path / "plain.tif")
    make_jpeg(tmp_path / "photo.jpg")

    assert linear_tiff.is_linear(path)
    assert not linear_tiff.is_linear(tmp_path / "plain.tif")
    assert not linear_tiff.is_linear(tmp_path / "photo.jpg")
    assert not linear_tiff.is_linear(tmp_path / "gone.tif")


def test_a_file_cut_short_is_an_error_naming_it(tmp_path):
    path = written(tmp_path / "merge.tif", light())
    with open(path, "r+b") as file:
        file.truncate(os.path.getsize(path) - 100)

    with pytest.raises(PhotoError) as raised:
        linear_tiff.read(path)
    assert raised.value.path == str(path)


def test_the_editor_decodes_it_as_light_past_white(tmp_path):
    image = light()
    path = written(tmp_path / "merge.tif", image)

    decoded = source.load_linear(path)
    proxy = source.make_proxy(decoded, pixels=200)

    assert decoded.dtype == np.float16 and decoded.max() > 4
    # Made smaller, the light past white is still there to bring back
    assert proxy.max() > 1


def test_exposure_brings_back_what_was_past_white(tmp_path):
    image = np.full((20, 30, 3), 0.18, np.float32)
    # Two stops past white: white unedited, light grey two stops down
    image[:, 15:] = 4.0 * 0.5
    path = written(tmp_path / "merge.tif", image)
    linear = source.load_linear(path)

    plain = pipeline.render(linear, Params())
    darker = pipeline.render(linear, replace(Params(), exposure=-2.0))

    assert plain[0, -1].tolist() == [255, 255, 255]
    # 0.5 linear is 188 encoded
    assert abs(int(darker[0, -1, 0]) - 188) <= 1


def test_the_viewer_is_told_its_full_size_from_a_smaller_read(tmp_path):
    path = written(tmp_path / "merge.tif", light())

    pixels = preview.edited_pixels(path, 10, Params().to_json())

    assert pixels.full == (37, 23)


def test_the_grid_shows_it_clipped_at_white(tmp_path):
    image = np.full((20, 30, 3), 0.18, np.float32)
    image[:, 15:] = 8.0
    path = written(tmp_path / "merge.tif", image)

    pixels = images.load_pixels(path, 15)

    rgb = np.frombuffer(pixels.data, np.uint8).reshape(pixels.height, pixels.width, 3)
    assert pixels.full == (30, 20)
    # Middle grey encodes to about 118; past white is white
    assert abs(int(rgb[0, 0, 0]) - 118) <= 1 and rgb[0, -1].tolist() == [255, 255, 255]


def test_exiftool_reads_what_it_says_of_itself(tmp_path, exiftool):
    tags = linear_tiff.Tags(
        description="HDR merge of 3 photos",
        make="Canon",
        model="Canon EOS 90D",
        lens="EF-S10-18mm",
        captured="2026:10:16 14:03:05",
    )
    path = written(tmp_path / "merge.tif", light(), tags)

    data = exiftool.read(str(path))

    assert data.model == "Canon EOS 90D" and data.make == "Canon"
    assert data.lens == "EF-S10-18mm"
    assert data.captured.isoformat() == "2026-10-16T14:03:05"
    assert data.file_type == "TIFF"
