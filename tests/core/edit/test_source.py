import numpy as np
import pytest
from photos import make_jpeg
from rawfiles import RAW_SIZE, make_dng

from lumina.core.edit import source
from lumina.core.edit.pipeline import srgb_decode
from lumina.core.errors import PhotoError


def test_a_jpeg_is_decoded_to_linear_16_bits(tmp_path):
    path = make_jpeg(tmp_path / "a.jpg", size=(40, 20), colour=(128, 128, 128))

    linear = source.load_linear(path)

    assert linear.shape == (20, 40, 3) and linear.dtype == np.uint16
    expected = srgb_decode(np.array(128 / 255)) * 65535
    assert abs(float(linear.mean()) - expected) < 0.02 * 65535


def test_the_exif_orientation_is_applied(tmp_path):
    assert source.load_linear(make_jpeg(tmp_path / "a.jpg", size=(40, 20), orientation=6)).shape[
        :2
    ] == (40, 20)


def test_a_raw_file_is_decoded_in_full_and_upright(tmp_path):
    width, height = RAW_SIZE

    plain = source.load_linear(make_dng(tmp_path / "a.dng"))
    turned = source.load_linear(make_dng(tmp_path / "b.dng", orientation=6))

    assert plain.dtype == np.uint16
    assert plain.shape[:2] == (height, width)
    assert turned.shape[:2] == (width, height)


def test_a_broken_file_is_an_error_naming_it(tmp_path):
    for name in ("broken.jpg", "broken.CR3"):
        path = tmp_path / name
        path.write_bytes(b"\x00" * 100)
        with pytest.raises(PhotoError) as raised:
            source.load_linear(path)
        assert raised.value.path == str(path)


def test_the_proxy_is_about_2_mp_and_keeps_the_brightness():
    image = np.full((3000, 2000, 3), 30000, dtype=np.uint16)

    proxy = source.make_proxy(image)

    assert proxy.dtype == np.float32
    assert proxy.shape[0] * proxy.shape[1] <= source.PROXY_PIXELS * 1.01
    assert proxy.shape[0] / proxy.shape[1] == pytest.approx(1.5, rel=0.01)
    assert proxy.mean() == pytest.approx(30000 / 65535, rel=1e-3)


def test_a_small_photo_is_its_own_proxy():
    image = np.full((100, 50, 3), 65535, dtype=np.uint16)

    assert source.make_proxy(image).shape == (100, 50, 3)


def test_fit_never_grows():
    assert source.fit(6000, 4000, pixels=2_400_000) == (1897, 1265)
    assert source.fit(6000, 4000, side=3000) == (3000, 2000)
    assert source.fit(300, 200, side=3000) == (300, 200)


def test_the_source_is_decoded_once(tmp_path, monkeypatch):
    path = make_jpeg(tmp_path / "a.jpg")
    calls = []
    real = source.load_linear
    monkeypatch.setattr(source, "load_linear", lambda p: calls.append(p) or real(p))
    opened = source.EditSource(path)

    opened.full, opened.proxy, opened.full, opened.size()

    assert calls == [path]
