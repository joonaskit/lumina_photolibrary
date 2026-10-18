import pytest
from fakes import FakeExifTool
from photos import make_jpeg
from rawfiles import make_dng

from lumina.core import volumes
from lumina.integration import style, theme

TIMEOUT = 20000
DRIVE = "uuid:TEST-DRIVE"


def written(write, *args, **kwargs):
    """What a LibraryService write that tells through `on_done` gave, once it is made."""
    results = []
    write(*args, on_done=results.append, **kwargs)
    write.__self__.flush()
    [result] = results
    return result


@pytest.fixture(autouse=True)
def themed_app(qapp):
    style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))
    return qapp


@pytest.fixture(autouse=True)
def fresh_settings():
    """Nothing remembered from an earlier test."""
    from lumina.ui import settings

    settings.clear()
    yield
    settings.clear()


@pytest.fixture(autouse=True)
def comfortable_density():
    """The density is the app's, not a window's: back to the default after a test."""
    yield
    style.set_density("comfortable")


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeExifTool.reset()


@pytest.fixture(scope="session", autouse=True)
def stop_workers():
    from lumina.qt_models import workers

    yield
    workers.shutdown()


@pytest.fixture
def drive(tmp_path):
    """A "USB drive" with a Photos folder of three: a folder that `mounts` says is the
    volume DRIVE while it is plugged in."""
    path = tmp_path / "drive"
    (path / "Photos").mkdir(parents=True)
    make_jpeg(path / "Photos" / "a.jpg", size=(300, 200))
    make_jpeg(path / "Photos" / "b.jpg", size=(200, 300), colour=(10, 200, 10))
    make_dng(path / "Photos" / "c.dng", preview="jpeg", preview_size=(240, 160))
    return path


class Mounts:
    """What the service reads as the system's mounts: this machine's, plus the drive
    while it is plugged in."""

    def __init__(self, drive):
        self.drive = drive
        self.plugged = True

    def __call__(self):
        found = volumes.read_mounts()
        if self.plugged:
            found.append(
                volumes.Mount(DRIVE, str(self.drive), "/", "exfat", "/dev/sdz1", "Photos drive")
            )
        return found


@pytest.fixture
def mounts(drive):
    return Mounts(drive)


@pytest.fixture
def make_service(tmp_path, mounts, qtbot):
    from lumina.qt_models.library import LibraryService

    made = []

    def make(start=True):
        service = LibraryService(
            tmp_path / "library.sqlite3", read_mounts=mounts, exiftool=FakeExifTool, mounts_ms=50
        )
        made.append(service)
        if start:
            service.start()
        return service

    yield make
    for service in made:
        service.close()
        if service.jobs.future is not None:
            service.jobs.future.result(timeout=TIMEOUT / 1000)


@pytest.fixture(autouse=True)
def windows_closed_before_services(qtbot, make_service):
    """Windows are closed, and the results workers have delivered handled, while the
    services' databases are still open. Set up after `make_service`, so torn down
    before it: a result arriving after the database is closed raises in the event loop."""
    yield
    from PySide6.QtWidgets import QApplication

    for widget in QApplication.topLevelWidgets():
        widget.close()
    qtbot.wait(20)


@pytest.fixture
def service(make_service):
    return make_service()


@pytest.fixture
def wait_idle(qtbot):
    def wait(service):
        qtbot.waitUntil(lambda: not service.is_busy(), timeout=TIMEOUT)

    return wait
