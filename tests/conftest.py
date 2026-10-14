import atexit
import gc
import os
import shutil
import tempfile

import pytest

# UI tests must not open windows on the developer's desktop
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Tests never touch the real home directory, its settings or a real photo library
_home = tempfile.mkdtemp(prefix="lumina-test-home-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["HOME"] = _home
os.environ["XDG_DATA_HOME"] = os.path.join(_home, ".local", "share")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, ".config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_home, ".cache")


# Python's garbage collector runs in whichever thread allocates enough, and a Qt object
# whose wrapper it collects in a worker thread aborts the process. Collecting is for the
# main thread, after each test.
gc.disable()


@pytest.fixture(autouse=True)
def collect_garbage_on_the_main_thread():
    yield
    gc.collect()
