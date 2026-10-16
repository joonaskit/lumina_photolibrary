"""Keywords and collections in the window: giving and taking off keywords, undo with the
flags, the keyword filter and the sidebar's list, manual and smart collections."""

import json

from conftest import TIMEOUT
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtWidgets import QInputDialog
from test_flags import choose, names, open_window, press

from lumina.core import db as dbm
from lumina.qt_models.library_model import PHOTOS_MIME, PICKED
from lumina.ui.keywords_dialog import KeywordsDialog
from lumina.ui.sidebar import KIND_ROLE


def stored(service):
    """{name: sorted keyword paths} as the library has them."""
    with dbm.Library(service.path) as library:
        paths = {i: path for i, path, _count in library.keyword_list()}
        return {
            photo.path.rsplit("/", 1)[-1]: sorted(
                (paths[k] for k in photo.keywords), key=str.casefold
            )
            for photo in library.photos()
        }


def tagged(window):
    return {
        window.model.name(row): window.model.keywords(row) for row in range(window.model.rowCount())
    }


def sidebar_keywords(window):
    return [
        window.sidebar.keyword_list.item(n).text()
        for n in range(window.sidebar.keyword_list.count())
    ]


def collection_items(window):
    return [
        (item.text(), item.data(KIND_ROLE))
        for item in (window.sidebar.list.item(n) for n in range(window.sidebar.list.count()))
        if item.data(KIND_ROLE) in ("collection", "smart")
    ]


def test_chosen_photos_are_given_keywords_which_are_kept(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0, 2)

    given = window.model.add_keywords([0, 2], ["Places > Helsinki", " cat ", ""])

    assert given == ["Places > Helsinki", "cat"]
    first, middle, last = names(window)
    assert tagged(window) == {
        first: ["cat", "Places > Helsinki"],
        middle: [],
        last: ["cat", "Places > Helsinki"],
    }
    assert stored(service)[first] == ["cat", "Places > Helsinki"] and stored(service)[middle] == []
    # Again with what they have: no change, no history
    assert window.model.add_keywords([0], ["cat"]) == []


def test_keywords_are_taken_off_the_chosen_photos_only(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    window.model.add_keywords([0, 1, 2], ["Cat", "Dog"])

    assert window.model.remove_keywords([0, 1], ["cat"]) == 2

    first, second, third = names(window)
    assert tagged(window) == {first: ["Dog"], second: ["Dog"], third: ["Cat", "Dog"]}
    assert stored(service)[first] == ["Dog"] and stored(service)[third] == ["Cat", "Dog"]


def test_keyword_changes_are_undone_in_order_with_flags_and_ratings(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)
    window.model.add_keywords([0], ["Cat"])
    window.pick_action.trigger()
    window.model.add_keywords([0], ["Dog"])
    assert window.model.keywords(0) == ["Cat", "Dog"] and window.model.flag(0) == PICKED

    window.undo_flag_action.trigger()
    assert window.model.keywords(0) == ["Cat"] and window.model.flag(0) == PICKED
    window.undo_flag_action.trigger()
    assert window.model.flag(0) == 0 and window.model.keywords(0) == ["Cat"]
    window.undo_flag_action.trigger()
    assert window.model.keywords(0) == [] and stored(service)[names(window)[0]] == []

    window.redo_flag_action.trigger()
    assert window.model.keywords(0) == ["Cat"]
    assert stored(service)[names(window)[0]] == ["Cat"]


def test_the_keywords_dialog_adds_and_removes_for_the_chosen_photos(
    qtbot, service, drive, monkeypatch
):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0, 1)
    seen = []

    def run(dialog):
        seen.append(dialog.heading.text())
        dialog.add_requested.emit("Cat, Places > Helsinki")
        seen.append([dialog.list.item(n).text() for n in range(dialog.list.count())])
        dialog.list.item(0).setSelected(True)
        dialog.remove_requested.emit([dialog.list.item(0).data(Qt.ItemDataRole.UserRole + 1)])
        seen.append([dialog.list.item(n).text() for n in range(dialog.list.count())])
        return 0

    monkeypatch.setattr(KeywordsDialog, "exec", run)

    press(qtbot, window.grid, Qt.Key.Key_K)

    assert seen == ["Keywords of 2 photos", ["Cat", "Places > Helsinki"], ["Places > Helsinki"]]
    assert [window.model.keywords(row) for row in range(2)] == [["Places > Helsinki"]] * 2


