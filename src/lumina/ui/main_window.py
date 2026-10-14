"""The window: a toolbar, then the empty start, the grid of a folder, or one photo."""

import os

from PySide6.QtCore import QMargins, QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStyledItemDelegate,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style
from lumina.qt_models.photo_model import PhotoModel
from lumina.qt_models.previews import PreviewLoader
from lumina.ui import settings
from lumina.ui.image_view import ImageView

APP_NAME = "Lumina"
# Size of a thumbnail in the grid, and of its cell with the name under it, unscaled
GRID_ICON = 168
GRID_CELL = QSize(196, 214)
# The largest image the viewer asks for: a 4K screen's width
VIEWER_MAX_SIDE = 3840

# Space between neighbouring cells, so their hover and selection backgrounds stay apart
GRID_GAP = 8

_EMPTY, _GRID, _VIEWER = range(3)


class _CellDelegate(QStyledItemDelegate):
    """Every cell is the same size, whatever its icon. The default size comes from the
    icon, which is a small placeholder until the thumbnail arrives, and with uniform item
    sizes the first row's size is every row's: the thumbnails would be cut to a strip."""

    def __init__(self, size, parent=None):
        super().__init__(parent)
        self._size = size

    def sizeHint(self, _option, _index):
        return self._size


class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(style.app_icon())
        self.resize(style.px(1200), style.px(800))

        self.model = PhotoModel(self)
        self.model.rowsInserted.connect(self._update_status)
        self.model.modelReset.connect(self._update_status)
        self.model.scanning_changed.connect(self._update_status)
        self.previews = PreviewLoader(self)
        self.previews.ready.connect(self._on_preview_ready)
        self.previews.failed.connect(self._on_preview_failed)
        self._viewed_row = None

        self._make_actions()
        self._make_toolbar()
        self.pages = QStackedWidget()
        self.pages.addWidget(self._make_empty_page())
        self.pages.addWidget(self._framed(self._make_grid()))
        self.pages.addWidget(self._framed(self._make_viewer()))
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(style.px(8), 0, style.px(8), 0)
        layout.addWidget(self.pages)
        self.setCentralWidget(outer)

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().setSizeGripEnabled(False)

        self._draw_icons()
        style.themed.changed.connect(self._draw_icons)
        self._show_page(_EMPTY)

    # Building

    def _make_actions(self):
        self.open_action = QAction("Open Folder…", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.choose_folder)
        self.back_action = QAction("Back to Grid", self)
        self.back_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        self.back_action.triggered.connect(self.show_grid)
        self.previous_action = QAction("Previous Photo", self)
        self.previous_action.setShortcut(QKeySequence(Qt.Key.Key_Left))
        self.previous_action.triggered.connect(lambda: self.step(-1))
        self.next_action = QAction("Next Photo", self)
        self.next_action.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.next_action.triggered.connect(lambda: self.step(1))
        for action in (self.back_action, self.previous_action, self.next_action):
            # Only while a photo is shown: in the grid the arrows move the selection
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

    def _make_toolbar(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.addToolBar(toolbar)

        # A widget in a toolbar is shown and hidden through the action that holds it
        self.navigation = toolbar.addWidget(
            self._group(self.back_action, self.previous_action, self.next_action)
        )
        self.title = QLabel(APP_NAME)
        self.title.setObjectName("title")
        toolbar.addWidget(self.title)
        spacer = QWidget()
        spacer.setSizePolicy(
            spacer.sizePolicy().Policy.Expanding, spacer.sizePolicy().Policy.Preferred
        )
        toolbar.addWidget(spacer)
        toolbar.addWidget(self._group(self.open_action))

    def _group(self, *actions):
        """Buttons in one rounded card, as filehouse's toolbar has them."""
        frame = QFrame()
        frame.setObjectName("group")
        layout = QHBoxLayout(frame)
        padding = style.px(style.GROUP_PADDING)
        layout.setContentsMargins(padding, padding, padding, padding)
        layout.setSpacing(style.px(2))
        for action in actions:
            button = QToolButton()
            button.setDefaultAction(action)
            # Square with an icon; wider when the icon theme has none and the text shows
            button.setFixedHeight(style.px(style.CONTROL_HEIGHT))
            button.setMinimumWidth(style.px(style.CONTROL_HEIGHT))
            button.setIconSize(QSize(style.px(18), style.px(18)))
            layout.addWidget(button)
        return frame

    def _framed(self, widget):
        frame = QFrame()
        frame.setObjectName("pane")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        return frame

    def _make_empty_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        heading = QLabel("No folder open")
        heading.setObjectName("title")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("Choose a folder of photos: JPEG, PNG, TIFF, CR3 and other RAW files.")
        hint.setObjectName("placeholder")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        button = QPushButton("Open Folder…")
        button.clicked.connect(self.choose_folder)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        row.addStretch(1)
        layout.addWidget(heading)
        layout.addSpacing(style.px(6))
        layout.addWidget(hint)
        layout.addSpacing(style.px(16))
        layout.addLayout(row)
        layout.addStretch(2)
        return page

    def _make_grid(self):
        self.grid = QListView()
        self.grid.setModel(self.model)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setLayoutMode(QListView.LayoutMode.Batched)
        self.grid.setBatchSize(500)
        self.grid.setIconSize(QSize(style.px(GRID_ICON), style.px(GRID_ICON)))
        cell = QSize(style.px(GRID_CELL.width()), style.px(GRID_CELL.height()))
        self.grid.setGridSize(cell)
        gap = style.px(GRID_GAP)
        self.grid.setItemDelegate(_CellDelegate(cell.shrunkBy(_margins(gap)), self.grid))
        self.grid.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.grid.setWordWrap(False)
        self.grid.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.grid.activated.connect(lambda index: self.show_photo(index.row()))
        return self.grid

    def _make_viewer(self):
        self.viewer = ImageView()
        self.viewer.addActions([self.back_action, self.previous_action, self.next_action])
        return self.viewer

    def _draw_icons(self):
        self.open_action.setIcon(style.icon("folder-open", "document-open"))
        self.back_action.setIcon(style.icon("view-grid", "view-list-icons", "go-previous"))
        self.previous_action.setIcon(style.icon("go-previous"))
        self.next_action.setIcon(style.icon("go-next"))

    # Folders

    def choose_folder(self):
        start = self.model.folder() or settings.last_folder() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Open Folder", start)
        if folder:
            self.open_folder(folder)

    def open_folder(self, folder):
        folder = os.path.abspath(folder)
        self.previews.cancel()
        self.model.set_folder(folder)
        settings.set_last_folder(folder)
        name = os.path.basename(folder.rstrip(os.sep)) or folder
        self.title.setText(name)
        self.setWindowTitle(f"{name} — {APP_NAME}")
        self.show_grid()

    # The grid and the viewer

    def show_grid(self):
        self.previews.cancel()
        if self.model.folder() is None:
            self._show_page(_EMPTY)
            return
        self._show_page(_GRID)
        if self._viewed_row is not None and self._viewed_row < self.model.rowCount():
            index = self.model.index(self._viewed_row)
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)
        self._viewed_row = None
        self.grid.setFocus()

    def show_photo(self, row):
        if not 0 <= row < self.model.rowCount():
            return
        self._viewed_row = row
        path = self.model.path(row)
        # The thumbnail at once, while the larger one is decoded
        thumbnail = self.model.thumbnail_pixmap(row)
        if thumbnail is not None:
            self.viewer.set_image(thumbnail)
        else:
            self.viewer.set_message("Loading…")
        self._show_page(_VIEWER)
        self.viewer.setFocus()
        self.title.setText(os.path.basename(path))
        self.previews.request(path, self._viewer_side())
        self._update_status()

    def step(self, delta):
        if self._viewed_row is not None:
            self.show_photo(self._viewed_row + delta)

    def _viewer_side(self):
        screen = self.screen()
        size = screen.size() * screen.devicePixelRatio()
        return min(VIEWER_MAX_SIDE, max(size.width(), size.height()))

    def _on_preview_ready(self, path, image):
        if self._viewed_path() == path:
            self.viewer.set_image(image)

    def _on_preview_failed(self, path, message):
        if self._viewed_path() == path:
            self.viewer.set_message(f"Cannot show {os.path.basename(path)}\n{message}")

    def _viewed_path(self):
        if self._viewed_row is None or self._viewed_row >= self.model.rowCount():
            return None
        return self.model.path(self._viewed_row)

    def _show_page(self, page):
        self.pages.setCurrentIndex(page)
        viewing = page == _VIEWER
        self.navigation.setVisible(viewing)
        for action in (self.back_action, self.previous_action, self.next_action):
            action.setEnabled(viewing)
        if not viewing and self.model.folder() is not None:
            self.title.setText(os.path.basename(self.model.folder().rstrip(os.sep)))
        self._update_status()

    def _update_status(self, *_args):
        count = self.model.rowCount()
        if self.model.folder() is None:
            text = ""
        elif self.pages.currentIndex() == _VIEWER and self._viewed_row is not None:
            text = f"{self._viewed_row + 1} of {count}  ·  {self._viewed_path()}"
        else:
            noun = "photo" if count == 1 else "photos"
            text = f"{count} {noun}" + ("  ·  Scanning…" if self.model.is_scanning() else "")
        self.status.setText(text)

    def closeEvent(self, event):
        self.model.stop()
        self.previews.cancel()
        super().closeEvent(event)


def _margins(gap):
    # Half the gap on each side of a cell
    return QMargins(gap // 2, gap // 2, gap - gap // 2, gap - gap // 2)
