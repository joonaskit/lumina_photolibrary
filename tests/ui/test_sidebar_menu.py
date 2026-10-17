"""The sidebar's menus: each item does what it says."""

from lumina.qt_models.library import FolderInfo
from lumina.ui.sidebar import FOLDER_ROLE, Sidebar


def sidebar_with_a_folder(qtbot):
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.set_folders([FolderInfo(7, "Studio", "/photos/Studio", True, None)])
    return sidebar


def item_of(sidebar, folder_id):
    for number in range(sidebar.list.count()):
        item = sidebar.list.item(number)
        if item.data(FOLDER_ROLE) == folder_id:
            return item
    raise AssertionError(folder_id)


def trigger(menu, text):
    (action,) = [action for action in menu.actions() if action.text() == text]
    action.trigger()


def test_the_menu_below_the_folders_rescans_all_and_makes_a_collection(qtbot):
    sidebar = sidebar_with_a_folder(qtbot)

    with qtbot.waitSignal(sidebar.rescan_all_requested, timeout=1000):
        trigger(sidebar.menu_for(None), "Rescan All Folders")
    with qtbot.waitSignal(sidebar.new_collection_requested, timeout=1000):
        trigger(sidebar.menu_for(None), "New Collection…")


def test_a_folders_menu_tethers_it_and_stops_tethering_it(qtbot):
    sidebar = sidebar_with_a_folder(qtbot)
    item = item_of(sidebar, 7)

    with qtbot.waitSignal(sidebar.tether_requested, timeout=1000) as asked:
        trigger(sidebar.menu_for(item), "Tether…")
    assert asked.args == [7]

    sidebar.set_tethered(7)
    texts = [action.text() for action in sidebar.menu_for(item).actions()]
    assert "Tether…" not in texts
    with qtbot.waitSignal(sidebar.stop_tether_requested, timeout=1000):
        trigger(sidebar.menu_for(item), "Stop Tethering")


def test_a_folders_menu_rescans_and_removes_it(qtbot):
    sidebar = sidebar_with_a_folder(qtbot)
    item = item_of(sidebar, 7)

    with qtbot.waitSignal(sidebar.rescan_requested, timeout=1000) as asked:
        trigger(sidebar.menu_for(item), "Rescan")
    assert asked.args == [7]
    with qtbot.waitSignal(sidebar.remove_requested, timeout=1000) as asked:
        trigger(sidebar.menu_for(item), "Remove from Library…")
    assert asked.args == [7]
