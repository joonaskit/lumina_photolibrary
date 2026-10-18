"""The window: a toolbar; the library's folders on the left; on the right the filters
and the grid, one photo, or, with no folders yet, a way to add one."""

import os

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
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
from lumina.qt_models.develop import DevelopSession
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
    UNFLAGGED,
    LibraryModel,
)
from lumina.qt_models.previews import PreviewLoader
from lumina.ui import badges, damage, flags, settings, shortcuts, stars
from lumina.ui.camera import CameraBar
from lumina.ui.develop_view import DevelopView
from lumina.ui.filters import FilterBar
from lumina.ui.grid_size import NAMES as GRID_SIZE_NAMES
from lumina.ui.grid_size import GridSizeSwitch
from lumina.ui.image_view import ImageView
from lumina.ui.job_status import JobStatus, export_status, import_status, merge_status
from lumina.ui.live_view import LivePanel
from lumina.ui.search import SearchField, SearchStatus
from lumina.ui.settings_dialog import SettingsDialog
from lumina.ui.sidebar import Sidebar
from lumina.ui.tether import TetherStatus
from lumina.ui.window_collecting import CollectingMixin
from lumina.ui.window_common import (  # noqa: F401 (the tests and the app read them here)
    _DEVELOP,
    _EMPTY,
    _GRID,
    _LIVE,
    _NO_PHOTO,
    _TRIM_MS,
    _VIEW_NAMES,
    _VIEWER,
    APP_NAME,
    DIMMED_OPACITY,
    FLAG_NAMES,
    GRID_CELL_EXTRA,
    GRID_GAP,
    GRID_ICONS,
    VIEWER_MAX_SIDE,
    _inset,
    _margins,
    describe_photo,
)
from lumina.ui.window_copies import CopiesMixin
from lumina.ui.window_flagging import FlaggingMixin
from lumina.ui.window_importing import ImportingMixin
from lumina.ui.window_keywording import KeywordingMixin
from lumina.ui.window_merging import MergingMixin
from lumina.ui.window_opening import OpeningMixin
from lumina.ui.window_search import SearchMixin
from lumina.ui.window_settings_copy import SettingsCopyMixin
from lumina.ui.window_stacking import StackingMixin
from lumina.ui.window_tether import TetherMixin


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


