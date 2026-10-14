import os
import shutil

import pytest
from photos import make_jpeg
from rawfiles import make_dng

from lumina.core import images, thumbs
from lumina.core.errors import PhotoError
from lumina.core.fingerprint import content_id


@pytest.fixture
def cache_root(tmp_path):
    return tmp_path / "cache"


@pytest.fixture
def photos(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    return folder


def _no_decoding(monkeypatch):
    def fail(path, _side):
        raise AssertionError(f"decoded {path} again")

    monkeypatch.setattr(images, "load_pixels", fail)


def test_a_thumbnail_is_made_once_and_then_read_from_the_cache(photos, cache_root, monkeypatch):
    path = make_jpeg(photos / "a.jpg", size=(640, 320))

    made = thumbs.load_thumbnail(path, 160, cache_root)
    _no_decoding(monkeypatch)
    again = thumbs.load_thumbnail(path, 160, cache_root)

    assert (made.width, made.height) == (160, 80)
    assert (again.width, again.height) == (160, 80)
    assert thumbs.ThumbnailCache(cache_root).path(content_id(path), 160).is_file()


def test_raw_thumbnails_are_cached_too(photos, cache_root, monkeypatch):
    path = make_dng(photos / "a.dng", preview="jpeg", preview_size=(400, 200))

    thumbs.load_thumbnail(path, 100, cache_root)
    _no_decoding(monkeypatch)

    assert thumbs.load_thumbnail(path, 100, cache_root).width == 100


def test_a_moved_photo_keeps_its_thumbnail(photos, cache_root, monkeypatch, tmp_path):
    path = make_jpeg(photos / "a.jpg")
    thumbs.load_thumbnail(path, 160, cache_root)
    elsewhere = tmp_path / "other drive"
    elsewhere.mkdir()
    moved = shutil.copy2(path, elsewhere / "renamed.jpg")

    _no_decoding(monkeypatch)

    assert thumbs.load_thumbnail(moved, 160, cache_root).width == 64


def test_a_changed_photo_gets_a_new_thumbnail(photos, cache_root):
    path = make_jpeg(photos / "a.jpg", size=(64, 32))
    first = content_id(path)
    thumbs.load_thumbnail(path, 160, cache_root)

    make_jpeg(path, size=(32, 64))

    assert content_id(path) != first
    assert (thumbs.load_thumbnail(path, 160, cache_root).height) == 64


def test_a_damaged_cache_file_is_replaced(photos, cache_root):
    path = make_jpeg(photos / "a.jpg")
    thumbs.load_thumbnail(path, 160, cache_root)
    cached = thumbs.ThumbnailCache(cache_root).path(content_id(path), 160)
    cached.write_bytes(b"RIFF broken")

    assert thumbs.load_thumbnail(path, 160, cache_root).width == 64
    assert thumbs.ThumbnailCache(cache_root).get(content_id(path), 160) is not None


def test_a_cache_that_cannot_be_written_still_gives_thumbnails(photos, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    path = make_jpeg(photos / "a.jpg")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert thumbs.load_thumbnail(path, 160, locked / "cache").width == 64
    finally:
        locked.chmod(0o700)


def test_nothing_is_written_beside_the_photos(photos, cache_root):
    make_jpeg(photos / "a.jpg")
    make_dng(photos / "b.dng", preview="jpeg")
    before = sorted(p.name for p in photos.iterdir())

    for path in list(photos.iterdir()):
        thumbs.load_thumbnail(path, 160, cache_root)

    assert sorted(p.name for p in photos.iterdir()) == before
    assert not list(cache_root.rglob("*.part"))


def test_invalidate_forgets_every_size(photos, cache_root):
    path = make_jpeg(photos / "a.jpg")
    for side in (64, 160):
        thumbs.load_thumbnail(path, side, cache_root)
    cache, cid = thumbs.ThumbnailCache(cache_root), content_id(path)

    cache.invalidate(cid)

    assert cache.get(cid, 64) is None and cache.get(cid, 160) is None


def test_an_unreadable_photo_is_an_error_naming_it(photos, cache_root):
    with pytest.raises(PhotoError) as raised:
        thumbs.load_thumbnail(photos / "gone.jpg", 160, cache_root)

    assert raised.value.path == str(photos / "gone.jpg")


def test_the_default_folder_follows_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    assert thumbs.default_cache_dir() == str(tmp_path / "lumina" / "thumbnails")
