import atexit
import gc
import os
import shutil
import tempfile

import pytest

# On CI exiftool is installed, and a test that needs it must not pass by being skipped
_REQUIRE_EXIFTOOL = os.environ.get("LUMINA_REQUIRE_EXIFTOOL") == "1"

# UI tests must not open windows on the developer's desktop
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Tests never touch the real home directory, its settings or a real photo library
_home = tempfile.mkdtemp(prefix="lumina-test-home-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["HOME"] = _home
os.environ["XDG_DATA_HOME"] = os.path.join(_home, ".local", "share")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, ".config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_home, ".cache")


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch, tmp_path_factory):
    """Each test its own cache folder, so thumbnails one made are not found by the next."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))


# Python's garbage collector runs in whichever thread allocates enough, and a Qt object
# whose wrapper it collects in a worker thread aborts the process. Collecting is for the
# main thread, after each test.
gc.disable()


@pytest.fixture(autouse=True)
def collect_garbage_on_the_main_thread():
    yield
    gc.collect()


@pytest.fixture
def exiftool():
    """A running ExifTool. Skipped where exiftool is not installed, failed on CI."""
    from lumina.core.metadata import ExifTool, MetadataUnavailable

    try:
        tool = ExifTool()
    except MetadataUnavailable as error:
        if _REQUIRE_EXIFTOOL:
            pytest.fail(f"LUMINA_REQUIRE_EXIFTOOL is set, but {error}")
        pytest.skip(str(error))
    yield tool
    tool.close()


@pytest.fixture
def drafts(monkeypatch):
    """What each `draft` of a HEIC chose: the thumbnail's (mode, box), or None for the
    photo itself."""
    from pillow_heif.as_plugin import HeifImageFile

    chosen = []
    draft = HeifImageFile.draft

    def spy(self, mode, size):
        result = draft(self, mode, size)
        chosen.append(result)
        return result

    monkeypatch.setattr(HeifImageFile, "draft", spy)
    return chosen
