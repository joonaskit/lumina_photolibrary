"""The app's settings, in one window. A change is made as it is picked: there is no
OK to press, only Close. What is chosen is kept in `ui/settings.py`."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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

from lumina.integration import style
from lumina.ui import settings
from lumina.ui.develop_view import SECTION_NAMES, SECTIONS

_DENSITIES = (("comfortable", "Comfortable"), ("compact", "Compact"))
# After the limit was last changed, so that spinning it down does not delete on the way
_LIMIT_SETTLE_MS = 1500
_MAX_LIMIT_GB = 1000


def size_text(size):
    """Bytes for people: megabytes below a gigabyte."""
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GB"
    return f"{size / 1024**2:.0f} MB"


class SettingsDialog(QDialog):
    """`search` is the SearchService and `library` the LibraryService; without one there
    is no Search, or no Thumbnails, section."""

    def __init__(self, search=None, library=None, parent=None):
        super().__init__(parent)
        self.search = search
        self.library = library
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

        layout = page("General")

        self.density = QComboBox()
        for key, text in _DENSITIES:
            self.density.addItem(text, key)
        self.density.setCurrentIndex(max(0, self.density.findData(settings.density())))
        self.density.currentIndexChanged.connect(self._on_density_changed)
        appearance = QFormLayout()
        appearance.addRow("Density", self.density)
        layout.addWidget(self._heading("Appearance"))
        layout.addLayout(appearance)
        layout.addWidget(
            self._note(
                "Compact takes less room for the menus, buttons and the Develop panel. "
                "The text, the icons and the thumbnails stay as large."
            )
        )

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

        for made in pages:
            made.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        outer.addWidget(close)

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
        if self._limit_timer_pending():
            self.apply_cache_limit()
        if self.library is not None:
            self.library.thumbnails_pruned.disconnect(self._show_usage)
            self.library.watcher.status_changed.disconnect(self._show_watching)
        if self.search is not None:
            self.search.model_changed.disconnect(self._show_model)
        super().done(result)
