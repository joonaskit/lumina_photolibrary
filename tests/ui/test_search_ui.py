import pytest
from conftest import TIMEOUT
from fakes import FakeEncoder, encode_colour_text, install_model
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from lumina.qt_models.search import SearchService
from lumina.ui.main_window import MainWindow


@pytest.fixture
def model_root(tmp_path):
    return tmp_path / "models"


@pytest.fixture
def open_window(qtbot, make_service, wait_idle, model_root, drive):
    made = []

    def open_(installed=True):
        if installed:
            install_model(model_root)
        service = make_service(start=False)
        encoder = FakeEncoder()
        search = SearchService(
            service,
            model_root,
            encode=lambda _folder, paths: encoder(paths),
            encode_text=encode_colour_text,
        )
        made.append(search)
        window = MainWindow(service, search)
        qtbot.addWidget(window)
        window.show()
        service.start()
        search.start()
        window.add_folder(str(drive / "Photos"))
        wait_idle(service)
        if installed:
            qtbot.waitUntil(lambda: search.index_future is not None, timeout=TIMEOUT)
            qtbot.waitUntil(lambda: not search.is_indexing(), timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
        return window

    yield open_
    for search in made:
        search.close()
        if search.index_future is not None:
            search.index_future.result(timeout=TIMEOUT / 1000)


def names(window):
    return [window.model.name(row) for row in range(window.model.rowCount())]


def type_search(qtbot, window, text):
    window.search_field.edit.setText(text)
    window.search_field.edit.returnPressed.emit()
    qtbot.waitUntil(lambda: window.model.search is not None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)


def test_a_text_search_ranks_the_grid_and_says_so(qtbot, open_window):
    window = open_window()

    type_search(qtbot, window, "green")

    assert names(window)[0] == "b.jpg"
    assert window.status.text() == "3 photos match “green”"


def test_typing_searches_after_a_pause(qtbot, open_window):
    window = open_window()

    qtbot.keyClicks(window.search_field.edit, "red")
    qtbot.waitUntil(lambda: window.model.search is not None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert names(window)[0] == "a.jpg"


def test_the_filters_still_apply_to_a_search(qtbot, open_window):
    window = open_window()
    type_search(qtbot, window, "red")

    # Choosing the year again reads the rows again, and the search runs on them
    window.filters.year.setCurrentIndex(1)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert window.model.search is not None and names(window)[0] == "a.jpg"


def test_escape_and_the_clear_button_show_every_photo_again(qtbot, open_window):
    window = open_window()
    before = names(window)
    type_search(qtbot, window, "green")

    qtbot.keyClick(window.search_field.edit, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert window.model.search is None and names(window) == before
    assert window.search_field.text() == ""

    type_search(qtbot, window, "green")
    window.search_field.edit.clear()
    qtbot.waitUntil(lambda: window.model.search is None, timeout=TIMEOUT)


def test_find_similar_shows_a_chip_that_ends_it(qtbot, open_window):
    window = open_window()
    row = names(window).index("a.jpg")
    window.grid.setCurrentIndex(window.model.index(row))

    window.similar_action.trigger()
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert names(window)[0] == "a.jpg"
    assert window.search_field.chip.isVisible()
    assert "Like a.jpg" in window.search_field.chip.text()
    assert window.status.text() == "3 photos like a.jpg"

    window.search_field.chip.click()
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    assert window.model.search is None and not window.search_field.chip.isVisible()


def test_find_similar_from_the_viewer_goes_back_to_the_grid(qtbot, open_window):
    window = open_window()
    window.show_photo(names(window).index("b.jpg"))

    window.similar_action.trigger()
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)

    assert window.pages.currentWidget().isAncestorOf(window.grid)
    assert names(window)[0] == "b.jpg"


def test_without_the_model_searching_offers_it_and_not_now_fetches_nothing(qtbot, open_window):
    window = open_window(installed=False)
    offered = []

    def answer():
        box = QApplication.activeModalWidget()
        offered.append(box.text())
        box.reject()

    QTimer.singleShot(100, answer)
    window.search_field.edit.setText("red")
    window.search_field.edit.returnPressed.emit()

    assert offered and "downloaded once" in offered[0]
    assert window.model.search is None
    assert not window.search.is_downloading() and window.search.download_future is None


def test_indexing_shows_in_the_status_bar(qtbot, open_window):
    window = open_window()

    assert window.search_status.isVisible()
    assert window.search_status.label.text() == "3 photos indexed for search"


def test_flags_narrow_a_search_and_keep_its_order(qtbot, open_window):
    from lumina.qt_models.library_model import HIDE

    window = open_window()
    type_search(qtbot, window, "green")
    ranked = names(window)
    window.grid.setCurrentIndex(window.model.index(0))

    window.reject_action.trigger()
    window.model.set_rejected(HIDE)

    assert names(window) == ranked[1:]
    assert window.status.text() == "2 photos match “green”  ·  1 hidden by flags"
    window.undo_flags()
    assert names(window) == ranked


def test_a_search_typed_above_a_photo_shows_the_results_in_the_grid(qtbot, open_window):
    window = open_window()
    window.show_photo(0)
    assert window.search_field.isVisible()

    type_search(qtbot, window, "green")

    assert window.pages.currentIndex() == 1
    assert names(window)[0] == "b.jpg"
    # Still typing: the field keeps the focus and its text
    assert window.search_field.edit.hasFocus() or not window.isActiveWindow()
    assert window.search_field.text() == "green"
