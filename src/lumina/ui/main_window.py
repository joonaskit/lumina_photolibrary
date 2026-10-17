"""The window: a toolbar; the library's folders on the left; on the right the filters
and the grid, one photo, or, with no folders yet, a way to add one."""

import os

from PySide6.QtCore import QEvent, QMargins, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style
from lumina.qt_models import tether as camera_service
from lumina.qt_models.develop import DevelopSession
from lumina.qt_models.library import NewerParams
from lumina.qt_models.library_model import (
    COPIES_ROLE,
    COPY_ROLE,
    DAMAGE_ROLE,
    DIMMED_ROLE,
    FLAG_ROLE,
    PICKED,
    RATING_ROLE,
    REJECTED,
    SIMILAR,
    STACK_ROLE,
    TEXT,
    UNFLAGGED,
    Filter,
    LibraryModel,
    SmartQuery,
)
from lumina.qt_models.previews import PreviewLoader
from lumina.ui import badges, damage, flags, settings, stars
from lumina.ui.camera import CameraBar, CameraDialog
from lumina.ui.develop_view import DevelopView
from lumina.ui.export_dialog import ExportDialog
from lumina.ui.filters import FilterBar
from lumina.ui.grid_size import NAMES as GRID_SIZE_NAMES
from lumina.ui.grid_size import GridSizeSwitch
from lumina.ui.image_view import ImageView
from lumina.ui.import_dialog import ImportDialog
from lumina.ui.job_status import JobStatus, import_status, merge_status
from lumina.ui.keywords_dialog import KeywordsDialog
from lumina.ui.merge_dialog import MergeDialog, PanoramaDialog
from lumina.ui.preset_dialog import CopyDialog
from lumina.ui.quick_view import QuickView
from lumina.ui.search import SearchField, SearchStatus
from lumina.ui.settings_dialog import SettingsDialog
from lumina.ui.sidebar import Sidebar
from lumina.ui.tether import TetherDialog, TetherStatus

APP_NAME = "Lumina"
# Size of a thumbnail in the grid, unscaled, by the size chosen. Large stays within the
# thumbnails' own 320 pixels (qt_models/thumbnails.py), so it is sharp on a 1x screen
GRID_ICONS = {"small": 112, "medium": 168, "large": 280}
# A cell is its thumbnail with room around it and the name under it
GRID_CELL_EXTRA = QSize(28, 46)
# The largest image the viewer asks for: a 4K screen's width
VIEWER_MAX_SIDE = 3840

# Space between neighbouring cells, so their hover and selection backgrounds stay apart
GRID_GAP = 8
# How long after the grid stopped scrolling the thumbnails not wanted are dropped
_TRIM_MS = 150
# How much of a dimmed thumbs-down photo shows
DIMMED_OPACITY = 0.35

FLAG_NAMES = {PICKED: "Thumbs up", REJECTED: "Thumbs down"}

_EMPTY, _GRID, _VIEWER, _DEVELOP = range(4)


class _CellDelegate(QStyledItemDelegate):
    """Every cell is the same size, whatever its icon. The default size comes from the
    icon, which is a small placeholder until the thumbnail arrives, and with uniform item
    sizes the first row's size is every row's: the thumbnails would be cut to a strip."""

    def __init__(self, size, parent=None):
        super().__init__(parent)
        self._size = size

    def set_size(self, size):
        self._size = size

    def sizeHint(self, _option, _index):
        return self._size

    def photo_rect(self, option, index):
        """Where the photo is in the cell: the icon keeps the photo's shape within its
        square."""
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        widget = option.widget
        area = widget.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemDecoration, item, widget
        )
        photo = QRect(QPoint(0, 0), item.icon.actualSize(item.decorationSize))
        photo.moveCenter(area.center())
        return photo

    def stack_badge_at(self, grid, index, point):
        """Whether `point` (in the grid's viewport) is on the stack badge of the cell."""
        stacked = index.data(STACK_ROLE)
        if not stacked:
            return False
        option = QStyleOptionViewItem()
        grid.initViewItemOption(option)
        option.rect = grid.visualRect(index)
        option.widget = grid
        card = badges.pill_rect(self.photo_rect(option, index), badges.stack_text(stacked))
        if card is None:
            return False
        if stacked[2]:
            behind = style.px(3)
            card = card.adjusted(0, 0, behind, behind)
        return card.contains(point)

    def copies_badge_at(self, grid, index, point):
        """Whether `point` (in the grid's viewport) is on the virtual copies' badge."""
        virtual = index.data(COPIES_ROLE)
        if not virtual:
            return False
        option = QStyleOptionViewItem()
        grid.initViewItemOption(option)
        option.rect = grid.visualRect(index)
        option.widget = grid
        photo = self.photo_rect(option, index)
        stacked = index.data(STACK_ROLE)
        left = 0.0
        if stacked:
            pill = badges.pill_rect(photo, badges.stack_text(stacked))
            if pill is not None:
                left = pill.width() + style.px(4) + (style.px(3) if stacked[2] else 0)
        number = index.data(COPY_ROLE) or virtual
        card = badges.diamond_rect(photo, str(number), left)
        return card is not None and card.contains(point)

    def paint(self, painter, option, index):
        """The cell, greyed out for a dimmed thumbs-down photo, its flag's badge on the
        photo's bottom right corner, its stars on the bottom left and a warning on the
        top right when its raw data may be damaged, and the number of its virtual copies on
        the top left, after its stack's."""
        flag, rating = index.data(FLAG_ROLE), index.data(RATING_ROLE)
        damaged = index.data(DAMAGE_ROLE)
        virtual, stacked = index.data(COPIES_ROLE), index.data(STACK_ROLE)
        # A virtual copy's cell has its own number on the badge, the photo's the count
        number = index.data(COPY_ROLE) or virtual
        photo = self.photo_rect(option, index)
        painter.save()
        if index.data(DIMMED_ROLE):
            painter.setOpacity(DIMMED_OPACITY)
        if stacked and stacked[2]:
            badges.paint_stack_cue(painter, photo)
        super().paint(painter, option, index)
        painter.restore()
        if flag not in flags.COLOURS and not rating and not damaged and not virtual and not stacked:
            return
        if flag in flags.COLOURS:
            flags.paint_badge(painter, photo, flag)
        stars.paint_stars(painter, photo, rating or 0)
        if damaged:
            damage.paint_badge(painter, photo)
        # Top left, side by side: the stack, then the virtual copies
        left = 0.0
        if stacked:
            left = badges.paint_pill(
                painter, photo, badges.stack_text(stacked), left, stacked=stacked[2]
            )
        if virtual:
            badges.paint_diamond(painter, photo, str(number), left)


