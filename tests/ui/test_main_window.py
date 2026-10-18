from conftest import TIMEOUT
from fakes import FakeExifTool
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QMessageBox, QToolButton
from rawfiles import make_dng

from lumina.core.dbtypes import Photo
from lumina.qt_models.library_model import DAMAGE_ROLE
from lumina.ui import settings
from lumina.ui.main_window import MainWindow, describe_photo


def open_window(qtbot, service):
    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    return window


def add_and_wait(qtbot, window, folder):
    with qtbot.waitSignal(window.service.job_ended, timeout=TIMEOUT):
        window.add_folder(folder)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)


def ask_for_thumbnails(window):
    for row in range(window.model.rowCount()):
        window.model.data(window.model.index(row), Qt.ItemDataRole.DecorationRole)


def test_an_empty_library_asks_for_a_folder(qtbot, service):
    window = open_window(qtbot, service)

    assert window.pages.currentIndex() == 0
    assert window.sidebar.list.count() == 1
    assert not window.navigation.isVisible()


def test_an_added_folder_fills_the_grid(qtbot, service, drive):
    window = open_window(qtbot, service)

    add_and_wait(qtbot, window, drive / "Photos")

    assert window.pages.currentIndex() == 1
    assert window.model.rowCount() == 3
    assert window.status.text() == "3 photos"
    assert window.windowTitle() == "Photos — Lumina"
    assert window.sidebar.current_folder() is not None
    assert "Scanned Photos: 3 photos, 3 new" in window.job_status.label.text()
    ask_for_thumbnails(window)
    qtbot.waitUntil(lambda: window.model.thumbnail_pixmap(0) is not None, timeout=TIMEOUT)


def test_the_filters_offer_what_there_is_and_narrow_the_grid(qtbot, service, drive):
    FakeExifTool.models = {"b.jpg": "Canon EOS R6"}
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")
    camera = window.filters.camera

    assert [camera.itemText(i) for i in range(camera.count())] == [
        "All cameras",
        "Canon EOS 90D",
        "Canon EOS R6",
    ]
    camera.setCurrentIndex(camera.findData("Canon EOS R6"))
    qtbot.waitUntil(lambda: window.model.rowCount() == 1, timeout=TIMEOUT)
    assert window.model.name(0) == "b.jpg"


def test_all_photos_and_a_folder(qtbot, service, drive, tmp_path):
    from photos import make_jpeg

    other = tmp_path / "other"
    other.mkdir()
    make_jpeg(other / "x.jpg")
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")
    add_and_wait(qtbot, window, other)

    window.sidebar.select(None)
    qtbot.waitUntil(lambda: window.model.rowCount() == 4, timeout=TIMEOUT)
    assert window.windowTitle() == "All Photos — Lumina"
    assert settings.selected_folder() is None


def test_the_viewer_shows_a_photo_and_what_it_was_taken_with(qtbot, service, drive):
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")
    row = next(r for r in range(3) if window.model.name(r) == "a.jpg")

    window.show_photo(row)
    qtbot.waitUntil(
        lambda: window.viewer.pixmap() is not None and window.viewer.pixmap().width() == 300,
        timeout=TIMEOUT,
    )

    assert window.navigation.isVisible()
    assert "Canon EOS 90D" in window.status.text()
    assert "EF-S55-250mm" in window.status.text()
    window.show_grid()
    assert window.grid.currentIndex().row() == row


def test_photos_on_an_unplugged_drive_keep_their_thumbnails(qtbot, service, drive, mounts):
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")
    ask_for_thumbnails(window)
    qtbot.waitUntil(
        lambda: all(window.model.thumbnail_pixmap(r) is not None for r in range(3)),
        timeout=TIMEOUT,
    )

    mounts.plugged = False
    qtbot.waitUntil(lambda: window.model.path(0) is None, timeout=TIMEOUT)
    ask_for_thumbnails(window)

    qtbot.waitUntil(
        lambda: all(window.model.thumbnail_pixmap(r) is not None for r in range(3)),
        timeout=TIMEOUT,
    )
    assert window.model.rowCount() == 3
    tooltip = window.model.data(window.model.index(0), Qt.ItemDataRole.ToolTipRole)
    assert "not connected" in tooltip
    folder_item = window.sidebar.list.item(1)
    assert "not connected" in folder_item.toolTip()
    window.show_photo(0)
    assert "(not connected)" in window.status.text()


