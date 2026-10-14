from photos import make_jpeg
from PySide6.QtCore import Qt

from lumina.ui import settings
from lumina.ui.main_window import MainWindow

TIMEOUT = 20000


def make_folder(tmp_path):
    make_jpeg(tmp_path / "a.jpg", size=(300, 200))
    make_jpeg(tmp_path / "b.jpg", size=(200, 300), colour=(10, 200, 10))
    (tmp_path / "broken.jpg").write_bytes(b"not a photo")
    return tmp_path


def test_starts_empty(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    assert window.pages.currentIndex() == 0
    assert window.model.rowCount() == 0
    assert not window.navigation.isVisible()


def test_a_folder_fills_the_grid_and_its_thumbnails_arrive(qtbot, tmp_path):
    folder = make_folder(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    window.open_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_scanning(), timeout=TIMEOUT)

    assert window.model.rowCount() == 3
    assert window.status.text() == "3 photos"
    assert window.windowTitle() == f"{tmp_path.name} — Lumina"
    assert settings.last_folder() == str(folder)
    # Asking for each row's picture starts its thumbnail
    for row in range(3):
        window.model.data(window.model.index(row), Qt.ItemDataRole.DecorationRole)
    qtbot.waitUntil(lambda: window.model.thumbnail_pixmap(0) is not None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: window.model.thumbnail_pixmap(1) is not None, timeout=TIMEOUT)
    assert window.model.thumbnail_pixmap(1).height() > window.model.thumbnail_pixmap(1).width()


def test_a_broken_photo_is_reported_by_name(qtbot, tmp_path):
    folder = make_folder(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window.open_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_scanning(), timeout=TIMEOUT)
    row = [window.model.path(r) for r in range(3)].index(str(folder / "broken.jpg"))

    with qtbot.waitSignal(window.model.failed, timeout=TIMEOUT) as failed:
        window.model.data(window.model.index(row), Qt.ItemDataRole.DecorationRole)

    assert failed.args[0] == str(folder / "broken.jpg")
    tooltip = window.model.data(window.model.index(row), Qt.ItemDataRole.ToolTipRole)
    assert str(folder / "broken.jpg") in tooltip


def test_the_viewer_shows_a_photo_and_steps_through_the_folder(qtbot, tmp_path):
    folder = make_folder(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.open_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_scanning(), timeout=TIMEOUT)

    window.show_photo(0)
    qtbot.waitUntil(
        lambda: window.viewer.pixmap() is not None and window.viewer.pixmap().width() == 300,
        timeout=TIMEOUT,
    )
    assert window.pages.currentIndex() == 2
    assert window.navigation.isVisible()

    window.step(1)
    qtbot.waitUntil(
        lambda: window.viewer.pixmap() is not None and window.viewer.pixmap().height() == 300,
        timeout=TIMEOUT,
    )

    window.step(1)
    qtbot.waitUntil(lambda: "broken.jpg" in window.viewer.message(), timeout=TIMEOUT)

    window.show_grid()
    assert window.pages.currentIndex() == 1
    assert not window.navigation.isVisible()
    assert window.grid.currentIndex().row() == 2


def test_cells_keep_the_full_size_while_thumbnails_are_placeholders(qtbot, tmp_path):
    # A placeholder icon only made in small sizes must not shrink the cells: with
    # uniform item sizes the first row's size is every row's, and the thumbnails that
    # come later would be cut to a strip
    from PySide6.QtGui import QIcon, QPixmap

    small = QPixmap(32, 32)
    small.fill()
    folder = make_folder(tmp_path)
    window = MainWindow()
    window.model._placeholder_icon = lambda: QIcon(small)
    qtbot.addWidget(window)
    window.show()
    window.open_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_scanning(), timeout=TIMEOUT)

    rect = window.grid.visualRect(window.model.index(0))

    assert rect.height() >= window.grid.iconSize().height()
