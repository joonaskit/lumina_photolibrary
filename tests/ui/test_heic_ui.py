"""A HEIC in the library is listed, has a thumbnail and opens in the viewer."""

from conftest import TIMEOUT
from photos import make_heic
from PySide6.QtCore import Qt

from lumina.ui.main_window import MainWindow


def test_a_heic_is_listed_has_a_thumbnail_and_opens(qtbot, service, drive):
    make_heic(drive / "Photos" / "phone.heic", size=(160, 120), colour=(20, 150, 40), orientation=6)
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.resize(1000, 700)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.browser.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    names = [window.model.name(row) for row in range(window.model.rowCount())]
    assert "phone.heic" in names
    row = names.index("phone.heic")

    window.model.data(window.model.index(row), Qt.ItemDataRole.DecorationRole)
    qtbot.waitUntil(lambda: window.model.thumbnail_pixmap(row) is not None, timeout=TIMEOUT)
    thumbnail = window.model.thumbnail_pixmap(row)
    # Stored 160 x 120 and turned a quarter: upright it is portrait
    assert thumbnail.height() > thumbnail.width()

    window.nav.show_photo(row)
    qtbot.waitUntil(
        lambda: window.viewer.pixmap() is not None and window.viewer.pixmap().width() == 120,
        timeout=TIMEOUT,
    )
    assert window.viewer.pixmap().height() == 160
