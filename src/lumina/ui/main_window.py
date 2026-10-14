"""The window: a toolbar; the library's folders on the left; on the right the filters
and the grid, one photo, or, with no folders yet, a way to add one."""

import os

from PySide6.QtCore import QMargins, QPoint, QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyledItemDelegate,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style
from lumina.qt_models.develop import DevelopSession
from lumina.qt_models.library_model import Filter, LibraryModel
from lumina.qt_models.previews import PreviewLoader
from lumina.ui import settings
from lumina.ui.develop_view import DevelopView
from lumina.ui.export_dialog import ExportDialog
from lumina.ui.filters import FilterBar
from lumina.ui.image_view import ImageView
from lumina.ui.job_status import JobStatus
from lumina.ui.sidebar import Sidebar

APP_NAME = "Lumina"
# Size of a thumbnail in the grid, and of its cell with the name under it, unscaled
GRID_ICON = 168
GRID_CELL = QSize(196, 214)
# The largest image the viewer asks for: a 4K screen's width
VIEWER_MAX_SIDE = 3840

# Space between neighbouring cells, so their hover and selection backgrounds stay apart
GRID_GAP = 8

_EMPTY, _GRID, _VIEWER, _DEVELOP = range(4)


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
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(style.app_icon())
        self.resize(style.px(1280), style.px(820))

        self.model = LibraryModel(service, self)
        self.model.modelReset.connect(self._update_status)
        self.model.about_to_refresh.connect(self._save_place)
        self.model.refreshed.connect(self._restore_place)
        self.previews = PreviewLoader(self)
        self.previews.ready.connect(self._on_preview_ready)
        self.previews.failed.connect(self._on_preview_failed)
        self._viewed_id = None
        self._place = (None, None)
        # Where the Develop view was opened from, to go back to
        self._develop_from = _GRID
        self.session = DevelopSession(service, self)
        self.session.history_changed.connect(self._update_history)
        self.session.exported.connect(self._on_exported)
        self.session.export_exists.connect(self._on_export_exists)

        self._make_actions()
        self._make_toolbar()
        self.sidebar = Sidebar()
        self.sidebar.selected.connect(self._on_folder_selected)
        self.sidebar.add_requested.connect(self.choose_folder)
        self.sidebar.rescan_requested.connect(service.rescan)
        self.sidebar.rescan_all_requested.connect(service.rescan)
        self.sidebar.remove_requested.connect(self.remove_folder)
        self.filters = FilterBar()
        self.filters.changed.connect(self._apply_filter)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._make_empty_page())
        self.pages.addWidget(self._make_grid_page())
        self.pages.addWidget(self._framed(self._make_viewer()))
        self.develop = DevelopView(self.session)
        self.develop.addActions(
            [
                self.back_action,
                self.previous_action,
                self.next_action,
                self.undo_action,
                self.redo_action,
            ]
        )
        self.pages.addWidget(self.develop)
        outer = QWidget()
        layout = QHBoxLayout(outer)
        layout.setContentsMargins(style.px(8), 0, style.px(8), 0)
        layout.setSpacing(style.px(8))
        layout.addWidget(self.sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(outer)

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.job_status = JobStatus()
        self.job_status.cancel_requested.connect(service.cancel)
        self.statusBar().addPermanentWidget(self.job_status)
        self.statusBar().setSizeGripEnabled(False)

        service.folders_changed.connect(self._refresh_folders)
        service.job_changed.connect(self.job_status.show_status)
        service.job_ended.connect(lambda _status: self._refresh_choices())

        self._draw_icons()
        style.themed.changed.connect(self._draw_icons)
        self._refresh_folders(select=settings.selected_folder())

    # Building

    def _make_actions(self):
        self.add_action = QAction("Add Folder…", self)
        self.add_action.setShortcut(QKeySequence.StandardKey.Open)
        self.add_action.triggered.connect(self.choose_folder)
        self.back_action = QAction("Back", self)
        self.back_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        self.back_action.triggered.connect(self.go_back)
        self.edit_action = QAction("Edit", self)
        self.edit_action.setShortcut(QKeySequence(Qt.Key.Key_E))
        self.edit_action.setToolTip("Edit the photo (E)")
        self.edit_action.triggered.connect(self.edit_current)
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(lambda: self.session.undo())
        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(lambda: self.session.redo())
        self.reset_action = QAction("Reset", self)
        self.reset_action.setToolTip("Back to the original")
        self.reset_action.triggered.connect(lambda: self.session.reset())
        self.export_action = QAction("Export…", self)
        self.export_action.setShortcut(QKeySequence("Ctrl+Shift+E"))
        self.export_action.triggered.connect(self.export_photo)
        self.previous_action = QAction("Previous Photo", self)
        self.previous_action.setShortcut(QKeySequence(Qt.Key.Key_Left))
        self.previous_action.triggered.connect(lambda: self.step(-1))
        self.next_action = QAction("Next Photo", self)
        self.next_action.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.next_action.triggered.connect(lambda: self.step(1))
        for action in (
            self.back_action,
            self.previous_action,
            self.next_action,
            self.undo_action,
            self.redo_action,
        ):
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
        self.editing = toolbar.addWidget(
            self._group(self.undo_action, self.redo_action, self.reset_action, self.export_action)
        )
        self.browsing = toolbar.addWidget(self._group(self.edit_action, self.add_action))

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
        heading = QLabel("Your library is empty")
        heading.setObjectName("title")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel(
            "Add a folder of photos: JPEG, PNG, TIFF, CR3 and other RAW files.\n"
            "Lumina only reads them; nothing in the folder is changed."
        )
        hint.setObjectName("placeholder")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        button = QPushButton("Add Folder…")
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

    def _make_grid_page(self):
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
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.filters)
        layout.addWidget(self._framed(self.grid), 1)
        return page

    def _make_viewer(self):
        self.viewer = ImageView()
        self.viewer.addActions([self.back_action, self.previous_action, self.next_action])
        return self.viewer

    def _draw_icons(self):
        self.add_action.setIcon(style.icon("list-add", "folder-new", "folder-open"))
        self.edit_action.setIcon(style.icon("document-edit", "edit-image", "image-x-generic"))
        self.undo_action.setIcon(style.icon("edit-undo"))
        self.redo_action.setIcon(style.icon("edit-redo"))
        self.reset_action.setIcon(style.icon("edit-reset", "view-refresh"))
        self.export_action.setIcon(style.icon("document-export", "document-save-as"))
        self.back_action.setIcon(style.icon("view-grid", "view-list-icons", "go-previous"))
        self.previous_action.setIcon(style.icon("go-previous"))
        self.next_action.setIcon(style.icon("go-next"))

    # Folders

    def choose_folder(self):
        start = settings.last_folder() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Add Folder to Library", start)
        if folder:
            self.add_folder(folder)

    def add_folder(self, path):
        """Add a folder to the library and show it. Returns its id, or None when it can't
        be added (the user is told why)."""
        try:
            folder = self.service.add_folder(path)
        except ValueError as error:
            QMessageBox.warning(self, "Cannot Add Folder", str(error))
            return None
        settings.set_last_folder(os.path.dirname(os.path.realpath(path)))
        self.sidebar.select(folder)
        return folder

    def remove_folder(self, folder_id):
        info = next((f for f in self.service.folders() if f.id == folder_id), None)
        if info is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove from Library",
            f"Remove “{info.name}” from the library?\n\n"
            "Lumina forgets its photos and what it knows of them. "
            "The files themselves are not touched.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Ok:
            self.service.remove_folder(folder_id)

    def _refresh_folders(self, select=None):
        folders = self.service.folders()
        current = select if select is not None else self.sidebar.current_folder()
        if current is not None and all(f.id != current for f in folders):
            current = None
        self.sidebar.set_folders(folders, current)
        if not folders:
            self._show_page(_EMPTY)
        elif self.pages.currentIndex() == _EMPTY:
            self._show_page(_GRID)
        self._on_folder_selected(current, force=select is not None)

    def _on_folder_selected(self, folder_id, force=True):
        changed = folder_id != self.model.filter.folder_id
        settings.set_selected_folder(folder_id)
        self._refresh_choices(folder_id)
        if changed or force:
            self._apply_filter()
        if self.pages.currentIndex() == _VIEWER and changed:
            self.show_grid()

    def _refresh_choices(self, folder_id=None):
        folder_id = folder_id if folder_id is not None else self.sidebar.current_folder()
        self.filters.blockSignals(True)
        self.filters.set_choices(*self.service.choices(folder_id))
        self.filters.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self):
        year, camera, lens = self.filters.values()
        shown = Filter(self.sidebar.current_folder(), year, camera, lens)
        if shown != self.model.filter or self.model.rowCount() == 0:
            self.model.set_filter(shown)
        self._update_title()

    # Keeping the grid's place while a scan adds photos

    def _save_place(self):
        top = self.grid.indexAt(self.grid.viewport().rect().topLeft() + _inset())
        current = self.grid.currentIndex()
        self._place = (
            self.model.photo_id(top.row()) if top.isValid() else None,
            self.model.photo_id(current.row()) if current.isValid() else None,
        )

    def _restore_place(self):
        top, current = self._place
        for photo_id, make_current in ((current, True), (top, False)):
            row = self.model.row_of(photo_id) if photo_id is not None else None
            if row is None:
                continue
            index = self.model.index(row)
            if make_current:
                self.grid.setCurrentIndex(index)
            else:
                self.grid.scrollTo(index, QListView.ScrollHint.PositionAtTop)
        self._update_status()

    # The grid and the viewer

    def show_grid(self):
        self.previews.cancel()
        if not self.service.folders():
            self._show_page(_EMPTY)
            return
        self._show_page(_GRID)
        row = self.model.row_of(self._viewed_id) if self._viewed_id is not None else None
        if row is not None:
            index = self.model.index(row)
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)
        self._viewed_id = None
        self.grid.setFocus()

    def show_photo(self, row):
        if not 0 <= row < self.model.rowCount():
            return
        self._viewed_id = self.model.photo_id(row)
        path = self.model.path(row)
        # The thumbnail at once, while the larger one is decoded
        thumbnail = self.model.thumbnail_pixmap(row)
        if thumbnail is not None:
            self.viewer.set_image(thumbnail)
        elif path is None:
            self.viewer.set_message(f"{self.model.name(row)} is on a drive that is not connected")
        else:
            self.viewer.set_message("Loading…")
        self._show_page(_VIEWER)
        self.viewer.setFocus()
        self.title.setText(self.model.name(row))
        if path is not None:
            # From the library, not the grid's rows: an edit just made may not be in them yet
            edit = self.service.edit_json(self._viewed_id)
            self.previews.request(path, self._viewer_side(), edit)
        else:
            self.previews.cancel()
        self._update_status()

    def step(self, delta):
        row = self._viewed_row()
        if row is None:
            return
        if self.pages.currentIndex() == _DEVELOP:
            self.show_develop(row + delta)
        else:
            self.show_photo(row + delta)

    def go_back(self):
        """Escape: out of the crop, then out of the Develop view, then to the grid."""
        if self.pages.currentIndex() == _DEVELOP:
            if self.develop.crop_button.isChecked():
                self.develop.set_cropping(False)
                return
            self.session.close()
            row = self._viewed_row()
            if self._develop_from == _VIEWER and row is not None:
                self.show_photo(row)
                return
        self.show_grid()

    # Editing

    def edit_current(self):
        """Edit the photo shown, or the one chosen in the grid."""
        if self.pages.currentIndex() == _VIEWER:
            row = self._viewed_row()
        elif self.pages.currentIndex() == _GRID and self.grid.currentIndex().isValid():
            row = self.grid.currentIndex().row()
        else:
            return
        if row is not None:
            self._develop_from = self.pages.currentIndex()
            self.show_develop(row)

    def show_develop(self, row):
        if not 0 <= row < self.model.rowCount():
            return
        path = self.model.path(row)
        if path is None:
            QMessageBox.information(
                self,
                "Not Connected",
                f"{self.model.name(row)} is on a drive that is not connected. "
                "Connect it to edit the photo.",
            )
            return
        self.previews.cancel()
        self._viewed_id = self.model.photo_id(row)
        self.develop.show_loading(self.model.name(row))
        self.session.screen_side = self._viewer_side()
        self._show_page(_DEVELOP)
        self.session.open(self._viewed_id, path)
        self.title.setText(self.model.name(row))
        self.develop.setFocus()
        self._update_status()

    def _update_history(self):
        self.undo_action.setEnabled(self.session.can_undo())
        self.redo_action.setEnabled(self.session.can_redo())

    def export_photo(self):
        if not self.session.is_open():
            return
        dialog = ExportDialog(self.session.path, self)
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return
        destination, options = dialog.destination(), dialog.options()
        self._export_options = options
        replace = False
        if os.path.lexists(destination):
            if not self._ask_replace(destination):
                return
            replace = True
        self.statusBar().showMessage(f"Exporting {os.path.basename(destination)}…")
        self.session.export(destination, options, replace)

    def _ask_replace(self, destination):
        answer = QMessageBox.question(
            self,
            "Replace File?",
            f"“{os.path.basename(destination)}” already exists in "
            f"{os.path.dirname(destination)}.\n\nReplace it with this export?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_export_exists(self, destination):
        # Taken while the export was being written: ask, as before
        self.statusBar().clearMessage()
        if self._ask_replace(destination):
            self.session.export(destination, self._export_options, True)

    def _on_exported(self, destination, error):
        self.statusBar().clearMessage()
        if error is None:
            self.statusBar().showMessage(f"Exported to {destination}", 8000)
        else:
            QMessageBox.warning(self, "Export Failed", f"{destination}\n\n{error}")

    def _viewed_row(self):
        return self.model.row_of(self._viewed_id) if self._viewed_id is not None else None

    def _viewed_path(self):
        row = self._viewed_row()
        return self.model.path(row) if row is not None else None

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

    def _show_page(self, page):
        self.pages.setCurrentIndex(page)
        viewing = page in (_VIEWER, _DEVELOP)
        developing = page == _DEVELOP
        self.navigation.setVisible(viewing)
        self.editing.setVisible(developing)
        self.browsing.setVisible(not developing)
        for action in (self.back_action, self.previous_action, self.next_action):
            action.setEnabled(viewing)
        for action in (self.reset_action, self.export_action):
            action.setEnabled(developing)
        self.edit_action.setEnabled(page in (_GRID, _VIEWER))
        self._update_history()
        if not viewing:
            self._update_title()
        self._update_status()

    def _update_title(self):
        if self.pages.currentIndex() in (_VIEWER, _DEVELOP):
            return
        folder = self.sidebar.current_folder()
        info = next((f for f in self.service.folders() if f.id == folder), None)
        name = info.name if info is not None else "All Photos"
        self.title.setText(name if self.service.folders() else APP_NAME)
        self.setWindowTitle(f"{name} — {APP_NAME}" if self.service.folders() else APP_NAME)

    def _update_status(self, *_args):
        count = self.model.rowCount()
        row = self._viewed_row()
        if not self.service.folders():
            text = ""
        elif self.pages.currentIndex() in (_VIEWER, _DEVELOP) and row is not None:
            details = describe_photo(self.service.photo_details(self._viewed_id))
            where = self.model.path(row) or f"{self.model.name(row)} (not connected)"
            text = "  ·  ".join(part for part in (f"{row + 1} of {count}", details, where) if part)
        else:
            text = f"{count} photo" + ("" if count == 1 else "s")
        self.status.setText(text)

    def closeEvent(self, event):
        self.previews.cancel()
        self.session.close()
        super().closeEvent(event)


def describe_photo(photo):
    """When, with what and how a photo was taken, in one line: "2022-05-09 21:03 ·
    Canon EOS 90D · EF-S55-250mm f/4-5.6 IS STM · 250 mm · 1/250 s · f/5.6 · ISO 400"."""
    if not photo:
        return ""
    parts = []
    taken = photo.get("captured") or photo.get("file_time")
    if taken:
        parts.append(taken[:16])
    for key in ("model", "lens"):
        if photo.get(key):
            parts.append(photo[key])
    if photo.get("focal_length"):
        parts.append(f"{photo['focal_length']:g} mm")
    exposure = photo.get("exposure_time")
    if exposure:
        parts.append(f"1/{round(1 / exposure)} s" if exposure < 1 else f"{exposure:g} s")
    if photo.get("aperture"):
        parts.append(f"f/{photo['aperture']:g}")
    if photo.get("iso"):
        parts.append(f"ISO {photo['iso']}")
    return "  ·  ".join(parts)


def _margins(gap):
    # Half the gap on each side of a cell
    return QMargins(gap // 2, gap // 2, gap - gap // 2, gap - gap // 2)


def _inset():
    # Inside the first cell, past the grid's own margin
    return QPoint(style.px(GRID_GAP * 2), style.px(GRID_GAP * 2))