def test_the_dialog_says_how_many_of_the_photos_have_a_keyword(qtbot):
    dialog = KeywordsDialog()
    qtbot.addWidget(dialog)

    dialog.set_state("Keywords of 3 photos", ["Cat", "Dog"], {"Cat": 3, "Dog": 1}, 3)

    assert [dialog.list.item(n).text() for n in range(2)] == ["Cat", "Dog  (1 of 3)"]
    added = []
    dialog.add_requested.connect(added.append)
    dialog.field.setText("  Bird ")
    qtbot.keyClick(dialog.field, Qt.Key.Key_Return)
    assert added == ["Bird"] and dialog.field.text() == ""
    assert not dialog.remove_button.isEnabled()


def test_the_sidebar_lists_keywords_with_counts_and_filters_by_one(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    assert window.sidebar.keyword_list.isHidden()
    window.model.add_keywords([0, 1], ["Places > Helsinki"])
    window.model.add_keywords([2], ["Places > Espoo"])

    qtbot.waitUntil(
        lambda: (
            sidebar_keywords(window) == ["Places  (3)", "\u2003Espoo  (1)", "\u2003Helsinki  (2)"]
        ),
        timeout=TIMEOUT,
    )
    assert not window.sidebar.keyword_list.isHidden()

    window.sidebar.keyword_list.itemClicked.emit(window.sidebar.keyword_list.item(2))
    assert window.filters.keyword_value() == "Places > Helsinki"
    assert window.model.rowCount() == 2

    # Above it: everything inside
    window.sidebar.keyword_list.itemClicked.emit(window.sidebar.keyword_list.item(0))
    assert window.model.rowCount() == 3

    # The same again stops it
    window.sidebar.keyword_list.itemClicked.emit(window.sidebar.keyword_list.item(0))
    assert window.filters.keyword_value() is None and window.model.rowCount() == 3


def test_a_photo_that_loses_the_keyword_filtered_by_stays_until_the_user_moves_on(
    qtbot, service, drive
):
    window = open_window(qtbot, service, drive / "Photos")
    window.model.add_keywords([0, 1], ["Cat"])
    window.filters.set_keyword("Cat")
    assert window.model.rowCount() == 2

    window.model.remove_keywords([0], ["Cat"])
    assert window.model.rowCount() == 2
    window.model.settle()
    assert window.model.rowCount() == 1


def test_deleting_and_renaming_a_keyword_change_the_photos_and_the_filter(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    window.model.add_keywords([0, 1], ["Trip > Spain"])
    window.filters.set_keyword("Trip > Spain")

    service.rename_keyword("Trip", "Holiday")
    qtbot.waitUntil(lambda: "Holiday > Spain" in window.service.keyword_paths.values())
    # The filter's choice went with the name
    assert window.filters.keyword_value() is None
    assert window.model.rowCount() == 3
    assert window.model.keywords(0) == ["Holiday > Spain"]

    service.delete_keyword("Holiday")
    assert window.model.keywords(0) == [] and stored(service)[names(window)[0]] == []
    assert sidebar_keywords(window) == []


def test_a_collection_holds_photos_added_by_menu_or_drop(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, third = names(window)
    box = service.add_collection("Best")
    assert collection_items(window) == [("Best", "collection")]

    choose(window, 0)
    window.add_to_collection(box)
    window.sidebar.photos_dropped.emit(box, [window.model.photo_id(2)])

    window.sidebar.select_collection(box)
    qtbot.waitUntil(lambda: window.model.rowCount() == 2, timeout=TIMEOUT)
    assert names(window) == [first, third]
    assert window.windowTitle().startswith("Best")

    # Out of the collection, not out of the library
    choose(window, 0)
    window.remove_from_collection(box)
    qtbot.waitUntil(lambda: window.model.rowCount() == 1, timeout=TIMEOUT)
    assert names(window) == [third]
    window.sidebar.select(None)
    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    assert second in names(window)


def test_deleting_the_collection_shown_goes_back_to_all_photos(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    box = service.add_collection("Best")
    window.sidebar.select_collection(box)
    qtbot.waitUntil(lambda: window.model.rowCount() == 0, timeout=TIMEOUT)

    service.delete_collection(box)

    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    assert collection_items(window) == [] and window.sidebar.current_collection() is None


def test_collection_names_are_checked(qtbot, service, drive, monkeypatch):
    window = open_window(qtbot, service, drive / "Photos")
    service.add_collection("Best")
    warned = []
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: ("best", True))
    monkeypatch.setattr(
        "lumina.ui.main_window.QMessageBox.warning", lambda *_a: warned.append(_a[-1])
    )

    assert window.new_collection() is None

    assert warned and "already" in warned[0]
    assert [c.name for c in service.collections()] == ["Best"]


def test_dragging_photos_from_the_grid_carries_their_ids(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0, 2)

    data = window.model.mimeData([window.model.index(0), window.model.index(2)])

    assert isinstance(data, QMimeData)
    assert json.loads(bytes(data.data(PHOTOS_MIME)).decode()) == [
        window.model.photo_id(0),
        window.model.photo_id(2),
    ]
    assert window.grid.dragEnabled()


def test_a_smart_collection_saves_the_filters_and_updates_itself(
    qtbot, service, drive, monkeypatch
):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)
    window.pick_action.trigger()
    window.model.add_keywords([0, 1], ["Cat"])
    window.filters.flag.setCurrentIndex(window.filters.flag.findData(PICKED))
    window.filters.set_keyword("Cat")
    assert window.model.rowCount() == 1
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: ("Cat picks", True))

    window.new_smart_collection()

    assert collection_items(window) == [("Cat picks", "smart")]
    assert window.sidebar.current_collection()[1] is True
    # Filters off, then the collection brings them back
    window.filters.clear()
    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    window.sidebar.select(None)
    smart = next(c.id for c in service.collections())
    window.sidebar.select_collection(smart)
    qtbot.waitUntil(lambda: window.model.rowCount() == 1, timeout=TIMEOUT)
    assert window.filters.flag_value() == PICKED and window.filters.keyword_value() == "Cat"

    # A photo that now fits is in it with nothing saved again
    window.filters.clear()
    window.model.add_keywords([2], ["Cat"])
    choose(window, 2)
    window.pick_action.trigger()
    window.sidebar.select(None)
    window.sidebar.select_collection(smart)
    qtbot.waitUntil(lambda: window.model.rowCount() == 2, timeout=TIMEOUT)


def test_nothing_set_is_not_saved_as_a_smart_collection(qtbot, service, drive, monkeypatch):
    window = open_window(qtbot, service, drive / "Photos")
    asked = []
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: asked.append(1) or ("x", True))

    assert not window.filters.save_button.isEnabled()
    window.new_smart_collection()

    assert asked == [] and service.collections() == []


