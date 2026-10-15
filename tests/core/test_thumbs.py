import os
import shutil
import time

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


def _fake_thumbnail(cache_root, name, size, age):
    """A file of `size` bytes in the cache, last used `age` seconds ago."""
    path = thumbs.ThumbnailCache(cache_root).root / name[:2] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    used = time.time() - age
    os.utime(path, (used, used))
    return path


def test_the_cache_is_left_alone_within_its_limit(cache_root):
    paths = [_fake_thumbnail(cache_root, f"aa{n}-320.webp", 1000, n) for n in range(5)]

    result = thumbs.prune(cache_root, 5000)

    assert (result.removed, result.freed, result.remaining) == (0, 0, 5000)
    assert all(path.exists() for path in paths)
    assert thumbs.prune(cache_root, None).removed == 0
    assert thumbs.cache_size(cache_root) == 5000


def test_over_the_limit_the_thumbnails_used_longest_ago_go_first_down_to_90_percent(cache_root):
    # n 0 is the newest
    paths = [_fake_thumbnail(cache_root, f"aa{n:02}-320.webp", 1000, 100 * n) for n in range(10)]

    result = thumbs.prune(cache_root, 8000)

    # 10 000 bytes to 7200 or less: three go, the three used longest ago
    assert (result.removed, result.freed, result.remaining) == (3, 3000, 7000)
    assert [path.exists() for path in paths] == [True] * 7 + [False] * 3
    assert thumbs.cache_size(cache_root) == 7000


def test_thumbnails_of_photos_that_are_not_connected_go_last(cache_root):
    offline = "bb" + "0" * 30
    online = "cc" + "0" * 30
    # The offline ones are the oldest, and an edited one's name has the edit in it
    old = [_fake_thumbnail(cache_root, f"{offline}-{side}.webp", 1000, 9000) for side in (160, 320)]
    old.append(_fake_thumbnail(cache_root, f"{offline}-ed1234-320.webp", 1000, 9000))
    new = [_fake_thumbnail(cache_root, f"{online}-{side}.webp", 1000, 10) for side in (160, 320)]

    result = thumbs.prune(cache_root, 5000, keep={offline})

    # 5000 bytes is not over 5000, so lower the limit: 3 of 5 may stay (90 % of 3500)
    assert result.removed == 0
    result = thumbs.prune(cache_root, 3500, keep={offline})
    assert result.removed == 2 and result.remaining == 3000
    assert not any(path.exists() for path in new)
    assert all(path.exists() for path in old)

    # Only when there is nothing else do they go, the oldest first
    thumbs.prune(cache_root, 1100, keep={offline})
    assert thumbs.cache_size(cache_root) <= 1000


def test_only_thumbnails_are_deleted(cache_root, tmp_path):
    thumbnail = _fake_thumbnail(cache_root, "aa01-320.webp", 1000, 500)
    note = _fake_thumbnail(cache_root, "aa02-notes.txt", 1000, 9000)
    stale = _fake_thumbnail(cache_root, "aa03-320.webp.part", 100, 9000)
    writing = _fake_thumbnail(cache_root, "aa04-320.webp.part", 100, 5)
    beside = tmp_path / "cache" / "unrelated.webp"
    beside.write_bytes(b"x" * 5000)
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"x" * 5000)

    result = thumbs.prune(cache_root, 1)

    assert not thumbnail.exists() and not stale.exists()
    assert note.exists() and writing.exists() and beside.exists() and photo.exists()
    assert result.removed == 2


def test_a_missing_cache_is_empty(cache_root):
    assert thumbs.prune(cache_root, 100) == thumbs.Pruned(0, 0, 0)
    assert thumbs.cache_size(cache_root) == 0


def test_a_thumbnail_read_is_noted_for_pruning_but_not_every_time(photos, cache_root):
    path = make_jpeg(photos / "a.jpg", size=(640, 320))
    thumbs.load_thumbnail(path, 160, cache_root)
    cached = next(thumbs.ThumbnailCache(cache_root).root.rglob("*.webp"))

    old = time.time() - 10 * 86400
    os.utime(cached, (old, old))
    thumbs.load_thumbnail(path, 160, cache_root)
    assert time.time() - cached.stat().st_mtime < 60

    # Read again at once: not written again
    recent = time.time() - 600
    os.utime(cached, (recent, recent))
    thumbs.load_thumbnail(path, 160, cache_root)
    assert cached.stat().st_mtime == pytest.approx(recent, abs=1)


def test_what_was_read_last_is_what_pruning_keeps(photos, cache_root):
    first = make_jpeg(photos / "a.jpg", size=(640, 320))
    second = make_jpeg(photos / "b.jpg", size=(320, 640), colour=(10, 200, 10))
    for path in (first, second):
        thumbs.load_thumbnail(path, 160, cache_root)
    files = {p.name: p for p in thumbs.ThumbnailCache(cache_root).root.rglob("*.webp")}
    for path in files.values():
        old = time.time() - 10 * 86400
        os.utime(path, (old, old))
    # The first is looked at again
    thumbs.load_thumbnail(first, 160, cache_root)
    total = thumbs.cache_size(cache_root)

    # Just over the limit: the one used longest ago goes
    thumbs.prune(cache_root, total - 1)

    remaining = [p for p in files.values() if p.exists()]
    assert len(remaining) == 1
    assert remaining[0].name.startswith(content_id(first))
