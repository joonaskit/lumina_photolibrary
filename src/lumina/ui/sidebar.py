"""The library's folders, in a card on the left: all photos, then each folder."""

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
    QVBoxLayout,
)

from lumina.integration import style

FOLDER_ROLE = Qt.ItemDataRole.UserRole + 1
WIDTH = 240


class Sidebar(QFrame):
    # The folder id chosen, or None for all photos
    selected = Signal(object)
    add_requested = Signal()
    rescan_requested = Signal(int)
    rescan_all_requested = Signal()
    remove_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(style.px(WIDTH))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(style.px(4))
        heading = QLabel("LIBRARY")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        self.list = QListWidget()
        self.list.setIconSize(QSize(style.px(18), style.px(18)))
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.currentItemChanged.connect(self._on_current)
        layout.addWidget(self.list, 1)
        self.add_action = QAction("Add Folder…", self)
        self.add_action.triggered.connect(self.add_requested)
        self.add_button = QToolButton()
        self.add_button.setDefaultAction(self.add_action)
        self.add_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.add_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.add_button.setFixedHeight(style.px(style.CONTROL_HEIGHT))
        self.add_button.setIconSize(QSize(style.px(18), style.px(18)))
        layout.addWidget(self.add_button)
        self._folders = []
        self._draw_icons()
        style.themed.changed.connect(self._draw_icons)

    def set_folders(self, folders, selected=None):
        """Show `folders` (FolderInfo), with `selected` (an id, or None for all photos)
        chosen; signals nothing."""
        self._folders = list(folders)
        self.list.blockSignals(True)
        self.list.clear()
        everything = QListWidgetItem("All Photos")
        everything.setData(FOLDER_ROLE, None)
        self.list.addItem(everything)
        current = everything
        for folder in self._folders:
            item = QListWidgetItem(folder.name)
            item.setData(FOLDER_ROLE, folder.id)
            if folder.connected:
                item.setToolTip(folder.path)
            else:
                where = f"“{folder.volume_label}”" if folder.volume_label else "a drive"
                item.setToolTip(f"{folder.path}\nOn {where}, which is not connected")
                item.setForeground(style.color("textSecondary"))
            self.list.addItem(item)
            if folder.id == selected:
                current = item
        self._draw_icons()
        self.list.setCurrentItem(current)
        self.list.blockSignals(False)

    def current_folder(self):
        item = self.list.currentItem()
        return item.data(FOLDER_ROLE) if item is not None else None

    def select(self, folder_id):
        for number in range(self.list.count()):
            item = self.list.item(number)
            if item.data(FOLDER_ROLE) == folder_id:
                self.list.setCurrentItem(item)
                return

    def _on_current(self, item, _previous):
        if item is not None:
            self.selected.emit(item.data(FOLDER_ROLE))

    def _menu(self, point):
        item = self.list.itemAt(point)
        folder_id = item.data(FOLDER_ROLE) if item is not None else None
        menu = QMenu(self)
        if folder_id is None:
            menu.addAction("Rescan All Folders", self.rescan_all_requested)
        else:
            menu.addAction("Rescan", lambda: self.rescan_requested.emit(folder_id))
            menu.addSeparator()
            menu.addAction("Remove from Library…", lambda: self.remove_requested.emit(folder_id))
        menu.exec(self.list.viewport().mapToGlobal(point))

    def _draw_icons(self):
        connected = {f.id: f.connected for f in self._folders}
        for number in range(self.list.count()):
            item = self.list.item(number)
            folder_id = item.data(FOLDER_ROLE)
            if folder_id is None:
                item.setIcon(style.icon("folder-pictures", "camera-photo", "image-x-generic"))
            elif connected.get(folder_id, True):
                item.setIcon(style.icon("folder", "folder-pictures"))
            else:
                item.setIcon(style.icon("drive-removable-media", "folder", colour="textSecondary"))
        self.add_action.setIcon(style.icon("list-add", "folder-new"))
