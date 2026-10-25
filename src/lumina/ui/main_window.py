"""The window: a toolbar; the library's folders on the left; on the right the filters
and the grid, one photo, or, with no folders yet, a way to add one."""

from types import SimpleNamespace

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMenu,
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

from lumina.core.dbtypes import PICKED, REJECTED, UNFLAGGED
from lumina.integration import style
from lumina.qt_models.develop import DevelopSession
from lumina.qt_models.library_model import (
    COPIES_ROLE,
    COPY_ROLE,
    DAMAGE_ROLE,
    DIMMED_ROLE,
    FLAG_ROLE,
    RATING_ROLE,
    STACK_ROLE,
    LibraryModel,
)
from lumina.ui import badges, damage, flags, settings, shortcuts, stars
from lumina.ui.camera import CameraBar
from lumina.ui.controllers.browser import Browser
from lumina.ui.controllers.collecting import Collecting
from lumina.ui.controllers.copies import VirtualCopies
from lumina.ui.controllers.culling import Culling
from lumina.ui.controllers.develop import DevelopPage
from lumina.ui.controllers.grid import GridPage
from lumina.ui.controllers.importing import Importing
from lumina.ui.controllers.keywording import Keywording
from lumina.ui.controllers.merging import Merging
from lumina.ui.controllers.navigation import Navigator
from lumina.ui.controllers.opening import Opening
from lumina.ui.controllers.searching import Searching
from lumina.ui.controllers.settings_copy import SettingsCopying
from lumina.ui.controllers.stacking import Stacking
from lumina.ui.controllers.status import StatusLine
from lumina.ui.controllers.tethering import Tethering
from lumina.ui.controllers.viewer import ViewerPage
from lumina.ui.develop_view import DevelopView
from lumina.ui.filters import FilterBar
from lumina.ui.grid_size import GridSizeSwitch
from lumina.ui.image_view import ImageView
from lumina.ui.job_status import JobStatus, export_status, import_status, merge_status
from lumina.ui.live_view import LivePanel
from lumina.ui.search import SearchField, SearchStatus
from lumina.ui.settings_dialog import SettingsDialog
from lumina.ui.sidebar import Sidebar
from lumina.ui.tether import TetherStatus
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
        exporter=None,
        reports=None,
    ):
        """`search` is the SearchService; without one there is no search. `merge` is the
        MergeService; without one there are no merges. `importer` is the ImportService;
        without one there is no importing. `backups` is the BackupService; without one
        the settings have no Backups section. `tether` is the HotFolder; without one no
        folder can be tethered. `camera` is the CameraService (which takes its photos in
        through `tether`); without one there is no shooting with a camera. `exporter` is the
        ExportService; without one the photos chosen in the grid can't be exported.
        `reports` are the JobReports (the jobs in the island and notifications), which
        the settings turn on and off.

        The window builds the widgets and the actions; what they do is the controllers'
        (ui/controllers/), which it makes and connects them to."""
        super().__init__(parent)
        self.service = service
        self.backups = backups
        self.tether = tether
        self.camera = camera
        self.search = search
        self.merge = merge
        self.importer = importer
        self.exporter = exporter
        self.reports = reports
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(style.app_icon())
        self.resize(style.px(1280), style.px(820))

        self.model = LibraryModel(service, search, self)
        self.session = DevelopSession(service, self, exporter)
        self.session.profile_folders = settings.profile_folders

        self._make_actions()
        self.search_field = SearchField()
        self._make_toolbar()
        self.sidebar = Sidebar()
        self.sidebar.set_import_shown(importer is not None)
        self.sidebar.set_camera_shown(camera is not None)
        self.filters = FilterBar()
        self.flag_options = flags.FlagOptions()
        self.flag_options.set_rejected(settings.rejected_photos())
        self.model.set_rejected(settings.rejected_photos())
        self.grid_size = GridSizeSwitch()

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
        self.pages.addWidget(self.develop)
        self.live_view = LivePanel()
        self.pages.addWidget(self.live_view)
        outer = QWidget()
        column = QVBoxLayout(outer)
        # Between the toolbar and the filters
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        column.addWidget(divider)
        self.camera_bar = CameraBar()
        column.addWidget(self.camera_bar)
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
        self._make_status_bar()
        self._make_controllers()
        self._connect()

        self._draw_icons()
        style.themed.changed.connect(self._draw_icons)
        service.keyword_list()
        self.keywording.on_keywords_changed()
        self.browser.refresh_folders(select=settings.selected_folder())
        self.sidebar.set_collections(service.collections())
        self.sidebar.set_last_import(service.last_import())

    # Building

    def _make_controllers(self):
        messages = self.statusBar()
        model, service = self.model, self.service
        self.grid_page = GridPage(self.grid, self._delegate, self.grid_size, model, self)
        self.viewer_page = ViewerPage(self.viewer, self.pages, model, service, self)
        self.develop_page = DevelopPage(
            self.develop, self.session, self.undo_action, self.redo_action, messages, self
        )
        self.nav = nav = Navigator(
            self.pages, self.grid, model, service, self.viewer_page, self.develop_page, self
        )
        self.status_line = StatusLine(self.status, nav, model, service, self.search, self)
        self.browser = Browser(
            nav, model, service, self.sidebar, self.filters, self.title, messages, self
        )
        self.culling = Culling(
            nav,
            model,
            SimpleNamespace(
                pick=self.pick_action,
                reject=self.reject_action,
                unflag=self.unflag_action,
                rate=self.rate_actions,
                undo_flag=self.undo_flag_action,
                redo_flag=self.redo_flag_action,
            ),
            self.rating_bar,
            messages,
            self,
        )
        self.keywording = Keywording(
            nav, model, service, self.sidebar, self.filters, messages, self
        )
        self.searching = Searching(
            nav, model, self.search, self.search_field, self.search_status, messages, self
        )
        self.collecting = Collecting(
            nav,
            model,
            service,
            self.browser,
            self.searching,
            self.sidebar,
            self.filters,
            messages,
            self,
        )
        self.copies = VirtualCopies(
            nav,
            model,
            service,
            self.session,
            SimpleNamespace(
                make=self.make_copy_action,
                delete=self.delete_copy_action,
                previous=self.previous_copy_action,
                next=self.next_copy_action,
            ),
            messages,
            self,
        )
        self.settings_copy = SettingsCopying(
            nav,
            model,
            service,
            self.session,
            SimpleNamespace(
                copy=self.copy_settings_action,
                paste=self.paste_settings_action,
                sync=self.sync_settings_action,
            ),
            self.develop.paste_settings_button,
            messages,
            self,
        )
        self.stacking = Stacking(
            nav,
            model,
            service,
            self.exporter,
            self.browser,
            SimpleNamespace(
                stack=self.stack_action,
                unstack=self.unstack_action,
                stack_bursts=self.stack_bursts_action,
                pick_best=self.pick_best_action,
                reject_rest=self.reject_rest_action,
                leave_stack=self.leave_stack_action,
                toggle_stack=self.toggle_stack_action,
                stack_top=self.stack_top_action,
                export_selected=self.export_selected_action,
            ),
            messages,
            self,
        )
        self.merging = Merging(nav, model, service, self.merge, self)
        self.opening = Opening(nav, model, service, self.browser, self.sidebar, self)
        self.importing = Importing(
            service, self.importer, self.browser, self.sidebar, messages, self
        )
        self.tethering = Tethering(
            nav,
            model,
            service,
            self.tether,
            self.camera,
            SimpleNamespace(
                sidebar=self.sidebar,
                camera_bar=self.camera_bar,
                live_view=self.live_view,
                tether_status=self.tether_status,
            ),
            self.capture_action,
            messages,
            self,
        )

    def _connect(self):
        """The widgets' signals that the controllers answer, and what the window itself
        changes with the page: the toolbar's parts and the actions of each page."""
        model, service, nav = self.model, self.service, self.nav
        status = self.status_line
        for signal in (model.modelReset, model.loading_changed, model.rowsRemoved):
            signal.connect(status.update)
        # After the grid's place is kept
        model.refreshed.connect(status.update)
        self.viewer_page.zoom_changed.connect(status.zoom_changed)
        self.develop.image.zoom_changed.connect(status.zoom_changed)
        self.session.region_ready.connect(status.zoom_changed)
        self.session.image_ready.connect(status.zoom_changed)
        nav.page_changed.connect(self._on_page_changed)
        nav.page_changed.connect(self.develop_page.update_history)

        sidebar, browser = self.sidebar, self.browser
        sidebar.selected.connect(browser.on_folder_selected)
        sidebar.add_requested.connect(lambda: self.browser.choose_folder())
        sidebar.import_requested.connect(lambda: self.importing.import_photos())
        sidebar.rescan_requested.connect(service.rescan)
        sidebar.rescan_all_requested.connect(service.rescan)
        sidebar.remove_requested.connect(browser.remove_folder)
        sidebar.tether_requested.connect(self.tethering.tether_folder)
        sidebar.camera_requested.connect(lambda: self.tethering.tether_camera())
        sidebar.collection_selected.connect(self.collecting.on_collection_selected)
        sidebar.import_selected.connect(browser.on_import_selected)
        sidebar.new_collection_requested.connect(lambda: self.collecting.new_collection())
        sidebar.rename_collection_requested.connect(self.collecting.rename_collection)
        sidebar.delete_collection_requested.connect(self.collecting.delete_collection)
        sidebar.update_smart_requested.connect(self.collecting.update_smart_collection)
        sidebar.photos_dropped.connect(self.collecting.add_ids_to_collection)
        sidebar.keyword_chosen.connect(lambda path: self.filters.set_keyword(path))
        sidebar.rename_keyword_requested.connect(self.keywording.rename_keyword)
        sidebar.delete_keyword_requested.connect(self.keywording.delete_keyword)
        self.filters.changed.connect(browser.apply_filter)
        self.filters.keyword_changed.connect(self.keywording.set_keyword_filter)
        self.filters.save_requested.connect(lambda: self.collecting.new_smart_collection())
        self.filters.flag_changed.connect(browser.set_flag_filter)
        self.filters.rating_changed.connect(browser.set_rating_filter)
        self.filters.edit_changed.connect(browser.set_edit_filter)
        self.filters.sharpness_changed.connect(browser.set_sharpness_filter)
        self.flag_options.rejected_changed.connect(self.grid_page.set_rejected)
        self.flag_options.rejected_changed.connect(nav.status_changed)
        self.grid_page.stack_badge_clicked.connect(self.stacking.toggle_stack_of)
        self.rating_bar.chosen.connect(self.culling.rate_photos)
        self.develop.copy_settings_button.clicked.connect(
            lambda: self.settings_copy.copy_settings()
        )
        self.develop.paste_settings_button.clicked.connect(
            lambda: self.settings_copy.paste_settings()
        )
        self.grid.activated.connect(
            lambda index: self.nav.show_photo(index.row(), self.model.copy(index.row()))
        )

        service.folders_changed.connect(browser.refresh_folders)
        service.failed.connect(lambda message: self.statusBar().showMessage(message, 10000))
        service.sidecars_written.connect(browser.on_sidecars_written)
        service.job_changed.connect(self.job_status.show_status)
        # Methods, not lambdas: the connections go with the window, and a scan that ends
        # after the window is closed does not reach its deleted widgets
        service.job_ended.connect(browser.on_job_ended)
        service.job_ended.connect(self.opening.on_job_ended)

    def _on_page_changed(self, page):
        viewing = page in (_VIEWER, _DEVELOP)
        developing = page == _DEVELOP
        self.navigation.setVisible(viewing)
        self.editing.setVisible(developing)
        self.marking.setVisible(page in (_GRID, _VIEWER, _DEVELOP))
        self.search_field.setVisible(self.search is not None and page in (_GRID, _VIEWER, _DEVELOP))
        self.grid_view_options.setVisible(page == _GRID)
        for action in (self.back_action, self.previous_action, self.next_action):
            shortcuts.enable(action, viewing, "With a photo open")
        self.back_action.setEnabled(viewing or page == _LIVE)
        for action in (self.reset_action, self.export_action):
            shortcuts.enable(action, developing, "In the Develop view")
        shortcuts.enable(self.edit_action, page in (_GRID, _VIEWER), "In the grid and the viewer")

    def _make_status_bar(self):
        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.job_status = JobStatus()
        self.job_status.cancel_requested.connect(self.service.cancel)
        self.statusBar().addPermanentWidget(self.job_status)
        self.merge_status = merge_status()
        self.statusBar().addPermanentWidget(self.merge_status)
        if self.merge is not None:
            self.merge_status.cancel_requested.connect(self.merge.cancel)
            self.merge.job_changed.connect(self.merge_status.show_status)
        self.import_status = import_status()
        self.statusBar().addPermanentWidget(self.import_status)
        self.export_status = export_status()
        self.statusBar().addPermanentWidget(self.export_status)
        if self.exporter is not None:
            self.export_status.cancel_requested.connect(self.exporter.cancel)
            self.exporter.job_changed.connect(self.export_status.show_status)
        self.tether_status = TetherStatus()
        self.statusBar().addPermanentWidget(self.tether_status)
        if self.importer is not None:
            self.import_status.cancel_requested.connect(self.importer.cancel)
            self.importer.job_changed.connect(self.import_status.show_status)
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

    def _make_actions(self):
        """The actions of the toolbar, the menus and the keys. They call the controllers,
        which are made after them, when they are triggered."""
        self.add_action = QAction("Add Folder…", self)
        self.add_action.setShortcut(QKeySequence.StandardKey.Open)
        self.add_action.triggered.connect(lambda: self.browser.choose_folder())
        # A key only: the button is the sidebar's, under the folders it adds to
        self.addAction(self.add_action)
        self.import_action = QAction("Import Photos…", self)
        self.import_action.setShortcut(QKeySequence("Ctrl+I"))
        self.import_action.setToolTip("Copy photos from a memory card into the library (Ctrl+I)")
        self.import_action.triggered.connect(lambda: self.importing.import_photos())
        self.addAction(self.import_action)

        self.camera_action = QAction("Tether Camera…", self)
        self.camera_action.setShortcut(QKeySequence("Ctrl+T"))
        self.camera_action.triggered.connect(lambda: self.tethering.tether_camera())
        self.addAction(self.camera_action)
        # A foot pedal can send either
        self.capture_action = QAction("Capture", self)
        self.capture_action.setShortcuts([QKeySequence("Space"), QKeySequence("F12")])
        shortcuts.enable(self.capture_action, False, "No camera connected")
        self.capture_action.triggered.connect(lambda: self.tethering.capture())
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
        self.back_action.triggered.connect(lambda: self.nav.go_back())
        self.edit_action = QAction("Edit", self)
        self.edit_action.setShortcut(QKeySequence(Qt.Key.Key_E))
        self.edit_action.setToolTip("Edit the photo (E)")
        self.edit_action.triggered.connect(lambda: self.nav.edit_current())
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
        self.export_action.triggered.connect(lambda: self.develop_page.export_photo())
        self.make_copy_action = QAction("Make Virtual Copy", self)
        self.make_copy_action.setShortcut(QKeySequence("Ctrl+'"))
        self.make_copy_action.setToolTip(
            "Another edit of the photo, to try a different look; the file is not touched (Ctrl+')"
        )
        self.make_copy_action.triggered.connect(lambda: self.copies.make_virtual_copy())
        self.delete_copy_action = QAction("Delete Virtual Copy", self)
        self.delete_copy_action.triggered.connect(lambda: self.copies.delete_virtual_copy())
        self.previous_copy_action = QAction("Previous Copy", self)
        self.previous_copy_action.setShortcut(QKeySequence("Ctrl+Up"))
        self.previous_copy_action.triggered.connect(lambda: self.copies.step_copy(-1))
        self.next_copy_action = QAction("Next Copy", self)
        self.next_copy_action.setShortcut(QKeySequence("Ctrl+Down"))
        self.next_copy_action.triggered.connect(lambda: self.copies.step_copy(1))
        self.export_selected_action = QAction("Export Selected…", self)
        self.export_selected_action.setToolTip(
            "Write the chosen photos, with their edits, to a folder as JPEG, PNG or TIFF"
        )
        self.export_selected_action.triggered.connect(lambda: self.stacking.export_selected())
        self.stack_action = QAction("Stack", self)
        self.stack_action.setShortcut(QKeySequence("Ctrl+G"))
        self.stack_action.setToolTip("Group the chosen photos in one stack (Ctrl+G)")
        self.stack_action.triggered.connect(lambda: self.stacking.stack_photos())
        self.unstack_action = QAction("Unstack", self)
        self.unstack_action.setShortcut(QKeySequence("Ctrl+Shift+G"))
        self.unstack_action.triggered.connect(lambda: self.stacking.unstack_photos())
        self.stack_bursts_action = QAction("Stack Bursts", self)
        self.stack_bursts_action.setToolTip(
            "Stack shots taken a moment apart that look alike, the best on top"
        )
        self.stack_bursts_action.triggered.connect(lambda: self.stacking.stack_bursts())
        self.pick_best_action = QAction("Pick Sharpest of Each Stack", self)
        self.pick_best_action.setToolTip(
            "Put the sharpest photo on top of each stack chosen, or of every stack shown"
        )
        self.pick_best_action.triggered.connect(lambda: self.stacking.pick_best())
        self.reject_rest_action = QAction("Reject the Rest of Each Stack…", self)
        self.reject_rest_action.setToolTip(
            "Give a thumbs down to all but the top photo of each stack chosen, or shown"
        )
        self.reject_rest_action.triggered.connect(lambda: self.stacking.reject_rest())
        self.find_duplicates_action = QAction("Find Duplicates…", self)
        self.find_duplicates_action.setToolTip(
            "Look through the library for photos that look alike or are copies of one file"
        )
        self.find_duplicates_action.triggered.connect(lambda: self.stacking.find_duplicates())
        self.addAction(self.find_duplicates_action)
        self.leave_stack_action = QAction("Remove from Stack", self)
        self.leave_stack_action.triggered.connect(lambda: self.stacking.leave_stack())
        self.toggle_stack_action = QAction("Expand or Collapse Stack", self)
        self.toggle_stack_action.setShortcut(QKeySequence("Ctrl+Alt+G"))
        self.toggle_stack_action.triggered.connect(lambda: self.stacking.toggle_stacks())
        self.stack_top_action = QAction("Set as Top of Stack", self)
        self.stack_top_action.triggered.connect(lambda: self.stacking.set_stack_top())
        self.stack_actions = (
            self.stack_action,
            self.unstack_action,
            self.stack_bursts_action,
            self.pick_best_action,
            self.reject_rest_action,
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
        self.previous_action.triggered.connect(lambda: self.nav.step(-1))
        self.next_action = QAction("Next Photo", self)
        self.next_action.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.next_action.triggered.connect(lambda: self.nav.step(1))
        self.find_action = QAction("Search", self)
        self.find_action.setShortcut(QKeySequence.StandardKey.Find)
        self.find_action.triggered.connect(lambda: self.searching.focus_search())
        self.addAction(self.find_action)
        self.similar_action = QAction("Find Similar Photos", self)
        self.similar_action.setShortcut(QKeySequence(Qt.Key.Key_S))
        self.similar_action.setToolTip("Show the photos most like this one (S)")
        self.similar_action.triggered.connect(lambda: self.searching.find_similar())
        self.addAction(self.similar_action)
        self.pick_action = QAction("Thumbs Up", self)
        self.pick_action.setShortcut(QKeySequence(Qt.Key.Key_P))
        self.pick_action.setToolTip("Thumbs up (P)")
        self.pick_action.setCheckable(True)
        self.pick_action.triggered.connect(lambda: self.culling.flag_photos(PICKED))
        self.reject_action = QAction("Thumbs Down", self)
        self.reject_action.setShortcut(QKeySequence(Qt.Key.Key_X))
        self.reject_action.setToolTip("Thumbs down (X)")
        self.reject_action.setCheckable(True)
        self.reject_action.triggered.connect(lambda: self.culling.flag_photos(REJECTED))
        self.unflag_action = QAction("Clear Flag", self)
        self.unflag_action.setShortcut(QKeySequence(Qt.Key.Key_U))
        self.unflag_action.triggered.connect(lambda: self.culling.flag_photos(UNFLAGGED))
        # In the grid and the viewer; the Develop view's Undo is its edit's
        self.undo_flag_action = QAction("Undo Flag, Rating or Keywords", self)
        self.undo_flag_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_flag_action.triggered.connect(lambda: self.culling.undo_flags())
        self.redo_flag_action = QAction("Redo Flag, Rating or Keywords", self)
        self.redo_flag_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_flag_action.triggered.connect(lambda: self.culling.redo_flags())
        # 1 to 5 stars, 0 for none; the same key again clears it
        self.rate_actions = []
        for number in range(6):
            action = QAction(
                "No Stars" if number == 0 else f"{number} Star{'s' * (number > 1)}", self
            )
            action.setShortcut(QKeySequence(Qt.Key(Qt.Key.Key_0 + number)))
            action.setToolTip(f"Rate {number} (key {number})" if number else "Clear the rating (0)")
            action.triggered.connect(
                lambda _on=False, stars=number: self.culling.rate_photos(stars)
            )
            self.rate_actions.append(action)
        self.keywords_action = QAction("Keywords…", self)
        self.keywords_action.setShortcut(QKeySequence(Qt.Key.Key_K))
        self.keywords_action.setToolTip("Give the photos keywords, or take them off (K)")
        self.keywords_action.triggered.connect(lambda: self.keywording.edit_keywords())
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
        self.larger_action.triggered.connect(lambda: self.grid_page.step_grid_size(1))
        self.smaller_action = QAction("Smaller Thumbnails", self)
        self.smaller_action.setShortcuts([QKeySequence.StandardKey.ZoomOut])
        self.smaller_action.triggered.connect(lambda: self.grid_page.step_grid_size(-1))
        # The zoom of the viewer and of the Develop view: Z between fitted and 100 %,
        # Ctrl+Plus and Minus in steps
        self.zoom_action = QAction("Zoom to 100 %", self)
        self.zoom_action.setShortcut(QKeySequence(Qt.Key.Key_Z))
        self.zoom_action.triggered.connect(lambda: self.nav.zoom_view().toggle_zoom())
        self.zoom_in_action = QAction("Zoom In", self)
        self.zoom_in_action.setShortcuts([QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")])
        self.zoom_in_action.triggered.connect(lambda: self.nav.zoom_view().zoom_in())
        self.zoom_out_action = QAction("Zoom Out", self)
        self.zoom_out_action.setShortcuts([QKeySequence.StandardKey.ZoomOut])
        self.zoom_out_action.triggered.connect(lambda: self.nav.zoom_view().zoom_out())
        self.zoom_actions = (self.zoom_action, self.zoom_in_action, self.zoom_out_action)
        self.copy_settings_action = QAction("Copy Settings…", self)
        self.copy_settings_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self.copy_settings_action.triggered.connect(lambda: self.settings_copy.copy_settings())
        self.paste_settings_action = QAction("Paste Settings", self)
        self.paste_settings_action.setShortcut(QKeySequence("Ctrl+Shift+V"))
        self.paste_settings_action.triggered.connect(lambda: self.settings_copy.paste_settings())
        self.sync_settings_action = QAction("Sync Settings…", self)
        self.sync_settings_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.sync_settings_action.triggered.connect(lambda: self.settings_copy.sync_settings())
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
        self.merge_hdr_action.triggered.connect(lambda: self.merging.merge_hdr())
        self.stitch_panorama_action = QAction("Stitch Panorama…", self)
        self.stitch_panorama_action.setShortcut(QKeySequence("Ctrl+M"))
        self.stitch_panorama_action.setToolTip(
            "Stitch overlapping photos, taken turning the camera, into one panorama (Ctrl+M)"
        )
        self.stitch_panorama_action.triggered.connect(lambda: self.merging.stitch_panorama())
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
        button.clicked.connect(lambda: self.browser.choose_folder())
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        if self.importer is not None:
            importing = QPushButton("Import from a Card…")
            importing.clicked.connect(lambda: self.importing.import_photos())
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
        # Only while the grid shows: the Develop view may want these keys for its own zoom
        for action in (self.larger_action, self.smaller_action):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.grid.addAction(action)
        self.grid.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.grid.setWordWrap(False)
        self.grid.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        # Photos dragged onto a collection in the sidebar
        self.grid.setDragEnabled(True)
        self.grid.setDragDropMode(QListView.DragDropMode.DragOnly)
        self.grid.setDefaultDropAction(Qt.DropAction.CopyAction)
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
        # Escape in the grid ends a search
        self.end_search_action = QAction("Show All Photos", self.grid)
        self.end_search_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        self.end_search_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.end_search_action.triggered.connect(lambda: self.searching.end_search())
        shortcuts.set_group("Library", self.end_search_action)
        self.grid.addAction(self.end_search_action)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._grid_menu)
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
        dialog = SettingsDialog(
            self.search, self.service, self, backups=self.backups, reports=self.reports
        )
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

    def present(self):
        """Bring the window forward, out of the taskbar if it was minimised."""
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    # The photos' menus

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
        self.collecting.add_menu(menu)
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
        if not in_grid and self.copies.copy_to_delete() is not None:
            menu.addAction(self.delete_copy_action)
        else:
            rows = self.nav.chosen_rows()
            if len(rows) == 1 and self.model.copies(rows[0]):
                shown = self.model.copies_open(rows[0])
                menu.addAction(
                    "Hide Virtual Copies" if shown else "Show Virtual Copies",
                    lambda: self.model.toggle_copies(self.model.photo_id(rows[0])),
                )
            if self.copies.copy_to_delete() is not None:
                menu.addAction(self.delete_copy_action)
        if in_grid:
            menu.addSeparator()
            for action in self.stack_menu_actions:
                menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.copy_settings_action)
        menu.addAction(self.paste_settings_action)
        if in_grid:
            menu.addAction(self.sync_settings_action)
            if self.exporter is not None:
                menu.addSeparator()
                menu.addAction(self.export_selected_action)
        if self.merge is not None:
            menu.addSeparator()
            menu.addAction(self.merge_hdr_action)
            menu.addAction(self.stitch_panorama_action)
        return menu

    def closeEvent(self, event):
        self.viewer_page.cancel()
        self.session.close()
        super().closeEvent(event)
