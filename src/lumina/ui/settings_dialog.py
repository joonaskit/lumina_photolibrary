"""The app's settings, in one window. A change is made as it is picked: there is no
OK to press, only Close. What is chosen is kept in `ui/settings.py`."""

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import appearance, style, theme
from lumina.qt_models import develop
from lumina.qt_models.backups import (
    BEFORE_MIGRATION,
    BEFORE_RESTORE,
    DAMAGED,
    KEEP_DAILY,
    KEEP_WEEKLY,
)
from lumina.ui import settings
from lumina.ui.appearance import AccentDots, Segments
from lumina.ui.develop_view import SECTION_NAMES, SECTIONS

_DENSITIES = (("comfortable", "Comfortable"), ("compact", "Compact"))
# After the limit was last changed, so that spinning it down does not delete on the way
_LIMIT_SETTLE_MS = 1500
_MAX_LIMIT_GB = 1000


_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def size_text(size):
    """Bytes for people: megabytes below a gigabyte, kilobytes below a megabyte."""
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.0f} MB"
    return f"{max(1, round(size / 1024))} KB"


def when_text(when, joiner=", "):
    """ "Friday 16 October 2026, 09:30": in English as the rest of the app, not in the
    locale's words, which Qt makes strftime use."""
    day = f"{_DAYS[when.weekday()]} {when.day} {_MONTHS[when.month - 1]} {when.year}"
    return f"{day}{joiner}{when:%H:%M}"


_KINDS = {
    BEFORE_MIGRATION: "before an update",
    BEFORE_RESTORE: "before a restore",
    DAMAGED: "damaged, moved aside",
}


def backup_text(made):
    """A backup in the list: when, how large, and why when it is not a daily one."""
    text = f"{when_text(made.taken)}  ·  {size_text(made.size)}"
    if made.kind in _KINDS:
        text += f"  ·  {_KINDS[made.kind]}"
    return text