def test_the_viewers_status_names_the_keywords(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    window.model.add_keywords([0], ["Places > Helsinki", "Cat"])
    choose(window, 0)

    window.show_photo(0)

    assert "Keywords: Cat, Places > Helsinki" in window.status.text()


def test_the_last_import_is_a_view_of_the_photos_it_copied(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first, second, third = names(window)
    assert collection_items(window) == [] and window.sidebar.current_import() is None
    with dbm.Library(service.path) as library:
        folder = library.folders()[0].id
        job = library.add_import_job(
            folder,
            [
                dbm.ImportItem(
                    source_key="uuid:CARD",
                    source_path="DCIM/1.CR3",
                    size=1,
                    content_id="1",
                    key=library.folders()[0].volume_key,
                    folder="Photos",
                    name=second,
                )
            ],
        )
        library.set_import_item(
            library.import_items(job)[0].id, dbm.COPIED, path=f"Photos/{second}"
        )
        # The photo is in the library under that name already: the scan found it
        assert any(p.path.endswith(second) for p in library.photos())

    window._refresh_last_import()
    window.sidebar.select_import()

    qtbot.waitUntil(lambda: window.model.rowCount() == 1, timeout=TIMEOUT)
    assert names(window) == [second] and window.windowTitle().startswith("Last Import")
    window.sidebar.select(None)
    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    assert first in names(window) and third in names(window)
