"""The library's folders, in a card on the left: all photos, then each folder."""

import json
from datetime import date

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from lumina.integration import style
from lumina.qt_models.library_model import PHOTOS_MIME

FOLDER_ROLE = Qt.ItemDataRole.UserRole + 1
# "folder" (also All Photos), "collection" or "smart"
KIND_ROLE = Qt.ItemDataRole.UserRole + 2
COLLECTION_ROLE = Qt.ItemDataRole.UserRole + 3
PATH_ROLE = Qt.ItemDataRole.UserRole + 4
# An event's or On This Day's [(low, high)] ranges of time, and an event's start
RANGES_ROLE = Qt.ItemDataRole.UserRole + 5
START_ROLE = Qt.ItemDataRole.UserRole + 6
WIDTH = 240


class _PhotoDropList(QListWidget):
    """The folders and collections; photos dragged from the grid can be dropped on a
    manual collection."""

    # (collection id, photo ids)
    photos_dropped = Signal(int, object)

    def _collection_at(self, point):
        item = self.itemAt(point)
        if item is not None and item.data(KIND_ROLE) == "collection":
            return item
        return None

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(PHOTOS_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if not event.mimeData().hasFormat(PHOTOS_MIME):
            super().dragMoveEvent(event)
        elif self._collection_at(event.position().toPoint()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        item = self._collection_at(event.position().toPoint())
        if item is None or not event.mimeData().hasFormat(PHOTOS_MIME):
            event.ignore()
            return
        ids = json.loads(bytes(event.mimeData().data(PHOTOS_MIME)).decode())
        event.acceptProposedAction()
        self.photos_dropped.emit(item.data(COLLECTION_ROLE), ids)


class Sidebar(QFrame):
    # The folder id chosen, or None for all photos
    selected = Signal(object)
    add_requested = Signal()
    import_requested = Signal()
    camera_requested = Signal()
    rescan_requested = Signal(int)
    rescan_all_requested = Signal()
    remove_requested = Signal(int)
    # Tether the folder (its id), or stop tethering
    tether_requested = Signal(int)
    stop_tether_requested = Signal()
    # A collection was chosen: its id and whether it is a smart one
    collection_selected = Signal(int, bool)
    # The last import was chosen: its job id
    import_selected = Signal(int)
    new_collection_requested = Signal()
    rename_collection_requested = Signal(int)
    delete_collection_requested = Signal(int)
    # Save the filters now set as the smart collection's
    update_smart_requested = Signal(int)
    # Photos (ids) dropped on a manual collection: its id
    photos_dropped = Signal(int, object)
    # A keyword path chosen to filter by, or None to stop
    keyword_chosen = Signal(object)
    # An event, or On This Day, was chosen: its name
    time_selected = Signal(str)
    rename_keyword_requested = Signal(str)
    delete_keyword_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(style.px(WIDTH))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("LIBRARY")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        self.list = _PhotoDropList()
        self.list.setAcceptDrops(True)
        self.list.setDropIndicatorShown(True)
        self.list.photos_dropped.connect(self.photos_dropped)
        self.list.setIconSize(QSize(style.px(18), style.px(18)))
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.currentItemChanged.connect(self._on_current)
        layout.addWidget(self.list, 3)
        self.events_heading = QLabel("EVENTS")
        self.events_heading.setObjectName("heading")
        layout.addWidget(self.events_heading)
        self.event_tree = QTreeWidget()
        self.event_tree.setHeaderHidden(True)
        # The years open and close with an arrow of their own, and the events under them
        # are indented as the keywords are: the style paints a branch in the highlight
        # colour behind a chosen row
        self.event_tree.setRootIsDecorated(False)
        self.event_tree.setIndentation(0)
        self.event_tree.itemClicked.connect(self._on_event_clicked)
        self.event_tree.itemExpanded.connect(self._mark_year)
        self.event_tree.itemCollapsed.connect(self._mark_year)
        self.event_tree.setIconSize(QSize(style.px(18), style.px(18)))
        self.event_tree.currentItemChanged.connect(self._on_event_current)
        layout.addWidget(self.event_tree, 2)
        self._events_shown(False)
        self.keywords_heading = QLabel("KEYWORDS")
        self.keywords_heading.setObjectName("heading")
        layout.addWidget(self.keywords_heading)
        self.keyword_list = QListWidget()
        self.keyword_list.setIconSize(QSize(style.px(18), style.px(18)))
        self.keyword_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.keyword_list.customContextMenuRequested.connect(self._keyword_menu)
        self.keyword_list.itemClicked.connect(self._on_keyword_clicked)
        layout.addWidget(self.keyword_list, 2)
        self._keyword = None
        self._set_keywords_shown(False)
        self.add_action = QAction("Add Folder…", self)
        self.add_action.triggered.connect(self.add_requested)
        self.add_button = QToolButton()
        self.add_button.setDefaultAction(self.add_action)
        self.add_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.add_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.add_button.setIconSize(QSize(style.px(18), style.px(18)))
        layout.addWidget(self.add_button)
        self.import_action = QAction("Import…", self)
        self.import_action.setToolTip("Copy photos from a memory card into the library (Ctrl+I)")
        self.import_action.triggered.connect(self.import_requested)
        self.import_button = QToolButton()
        self.import_button.setDefaultAction(self.import_action)
        self.import_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.import_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.import_button.setIconSize(QSize(style.px(18), style.px(18)))
        layout.addWidget(self.import_button)
        self.camera_action = QAction("Tether Camera…", self)
        self.camera_action.setToolTip("Shoot with a camera connected by USB (Ctrl+T)")
        self.camera_action.triggered.connect(self.camera_requested)
        self.camera_button = QToolButton()
        self.camera_button.setDefaultAction(self.camera_action)
        self.camera_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.camera_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.camera_button.setIconSize(QSize(style.px(18), style.px(18)))
        self.camera_button.hide()
        layout.addWidget(self.camera_button)
        style.follow_density(self, lambda: self._fit(layout))
        self._folders = []
        self._tethered = None
        self._collections = []
        self._last_import = None
        self._draw_icons()
        style.themed.changed.connect(self._draw_icons)

    def set_folders(self, folders, selected=None):
        """Show `folders` (FolderInfo), with `selected` (an id, or None for all photos)
        chosen; signals nothing."""
        self._folders = list(folders)
        chosen = self.current_collection() if selected is None else None
        was_import = selected is None and self.current_import() is not None
        self.list.blockSignals(True)
        self.list.clear()
        everything = QListWidgetItem("All Photos")
        everything.setData(FOLDER_ROLE, None)
        everything.setData(KIND_ROLE, "folder")
        self.list.addItem(everything)
        current = everything
        for folder in self._folders:
            item = QListWidgetItem(folder.name)
            item.setData(FOLDER_ROLE, folder.id)
            item.setData(KIND_ROLE, "folder")
            if folder.connected:
                item.setToolTip(folder.path)
            else:
                where = f"“{folder.volume_label}”" if folder.volume_label else "a drive"
                item.setToolTip(f"{folder.path}\nOn {where}, which is not connected")
                item.setForeground(style.color("textSecondary"))
            self.list.addItem(item)
            if folder.id == selected:
                current = item
        self.list.setCurrentItem(current)
        self.list.blockSignals(False)
        self._add_collections()
        if chosen is not None or was_import:
            self.list.blockSignals(True)
            if chosen is not None:
                self.select_collection(chosen[0])
            else:
                self.select_import()
            self.list.blockSignals(False)
        self._draw_icons()

    def set_collections(self, collections):
        """Show `collections` (db.Collection) under the folders, the chosen one staying
        chosen when it is still there; signals nothing."""
        self._collections = list(collections)
        self.list.blockSignals(True)
        chosen = self.current_collection()
        was_import = self.current_import() is not None
        for number in reversed(range(self.list.count())):
            if self.list.item(number).data(KIND_ROLE) in ("collection", "smart", "import"):
                self.list.takeItem(number)
        self._add_collections()
        if chosen is not None:
            self.select_collection(chosen[0])
        elif was_import:
            self.select_import()
        self.list.blockSignals(False)
        self._draw_icons()

    def set_last_import(self, job_id):
        """Show "Last Import" under the folders when there was an import (its job id), none
        with None; the chosen one stays chosen. Signals nothing."""
        self._last_import = job_id
        self.set_collections(self._collections)

    def current_import(self):
        """The job id of the last import if it is chosen, else None."""
        item = self.list.currentItem()
        if item is not None and item.data(KIND_ROLE) == "import":
            return item.data(COLLECTION_ROLE)
        return None

    def select_import(self):
        for number in range(self.list.count()):
            if self.list.item(number).data(KIND_ROLE) == "import":
                self.list.setCurrentItem(self.list.item(number))
                return

    def _add_collections(self):
        self.list.blockSignals(True)
        if self._last_import is not None:
            item = QListWidgetItem("Last Import")
            item.setData(KIND_ROLE, "import")
            item.setData(COLLECTION_ROLE, self._last_import)
            item.setToolTip("The photos the last import copied")
            self.list.addItem(item)
        for collection in self._collections:
            smart = collection.query is not None
            item = QListWidgetItem(collection.name)
            item.setData(KIND_ROLE, "smart" if smart else "collection")
            item.setData(COLLECTION_ROLE, collection.id)
            item.setToolTip(
                "A smart collection: the photos its saved filters pick"
                if smart
                else f"{collection.count} photo{'' if collection.count == 1 else 's'}"
            )
            self.list.addItem(item)
        self.list.blockSignals(False)

    def current_collection(self):
        """(id, whether smart) of the chosen collection, or None when a folder is chosen."""
        item = self.list.currentItem()
        if item is not None and item.data(KIND_ROLE) in ("collection", "smart"):
            return item.data(COLLECTION_ROLE), item.data(KIND_ROLE) == "smart"
        return None

    def set_events(self, events, day_ranges):
        """Show On This Day (when `day_ranges` has any) and, by year, the `events`
        (core/events.py), newest first; the one chosen stays chosen. Signals nothing, but All
        Photos is chosen when the one chosen is gone."""
        chosen = self.current_time()
        opened = {
            self.event_tree.topLevelItem(n).data(0, PATH_ROLE)
            for n in range(self.event_tree.topLevelItemCount())
            if self.event_tree.topLevelItem(n).isExpanded()
        }
        this_year = str(date.today().year)
        self.event_tree.blockSignals(True)
        self.event_tree.clear()
        found = None
        if day_ranges:
            today = QTreeWidgetItem(["On This Day"])
            today.setData(0, KIND_ROLE, "day")
            today.setData(0, RANGES_ROLE, list(day_ranges))
            today.setToolTip(0, "Photos taken on this date in earlier years")
            self.event_tree.addTopLevelItem(today)
            if chosen is not None and chosen[0] == "day":
                found = today
        years = {}
        for event in reversed(list(events)):
            year = str(event.start.year)
            if year not in years:
                node = QTreeWidgetItem([year])
                node.setData(0, PATH_ROLE, year)
                node.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.event_tree.addTopLevelItem(node)
                node.setExpanded(year in opened or (not opened and year == this_year))
                self._mark_year(node)
                years[year] = node
            item = QTreeWidgetItem([f"\u2003{event.name()}  ({event.count})"])
            item.setData(0, KIND_ROLE, "event")
            item.setData(0, RANGES_ROLE, [event.time_range()])
            item.setData(0, START_ROLE, event.start.isoformat())
            years[year].addChild(item)
            if chosen is not None and chosen[0] == event.start.isoformat():
                found = item
                years[year].setExpanded(True)
                self._mark_year(years[year])
        if found is not None:
            self.event_tree.setCurrentItem(found)
        self.event_tree.blockSignals(False)
        self._events_shown(self.event_tree.topLevelItemCount() > 0)
        self._draw_icons()
        if chosen is not None and found is None:
            self.list.setCurrentRow(0)

    def current_time(self):
        """(what identifies it, its [(low, high)] ranges, its name) of the event or On
        This Day chosen, else None."""
        item = self.event_tree.currentItem()
        kind = item.data(0, KIND_ROLE) if item is not None else None
        if kind not in ("event", "day"):
            return None
        key = item.data(0, START_ROLE) if kind == "event" else "day"
        name = item.text(0).strip("\u2003").rsplit("  (", 1)[0]
        return key, item.data(0, RANGES_ROLE), name

    def current_time_ranges(self):
        """The (low, high) ranges of the event or On This Day chosen; empty when none."""
        chosen = self.current_time()
        return tuple(chosen[1]) if chosen is not None else ()

    def _events_shown(self, shown):
        self.events_heading.setVisible(shown)
        self.event_tree.setVisible(shown)

    def _mark_year(self, node):
        """Show whether a year is open, as the text of its row."""
        year = node.data(0, PATH_ROLE)
        if year is not None:
            node.setText(0, f"{'▾' if node.isExpanded() else '▸'} {year}")

    def _on_event_clicked(self, item, _column):
        if item.data(0, PATH_ROLE) is not None:
            item.setExpanded(not item.isExpanded())

    def _on_event_current(self, item, _previous):
        if item is None or item.data(0, KIND_ROLE) not in ("event", "day"):
            return
        self.list.blockSignals(True)
        self.list.setCurrentItem(None)
        self.list.clearSelection()
        self.list.blockSignals(False)
        self.time_selected.emit(self.current_time()[2])

    def _clear_events(self):
        self.event_tree.blockSignals(True)
        self.event_tree.setCurrentItem(None)
        self.event_tree.clearSelection()
        self.event_tree.blockSignals(False)

    def select_collection(self, collection_id):
        for number in range(self.list.count()):
            item = self.list.item(number)
            if item.data(KIND_ROLE) in ("collection", "smart") and (
                item.data(COLLECTION_ROLE) == collection_id
            ):
                self.list.setCurrentItem(item)
                return

    def set_keywords(self, keywords):
        """Show [(id, path, count)] of the keywords, as a tree by name, with the photo
        counts; the chosen one stays chosen when it is still there."""
        self.keyword_list.blockSignals(True)
        self.keyword_list.clear()
        for _id, path, count in keywords:
            names = path.split(" > ")
            item = QListWidgetItem("\u2003" * (len(names) - 1) + f"{names[-1]}  ({count})")
            item.setData(PATH_ROLE, path)
            item.setToolTip(path)
            self.keyword_list.addItem(item)
            if path == self._keyword:
                self.keyword_list.setCurrentItem(item)
        if not any(path == self._keyword for _id, path, _count in keywords):
            self._keyword = None
        self.keyword_list.blockSignals(False)
        self._set_keywords_shown(bool(keywords))
        self._draw_icons()

    def select_keyword(self, path):
        """Show keyword `path` (None for none) as the one filtered by; signals nothing."""
        self._keyword = path
        self.keyword_list.blockSignals(True)
        self.keyword_list.clearSelection()
        self.keyword_list.setCurrentItem(None)
        for number in range(self.keyword_list.count()):
            item = self.keyword_list.item(number)
            if path is not None and item.data(PATH_ROLE) == path:
                self.keyword_list.setCurrentItem(item)
        self.keyword_list.blockSignals(False)

    def _set_keywords_shown(self, shown):
        self.keywords_heading.setVisible(shown)
        self.keyword_list.setVisible(shown)

    def _on_keyword_clicked(self, item):
        path = item.data(PATH_ROLE)
        # The chosen one again stops filtering
        chosen = None if path == self._keyword else path
        self.select_keyword(chosen)
        self.keyword_chosen.emit(chosen)

    def _keyword_menu(self, point):
        item = self.keyword_list.itemAt(point)
        if item is None:
            return
        path = item.data(PATH_ROLE)
        menu = QMenu(self)
        menu.addAction("Rename…", lambda: self.rename_keyword_requested.emit(path))
        menu.addAction("Delete…", lambda: self.delete_keyword_requested.emit(path))
        menu.exec(self.keyword_list.viewport().mapToGlobal(point))

    def set_tethered(self, folder_id):
        """Mark the folder `folder_id` as tethered (None: none)."""
        self._tethered = folder_id
        self._draw_icons()

    def set_import_shown(self, shown):
        self.import_button.setVisible(shown)

    def set_camera_shown(self, shown):
        self.camera_button.setVisible(shown)

    def current_folder(self):
        """The id of the folder chosen; None for All Photos, and for a collection."""
        item = self.list.currentItem()
        return item.data(FOLDER_ROLE) if item is not None else None

    def select(self, folder_id):
        for number in range(self.list.count()):
            item = self.list.item(number)
            if item.data(KIND_ROLE) == "folder" and item.data(FOLDER_ROLE) == folder_id:
                self.list.setCurrentItem(item)
                return

    def _on_current(self, item, _previous):
        if item is None:
            return
        self._clear_events()
        kind = item.data(KIND_ROLE)
        if kind in ("collection", "smart"):
            self.collection_selected.emit(item.data(COLLECTION_ROLE), kind == "smart")
        elif kind == "import":
            self.import_selected.emit(item.data(COLLECTION_ROLE))
        else:
            self.selected.emit(item.data(FOLDER_ROLE))

    def _menu(self, point):
        self.menu_for(self.list.itemAt(point)).exec(self.list.viewport().mapToGlobal(point))

    def menu_for(self, item):
        """The menu of the list's `item` (None: of the empty space below the items)."""
        kind = item.data(KIND_ROLE) if item is not None else None
        folder_id = item.data(FOLDER_ROLE) if item is not None else None
        menu = QMenu(self)
        if kind in ("collection", "smart"):
            collection_id = item.data(COLLECTION_ROLE)
            menu.addAction("Rename…", lambda: self.rename_collection_requested.emit(collection_id))
            if kind == "smart":
                menu.addAction(
                    "Save Current Filters Here",
                    lambda: self.update_smart_requested.emit(collection_id),
                )
            menu.addSeparator()
            menu.addAction("Delete…", lambda: self.delete_collection_requested.emit(collection_id))
        elif folder_id is None:
            menu.addAction("Rescan All Folders", self.rescan_all_requested.emit)
            menu.addAction("New Collection…", self.new_collection_requested.emit)
        else:
            menu.addAction("Rescan", lambda: self.rescan_requested.emit(folder_id))
            if folder_id == self._tethered:
                menu.addAction("Stop Tethering", self.stop_tether_requested.emit)
            else:
                menu.addAction("Tether…", lambda: self.tether_requested.emit(folder_id))
            menu.addSeparator()
            menu.addAction("Remove from Library…", lambda: self.remove_requested.emit(folder_id))
        return menu

    def _fit(self, layout):
        layout.setSpacing(style.gap(4))
        self.add_button.setFixedHeight(style.row_height())
        self.import_button.setFixedHeight(style.row_height())
        self.camera_button.setFixedHeight(style.row_height())

    def _draw_icons(self):
        connected = {f.id: f.connected for f in self._folders}
        for number in range(self.list.count()):
            item = self.list.item(number)
            folder_id = item.data(FOLDER_ROLE)
            kind = item.data(KIND_ROLE)
            if kind == "import":
                item.setIcon(style.icon("document-import", "media-flash", "camera-photo"))
            elif kind == "collection":
                item.setIcon(style.icon("folder-favorites", "bookmarks", "emblem-favorite"))
            elif kind == "smart":
                item.setIcon(style.icon("edit-find", "view-filter", "system-search"))
            elif folder_id is None:
                item.setIcon(style.icon("folder-pictures", "camera-photo", "image-x-generic"))
            elif folder_id == self._tethered:
                item.setIcon(style.icon("camera-photo", "folder-pictures", "folder"))
            elif connected.get(folder_id, True):
                item.setIcon(style.icon("folder", "folder-pictures"))
            else:
                item.setIcon(style.icon("drive-removable-media", "folder", colour="textSecondary"))
        for number in range(self.event_tree.topLevelItemCount()):
            top = self.event_tree.topLevelItem(number)
            if top.data(0, KIND_ROLE) == "day":
                top.setIcon(0, style.icon("view-calendar", "x-office-calendar", "appointment-new"))
            else:
                for child in range(top.childCount()):
                    top.child(child).setIcon(
                        0, style.icon("view-calendar", "x-office-calendar", "appointment-new")
                    )
        for number in range(self.keyword_list.count()):
            self.keyword_list.item(number).setIcon(
                style.icon("tag", "bookmark-new", "emblem-documents")
            )
        self.add_action.setIcon(style.icon("list-add", "folder-new"))
        self.import_action.setIcon(style.icon("document-import", "media-flash", "camera-photo"))
        self.camera_action.setIcon(style.icon("camera-photo", "camera-web", "media-record"))