class SettingsDialog(QDialog):
    """`search` is the SearchService, `library` the LibraryService and `backups` the
    BackupService; without one there is no Search, Library or Backups section."""

    def __init__(self, search=None, library=None, parent=None, backups=None):
        super().__init__(parent)
        self.search = search
        self.library = library
        self.backups = backups
        # What the backup chosen holds, once read: (path, Contents or None)
        self._contents = None
        self._closed = False
        self.setWindowTitle("Settings")
        self.setMinimumWidth(style.px(460))

        outer = QVBoxLayout(self)
        style.follow_density(self, lambda: outer.setSpacing(style.gap(10)))
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        pages = []

        def page(title):
            """A tab; returns the layout for its settings."""
            widget = QWidget()
            made = QVBoxLayout(widget)
            style.follow_density(widget, lambda: made.setSpacing(style.gap(10)))
            self.tabs.addTab(widget, title)
            pages.append(made)
            return made

        self._appearance_page(page("Appearance"))
        layout = page("General")

        self.advance = QCheckBox("Go to the next photo after flagging or rating")
        self.advance.setChecked(settings.advance_after_flag())
        self.advance.toggled.connect(settings.set_advance_after_flag)
        layout.addWidget(self._heading("Culling"))
        layout.addWidget(self.advance)
        layout.addWidget(
            self._note(
                "In the viewer, after a thumbs up or down or a rating; not after clearing "
                "one, and never in the Develop view."
            )
        )

        layout = page("Develop")
        self.sections = QListWidget()
        self.sections.setObjectName("settingsList")
        self.sections.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.sections.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._fill_sections()
        self.sections.itemChanged.connect(self._save_sections)
        self.sections.model().rowsMoved.connect(self._save_sections)
        self.move_up = QPushButton("Up")
        self.move_up.clicked.connect(lambda: self._move_section(-1))
        self.move_down = QPushButton("Down")
        self.move_down.clicked.connect(lambda: self._move_section(1))
        self.reset_sections = QPushButton("Default Order")
        self.reset_sections.clicked.connect(self._reset_sections)
        buttons = QHBoxLayout()
        buttons.addWidget(self.move_up)
        buttons.addWidget(self.move_down)
        buttons.addStretch(1)
        buttons.addWidget(self.reset_sections)
        layout.addWidget(self._heading("Develop panel"))
        layout.addWidget(self.sections)
        layout.addLayout(buttons)
        layout.addWidget(
            self._note(
                "Drag the sections, or use Up and Down, to put them in your order; untick "
                "one to leave it off the panel. A section left off keeps its values: it is "
                "still part of the edit, of presets, and of copied settings."
            )
        )

        self.profile_folders = QListWidget()
        self.profile_folders.setObjectName("settingsList")
        self.profile_folders.addItems(settings.profile_folders())
        self.profile_folders.currentRowChanged.connect(self._show_profile_buttons)
        self.add_profile_folder = QPushButton("Add Folder…")
        self.add_profile_folder.clicked.connect(self.choose_profile_folder)
        self.remove_profile_folder = QPushButton("Remove")
        self.remove_profile_folder.clicked.connect(self.remove_chosen_profile_folder)
        buttons = QHBoxLayout()
        buttons.addWidget(self.add_profile_folder)
        buttons.addWidget(self.remove_profile_folder)
        buttons.addStretch(1)
        layout.addWidget(self._heading("Camera profiles"))
        layout.addWidget(self.profile_folders)
        layout.addLayout(buttons)
        looked_in = ", ".join(os.fspath(f) for f in develop.PROFILE_FOLDERS())
        layout.addWidget(
            self._note(
                "Folders of DCP camera profiles (Adobe's, from a camera maker, your own). "
                "A new edit of a RAW file starts with the first profile for its camera, "
                f"after these from: {looked_in}. Without one it gets Lumina Standard."
            )
        )
        self._show_profile_buttons()

        if library is not None:
            layout = page("Library")
            self.gentle = QCheckBox("Scan large libraries gently")
            self.gentle.setChecked(settings.gentle_scans())
            self.gentle.toggled.connect(self._on_gentle_toggled)
            layout.addWidget(self._heading("Scanning"))
            layout.addWidget(self.gentle)
            layout.addWidget(
                self._note(
                    "From 2000 photos, a scan rests between batches and reads at the lowest "
                    "priority, so the computer stays responsive through a first import of a "
                    "large library. It takes about twice as long. The next scan, not one "
                    "running now, is gentle."
                )
            )

            self.watch = QCheckBox("Watch the folders for changes while Lumina runs")
            self.watch.setChecked(settings.watch_folders())
            self.watch.toggled.connect(self._on_watch_toggled)
            self.watch_note = self._note("")
            layout.addWidget(self.watch)
            layout.addWidget(self.watch_note)
            library.watcher.status_changed.connect(self._show_watching)
            self._show_watching()

            self.sidecars = QCheckBox("Keep XMP sidecars beside the photos")
            self.sidecars.setChecked(settings.sidecars())
            self.sidecars.toggled.connect(self._on_sidecars_toggled)
            layout.addWidget(self._heading("Sidecars"))
            layout.addWidget(self.sidecars)
            layout.addWidget(
                self._note(
                    "A .xmp file next to each photo you have marked or edited "
                    "(IMG_0001.CR3.xmp) with its edits, flag, stars and keywords, so they "
                    "travel with the photos and a library can be rebuilt from them. Other "
                    "programs see the stars and keywords too. The photos are never changed, "
                    "and another program's .xmp is left alone. Turning it on writes them for "
                    "the whole library and reads those already there; a sidecar newer than "
                    "the library's own record is taken in when a folder is scanned."
                )
            )

            self.cache_limit = QSpinBox()
            self.cache_limit.setRange(0, _MAX_LIMIT_GB)
            self.cache_limit.setSuffix(" GB")
            self.cache_limit.setSpecialValueText("No limit")
            self.cache_limit.setValue(settings.thumbnail_limit_gb())
            self.cache_limit.valueChanged.connect(self._on_limit_changed)
            self._limit_timer = QTimer(self)
            self._limit_timer.setSingleShot(True)
            self._limit_timer.setInterval(_LIMIT_SETTLE_MS)
            self._limit_timer.timeout.connect(self.apply_cache_limit)
            self.cache_usage = self._note("")
            form = QFormLayout()
            form.addRow("Size limit", self.cache_limit)
            layout.addWidget(self._heading("Thumbnails"))
            layout.addLayout(form)
            layout.addWidget(self.cache_usage)
            layout.addWidget(
                self._note(
                    "Over the limit, the thumbnails used longest ago are deleted, those of "
                    "photos on drives that are not connected last: they come again from "
                    "the photos, when the drive is. Your photos are never touched."
                )
            )
            library.thumbnails_pruned.connect(self._show_usage)
            self._show_usage()

        if search is not None:
            layout = page("Search")
            self.model_state = QLabel()
            self.model_state.setWordWrap(True)
            self.model_folder = self._note(search.folder)
            self.model_folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.download = QPushButton("Download")
            self.download.clicked.connect(search.download)
            self.remove = QPushButton("Delete…")
            self.remove.clicked.connect(self.remove_model)
            buttons = QHBoxLayout()
            buttons.addWidget(self.download)
            buttons.addWidget(self.remove)
            buttons.addStretch(1)
            layout.addWidget(self._heading("Search"))
            layout.addWidget(self.model_state)
            layout.addWidget(self.model_folder)
            layout.addLayout(buttons)
            search.model_changed.connect(self._show_model)
            self._show_model()

        if backups is not None:
            self._backups_page(page("Backups"))

        for made in pages:
            made.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        outer.addWidget(close)

    def _appearance_page(self, layout):
        """The scheme, the accent and the corners: the shell's, followed as it changes
        them, or chosen here; and the density."""
        layout.addWidget(self._heading("Theme"))
        self._shell = appearance.shell_found()
        self.follow = QCheckBox("Follow the shell's look")
        self.follow.setChecked(settings.follow_shell())
        self.follow.toggled.connect(self._on_follow_toggled)
        self.follow.setVisible(self._shell)
        layout.addWidget(self.follow)
        self.follow_note = self._note(
            "Quickshell Island's colours and corners, changing as you change them there. "
            "Turn this off to choose them here."
        )
        layout.addWidget(self.follow_note)
        self.scheme = Segments(appearance.SCHEMES)
        self.accent = AccentDots(appearance.ACCENTS)
        self.corners = Segments([(key, label) for key, label, _scale in appearance.CORNERS])
        for control in (self.scheme, self.accent, self.corners):
            control.chosen.connect(self._on_look_chosen)
        look = QFormLayout()
        look.addRow("Appearance", self.scheme)
        look.addRow("Accent", self.accent)
        look.addRow("Corners", self.corners)
        style.follow_density(self, lambda: look.setVerticalSpacing(style.gap(10)))
        layout.addLayout(look)
        self._show_look()
        # While following the shell, what it changes shows here
        style.themed.changed.connect(self._show_look)

        self.density = QComboBox()
        for key, text in _DENSITIES:
            self.density.addItem(text, key)
        self.density.setCurrentIndex(max(0, self.density.findData(settings.density())))
        self.density.currentIndexChanged.connect(self._on_density_changed)
        density = QFormLayout()
        density.addRow("Density", self.density)
        layout.addWidget(self._heading("Layout"))
        layout.addLayout(density)
        layout.addWidget(
            self._note(
                "Compact takes less room for the menus, buttons and the Develop panel. "
                "The text, the icons and the thumbnails stay as large."
            )
        )

    def _following(self):
        return self._shell and self.follow.isChecked()

    def _show_look(self):
        """The shell's look while it is followed, greyed; or the one chosen here."""
        following = self._following()
        shown = appearance.look_of(theme.load_theme()) if following else settings.look()
        self.scheme.set_value(shown.scheme)
        self.accent.set_value(shown.accent)
        self.corners.set_value(shown.corners)
        self.follow_note.setVisible(following)
        for control in (self.scheme, self.accent, self.corners):
            control.setEnabled(not following)

    def _on_follow_toggled(self, on):
        if not on:
            # Choosing by hand starts from the shell's look as it is: nothing changes yet
            settings.set_look(appearance.look_of(theme.load_theme()))
        settings.set_follow_shell(on)
        self._apply_look()
        self._show_look()

    def _on_look_chosen(self, _key):
        settings.set_look(
            appearance.Look(self.scheme.value(), self.accent.value(), self.corners.value())
        )
        self._apply_look()

    def _apply_look(self):
        appearance.apply(QApplication.instance(), settings.chosen_look(self._shell))

    def _backups_page(self, layout):
        layout.addWidget(self._heading("Backups"))
        layout.addWidget(
            self._note(
                "Your edits, flags, ratings and the index are in one database. It is "
                "copied each day Lumina starts, and before an update changes it. Your "
                "photos are not in it: back them up too."
            )
        )
        self.backup_list = QListWidget()
        self.backup_list.setObjectName("settingsList")
        self.backup_list.currentItemChanged.connect(self._show_backup)
        self.backup_details = self._note("")
        self.back_up_button = QPushButton("Back Up Now")
        self.back_up_button.clicked.connect(self._back_up)
        self.restore_button = QPushButton("Restore…")
        self.restore_button.clicked.connect(self.restore_backup)
        buttons = QHBoxLayout()
        buttons.addWidget(self.back_up_button)
        buttons.addStretch(1)
        buttons.addWidget(self.restore_button)
        layout.addWidget(self.backup_list)
        layout.addWidget(self.backup_details)
        layout.addLayout(buttons)

        self.extra_folder = QLabel()
        self.extra_folder.setWordWrap(True)
        self.choose_extra = QPushButton("Choose…")
        self.choose_extra.clicked.connect(self.choose_backup_folder)
        self.clear_extra = QPushButton("None")
        self.clear_extra.clicked.connect(lambda: self.set_backup_folder(None))
        extra = QHBoxLayout()
        extra.addWidget(self.extra_folder, 1)
        extra.addWidget(self.choose_extra)
        extra.addWidget(self.clear_extra)
        daily, weekly = settings.backups_kept(KEEP_DAILY, KEEP_WEEKLY)
        self.keep_daily = QSpinBox()
        self.keep_daily.setRange(1, 365)
        self.keep_daily.setSuffix(" daily")
        self.keep_daily.setValue(max(1, daily))
        self.keep_weekly = QSpinBox()
        self.keep_weekly.setRange(0, 520)
        self.keep_weekly.setSuffix(" weekly")
        self.keep_weekly.setValue(weekly)
        self.keep_daily.valueChanged.connect(self._on_kept_changed)
        self.keep_weekly.valueChanged.connect(self._on_kept_changed)
        kept = QHBoxLayout()
        kept.addWidget(self.keep_daily)
        kept.addWidget(self.keep_weekly)
        kept.addStretch(1)
        form = QFormLayout()
        form.addRow("Also back up to", extra)
        form.addRow("Keep", kept)
        layout.addLayout(form)
        layout.addWidget(
            self._note(
                "Another drive keeps a copy should this one fail; when it is not "
                "connected, the backup there waits for the next start. Those before an "
                "update or a restore are always kept."
            )
        )
        self.backups.changed.connect(self._fill_backups)
        self._show_extra_folder()
        self._fill_backups()

    def _fill_backups(self):
        chosen = self._chosen_backup()
        self.backup_list.blockSignals(True)
        self.backup_list.clear()
        for made in self.backups.backups():
            item = QListWidgetItem(backup_text(made))
            item.setData(Qt.ItemDataRole.UserRole, made)
            self.backup_list.addItem(item)
            if chosen is not None and made.path == chosen.path:
                self.backup_list.setCurrentItem(item)
        if self.backup_list.currentRow() < 0 and self.backup_list.count():
            self.backup_list.setCurrentRow(0)
        self.backup_list.blockSignals(False)
        self.back_up_button.setEnabled(not self.backups.is_busy())
        self._show_backup()

    def _chosen_backup(self):
        item = self.backup_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _show_backup(self, *_items):
        made = self._chosen_backup()
        self.restore_button.setEnabled(made is not None and made.kind != DAMAGED)
        if made is None:
            self.backup_details.setText("No backup yet.")
            return
        if self._contents is not None and self._contents[0] == made.path:
            self._show_contents(made, self._contents[1])
            return
        self.backup_details.setText("Reading…")
        self.backups.contents(made.path, lambda held: self._on_contents(made, held))

    def _on_contents(self, made, held):
        if self._closed:
            return
        self._contents = (made.path, held)
        if self._chosen_backup() == made:
            self._show_contents(made, held)

    def _show_contents(self, made, held):
        where = os.path.dirname(made.path)
        if held is None:
            self.backup_details.setText(f"It can't be read. In {where}.")
            return
        photos = f"{held.photos:,} photo{'s' * (held.photos != 1)}"
        text = f"{photos}, {held.edits:,} edited. In {where}."
        if not held.vectors:
            text += " Without the index for search, which is made again after a restore."
        self.backup_details.setText(text)

    def _back_up(self):
        self.backups.back_up()
        self.back_up_button.setEnabled(False)

    def restore_backup(self):
        """Ask, then have the app quit and start again on the backup chosen."""
        made = self._chosen_backup()
        if made is None:
            return
        held = self._contents[1] if self._contents and self._contents[0] == made.path else None
        what = ""
        if held is not None:
            what = f" It has {held.photos:,} photo{'s' * (held.photos != 1)}."
        answer = QMessageBox.question(
            self,
            "Restore This Backup?",
            f"The library goes back to how it was on {when_text(made.taken, ' at ')}."
            f"{what} Edits, flags and ratings made since then are not in it.\n\n"
            "The library as it is now is backed up first, then Lumina starts again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.accept()
            self.backups.request_restore(made.path)

    def choose_backup_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Also Back Up To", settings.backup_folder() or ""
        )
        if folder:
            self.set_backup_folder(folder)

    def set_backup_folder(self, folder):
        settings.set_backup_folder(folder)
        self.backups.extra_folder = folder or None
        self._show_extra_folder()
        self._fill_backups()

    def _show_extra_folder(self):
        folder = self.backups.extra_folder
        self.extra_folder.setText(folder or "Nowhere else")
        self.clear_extra.setEnabled(bool(folder))

    def _on_kept_changed(self):
        settings.set_backups_kept(self.keep_daily.value(), self.keep_weekly.value())
        self.backups.keep_daily = self.keep_daily.value()
        self.backups.keep_weekly = self.keep_weekly.value()

    def _show_profile_buttons(self, *_row):
        self.remove_profile_folder.setEnabled(self.profile_folders.currentRow() >= 0)

    def choose_profile_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder of Camera Profiles")
        if folder:
            self.add_profile_folder_path(folder)

    def add_profile_folder_path(self, folder):
        folders = settings.profile_folders()
        if folder not in folders:
            settings.set_profile_folders([*folders, folder])
            self.profile_folders.addItem(folder)

    def remove_chosen_profile_folder(self):
        row = self.profile_folders.currentRow()
        if row < 0:
            return
        folders = settings.profile_folders()
        del folders[row]
        settings.set_profile_folders(folders)
        self.profile_folders.takeItem(row)
        self._show_profile_buttons()

    def _fill_sections(self):
        titles = dict(SECTIONS)
        hidden = settings.hidden_sections()
        self.sections.blockSignals(True)
        self.sections.clear()
        for name in settings.develop_order(SECTION_NAMES):
            item = QListWidgetItem(titles[name].capitalize().replace("Hsl", "HSL"))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(
                (item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsDropEnabled
            )
            item.setCheckState(Qt.CheckState.Unchecked if name in hidden else Qt.CheckState.Checked)
            self.sections.addItem(item)
        self.sections.blockSignals(False)

    def _save_sections(self, *_args):
        items = [self.sections.item(row) for row in range(self.sections.count())]
        names = [item.data(Qt.ItemDataRole.UserRole) for item in items]
        settings.set_develop_order(names)
        settings.set_hidden_sections(
            [
                item.data(Qt.ItemDataRole.UserRole)
                for item in items
                if item.checkState() != Qt.CheckState.Checked
            ]
        )

    def _move_section(self, step):
        row = self.sections.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.sections.count():
            return
        item = self.sections.takeItem(row)
        self.sections.insertItem(target, item)
        self.sections.setCurrentRow(target)
        self._save_sections()

    def _reset_sections(self):
        settings.reset_develop_panel()
        self._fill_sections()

    def _heading(self, text):
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _note(self, text):
        label = QLabel(text)
        label.setEnabled(False)
        label.setWordWrap(True)
        return label

    def _on_watch_toggled(self, on):
        settings.set_watch_folders(on)
        self.library.watcher.set_enabled(on)

    def _show_watching(self):
        watcher = self.library.watcher
        text = (
            "A photo copied into a folder, moved or deleted shows up a few seconds later, "
            "without a Rescan."
        )
        if watcher.unwatched:
            names = {f.id: f.name for f in self.library.folders()}
            named = ", ".join(sorted(names.get(folder, "?") for folder in watcher.unwatched))
            text += f" Too many folders inside to watch: {named}. Rescan finds their changes."
        self.watch_note.setText(text)

    def _on_sidecars_toggled(self, on):
        settings.set_sidecars(on)
        self.library.set_sidecars(on)

    def _on_gentle_toggled(self, on):
        settings.set_gentle_scans(on)
        self.library.gentle_scans = on

    def _limit_timer_pending(self):
        return self.library is not None and self._limit_timer.isActive()

    def _on_limit_changed(self, gigabytes):
        settings.set_thumbnail_limit_gb(gigabytes)
        self._limit_timer.start()

    def apply_cache_limit(self):
        """Prune to the limit now: after the spin box has been left alone for a moment."""
        self._limit_timer.stop()
        self.library.set_thumbnail_limit(settings.thumbnail_limit())

    def _show_usage(self, *_pruned):
        self.cache_usage.setText("Counting…")
        self.library.thumbnail_usage(self._on_usage)

    def _on_usage(self, size):
        if self._closed:
            return
        self.cache_usage.setText(
            "They take " + size_text(size) + " now." if size is not None else ""
        )

    def _on_density_changed(self):
        name = self.density.currentData()
        settings.set_density(name)
        style.set_density(name, QApplication.instance())

    def _show_model(self):
        search = self.search
        size = f"{search.download_size() / 1e6:,.0f} MB"
        if search.is_removing():
            text = "The search model is being deleted…"
        elif search.is_downloading():
            text = f"The search model ({size}) is being downloaded."
        elif search.is_ready():
            text = f"The search model ({size}) is on this computer, in:"
        else:
            text = (
                f"The search model ({size}) is not downloaded. Search needs it; it is "
                "fetched once, and everything then runs on this computer. It goes in:"
            )
        self.model_state.setText(text)
        busy = search.is_removing() or search.is_downloading()
        self.download.setEnabled(not busy and not search.is_ready())
        self.remove.setEnabled(not search.is_removing() and (search.is_ready() or busy))

    def remove_model(self):
        """Delete the model, asking first. Download fetches it again."""
        answer = QMessageBox.question(
            self,
            "Delete the Search Model?",
            "Search will not work until it is downloaded again. Photos already indexed "
            "need not be indexed again for the same model.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.search.remove_model()

    def done(self, result):
        self._closed = True
        style.themed.changed.disconnect(self._show_look)
        if self._limit_timer_pending():
            self.apply_cache_limit()
        if self.library is not None:
            self.library.thumbnails_pruned.disconnect(self._show_usage)
            self.library.watcher.status_changed.disconnect(self._show_watching)
        if self.search is not None:
            self.search.model_changed.disconnect(self._show_model)
        if self.backups is not None:
            self.backups.changed.disconnect(self._fill_backups)
        super().done(result)