class MainWindow(QMainWindow):
    def __init__(
        self,
        service,
        search=None,
        merge=None,
        importer=None,
        parent=None,
        backups=None,
        tether=None,
        camera=None,
    ):
        """`search` is the SearchService; without one there is no search. `merge` is the
        MergeService; without one there are no merges. `importer` is the ImportService;
        without one there is no importing. `backups` is the BackupService; without one
        the settings have no Backups section. `tether` is the HotFolder; without one no
        folder can be tethered. `camera` is the CameraService (which takes its photos in
        through `tether`); without one there is no shooting with a camera."""
        super().__init__(parent)
        self.service = service
        self.backups = backups
        self.tether = tether
        self.camera = camera
        self.search = search
        self.merge = merge
        self.importer = importer
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(style.app_icon())
        self.resize(style.px(1280), style.px(820))

        self.model = LibraryModel(service, search, self)
        self.model.modelReset.connect(self._update_status)
        self.model.search_changed.connect(self._on_search_changed)
        self.model.loading_changed.connect(self._update_status)
        self.model.about_to_refresh.connect(self._save_place)
        self.model.refreshed.connect(self._restore_place)
        self.model.refreshed.connect(self._update_copy_actions)
        # A stack made, taken apart, opened or closed changes the rows, not the selection
        for signal in (
            self.model.rowsInserted,
            self.model.rowsRemoved,
            self.model.modelReset,
            self.model.dataChanged,
        ):
            signal.connect(self._update_stack_actions)
        self.previews = PreviewLoader(self)
        self.previews.ready.connect(self._on_preview_ready)
        self.previews.failed.connect(self._on_preview_failed)
        self._viewed_id = None
        # Which of its edits the viewer or the Develop view shows: 0 is the photo's own,
        # more are virtual copies
        self._viewed_copy = 0
        # A photo asked for from outside that the library does not have yet (its folder
        # is being scanned), opened as it arrives
        self._opening = None
        # A tethered folder's newest photo, and the one to show once the grid has it, with
        # whether in the Develop view
        self._last_shot = None
        self._showing_shot = None
        self.quick_views = []
        self._place = (None, None, 0)
        # Where the Develop view was opened from, to go back to
        self._develop_from = _GRID
        self.session = DevelopSession(service, self)
        self.session.profile_folders = settings.profile_folders
        self.session.history_changed.connect(self._update_history)
        self.session.exported.connect(self._on_exported)
        self.session.export_exists.connect(self._on_export_exists)

        self._make_actions()
        self.search_field = SearchField()
        self.search_field.searched.connect(self.search_text)
        self.search_field.similar_cleared.connect(self.model.clear_search)
        self._make_toolbar()
        self.sidebar = Sidebar()
        self.sidebar.selected.connect(self._on_folder_selected)
        self.sidebar.add_requested.connect(self.choose_folder)
        self.sidebar.import_requested.connect(self.import_photos)
        self.sidebar.set_import_shown(importer is not None)
        self.sidebar.rescan_requested.connect(service.rescan)
        self.sidebar.rescan_all_requested.connect(service.rescan)
        self.sidebar.remove_requested.connect(self.remove_folder)
        self.sidebar.tether_requested.connect(self.tether_folder)
        self.sidebar.camera_requested.connect(lambda: self.tether_camera())
        self.sidebar.set_camera_shown(camera is not None)
        self.sidebar.collection_selected.connect(self._on_collection_selected)
        self.sidebar.import_selected.connect(self._on_import_selected)
        self.sidebar.new_collection_requested.connect(lambda: self.new_collection())
        self.sidebar.rename_collection_requested.connect(self.rename_collection)
        self.sidebar.delete_collection_requested.connect(self.delete_collection)
        self.sidebar.update_smart_requested.connect(self.update_smart_collection)
        self.sidebar.photos_dropped.connect(self.add_ids_to_collection)
        self.sidebar.keyword_chosen.connect(lambda path: self.filters.set_keyword(path))
        self.sidebar.rename_keyword_requested.connect(self.rename_keyword)
        self.sidebar.delete_keyword_requested.connect(self.delete_keyword)
        self.filters = FilterBar()
        self.filters.changed.connect(self._apply_filter)
        self.filters.keyword_changed.connect(self.set_keyword_filter)
        self.filters.save_requested.connect(lambda: self.new_smart_collection())
        self.filters.flag_changed.connect(self.set_flag_filter)
        self.filters.rating_changed.connect(self.set_rating_filter)
        self.filters.edit_changed.connect(self.set_edit_filter)
        self.flag_options = flags.FlagOptions()
        self.flag_options.set_rejected(settings.rejected_photos())
        self.flag_options.rejected_changed.connect(self.set_rejected)
        self.model.set_rejected(settings.rejected_photos())
        self.model.flags_changed.connect(self._update_flag_actions)
        self.model.rowsRemoved.connect(self._update_status)
        self.grid_size = GridSizeSwitch()
        self.grid_size.changed.connect(self.set_grid_size)

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
                # The flags and the stars of the photo being edited; their Undo is the grid's
                # and the viewer's, the Develop view's is the edit's
                self.pick_action,
                self.reject_action,
                self.unflag_action,
                *self.rate_actions,
                *self.zoom_actions,
                *self.settings_actions,
                *self.copy_actions,
            ]
        )
        self.develop.image.zoom_changed.connect(self._on_zoom_changed)
        self.session.region_ready.connect(lambda *_part: self._on_zoom_changed())
        self.session.image_ready.connect(lambda *_image: self._on_zoom_changed())
        self.develop.copy_settings_button.clicked.connect(self.copy_settings)
        self.develop.paste_settings_button.clicked.connect(self.paste_settings)
        self.service.settings_copied.connect(self._update_settings_actions)
        self.pages.addWidget(self.develop)
        outer = QWidget()
        column = QVBoxLayout(outer)
        # Between the toolbar and the filters
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        column.addWidget(divider)
        self.camera_bar = CameraBar()
        column.addWidget(self.camera_bar)
        if camera is not None:
            self.camera_bar.capture_requested.connect(camera.capture)
            self.camera_bar.setting_chosen.connect(camera.set_setting)
            self.camera_bar.fetch_again_requested.connect(camera.fetch_again)
            self.camera_bar.end_requested.connect(camera.stop)
            camera.changed.connect(self._on_camera_changed)
            camera.failed.connect(
                lambda name, why: self.statusBar().showMessage(
                    f"{name} was not saved: {why}", 15000
                )
            )
            camera.error.connect(lambda why: self.statusBar().showMessage(why, 15000))
        layout = QHBoxLayout()
        layout.addWidget(self.sidebar)
        layout.addWidget(self.pages, 1)
        column.addLayout(layout, 1)
        self.setCentralWidget(outer)

        def space():
            column.setContentsMargins(style.gap(8), 0, style.gap(8), 0)
            column.setSpacing(style.gap(8))
            layout.setSpacing(style.gap(8))

        style.follow_density(outer, space)

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.job_status = JobStatus()
        self.job_status.cancel_requested.connect(service.cancel)
        self.statusBar().addPermanentWidget(self.job_status)
        self.merge_status = merge_status()
        self.statusBar().addPermanentWidget(self.merge_status)
        if merge is not None:
            self.merge_status.cancel_requested.connect(merge.cancel)
            merge.job_changed.connect(self.merge_status.show_status)
        self.import_status = import_status()
        self.statusBar().addPermanentWidget(self.import_status)
        self.tether_status = TetherStatus()
        self.statusBar().addPermanentWidget(self.tether_status)
        if tether is not None:
            self.tether_status.stop_requested.connect(self.stop_tethering)
            self.sidebar.stop_tether_requested.connect(self.stop_tethering)
            tether.changed.connect(self._on_tether_changed)
            tether.arrived.connect(self._on_shot_arrived)
            tether.failed.connect(self._on_shot_failed)
            tether.stopped.connect(lambda why: self.statusBar().showMessage(why, 15000))
        if importer is not None:
            self.import_status.cancel_requested.connect(importer.cancel)
            importer.job_changed.connect(self.import_status.show_status)
            importer.card_arrived.connect(self._on_card_arrived)
        self.statusBar().setSizeGripEnabled(False)

        self.search_status = SearchStatus()
        self.statusBar().addPermanentWidget(self.search_status)
        # How the grid shows the photos, not which: at the right of its status bar
        self.grid_view_options = QWidget()
        options = QHBoxLayout(self.grid_view_options)
        options.addWidget(self.flag_options)
        options.addWidget(self.grid_size)
        style.follow_density(
            self.grid_view_options,
            lambda: (
                options.setContentsMargins(style.gap(8), 0, 0, 0),
                options.setSpacing(style.gap(8)),
            ),
        )
        self.statusBar().addPermanentWidget(self.grid_view_options)
        if search is not None:
            self.search_status.cancel_download.connect(search.cancel_download)
            self.search_status.cancel_indexing.connect(search.cancel_indexing)
            search.download_progress.connect(self.search_status.show_download)
            search.download_failed.connect(self._on_download_failed)
            search.model_changed.connect(self._on_model_changed)
            search.indexing_changed.connect(self.search_status.show_indexing)
            search.indexing_changed.connect(self._update_status)

        service.folders_changed.connect(self._refresh_folders)
        service.keywords_changed.connect(self._on_keywords_changed)
        service.sidecars_written.connect(self._on_sidecars_written)
        service.collections_changed.connect(self._on_collections_changed)
        service.job_changed.connect(self.job_status.show_status)
        # A method, not a lambda: the connection goes with the window, and a scan that ends
        # after the window is closed does not reach its deleted widgets
        service.job_ended.connect(self._on_job_ended)
        # Rows a scan wrote arrive as inserted; a load that ends has all there is
        self.model.rowsInserted.connect(lambda: self._open_arrived())
        self.model.loading_changed.connect(
            lambda loading: loading or self._open_arrived(loaded=True)
        )
        self.model.rowsInserted.connect(lambda: self._show_shot())
        self.model.loading_changed.connect(lambda loading: loading or self._show_shot(loaded=True))

        self._draw_icons()
        style.themed.changed.connect(self._draw_icons)
        service.keyword_list()
        self._on_keywords_changed()
        self._refresh_folders(select=settings.selected_folder())
        self.sidebar.set_collections(service.collections())
        self.sidebar.set_last_import(service.last_import())

    def _on_sidecars_written(self, result):
        """Say what a pass of writing sidecars could not do; a quiet pass says nothing."""
        if not result.failures:
            return
        path, why = result.failures[0]
        more = f" (and {len(result.failures) - 1} more)" if len(result.failures) > 1 else ""
        self.statusBar().showMessage(
            f"{len(result.failures)} sidecar{'' if len(result.failures) == 1 else 's'} "
            f"not written: {path}: {why}{more}",
            10000,
        )

    def _on_job_ended(self, _status):
        self._refresh_last_import()
        self._refresh_choices()
        self._open_arrived(scanned=True)

    # Building

    def _make_actions(self):
        self.add_action = QAction("Add Folder…", self)
        self.add_action.setShortcut(QKeySequence.StandardKey.Open)
        self.add_action.triggered.connect(self.choose_folder)
        # A key only: the button is the sidebar's, under the folders it adds to
        self.addAction(self.add_action)
        self.import_action = QAction("Import Photos…", self)
        self.import_action.setShortcut(QKeySequence("Ctrl+I"))
        self.import_action.setToolTip("Copy photos from a memory card into the library (Ctrl+I)")
        self.import_action.triggered.connect(lambda: self.import_photos())
        self.addAction(self.import_action)

        self.camera_action = QAction("Tether Camera…", self)
        self.camera_action.setShortcut(QKeySequence("Ctrl+T"))
        self.camera_action.triggered.connect(lambda: self.tether_camera())
        self.addAction(self.camera_action)
        # A foot pedal can send either
        self.capture_action = QAction("Capture", self)
        self.capture_action.setShortcuts([QKeySequence("Space"), QKeySequence("F12")])
        self.capture_action.setEnabled(False)
        self.capture_action.triggered.connect(self.capture)
        self.addAction(self.capture_action)
        self.preferences_action = QAction("Settings", self)
        self.preferences_action.setShortcut(QKeySequence("Ctrl+,"))
        self.preferences_action.setToolTip("Settings (Ctrl+,)")
        self.preferences_action.triggered.connect(self.open_settings)
        self.addAction(self.preferences_action)
        self.back_action = QAction("Back", self)
        self.back_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        self.back_action.triggered.connect(self.go_back)
        self.edit_action = QAction("Edit", self)
        self.edit_action.setShortcut(QKeySequence(Qt.Key.Key_E))
        self.edit_action.setToolTip("Edit the photo (E)")
        self.edit_action.triggered.connect(self.edit_current)
        # A key and the photos' context menus; no button
        self.addAction(self.edit_action)
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
        self.make_copy_action = QAction("Make Virtual Copy", self)
        self.make_copy_action.setShortcut(QKeySequence("Ctrl+'"))
        self.make_copy_action.setToolTip(
            "Another edit of the photo, to try a different look; the file is not touched (Ctrl+')"
        )
        self.make_copy_action.triggered.connect(self.make_virtual_copy)
        self.delete_copy_action = QAction("Delete Virtual Copy", self)
        self.delete_copy_action.triggered.connect(self.delete_virtual_copy)
        self.previous_copy_action = QAction("Previous Copy", self)
        self.previous_copy_action.setShortcut(QKeySequence("Ctrl+Up"))
        self.previous_copy_action.triggered.connect(lambda: self.step_copy(-1))
        self.next_copy_action = QAction("Next Copy", self)
        self.next_copy_action.setShortcut(QKeySequence("Ctrl+Down"))
        self.next_copy_action.triggered.connect(lambda: self.step_copy(1))
        self.stack_action = QAction("Stack", self)
        self.stack_action.setShortcut(QKeySequence("Ctrl+G"))
        self.stack_action.setToolTip("Group the chosen photos in one stack (Ctrl+G)")
        self.stack_action.triggered.connect(self.stack_photos)
        self.unstack_action = QAction("Unstack", self)
        self.unstack_action.setShortcut(QKeySequence("Ctrl+Shift+G"))
        self.unstack_action.triggered.connect(self.unstack_photos)
        self.leave_stack_action = QAction("Remove from Stack", self)
        self.leave_stack_action.triggered.connect(self.leave_stack)
        self.toggle_stack_action = QAction("Expand or Collapse Stack", self)
        self.toggle_stack_action.setShortcut(QKeySequence("Ctrl+Alt+G"))
        self.toggle_stack_action.triggered.connect(self.toggle_stacks)
        self.stack_top_action = QAction("Set as Top of Stack", self)
        self.stack_top_action.triggered.connect(self.set_stack_top)
        self.stack_actions = (
            self.stack_action,
            self.unstack_action,
            self.leave_stack_action,
            self.toggle_stack_action,
            self.stack_top_action,
        )
        self.copy_actions = (
            self.make_copy_action,
            self.delete_copy_action,
            self.previous_copy_action,
            self.next_copy_action,
        )
        self.previous_action = QAction("Previous Photo", self)
        self.previous_action.setShortcut(QKeySequence(Qt.Key.Key_Left))
        self.previous_action.triggered.connect(lambda: self.step(-1))
        self.next_action = QAction("Next Photo", self)
        self.next_action.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.next_action.triggered.connect(lambda: self.step(1))
        self.find_action = QAction("Search", self)
        self.find_action.setShortcut(QKeySequence.StandardKey.Find)
        self.find_action.triggered.connect(self.focus_search)
        self.addAction(self.find_action)
        self.similar_action = QAction("Find Similar Photos", self)
        self.similar_action.setShortcut(QKeySequence(Qt.Key.Key_S))
        self.similar_action.setToolTip("Show the photos most like this one (S)")
        self.similar_action.triggered.connect(self.find_similar)
        self.addAction(self.similar_action)
        self.pick_action = QAction("Thumbs Up", self)
        self.pick_action.setShortcut(QKeySequence(Qt.Key.Key_P))
        self.pick_action.setToolTip("Thumbs up (P)")
        self.pick_action.setCheckable(True)
        self.pick_action.triggered.connect(lambda: self.flag_photos(PICKED))
        self.reject_action = QAction("Thumbs Down", self)
        self.reject_action.setShortcut(QKeySequence(Qt.Key.Key_X))
        self.reject_action.setToolTip("Thumbs down (X)")
        self.reject_action.setCheckable(True)
        self.reject_action.triggered.connect(lambda: self.flag_photos(REJECTED))
        self.unflag_action = QAction("Clear Flag", self)
        self.unflag_action.setShortcut(QKeySequence(Qt.Key.Key_U))
        self.unflag_action.triggered.connect(lambda: self.flag_photos(UNFLAGGED))
        # In the grid and the viewer; the Develop view's Undo is its edit's
        self.undo_flag_action = QAction("Undo Flag, Rating or Keywords", self)
        self.undo_flag_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_flag_action.triggered.connect(self.undo_flags)
        self.redo_flag_action = QAction("Redo Flag, Rating or Keywords", self)
        self.redo_flag_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_flag_action.triggered.connect(self.redo_flags)
        # 1 to 5 stars, 0 for none; the same key again clears it
        self.rate_actions = []
        for number in range(6):
            action = QAction(
                "No Stars" if number == 0 else f"{number} Star{'s' * (number > 1)}", self
            )
            action.setShortcut(QKeySequence(Qt.Key(Qt.Key.Key_0 + number)))
            action.setToolTip(f"Rate {number} (key {number})" if number else "Clear the rating (0)")
            action.triggered.connect(lambda _on=False, stars=number: self.rate_photos(stars))
            self.rate_actions.append(action)
        self.keywords_action = QAction("Keywords…", self)
        self.keywords_action.setShortcut(QKeySequence(Qt.Key.Key_K))
        self.keywords_action.setToolTip("Give the photos keywords, or take them off (K)")
        self.keywords_action.triggered.connect(self.edit_keywords)
        self.flag_actions = (
            self.keywords_action,
            self.pick_action,
            self.reject_action,
            self.unflag_action,
            *self.rate_actions,
            self.undo_flag_action,
            self.redo_flag_action,
        )
        self.larger_action = QAction("Larger Thumbnails", self)
        self.larger_action.setShortcuts([QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")])
        self.larger_action.triggered.connect(lambda: self.step_grid_size(1))
        self.smaller_action = QAction("Smaller Thumbnails", self)
        self.smaller_action.setShortcuts([QKeySequence.StandardKey.ZoomOut])
        self.smaller_action.triggered.connect(lambda: self.step_grid_size(-1))
        # The zoom of the viewer and of the Develop view: Z between fitted and 100 %,
        # Ctrl+Plus and Minus in steps
        self.zoom_action = QAction("Zoom to 100 %", self)
        self.zoom_action.setShortcut(QKeySequence(Qt.Key.Key_Z))
        self.zoom_action.triggered.connect(lambda: self._zoom_view().toggle_zoom())
        self.zoom_in_action = QAction("Zoom In", self)
        self.zoom_in_action.setShortcuts([QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")])
        self.zoom_in_action.triggered.connect(lambda: self._zoom_view().zoom_in())
        self.zoom_out_action = QAction("Zoom Out", self)
        self.zoom_out_action.setShortcuts([QKeySequence.StandardKey.ZoomOut])
        self.zoom_out_action.triggered.connect(lambda: self._zoom_view().zoom_out())
        self.zoom_actions = (self.zoom_action, self.zoom_in_action, self.zoom_out_action)
        self.copy_settings_action = QAction("Copy Settings…", self)
        self.copy_settings_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self.copy_settings_action.triggered.connect(self.copy_settings)
        self.paste_settings_action = QAction("Paste Settings", self)
        self.paste_settings_action.setShortcut(QKeySequence("Ctrl+Shift+V"))
        self.paste_settings_action.triggered.connect(self.paste_settings)
        self.sync_settings_action = QAction("Sync Settings…", self)
        self.sync_settings_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.sync_settings_action.triggered.connect(self.sync_settings)
        self.settings_actions = (
            self.copy_settings_action,
            self.paste_settings_action,
            self.sync_settings_action,
        )
        self.merge_hdr_action = QAction("Merge to HDR…", self)
        self.merge_hdr_action.setShortcut(QKeySequence("Ctrl+H"))
        self.merge_hdr_action.setToolTip(
            "Merge a bracket of exposures into one photo (Ctrl+H); with one photo chosen, "
            "the bracket it was taken in"
        )
        self.merge_hdr_action.triggered.connect(self.merge_hdr)
        self.stitch_panorama_action = QAction("Stitch Panorama…", self)
        self.stitch_panorama_action.setShortcut(QKeySequence("Ctrl+M"))
        self.stitch_panorama_action.setToolTip(
            "Stitch overlapping photos, taken turning the camera, into one panorama (Ctrl+M)"
        )
        self.stitch_panorama_action.triggered.connect(self.stitch_panorama)
        for action in (
            self.back_action,
            self.previous_action,
            self.next_action,
            self.undo_action,
            self.redo_action,
            *self.flag_actions,
            *self.zoom_actions,
            *self.settings_actions,
            *self.copy_actions,
            *self.stack_actions,
            self.merge_hdr_action,
            self.stitch_panorama_action,
        ):
            # Only while a photo is shown: in the grid the arrows move the selection.
            # The flags' keys are the grid's and the viewer's, not the search field's
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

    def _make_toolbar(self):
        """One row in three parts: where you are on the left, with the edit's buttons in
        the Develop view; the photo's thumbs and stars in the middle; search and the
        settings on the right. The sides share the rest equally, so the middle stays in
        the middle."""
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.addToolBar(toolbar)

        left = QWidget()
        left_row = QHBoxLayout(left)
        left_row.setContentsMargins(0, 0, 0, 0)
        self.navigation = self._group(self.back_action, self.previous_action, self.next_action)
        left_row.addWidget(self.navigation)
        self.title = QLabel(APP_NAME)
        self.title.setObjectName("title")
        left_row.addWidget(self.title)
        self.editing = self._group(
            self.undo_action, self.redo_action, self.reset_action, self.export_action
        )
        left_row.addWidget(self.editing)
        left_row.addStretch(1)

        self.rating_bar = stars.StarRating()
        self.rating_bar.chosen.connect(self.rate_photos)
        self.marking = self._group(self.pick_action, self.rating_bar, self.reject_action)

        right = QWidget()
        right_row = QHBoxLayout(right)
        right_row.setContentsMargins(0, 0, 0, 0)
        right_row.addStretch(1)
        right_row.addWidget(self.search_field)
        right_row.addWidget(self._group(self.preferences_action))

        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(left, 1)
        layout.addWidget(self.marking)
        layout.addWidget(right, 1)
        toolbar.addWidget(row)
        style.follow_density(
            row, lambda: [rows.setSpacing(style.gap(8)) for rows in (left_row, right_row, layout)]
        )

    def _group(self, *items):
        """Buttons for actions, and any widgets, in one rounded card, as filehouse's
        toolbar has them."""
        frame = QFrame()
        frame.setObjectName("group")
        layout = QHBoxLayout(frame)
        widgets = []
        for action in items:
            if isinstance(action, QWidget):
                widgets.append(action)
                layout.addWidget(action)
                continue
            button = QToolButton()
            button.setDefaultAction(action)
            button.setIconSize(QSize(style.px(18), style.px(18)))
            widgets.append(button)
            layout.addWidget(button)

        def fit():
            frame.setFixedHeight(style.row_height())
            padding = style.group_padding()
            layout.setContentsMargins(padding, padding, padding, padding)
            layout.setSpacing(style.gap(2))
            inner = style.row_inner_height()
            for widget in widgets:
                widget.setFixedHeight(inner)
                # Square with an icon; wider when the icon theme has none and the text shows
                if isinstance(widget, QToolButton):
                    widget.setMinimumWidth(inner)

        style.follow_density(frame, fit)
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
            "Add a folder of photos: JPEG, HEIC, PNG, TIFF, CR3 and other RAW files.\n"
            "Lumina only reads them; nothing in the folder is changed."
        )
        hint.setObjectName("placeholder")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        button = QPushButton("Add Folder…")
        button.clicked.connect(self.choose_folder)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        if self.importer is not None:
            importing = QPushButton("Import from a Card…")
            importing.clicked.connect(lambda: self.import_photos())
            row.addWidget(importing)
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
        # In one pass (11 ms for 50,000 photos): a batched layout, done again for rows
        # removed or inserted, scrolls the view back into its first batch
        self.grid.setLayoutMode(QListView.LayoutMode.SinglePass)
        self._delegate = _CellDelegate(QSize(), self.grid)
        self.grid.setItemDelegate(self._delegate)
        self._apply_grid_size(settings.grid_size())
        # Closer cells in a compact window; the thumbnails stay as large
        style.follow_density(
            self.grid, lambda: self._apply_grid_size(self.grid_size.value()), now=False
        )
        # Only while the grid shows: the Develop view may want these keys for its own zoom
        for action in (self.larger_action, self.smaller_action):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.grid.addAction(action)
        self.grid.viewport().installEventFilter(self)
        self.grid.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.grid.setWordWrap(False)
        self.grid.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        # Photos dragged onto a collection in the sidebar
        self.grid.setDragEnabled(True)
        self.grid.setDragDropMode(QListView.DragDropMode.DragOnly)
        self.grid.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.grid.activated.connect(
            lambda index: self.show_photo(index.row(), self.model.copy(index.row()))
        )
        self.grid.addActions(
            [
                *self.flag_actions,
                *self.settings_actions,
                self.make_copy_action,
                *self.stack_actions,
                self.merge_hdr_action,
                self.stitch_panorama_action,
            ]
        )
        self.grid.selectionModel().selectionChanged.connect(self._update_flag_actions)
        self.grid.selectionModel().currentChanged.connect(self._on_current_changed)
        # Escape in the grid ends a search
        self.end_search_action = QAction("Show All Photos", self.grid)
        self.end_search_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        self.end_search_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.end_search_action.triggered.connect(self.end_search)
        self.grid.addAction(self.end_search_action)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._grid_menu)
        # Scrolling fast asks for a thumbnail of every row painted on the way: once it
        # settles, those far from the view are not waited for
        self._trim_timer = QTimer(self)
        self._trim_timer.setSingleShot(True)
        self._trim_timer.setInterval(_TRIM_MS)
        self._trim_timer.timeout.connect(self.trim_thumbnails)
        self.grid.verticalScrollBar().valueChanged.connect(lambda _value: self._trim_timer.start())
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.filters)
        layout.addWidget(self._framed(self.grid), 1)
        return page

    def _make_viewer(self):
        self.viewer = ImageView(zoomable=True)
        self.viewer.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.viewer.customContextMenuRequested.connect(
            lambda point: self._photo_menu(in_grid=False).exec(self.viewer.mapToGlobal(point))
        )
        self.viewer.addActions(
            [
                self.back_action,
                self.previous_action,
                self.next_action,
                *self.flag_actions,
                *self.zoom_actions,
                *self.settings_actions,
                *self.copy_actions,
                self.merge_hdr_action,
                self.stitch_panorama_action,
            ]
        )
        self.viewer.zoom_changed.connect(self._on_zoom_changed)
        # The photo whose full size was asked for, so it is asked for once
        self._full_requested = None
        # What the status bar says of the zoom
        self._zoom_shown = None
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
        self.similar_action.setIcon(style.icon("edit-find", "system-search"))
        self.preferences_action.setIcon(
            style.icon("preferences-system", "emblem-system", "configure")
        )
        ratio = self.devicePixelRatioF()
        self.pick_action.setIcon(flags.icon(PICKED, ratio))
        self.reject_action.setIcon(flags.icon(REJECTED, ratio))
        self.search_field.draw_icons()

    def open_settings(self):
        dialog = SettingsDialog(self.search, self.service, self, backups=self.backups)
        dialog.exec()
        dialog.deleteLater()
        self.develop.arrange()

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

    # Importing from a card

    def import_photos(self, card=None):
        """Ask what to import from a card (`card`, or the first one connected) and where
        to, then queue the import. A folder chosen that is not in the library is added."""
        if self.importer is None:
            return
        dialog = ImportDialog(self.importer, self.service.folders(), card, self)
        accepted = dialog.exec() == ImportDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted:
            return
        destination = dialog.destination()
        folder = self.service.folder_holding(destination)
        if folder is None:
            folder = self.add_folder(destination)
            if folder is None:
                return
        try:
            self.importer.import_photos(
                dialog.chosen(),
                destination,
                dialog.layout_name(),
                dialog.naming(),
                dialog.backup(),
            )
        except ValueError as error:
            QMessageBox.warning(self, "Cannot Import", str(error))
            return
        self.sidebar.select(folder)

    def _on_card_arrived(self, path):
        name = os.path.basename(path.rstrip("/")) or path
        self.statusBar().showMessage(f"Memory card “{name}” connected: Ctrl+I to import", 15000)

    # Opening from outside: the file manager, or a second `lumina PATH`

    def open_paths(self, paths):
        """Show what the command line or the file manager asked for: a folder is added
        to the library and shown, a photo is opened. The window comes forward for what
        is in the library; a photo that is not is shown in a window of its own."""
        for path in paths:
            if os.path.isdir(path):
                self.present()
                self.add_folder(path)
            else:
                self.open_photo(path)

    def present(self):
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def open_photo(self, path):
        """Open the photo at `path` in the viewer, if its folder is in the library; else
        in a window of its own that asks whether to add the folder. Anything that is not
        a photo is told so."""
        if not self.service.is_photo_file(path):
            QMessageBox.warning(
                self, "Cannot Open", f"{path}\n\nThis is not a photo Lumina can open."
            )
            return
        folder, _photo = self.service.locate_photo(path)
        if folder is None:
            self.show_quick(path)
            return
        self.present()
        self._opening = os.path.realpath(path)
        self.sidebar.select(folder)
        self._open_arrived()

    def show_quick(self, path):
        view = QuickView(os.path.realpath(path))
        view.add_requested.connect(lambda folder, view=view: self._add_from_quick(view, folder))
        view.destroyed.connect(lambda _=None, view=view: self._forget_quick(view))
        self.quick_views.append(view)
        view.show()
        view.raise_()
        view.activateWindow()

    def _forget_quick(self, view):
        if view in self.quick_views:
            self.quick_views.remove(view)

    def _add_from_quick(self, view, folder):
        path = view.path
        view.close()
        self.present()
        if self.add_folder(folder) is not None:
            self._opening = path
            self._open_arrived()

    def _open_arrived(self, loaded=False, scanned=False):
        """The photo asked for, once the grid has it. The grid fills while the folder is
        scanned. It is given up when the grid has loaded and the library has the photo but
        the grid not (a filter hides it), or when the scan ended and the library still
        has no such photo."""
        path = self._opening
        if path is None:
            return
        _folder, photo = self.service.locate_photo(path)
        row = self.model.row_of(photo) if photo is not None else None
        if row is not None:
            self._opening = None
            self.show_photo(row)
        elif (scanned and photo is None) or (loaded and photo is not None):
            self._opening = None

    # Tethering

    def tether_folder(self, folder_id):
        """Ask for the preset, then tether the folder `folder_id` and show it."""
        if self.tether is None:
            return
        info = next((f for f in self.service.folders() if f.id == folder_id), None)
        if info is None:
            return
        presets = [name for name, _json in self.service.presets()]
        dialog = TetherDialog(info.name, presets, settings.tether_preset(), self)
        if dialog.exec() != TetherDialog.DialogCode.Accepted:
            return
        preset = dialog.chosen_preset()
        settings.set_tether_preset(preset)
        try:
            self.tether.start(folder_id, preset)
        except (KeyError, NewerParams) as error:
            QMessageBox.warning(self, "Cannot Tether", f"The preset can't be used: {error}")
            return
        self._last_shot = None
        self.sidebar.select(folder_id)

    def stop_tethering(self):
        """Stop the tethered folder, or the camera session whose photos come through it."""
        if self.camera is not None and self.camera.running:
            self.camera.stop()
        elif self.tether is not None:
            self.tether.stop()

    def tether_camera(self):
        """Ask where a camera session's photos go, then start it."""
        if self.camera is None:
            return
        if self.camera.running:
            self.camera_bar.show()
            return
        if not camera_service.available():
            QMessageBox.information(
                self,
                "Tether Camera",
                "Shooting with a camera needs python-gphoto2, which is not installed.\n\n"
                "Install it with: pip install 'lumina[tether]'",
            )
            return
        folders = [f for f in self.service.folders() if f.connected]
        presets = [name for name, _json in self.service.presets()]
        dialog = CameraDialog(folders, presets, settings.camera_choices(), self)
        if dialog.exec() != CameraDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        settings.set_camera_choices(chosen)
        try:
            self.camera.start(
                chosen["folder"], chosen["name"], chosen["layout"], chosen["keep"], chosen["preset"]
            )
        except (ValueError, KeyError, NewerParams) as error:
            QMessageBox.warning(self, "Cannot Tether", str(error))
            return
        self._last_shot = None
        self.sidebar.select(chosen["folder"])

    def capture(self):
        if self.camera is not None and self.camera.connected:
            self.camera.capture(self.camera_bar.focus.isChecked())

    def _on_camera_changed(self):
        self.camera_bar.show_service(self.camera)
        self.capture_action.setEnabled(self.camera.connected and not self.camera.ending)
        self._on_tether_changed()

    def _on_tether_changed(self):
        folder = self.tether.folder
        info = next((f for f in self.service.folders() if f.id == folder), None)
        self.sidebar.set_tethered(folder)
        # A camera session's folder is its bar's
        shooting = self.camera is not None and self.camera.running
        self.tether_status.show_state(
            info.name if info is not None and not shooting else None,
            self.tether.count,
            self.tether.preset,
        )

    def _on_shot_arrived(self, photo_id):
        """A tethered folder's new photo: shown in the viewer, or in the Develop view while
        that is open, unless an older photo is being looked at."""
        page = self.pages.currentIndex()
        following = (
            page in (_GRID, _EMPTY) or self._last_shot is None or self._viewed_id == self._last_shot
        )
        self._last_shot = photo_id
        if following:
            self._showing_shot = (photo_id, page == _DEVELOP)
            self._show_shot()

    def _show_shot(self, loaded=False):
        """The tethered photo to show, once the grid has it; given up when the grid has
        loaded without it (a filter or the folder chosen hides it)."""
        if self._showing_shot is None:
            return
        photo_id, develop = self._showing_shot
        row = self.model.row_of(photo_id)
        if row is None:
            if loaded and not self.model.is_loading():
                self._showing_shot = None
                name = self._photo_name(photo_id)
                self.statusBar().showMessage(f"New photo {name} is not among those shown", 10000)
            return
        self._showing_shot = None
        if develop:
            self._develop_from = _VIEWER
            self.show_develop(row)
        else:
            self.show_photo(row)

    def _on_shot_failed(self, path, why):
        self.statusBar().showMessage(f"Could not read {path}: {why}", 15000)

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

    def _on_collection_selected(self, collection_id, smart):
        settings.set_selected_folder(None)
        self._refresh_choices(None)
        if smart:
            self._apply_smart(collection_id)
        self._apply_filter()
        if self.pages.currentIndex() == _VIEWER:
            self.show_grid()

    def _on_import_selected(self, _job_id):
        settings.set_selected_folder(None)
        self._refresh_choices(None)
        self._apply_filter()
        if self.pages.currentIndex() == _VIEWER:
            self.show_grid()

    def _refresh_last_import(self):
        self.sidebar.set_last_import(self.service.last_import())
        if self.sidebar.current_import() is not None:
            self._apply_filter()

    def _refresh_choices(self, folder_id=None):
        folder_id = folder_id if folder_id is not None else self.sidebar.current_folder()
        self.filters.blockSignals(True)
        self.filters.set_choices(*self.service.choices(folder_id))
        self.filters.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self):
        year, camera, lens = self.filters.values()
        collection = self.sidebar.current_collection()
        shown = Filter(
            self.sidebar.current_folder(),
            year,
            camera,
            lens,
            collection[0] if collection is not None and not collection[1] else None,
            self.sidebar.current_import(),
        )
        if shown != self.model.filter or self.model.rowCount() == 0:
            self.model.set_filter(shown)
        self._update_title()

    # Search

    def focus_search(self):
        if self.search is None or self.pages.currentIndex() == _EMPTY:
            return
        if self.pages.currentIndex() != _GRID:
            self.go_back() if self.pages.currentIndex() == _DEVELOP else self.show_grid()
            if self.pages.currentIndex() != _GRID:
                return
        self.search_field.focus()

    def search_text(self, text):
        """Show the photos that match `text`; "" shows them all again."""
        if self.search is None:
            return
        if text and not self._model_ready():
            return
        if text and self.pages.currentIndex() in (_VIEWER, _DEVELOP):
            # Typed in the field above a photo: the results are in the grid. The Develop
            # view saves the edit on the way out
            if self.pages.currentIndex() == _DEVELOP:
                self.session.close()
            self.show_grid()
            # Still typing: nothing selected, or the next key would replace it
            self.search_field.edit.setFocus()
        self.model.search_text(text)

    def find_similar(self):
        """Show the photos most like the one chosen in the grid or shown in the viewer."""
        if self.search is None:
            return
        if self.pages.currentIndex() == _VIEWER:
            row = self._viewed_row()
        elif self.pages.currentIndex() == _GRID and self.grid.currentIndex().isValid():
            row = self.grid.currentIndex().row()
        else:
            return
        if row is None or not self._model_ready():
            return
        if self.pages.currentIndex() == _VIEWER:
            self.show_grid()
        self.search_field.clear()
        if not self.model.find_similar(row):
            self.statusBar().showMessage(
                f"{self.model.name(row)} is not indexed for search yet", 6000
            )

    def end_search(self):
        self.search_field.clear()
        self.model.clear_search()

    def _model_ready(self):
        """Whether search can run: if the model is not there, offer to fetch it."""
        if self.search.is_ready():
            return True
        if self.search.is_downloading():
            self.statusBar().showMessage("Search works once the model is downloaded", 6000)
            return False
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Download the Search Model?")
        box.setText("Search needs a model that is downloaded once.")
        box.setInformativeText(
            f"It is {self.search.download_size() / 1e6:,.0f} MB, from Hugging Face "
            "(SigLIP 2 by Google). After that, everything runs on this computer: "
            "your photos and searches never leave it.\n\n"
            "Lumina then indexes your photos in the background, newest first."
        )
        download = box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not Now", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(download)
        box.exec()
        if box.clickedButton() is download:
            self.search.download()
        return False

    def _on_model_changed(self):
        if self.search.is_downloading():
            self.search_status.show_download(0, self.search.download_size())
            return
        self.search_status.download_ended()
        # Typed while it came: search now
        if self.search.is_ready() and self.search_field.text():
            self.model.search_text(self.search_field.text())

    def _on_download_failed(self, error):
        self.search_status.download_ended(error)
        QMessageBox.warning(
            self,
            "Download Failed",
            f"The search model could not be downloaded.\n\n{error}\n\n"
            "What came is kept: the next try continues from there.",
        )

    def _on_search_changed(self):
        search = self.model.search
        self.search_field.show_similar(
            search.label if search is not None and search.kind == SIMILAR else None
        )
        self._update_status()

    def _grid_menu(self, point):
        index = self.grid.indexAt(point)
        if not index.isValid():
            return
        if not self.grid.selectionModel().isSelected(index):
            self.grid.setCurrentIndex(index)
        else:
            # Keep the selection: flagging from the menu is for all of it
            self.grid.selectionModel().setCurrentIndex(
                index, self.grid.selectionModel().SelectionFlag.NoUpdate
            )
        self._photo_menu(in_grid=True).exec(self.grid.viewport().mapToGlobal(point))

    def _photo_menu(self, in_grid):
        """For the photos chosen in the grid or the one in the viewer. Syncing is from
        one photo chosen in the grid to the others."""
        menu = QMenu(self)
        menu.addAction(self.edit_action)
        if self.search is not None:
            menu.addAction(self.similar_action)
        menu.addSeparator()
        menu.addAction(self.keywords_action)
        self._add_collection_menu(menu)
        menu.addSeparator()
        menu.addAction(self.pick_action)
        menu.addAction(self.reject_action)
        menu.addAction(self.unflag_action)
        rating = menu.addMenu("Rating")
        for action in self.rate_actions[1:]:
            rating.addAction(action)
        rating.addSeparator()
        rating.addAction(self.rate_actions[0])
        menu.addSeparator()
        menu.addAction(self.make_copy_action)
        if not in_grid and self._copy_to_delete() is not None:
            menu.addAction(self.delete_copy_action)
        else:
            rows = self._flag_rows()
            if len(rows) == 1 and self.model.copies(rows[0]):
                shown = self.model.copies_open(rows[0])
                menu.addAction(
                    "Hide Virtual Copies" if shown else "Show Virtual Copies",
                    lambda: self.model.toggle_copies(self.model.photo_id(rows[0])),
                )
            if self._copy_to_delete() is not None:
                menu.addAction(self.delete_copy_action)
        if in_grid:
            menu.addSeparator()
            for action in self.stack_actions:
                menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.copy_settings_action)
        menu.addAction(self.paste_settings_action)
        if in_grid:
            menu.addAction(self.sync_settings_action)
        if self.merge is not None:
            menu.addSeparator()
            menu.addAction(self.merge_hdr_action)
            menu.addAction(self.stitch_panorama_action)
        return menu

    # Flags

    def flag_photos(self, flag):
        """Thumbs up (PICKED), down (REJECTED) or neither for the photo shown, or the
        ones chosen in the grid; when they have it already, clear it."""
        page = self.pages.currentIndex()
        rows = self._flag_rows()
        given = self.model.toggle_flag(rows, flag) if rows else None
        if given in (PICKED, REJECTED) and page == _VIEWER and settings.advance_after_flag():
            self.step(1)
        self._update_flag_actions()
        self._update_status()

    def rate_photos(self, stars):
        """`stars` (0 for none) for the photo shown, open or the ones chosen in the grid;
        when they have that many already, clear it."""
        page = self.pages.currentIndex()
        rows = self._flag_rows()
        given = self.model.toggle_rating(rows, stars) if rows else None
        if given and page == _VIEWER and settings.advance_after_flag():
            self.step(1)
        self._update_flag_actions()
        self._update_status()

    def undo_flags(self):
        what = {"rating": "Rating", "keywords": "Keywords"}.get(self.model.last_change(), "Flag")
        if self.model.undo_flags():
            self.statusBar().showMessage(f"{what} change undone", 3000)
        self._update_flag_actions()
        self._update_status()

    def redo_flags(self):
        if self.model.redo_flags():
            self.statusBar().showMessage("Change redone", 3000)
        self._update_flag_actions()
        self._update_status()

    def set_flag_filter(self, flag):
        """Show only photos with a thumbs up, with none, or with a thumbs down, or all."""
        self.model.set_flag_filter(flag)
        self._update_status()

    def set_rating_filter(self, stars):
        """Show only photos with `stars` or more, only those without, or all."""
        self.model.set_rating_filter(stars)
        self._update_status()

    def set_edit_filter(self, which):
        """Show only edited photos, only unedited ones, or all."""
        self.model.set_edit_filter(which)
        self._update_status()

    def set_rejected(self, mode):
        """Show, dim or hide thumbs-down photos, and remember it."""
        settings.set_rejected_photos(mode)
        self.model.set_rejected(mode)
        self.grid.viewport().update()
        self._update_status()

    def _flag_rows(self):
        page = self.pages.currentIndex()
        if page in (_VIEWER, _DEVELOP):
            row = self._viewed_row()
            return [row] if row is not None else []
        if page == _GRID:
            rows = sorted(index.row() for index in self.grid.selectionModel().selectedIndexes())
            if not rows and self.grid.currentIndex().isValid():
                rows = [self.grid.currentIndex().row()]
            return rows
        return []

    def _update_flag_actions(self, *_args):
        rows = self._flag_rows()
        given = {self.model.flag(row) for row in rows}
        for action, flag in ((self.pick_action, PICKED), (self.reject_action, REJECTED)):
            action.setEnabled(bool(rows))
            action.setChecked(given == {flag})
        self.unflag_action.setEnabled(bool(rows))
        given_stars = {self.model.rating(row) for row in rows}
        for action in self.rate_actions:
            action.setEnabled(bool(rows))
        for number, action in enumerate(self.rate_actions[1:], 1):
            action.setCheckable(True)
            action.setChecked(given_stars == {number})
        # Photos that differ show no stars, as neither flag button shows as set
        self.rating_bar.setEnabled(bool(rows))
        self.rating_bar.set_rating(next(iter(given_stars)) if len(given_stars) == 1 else 0)
        self._update_copy_actions()
        self._update_stack_actions()
        page = self.pages.currentIndex()
        browsing = page in (_GRID, _VIEWER)
        self.undo_flag_action.setEnabled(browsing and self.model.can_undo_flags())
        self.redo_flag_action.setEnabled(browsing and self.model.can_redo_flags())
        self._update_settings_actions()

    def _on_current_changed(self, current, _previous):
        self._update_flag_actions()
        if current.isValid():
            photo_id = self.model.photo_id(current.row())
            # Not while the selection model is telling of the change
            QTimer.singleShot(0, self, lambda: self.model.settle(photo_id))

    # Keywords

    def set_keyword_filter(self, path):
        """Show only the photos given keyword `path` or one below it, or all."""
        self.model.set_keyword_filter(path)
        self.sidebar.select_keyword(path)
        self._update_status()

    def _on_keywords_changed(self):
        items = self.service.keyword_items
        self.sidebar.set_keywords(items)
        self.filters.set_keywords([path for _id, path, _count in items])
        self.model.refilter()
        self._update_flag_actions()
        self._update_status()

    def edit_keywords(self):
        """Ask which keywords the photos chosen (or shown) get or lose."""
        rows = self._flag_rows()
        if not rows:
            return
        dialog = KeywordsDialog(self)

        def refresh():
            have = {}
            for row in rows:
                for path in self.model.keywords(row):
                    have[path] = have.get(path, 0) + 1
            names = self.model.name(rows[0]) if len(rows) == 1 else f"{len(rows)} photos"
            known = [path for _id, path, _count in self.service.keyword_items]
            dialog.set_state(f"Keywords of {names}", known, have, len(rows))

        def add(text):
            given = self.model.add_keywords(rows, text.split(","))
            if not given:
                self.statusBar().showMessage("Nothing to add", 3000)
            refresh()

        def remove(paths):
            self.model.remove_keywords(rows, paths)
            refresh()

        dialog.add_requested.connect(add)
        dialog.remove_requested.connect(remove)
        refresh()
        dialog.exec()
        self._update_flag_actions()
        self._update_status()

    def rename_keyword(self, path):
        names = path.split(" > ")
        text, ok = QInputDialog.getText(
            self, "Rename Keyword", f"New name for {path}:", text=names[-1]
        )
        new = " > ".join([*names[:-1], text.strip()]) if ok and text.strip() else None
        if new is None or new == path:
            return
        try:
            self.service.rename_keyword(path, new)
        except ValueError as error:
            QMessageBox.warning(self, "Rename Keyword", str(error))
            return
        if self.filters.keyword_value() == path:
            self.filters.set_keyword(new)

    def delete_keyword(self, path):
        answer = QMessageBox.question(
            self,
            "Delete Keyword",
            f"Take {path}, and any keywords inside it, off every photo? The photos stay.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.service.delete_keyword(path)

    # Collections

    def _on_collections_changed(self, collection_id):
        chosen = self.sidebar.current_collection()
        self.sidebar.set_collections(self.service.collections())
        if chosen is not None and self.sidebar.current_collection() is None:
            # The one shown was deleted
            self.sidebar.select(None)
        elif chosen is not None and chosen[0] == collection_id and not chosen[1]:
            self.model.refresh()
        self._update_title()

    def _add_collection_menu(self, menu):
        manual = [c for c in self.service.collections() if c.query is None]
        sub = menu.addMenu("Add to Collection")
        for collection in manual:
            sub.addAction(
                collection.name, lambda _on=False, c=collection.id: self.add_to_collection(c)
            )
        if manual:
            sub.addSeparator()
        sub.addAction("New Collection…", lambda: self.new_collection(add_chosen=True))
        sub.setEnabled(bool(self._flag_rows()))
        shown = self.sidebar.current_collection()
        if shown is not None and not shown[1]:
            action = menu.addAction(
                "Remove from This Collection", lambda: self.remove_from_collection(shown[0])
            )
            action.setEnabled(bool(self._flag_rows()))

    def _ask_name(self, title, text=""):
        name, ok = QInputDialog.getText(self, title, "Name:", text=text)
        return name.strip() if ok and name.strip() else None

    def new_collection(self, add_chosen=False):
        """Make a manual collection, asking its name; the photos chosen go into it."""
        name = self._ask_name("New Collection")
        if name is None:
            return None
        try:
            made = self.service.add_collection(name)
        except ValueError as error:
            QMessageBox.warning(self, "New Collection", str(error))
            return None
        if add_chosen:
            self.add_to_collection(made)
        return made

    def add_to_collection(self, collection_id):
        rows = self._flag_rows()
        self.add_ids_to_collection(collection_id, [self.model.photo_id(row) for row in rows])

    def add_ids_to_collection(self, collection_id, photo_ids):
        added = self.service.add_to_collection(collection_id, photo_ids)
        name = next((c.name for c in self.service.collections() if c.id == collection_id), "")
        left = len(photo_ids) - added
        text = f"Added {added} photo{'' if added == 1 else 's'} to {name}"
        if left:
            text += f" ({left} already there)"
        self.statusBar().showMessage(text, 5000)

    def remove_from_collection(self, collection_id):
        ids = [self.model.photo_id(row) for row in self._flag_rows()]
        if ids:
            self.service.remove_from_collection(collection_id, ids)

    def rename_collection(self, collection_id):
        current = next((c.name for c in self.service.collections() if c.id == collection_id), "")
        name = self._ask_name("Rename Collection", current)
        if name is None or name == current:
            return
        try:
            self.service.rename_collection(collection_id, name)
        except ValueError as error:
            QMessageBox.warning(self, "Rename Collection", str(error))

    def delete_collection(self, collection_id):
        found = next((c for c in self.service.collections() if c.id == collection_id), None)
        if found is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete Collection",
            f"Delete the collection {found.name}? The photos stay in the library.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.service.delete_collection(collection_id)

    def _current_query(self):
        """The filters now set, as a smart collection's query."""
        year, camera, lens = self.filters.values()
        search = self.model.search
        return SmartQuery(
            year=year,
            model=camera,
            lens=lens,
            flag=self.filters.flag_value(),
            rating=self.filters.rating_value(),
            edit=self.filters.edit_value(),
            keyword=self.filters.keyword_value(),
            text=search.query if search is not None and search.kind == TEXT else None,
        )

    def new_smart_collection(self):
        """Save the filters (and the search) now set as a smart collection."""
        query = self._current_query()
        if query.is_empty():
            return
        name = self._ask_name("New Smart Collection")
        if name is None:
            return
        try:
            made = self.service.add_collection(name, query.to_json())
        except ValueError as error:
            QMessageBox.warning(self, "New Smart Collection", str(error))
            return
        self.sidebar.select_collection(made)

    def update_smart_collection(self, collection_id):
        query = self._current_query()
        if query.is_empty():
            return
        self.service.set_collection_query(collection_id, query.to_json())
        self.statusBar().showMessage("Smart collection saved with the filters now set", 4000)

    def _apply_smart(self, collection_id):
        found = next((c for c in self.service.collections() if c.id == collection_id), None)
        if found is None or found.query is None:
            return
        query = SmartQuery.from_json(found.query)
        self.filters.apply(
            {
                "year": query.year,
                "camera": query.model,
                "lens": query.lens,
                "flag": query.flag,
                "rating": query.rating,
                "edit": query.edit,
                "keyword": query.keyword,
            }
        )
        if query.text and self.search is not None and self._model_ready():
            self.search_field.set_text(query.text)
        elif self.model.is_searching():
            self.end_search()

    # Copying settings from one photo to others

    def _settings_source(self):
        """The id of the photo whose settings Copy takes: the one open, shown or current."""
        page = self.pages.currentIndex()
        if page == _DEVELOP:
            return self.session.photo_id
        if page == _VIEWER:
            return self._viewed_id
        if page == _GRID and self.grid.currentIndex().isValid():
            return self.model.photo_id(self.grid.currentIndex().row())
        return None

    def _sync_rows(self):
        """The rows Sync would copy to: the ones chosen in the grid but the current one;
        none unless the current one is among them and there are others."""
        if self.pages.currentIndex() != _GRID or not self.grid.currentIndex().isValid():
            return []
        chosen = {index.row() for index in self.grid.selectionModel().selectedIndexes()}
        current = self.grid.currentIndex().row()
        return sorted(chosen - {current}) if current in chosen else []

    def _update_settings_actions(self):
        page = self.pages.currentIndex()
        self.copy_settings_action.setEnabled(self._settings_source() is not None)
        can_paste = self.service.copied is not None and (
            page == _DEVELOP or (page in (_GRID, _VIEWER) and bool(self._flag_rows()))
        )
        self.paste_settings_action.setEnabled(can_paste)
        self.sync_settings_action.setEnabled(bool(self._sync_rows()))
        self.develop.paste_settings_button.setEnabled(self.service.copied is not None)

    # Merging

    def merge_hdr(self):
        """Merge the photos chosen, or the bracket of the one chosen or shown, into one HDR
        photo, after asking how."""
        if self.merge is None:
            return
        ids = [self.model.photo_id(row) for row in self._flag_rows()]
        if not ids:
            return
        ids = self.merge.bracket(ids)
        problem = self.merge.check(ids)
        if problem is not None:
            if len(ids) == 1:
                problem = (
                    f"{self._photo_name(ids[0])} is not in a bracket that Lumina can find. "
                    "Choose the photos to merge in the grid."
                )
            QMessageBox.information(self, "Merge to HDR", problem)
            return
        dialog = MergeDialog(self.merge.frames(ids), parent=self)
        if dialog.exec() != MergeDialog.DialogCode.Accepted:
            return
        try:
            self.merge.merge_hdr(ids, dialog.deghost(), dialog.align())
        except ValueError as error:
            QMessageBox.information(self, "Merge to HDR", str(error))

    def stitch_panorama(self):
        """Stitch the photos chosen into a panorama, after asking how."""
        if self.merge is None:
            return
        ids = [self.model.photo_id(row) for row in self._flag_rows()]
        if not ids:
            return
        problem = self.merge.check_panorama(ids)
        if problem is not None:
            if len(ids) == 1:
                problem = "Choose the photos to stitch in the grid: two or more that overlap."
            QMessageBox.information(self, "Stitch Panorama", problem)
            return
        dialog = PanoramaDialog(self.merge.panorama_frames(ids), parent=self)
        if dialog.exec() != PanoramaDialog.DialogCode.Accepted:
            return
        try:
            self.merge.stitch_panorama(ids, dialog.projection(), dialog.crop())
        except ValueError as error:
            QMessageBox.information(self, "Stitch Panorama", str(error))

    def _photo_name(self, photo_id):
        photo = self.service.photo_details(photo_id)
        return os.path.basename(photo["path"]) if photo else str(photo_id)

    def copy_settings(self):
        """Keep some sections of the edit of the photo shown, open or current, to paste
        over other photos."""
        photo_id = self._settings_source()
        if photo_id is None:
            return
        dialog = CopyDialog(parent=self)
        if dialog.exec() != CopyDialog.DialogCode.Accepted:
            return
        sections, name = dialog.sections(), self._photo_name(photo_id)
        try:
            if self.pages.currentIndex() == _DEVELOP:
                # The edit as it is now, saved or not
                document = self.session.settings_document(sections)
            else:
                document = self.service.settings_document(photo_id, sections)
        except NewerParams as error:
            QMessageBox.warning(
                self, "Copy Settings", f"The edit of {name} was made by a newer Lumina ({error})."
            )
            return
        self.service.copy_settings(document, name, sections)
        self.statusBar().showMessage(f"Copied settings of {name}", 5000)

    def paste_settings(self):
        """Put the copied settings over the edit of the photo open, shown, or the photos
        chosen in the grid."""
        copied = self.service.copied
        if copied is None:
            return
        if self.pages.currentIndex() == _DEVELOP:
            self.session.paste_settings(copied.document)
            self.statusBar().showMessage(f"Pasted settings of {copied.name}", 5000)
            return
        ids = [self.model.photo_id(row) for row in self._flag_rows()]
        if not ids:
            return
        if len(ids) > 1:
            answer = QMessageBox.question(
                self,
                "Paste Settings",
                f"Paste the settings of {copied.name} to {len(ids)} photos?\n\n"
                "They replace the same parts of those photos' edits; the rest of each "
                "edit stays as it is.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._pasted(self.service.paste_settings(ids), copied.name)

    def sync_settings(self):
        """Copy sections of the edit of the current photo to the others chosen."""
        rows = self._sync_rows()
        if not rows:
            return
        source = self.model.photo_id(self.grid.currentIndex().row())
        name = self._photo_name(source)
        dialog = CopyDialog(
            "Sync Settings",
            f"Copy these parts of the edit of {name} to the {len(rows)} other "
            f"photo{'' if len(rows) == 1 else 's'} chosen:",
            "Sync",
            parent=self,
        )
        if dialog.exec() != CopyDialog.DialogCode.Accepted:
            return
        try:
            document = self.service.settings_document(source, dialog.sections())
        except NewerParams as error:
            QMessageBox.warning(
                self, "Sync Settings", f"The edit of {name} was made by a newer Lumina ({error})."
            )
            return
        ids = [self.model.photo_id(row) for row in rows]
        self._pasted(self.service.paste_settings(ids, document), name)

    def _pasted(self, result, source_name):
        """Tell of a paste that went to photos in the library."""
        if self.pages.currentIndex() == _VIEWER and self._viewed_row() is not None:
            # The photo shown has a new edit
            self.show_photo(self._viewed_row())
        photos = f"{result.done} photo{'' if result.done == 1 else 's'}"
        self.statusBar().showMessage(f"Settings of {source_name} put on {photos}", 6000)
        if result.failed:
            lines = "\n".join(f"{name}: {why}" for name, why in result.failed)
            QMessageBox.warning(
                self, "Paste Settings", f"These photos were left as they were:\n\n{lines}"
            )

    # The grid's thumbnail size

    def set_grid_size(self, name):
        """Show thumbnails small, medium or large, and remember it."""
        if name not in GRID_ICONS:
            return
        settings.set_grid_size(name)
        self._apply_grid_size(name)

    def step_grid_size(self, delta):
        """One size larger (1) or smaller (-1), if there is one."""
        index = GRID_SIZE_NAMES.index(self.grid_size.value()) + delta
        if 0 <= index < len(GRID_SIZE_NAMES):
            self.set_grid_size(GRID_SIZE_NAMES[index])

    def visible_rows(self):
        """(first, last) of the rows in the grid's view and a screenful above and below,
        or None while there are none."""
        cell = self.grid.gridSize()
        if self.model.rowCount() == 0 or cell.width() <= 0 or cell.height() <= 0:
            return None
        view = self.grid.viewport().size()
        columns = max(1, view.width() // cell.width())
        screenful = columns * (view.height() // cell.height() + 1)
        first = 0
        for x in range(cell.width() // 2, max(view.width(), 1), cell.width()):
            index = self.grid.indexAt(QPoint(x, 2))
            if index.isValid():
                first = index.row()
                break
        return max(0, first - screenful), first + 2 * screenful

    def trim_thumbnails(self):
        """Stop waiting for the thumbnails of rows far from the view."""
        rows = self.visible_rows()
        if rows is not None:
            self.model.keep_thumbnails(*rows)

    def _apply_grid_size(self, name):
        self.grid_size.set_value(name)
        side = style.px(GRID_ICONS[name])
        cell = QSize(
            side + style.gap(GRID_CELL_EXTRA.width()), side + style.gap(GRID_CELL_EXTRA.height())
        )
        self.grid.setIconSize(QSize(side, side))
        self._delegate.set_size(cell.shrunkBy(_margins(style.gap(GRID_GAP))))
        self.grid.setGridSize(cell)
        # The photo the user was on stays in view
        if self.grid.currentIndex().isValid():
            self.grid.scrollTo(self.grid.currentIndex(), QListView.ScrollHint.PositionAtCenter)

    def eventFilter(self, watched, event):
        # Ctrl and the wheel over the grid: larger or smaller thumbnails
        if (
            event.type() == QEvent.Type.Wheel
            and watched is self.grid.viewport()
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            steps = event.angleDelta().y()
            if steps:
                self.step_grid_size(1 if steps > 0 else -1)
            return True
        # A click on a stack's or the virtual copies' badge opens or closes it, and chooses
        # nothing
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and watched is self.grid.viewport()
            and event.button() == Qt.MouseButton.LeftButton
        ):
            index = self.grid.indexAt(event.position().toPoint())
            if index.isValid() and self._delegate.stack_badge_at(
                self.grid, index, event.position()
            ):
                self.toggle_stack_of(index.row())
                return True
            if index.isValid() and self._delegate.copies_badge_at(
                self.grid, index, event.position()
            ):
                self.model.toggle_copies(self.model.photo_id(index.row()))
                return True
        return super().eventFilter(watched, event)

    # Keeping the grid's place while a scan adds photos

    def _save_place(self):
        top = self.grid.indexAt(self.grid.viewport().rect().topLeft() + _inset())
        current = self.grid.currentIndex()
        # How far the top photo's cell is above (negative) or below the view's top: a view
        # scrolled to the middle of a row stays so
        offset = self.grid.visualRect(top).top() - self.grid.viewport().rect().top()
        self._place = (
            self.model.photo_id(top.row()) if top.isValid() else None,
            self.model.photo_id(current.row()) if current.isValid() else None,
            offset if top.isValid() else 0,
        )

    def _restore_place(self):
        top, current, offset = self._place
        for photo_id, make_current in ((current, True), (top, False)):
            row = self.model.row_of(photo_id) if photo_id is not None else None
            if row is None:
                continue
            index = self.model.index(row)
            if make_current:
                self.grid.setCurrentIndex(index)
            else:
                # The view lays its cells out later; where the photo is now is asked of it
                # when it is laid out
                self.grid.doItemsLayout()
                self.grid.scrollTo(index, QListView.ScrollHint.PositionAtTop)
                bar = self.grid.verticalScrollBar()
                bar.setValue(bar.value() - offset)
        self._update_status()

    # The grid and the viewer

    def show_grid(self):
        self.previews.cancel()
        self.viewer.reset_zoom()
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

    def show_photo(self, row, copy=0):
        """The photo of `row`, as its own edit makes it or as virtual copy `copy` does."""
        if not 0 <= row < self.model.rowCount():
            return
        self._viewed_id = self.model.photo_id(row)
        self._viewed_copy = self._existing_copy(copy)
        self._full_requested = None
        # Moved on: photos flagged out of the view leave it
        self.model.settle(self._viewed_id)
        row = self.model.row_of(self._viewed_id)
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
        self.title.setText(self._viewed_title(row))
        if path is not None:
            # Zoomed: the same place of this photo at full size, as the last one's
            if self.viewer.is_zoomed():
                self._request_full()
            else:
                self.previews.request(path, self._viewer_side(), self._viewed_edit())
        else:
            self.previews.cancel()
        self._update_status()

    def _viewed_edit(self):
        # From the library, not the grid's rows: an edit just made may not be in them yet
        return self.service.edit_json(self._viewed_id, self._viewed_copy)

    def _request_full(self):
        path = self._viewed_path()
        if path is None or self._full_requested == (self._viewed_id, self._viewed_copy):
            return
        self._full_requested = (self._viewed_id, self._viewed_copy)
        self.previews.request(path, None, self._viewed_edit())

    def _on_zoom_changed(self):
        if self.pages.currentIndex() == _VIEWER and self.viewer.needs_full():
            self._request_full()
        # Not on every move of a drag: the status bar reads the library
        if self._zoom_state() != self._zoom_shown:
            self._update_status()

    def _zoom_view(self):
        """The image view that zooms: the Develop view's while it shows, else the viewer's."""
        return self.develop.image if self.pages.currentIndex() == _DEVELOP else self.viewer

    def _zoom_loading(self, view):
        """Whether the image shown is short of what the zoom shows."""
        if view is self.viewer:
            return view.needs_full()
        # Zoomed in the Develop view, the photo's own pixels come after the screen-sized image
        return view.is_zoomed() and not view.has_detail()

    def _zoom_state(self):
        view = self._zoom_view()
        return (view.is_zoomed(), view.zoom(), self._zoom_loading(view))

    def step(self, delta):
        row = self._viewed_row()
        if row is None:
            return
        # From the cell of the copy shown (when open), onto the cell next to it
        row = self.model.row_of(self._viewed_id, self._viewed_copy)
        target = row + delta
        if not 0 <= target < self.model.rowCount():
            return
        copy = self.model.copy(target)
        if self.pages.currentIndex() == _DEVELOP:
            self.show_develop(target, copy)
        else:
            self.show_photo(target, copy)

    def go_back(self):
        """Escape: cancel the crop, then out of the Develop view; in the viewer back to
        fitted, then to the grid."""
        if self.pages.currentIndex() == _VIEWER and self.viewer.is_zoomed():
            self.viewer.reset_zoom()
            return
        if self.pages.currentIndex() == _DEVELOP:
            if self.develop.is_cropping():
                self.develop.cancel_crop()
                return
            if self.develop.is_picking():
                self.develop.cancel_picking()
                return
            if self.develop.image.is_zoomed():
                self.develop.image.reset_zoom()
                return
            self.session.close()
            row = self._viewed_row()
            if self._develop_from == _VIEWER and row is not None:
                self.show_photo(row, self._viewed_copy)
                return
        self.show_grid()

    # Editing

    def edit_current(self):
        """Edit the photo shown, or the one chosen in the grid."""
        if self.pages.currentIndex() == _VIEWER:
            row, copy = self._viewed_row(), self._viewed_copy
        elif self.pages.currentIndex() == _GRID and self.grid.currentIndex().isValid():
            row = self.grid.currentIndex().row()
            copy = self.model.copy(row)
        else:
            return
        if row is not None:
            self._develop_from = self.pages.currentIndex()
            self.show_develop(row, copy)

    def show_develop(self, row, copy=0):
        """Edit the photo of `row`: its own edit, or virtual copy `copy`."""
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
        if self.pages.currentIndex() != _DEVELOP:
            # Stepping keeps the zoom, as in the viewer; coming in from outside starts fitted
            self.develop.image.reset_zoom()
        self._viewed_id = self.model.photo_id(row)
        self._viewed_copy = self._existing_copy(copy)
        self.model.settle(self._viewed_id)
        row = self.model.row_of(self._viewed_id)
        self.develop.show_loading(self._viewed_title(row))
        self.session.screen_side = self._viewer_side()
        self._show_page(_DEVELOP)
        self.session.open(self._viewed_id, path, self._viewed_copy)
        self.title.setText(self._viewed_title(row))
        self.develop.setFocus()
        self._update_status()

    # Stacks

    def _stack_rows(self):
        """The rows chosen in the grid (stacks are made and taken apart there)."""
        if self.pages.currentIndex() != _GRID:
            return []
        return self._flag_rows()

    def _update_stack_actions(self, *_args):
        rows = self._stack_rows()
        stacked = [row for row in rows if self.model.stack(row) is not None]
        self.stack_action.setEnabled(len(rows) > 1)
        self.unstack_action.setEnabled(bool(stacked))
        self.leave_stack_action.setEnabled(bool(stacked))
        self.toggle_stack_action.setEnabled(bool(stacked))
        self.stack_top_action.setEnabled(
            len(rows) == 1 and bool(stacked) and not self.model.is_stack_top(rows[0])
        )

    def stack_photos(self):
        """Group the photos chosen in one stack, the first shown on top, collapsed."""
        rows = self._stack_rows()
        if len(rows) < 2:
            return
        self.service.make_stack([self.model.photo_id(row) for row in rows])
        self.statusBar().showMessage(f"Stacked {len(rows)} photos", 4000)

    def unstack_photos(self):
        """Take apart the stacks the photos chosen are in."""
        ids = [self.model.photo_id(row) for row in self._stack_rows()]
        if ids:
            self.service.unstack(ids)

    def leave_stack(self):
        """Take the photos chosen out of their stacks; the rest of each stack stays."""
        ids = [
            self.model.photo_id(row)
            for row in self._stack_rows()
            if self.model.stack(row) is not None
        ]
        if ids:
            self.service.leave_stack(ids)

    def toggle_stack_of(self, row):
        """Open the stack of `row` if it is collapsed, else close it."""
        stack = self.model.stack(row)
        if stack is not None:
            collapsed = self.model.data(self.model.index(row), STACK_ROLE)[2]
            self.service.set_stacks_collapsed([stack], not collapsed)

    def toggle_stacks(self):
        """Expand the collapsed stacks among the photos chosen, or collapse them when
        none is collapsed."""
        rows = [row for row in self._stack_rows() if self.model.stack(row) is not None]
        if not rows:
            return
        collapsed = {
            self.model.stack(row): self.model.data(self.model.index(row), STACK_ROLE)[2]
            for row in rows
        }
        expand = any(collapsed.values())
        self.service.set_stacks_collapsed(list(collapsed), not expand)

    def set_stack_top(self):
        """Make the photo chosen the top of its stack, the cell shown while it is collapsed."""
        rows = self._stack_rows()
        if len(rows) == 1 and self.model.stack(rows[0]) is not None:
            self.service.set_stack_top(self.model.photo_id(rows[0]))

    # Virtual copies

    def _existing_copy(self, copy):
        """`copy` if the viewed photo has it, else its own edit (0)."""
        if copy and copy in self.service.copies(self._viewed_id):
            return copy
        return 0

    def _viewed_title(self, row):
        name = self.model.name(row)
        return f"{name} (copy {self._viewed_copy})" if self._viewed_copy else name

    def _update_copy_actions(self, *_args):
        page = self.pages.currentIndex()
        viewing = page in (_VIEWER, _DEVELOP)
        self.make_copy_action.setEnabled(bool(self._flag_rows()))
        self.delete_copy_action.setEnabled(self._copy_to_delete() is not None)
        several = viewing and self._viewed_id is not None and bool(self._others())
        self.previous_copy_action.setEnabled(several)
        self.next_copy_action.setEnabled(several)

    def _others(self):
        return self.service.copies(self._viewed_id)

    def _show_edit(self, copy):
        """The viewer or the Develop view on the same photo, as edit `copy` of it."""
        row = self._viewed_row()
        if row is None:
            return
        if self.pages.currentIndex() == _DEVELOP:
            self.show_develop(row, copy)
        else:
            self.show_photo(row, copy)

    def _copy_to_delete(self):
        """(photo id, copy) of the virtual copy the viewer or the Develop view shows, or of
        the one cell chosen in the grid when it is a copy's; else None."""
        page = self.pages.currentIndex()
        if page in (_VIEWER, _DEVELOP):
            if self._viewed_id is not None and self._viewed_copy > 0:
                return self._viewed_id, self._viewed_copy
            return None
        rows = self._flag_rows() if page == _GRID else []
        if len(rows) == 1 and self.model.copy(rows[0]) > 0:
            return self.model.photo_id(rows[0]), self.model.copy(rows[0])
        return None

    def make_virtual_copy(self):
        """A new virtual copy of the photo shown, from the edit as it is, and shown. In
        the grid one of each photo chosen, from its own edit, and nothing else changes."""
        page = self.pages.currentIndex()
        if page == _GRID:
            ids = [self.model.photo_id(row) for row in self._flag_rows()]
            if ids:
                self.service.make_copies(ids)
                what = "a photo" if len(ids) == 1 else f"{len(ids)} photos"
                self.statusBar().showMessage(f"Made a virtual copy of {what}", 4000)
            return
        if page not in (_VIEWER, _DEVELOP) or self._viewed_id is None:
            return
        if page == _DEVELOP and self.session.is_open():
            number = self.service.make_copy(
                self._viewed_id, params_json=self.session.params.to_json()
            )
        else:
            number = self.service.make_copy(self._viewed_id, self._viewed_copy)
        self._show_edit(number)

    def delete_virtual_copy(self):
        """Delete the virtual copy shown or chosen, after asking. In the viewer or Develop
        view the one before it is shown after."""
        target = self._copy_to_delete()
        if target is None:
            return
        photo_id, copy = target
        answer = QMessageBox.question(
            self,
            "Delete Virtual Copy",
            f"Delete copy {copy} of this photo and its edit? "
            "The photo and its other edits are not touched.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.pages.currentIndex() == _GRID:
            self.service.delete_copy(photo_id, copy)
            return
        # Closed first, so that nothing is saved over the copy once it is gone
        if self.pages.currentIndex() == _DEVELOP:
            self.session.close()
        numbers = [0, *self.service.copies(photo_id)]
        before = numbers[max(0, numbers.index(copy) - 1)]
        self.service.delete_copy(photo_id, copy)
        self._show_edit(before)

    def step_copy(self, delta):
        """The next or previous edit of the photo shown: its own, then its copies."""
        if self._viewed_id is None:
            return
        numbers = [0, *self.service.copies(self._viewed_id)]
        if self._viewed_copy not in numbers:
            return
        place = numbers.index(self._viewed_copy) + delta
        if 0 <= place < len(numbers):
            self._show_edit(numbers[place])

    def _update_history(self):
        self.undo_action.setEnabled(self.session.can_undo())
        self.redo_action.setEnabled(self.session.can_redo())

    def export_photo(self):
        if not self.session.is_open():
            return
        dialog = ExportDialog(self.session.path, self, self.session.copy)
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

    def _on_preview_ready(self, path, image, full):
        if self._viewed_path() == path:
            self.viewer.set_image(image, full)
            self._on_zoom_changed()

    def _on_preview_failed(self, path, message):
        if self._viewed_path() == path:
            self.viewer.set_message(f"Cannot show {os.path.basename(path)}\n{message}")

    def _show_page(self, page):
        self.pages.setCurrentIndex(page)
        viewing = page in (_VIEWER, _DEVELOP)
        developing = page == _DEVELOP
        self.navigation.setVisible(viewing)
        self.editing.setVisible(developing)
        self.marking.setVisible(page in (_GRID, _VIEWER, _DEVELOP))
        self.search_field.setVisible(self.search is not None and page in (_GRID, _VIEWER, _DEVELOP))
        self.grid_view_options.setVisible(page == _GRID)
        for action in (self.back_action, self.previous_action, self.next_action):
            action.setEnabled(viewing)
        for action in (self.reset_action, self.export_action):
            action.setEnabled(developing)
        self.edit_action.setEnabled(page in (_GRID, _VIEWER))
        self._update_history()
        self._update_flag_actions()
        if not viewing:
            self._update_title()
        self._update_status()

    def _update_title(self):
        if self.pages.currentIndex() in (_VIEWER, _DEVELOP):
            return
        folder = self.sidebar.current_folder()
        info = next((f for f in self.service.folders() if f.id == folder), None)
        name = info.name if info is not None else "All Photos"
        collection = self.sidebar.current_collection()
        if self.sidebar.current_import() is not None:
            name = "Last Import"
        elif collection is not None:
            found = next((c for c in self.service.collections() if c.id == collection[0]), None)
            name = found.name if found is not None else name
        self.title.setText(name if self.service.folders() else APP_NAME)
        self.setWindowTitle(f"{name} — {APP_NAME}" if self.service.folders() else APP_NAME)

    def _update_status(self, *_args):
        self._zoom_shown = self._zoom_state()
        count = self.model.rowCount()
        row = self._viewed_row()
        if not self.service.folders():
            text = ""
        elif self.pages.currentIndex() in (_VIEWER, _DEVELOP) and row is not None:
            details = describe_photo(self.service.photo_details(self._viewed_id))
            where = self.model.path(row) or f"{self.model.name(row)} (not connected)"
            flag = FLAG_NAMES.get(self.model.flag(row))
            rated = stars.text(self.model.rating(row))
            tagged = ", ".join(self.model.keywords(row))
            tagged = f"Keywords: {tagged}" if tagged else None
            zoom = None
            view = self._zoom_view()
            if view.is_zoomed():
                zoom = f"{round(view.zoom() * 100)} %"
                if self._zoom_loading(view):
                    zoom += ", loading full size…" if view is self.viewer else ", rendering…"
            damaged = "Raw data may be damaged" if self.model.damage(row) else None
            versions = None
            numbers = [0, *self.service.copies(self._viewed_id)]
            if len(numbers) > 1:
                place = numbers.index(self._viewed_copy) + 1 if self._viewed_copy in numbers else 1
                versions = f"Edit {place} of {len(numbers)}"
            parts = (
                f"{row + 1} of {count}",
                versions,
                zoom,
                flag,
                rated,
                tagged,
                damaged,
                details,
                where,
            )
            text = "  ·  ".join(part for part in parts if part)
        else:
            text = f"{count} photo" + ("" if count == 1 else "s")
            search = self.model.search
            if search is not None and self.model.is_loading():
                text = "Searching…"
                if search.kind != SIMILAR and self.search and not self.search.is_text_loaded():
                    text = "Loading the search model…"
            elif search is not None and search.kind == SIMILAR:
                text = (
                    f"{text} like {search.label}"
                    if count
                    else f"{search.label} is not indexed for search yet"
                )
            elif search is not None:
                verb = "matches" if count == 1 else "match"
                text = (
                    f"{text} {verb} “{search.label}”"
                    if count
                    else f"No photos match “{search.label}”"
                )
            hidden = self.model.hidden_count()
            if hidden and not self.model.is_loading():
                text += f"  ·  {hidden} hidden by flags"
            if search is not None and self.search.is_indexing():
                text += "  ·  more as indexing goes on"
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
    return QPoint(style.gap(GRID_GAP * 2), style.gap(GRID_GAP * 2))