def test_a_photo_exiftool_warns_of_damage_in_is_marked_in_the_grid(qtbot, service, drive):
    make_dng(drive / "Photos" / "d.cr3", preview="jpeg", preview_size=(240, 160))
    FakeExifTool.warnings = {"d.cr3": "Truncated CTMD record"}
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")
    names = [window.model.name(row) for row in range(window.model.rowCount())]
    damaged, fine = names.index("d.cr3"), names.index("a.jpg")

    def data(row, role):
        return window.model.data(window.model.index(row), role)

    assert "Truncated CTMD record" in data(damaged, DAMAGE_ROLE)
    assert "may be damaged" in data(damaged, Qt.ItemDataRole.ToolTipRole)
    assert data(fine, DAMAGE_ROLE) is None
    assert "damaged" not in data(fine, Qt.ItemDataRole.ToolTipRole)
    # The badge is drawn without trouble
    ask_for_thumbnails(window)
    qtbot.waitUntil(lambda: window.model.thumbnail_pixmap(damaged) is not None, timeout=TIMEOUT)
    assert not window.grid.grab().isNull()
    window.show_photo(damaged)
    assert "Raw data may be damaged" in window.status.text()
    window.show_photo(fine)
    assert "damaged" not in window.status.text()


def test_removing_a_folder_asks_first(qtbot, service, drive, monkeypatch):
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")
    folder = window.sidebar.current_folder()

    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Cancel)
    window.remove_folder(folder)
    assert len(service.folders()) == 1

    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Ok)
    window.remove_folder(folder)
    qtbot.waitUntil(lambda: window.pages.currentIndex() == 0, timeout=TIMEOUT)
    assert sorted(p.name for p in (drive / "Photos").iterdir()) == ["a.jpg", "b.jpg", "c.dng"]


def test_a_folder_that_cannot_be_added_is_explained(qtbot, service, tmp_path, monkeypatch):
    told = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, title, text: told.append(text))
    window = open_window(qtbot, service)

    assert window.add_folder(tmp_path / "missing") is None
    assert "Not a folder" in told[0]


def test_cells_keep_the_full_size_while_thumbnails_are_placeholders(qtbot, service, drive):
    # A placeholder icon only made in small sizes must not shrink the cells: with
    # uniform item sizes the first row's size is every row's, and the thumbnails that
    # come later would be cut to a strip
    small = QPixmap(32, 32)
    small.fill()
    window = open_window(qtbot, service)
    window.model.thumbnails._placeholder_icon = lambda: QIcon(small)
    add_and_wait(qtbot, window, drive / "Photos")

    rect = window.grid.visualRect(window.model.index(0))

    assert rect.height() >= window.grid.iconSize().height()


def test_describe_photo():
    photo = Photo(
        1,
        captured="2022-05-09 21:03:25.960000",
        model="Canon EOS 90D",
        lens="EF-S55-250mm f/4-5.6 IS STM",
        focal_length=250.0,
        exposure_time=0.004,
        aperture=5.6,
        iso=400,
    )

    assert describe_photo(photo) == (
        "2022-05-09 21:03  ·  Canon EOS 90D  ·  EF-S55-250mm f/4-5.6 IS STM  ·  250 mm"
        "  ·  1/250 s  ·  f/5.6  ·  ISO 400"
    )
    assert describe_photo(None) == ""


def test_a_selected_thumbnail_keeps_its_colours(qtbot, service, drive):
    # The style tints a selected icon with the highlight colour unless it has its own
    window = open_window(qtbot, service)
    add_and_wait(qtbot, window, drive / "Photos")
    ask_for_thumbnails(window)
    qtbot.waitUntil(lambda: window.model.thumbnail_pixmap(0) is not None, timeout=TIMEOUT)
    icon = window.model.data(window.model.index(0), Qt.ItemDataRole.DecorationRole)

    normal = icon.pixmap(64, QIcon.Mode.Normal).toImage()
    selected = icon.pixmap(64, QIcon.Mode.Selected).toImage()

    assert normal == selected


def test_folders_are_added_from_the_sidebar_and_with_ctrl_o(qtbot, service, monkeypatch):
    window = open_window(qtbot, service)
    asked = []
    monkeypatch.setattr(window, "choose_folder", lambda: asked.append(True))
    window.add_action.triggered.disconnect()
    window.add_action.triggered.connect(window.choose_folder)

    toolbar_buttons = [
        button.defaultAction()
        for button in window.findChildren(QToolButton)
        if button.defaultAction() is not None and not window.sidebar.isAncestorOf(button)
    ]
    assert window.add_action not in toolbar_buttons
    assert window.sidebar.add_button.isVisibleTo(window)
    assert window.add_action in window.actions()
    window.add_action.trigger()
    assert asked == [True]


def test_dialogs_come_from_the_desktop_unless_a_theme_was_chosen():
    from lumina.application import use_desktop_dialogs

    environ = {}
    use_desktop_dialogs(environ, "linux")
    assert environ == {"QT_QPA_PLATFORMTHEME": "xdgdesktopportal"}

    chosen = {"QT_QPA_PLATFORMTHEME": "gtk3"}
    use_desktop_dialogs(chosen, "linux")
    assert chosen == {"QT_QPA_PLATFORMTHEME": "gtk3"}

    elsewhere = {}
    use_desktop_dialogs(elsewhere, "darwin")
    assert elsewhere == {}