class MainWindow(
    TetherMixin,
    SearchMixin,
    FlaggingMixin,
    KeywordingMixin,
    CollectingMixin,
    SettingsCopyMixin,
    MergingMixin,
    StackingMixin,
    CopiesMixin,
    ImportingMixin,
    OpeningMixin,
    QMainWindow,
):
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
        exporter=None,
    ):
        """`search` is the SearchService; without one there is no search. `merge` is the
        MergeService; without one there are no merges. `importer` is the ImportService;
        without one there is no importing. `backups` is the BackupService; without one
        the settings have no Backups section. `tether` is the HotFolder; without one no
        folder can be tethered. `camera` is the CameraService (which takes its photos in
        through `tether`); without one there is no shooting with a camera. `exporter` is the
        ExportService; without one the photos chosen in the grid can't be exported."""
        super().__init__(parent)
        self.service = service
        self.backups = backups
        self.tether = tether
        self.camera = camera
        self.search = search
        self.merge = merge
        self.importer = importer
        self.exporter = exporter
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
        self.live_view = LivePanel()
        self.pages.addWidget(self.live_view)
        self._live_from = _GRID
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
            self.camera_bar.live_requested.connect(self._on_live_requested)
            self.camera_bar.focus_requested.connect(camera.focus)
            camera.live_changed.connect(self._on_live_changed)
            camera.frame_ready.connect(lambda: self.live_view.show_frame(camera.frame))
            camera.saved.connect(self._on_camera_saved)
            self.live_view.live_toggled.connect(camera.set_live)
            self.live_view.zoom_chosen.connect(camera.zoom)
            self.live_view.point_chosen.connect(camera.move_point)
            self.live_view.clipping_toggled.connect(camera.set_clipping)
            self.live_view.last_shot_wanted.connect(self._load_last_shot)
            self.last_shot = PreviewLoader(self)
            self.last_shot.ready.connect(
                lambda _path, image, _full: self.live_view.set_last_shot(image)
            )
            QApplication.instance().applicationStateChanged.connect(
                lambda state: camera.set_active(state == Qt.ApplicationState.ApplicationActive)
            )
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
        self.export_status = export_status()
        self.statusBar().addPermanentWidget(self.export_status)
        if exporter is not None:
            self.export_status.cancel_requested.connect(exporter.cancel)
            exporter.job_changed.connect(self.export_status.show_status)
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
        service.failed.connect(lambda message: self.statusBar().showMessage(message, 10000))
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
        shortcuts.enable(self.capture_action, False, "No camera connected")
        self.capture_action.triggered.connect(self.capture)
        self.addAction(self.capture_action)
        self.preferences_action = QAction("Settings", self)
        self.preferences_action.setShortcut(QKeySequence("Ctrl+,"))
        self.preferences_action.setToolTip("Settings (Ctrl+,)")
        self.preferences_action.triggered.connect(self.open_settings)
        self.addAction(self.preferences_action)
        self.shortcuts_action = QAction("Keyboard Shortcuts", self)
        self.shortcuts_action.setShortcuts([QKeySequence("?"), QKeySequence(Qt.Key.Key_F1)])
        self.shortcuts_action.triggered.connect(self.show_shortcuts)
        self.addAction(self.shortcuts_action)
        self.palette_action = QAction("Find an Action", self)
        self.palette_action.setShortcut(QKeySequence("Ctrl+K"))
        self.palette_action.triggered.connect(self.show_palette)
        # Not among the actions it finds; the overlay shows its key
        self.palette_action.setProperty("listed", False)
        self.addAction(self.palette_action)
        self._shortcut_overlay = None
        self._command_palette = None
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
        self.export_selected_action = QAction("Export Selected…", self)
        self.export_selected_action.setToolTip(
            "Write the chosen photos, with their edits, to a folder as JPEG, PNG or TIFF"
        )
        self.export_selected_action.triggered.connect(self.export_selected)
        self.stack_action = QAction("Stack", self)
        self.stack_action.setShortcut(QKeySequence("Ctrl+G"))
        self.stack_action.setToolTip("Group the chosen photos in one stack (Ctrl+G)")
        self.stack_action.triggered.connect(self.stack_photos)
        self.unstack_action = QAction("Unstack", self)
        self.unstack_action.setShortcut(QKeySequence("Ctrl+Shift+G"))
        self.unstack_action.triggered.connect(self.unstack_photos)
        self.stack_bursts_action = QAction("Stack Bursts", self)
        self.stack_bursts_action.setToolTip(
            "Stack shots taken a moment apart that look alike, the best on top"
        )
        self.stack_bursts_action.triggered.connect(self.stack_bursts)
        self.find_duplicates_action = QAction("Find Duplicates…", self)
        self.find_duplicates_action.setToolTip(
            "Look through the library for photos that look alike or are copies of one file"
        )
        self.find_duplicates_action.triggered.connect(self.find_duplicates)
        self.addAction(self.find_duplicates_action)
        self.similar_dialog = None
        self._similar_scope = set()
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
            self.stack_bursts_action,
            self.leave_stack_action,
            self.toggle_stack_action,
            self.stack_top_action,
        )
        # In the grid's menu with the stack actions, though not a thing done to the chosen
        self.stack_menu_actions = (*self.stack_actions, self.find_duplicates_action)
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
        # How the shortcuts' overlay and the palette group them
        shortcuts.set_group(
            "General",
            self.preferences_action,
            self.find_action,
            self.shortcuts_action,
            self.palette_action,
        )
        shortcuts.set_group(
            "Library",
            self.add_action,
            self.import_action,
            self.edit_action,
            self.similar_action,
            self.find_duplicates_action,
            self.export_selected_action,
            self.larger_action,
            self.smaller_action,
        )
        shortcuts.set_group("Culling", *self.flag_actions)
        shortcuts.set_group(
            "Viewer", self.back_action, self.previous_action, self.next_action, *self.zoom_actions
        )
        shortcuts.set_group(
            "Develop",
            self.undo_action,
            self.redo_action,
            self.reset_action,
            self.export_action,
            *self.settings_actions,
        )
        shortcuts.set_group("Stacks and Copies", *self.stack_actions, *self.copy_actions)
        shortcuts.set_group("Merges", self.merge_hdr_action, self.stitch_panorama_action)
        shortcuts.set_group("Camera", self.camera_action, self.capture_action)

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
        shortcuts.set_group("Library", self.end_search_action)
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

    def show_shortcuts(self):
        """The overlay of the keys of the view that is open."""
        if self._shortcut_overlay is None:
            self._shortcut_overlay = shortcuts.ShortcutOverlay(self)
        self._shortcut_overlay.open(shortcuts.entries(self), _VIEW_NAMES[self.pages.currentIndex()])

    def show_palette(self):
        """Find an action by name and run it."""
        if self._command_palette is None:
            self._command_palette = shortcuts.CommandPalette(self)
        self._command_palette.open(shortcuts.entries(self))

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
        fitted, then to the grid. Out of live view to where it was opened from."""
        if self.pages.currentIndex() == _LIVE:
            self._leave_live()
            return
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
