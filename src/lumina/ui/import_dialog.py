"""Choosing what to import from a card, and where to: the photos by the day they were
taken, the library folder, how the copies are laid out and named, and a second copy."""

import os
from collections import defaultdict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style
from lumina.qt_models import importing
from lumina.qt_models.importing import CameraBusy, NotConnected
from lumina.ui import settings

# Layouts and namings in words, with what they make of a photo taken on 14 October 2026
LAYOUTS = (
    ("year-day", "By year and day"),
    ("year-month", "By year and month"),
    ("day", "By day"),
    ("none", "All in one folder"),
)
NAMINGS = (
    ("original", "As on the card"),
    ("date", "Time taken first"),
)
_OTHER = "other"
# A camera on USB in the source box is "camera:" and its port
_CAMERA = "camera:"
DAY_ROLE = Qt.ItemDataRole.UserRole + 1


class ImportDialog(QDialog):
    def __init__(self, importer, folders, card=None, parent=None):
        """`importer` is the ImportService; `folders` the library's (FolderInfo), the
        connected ones of which the photos can go to; `card` the card to read first,
        else the first one mounted."""
        super().__init__(parent)
        self.setWindowTitle("Import Photos")
        self.importer = importer
        self.reading = None
        # The card or folder read, or None
        self._source = None
        choices = settings.import_choices()
        layout = QVBoxLayout(self)
        layout.setSpacing(style.px(10))

        # From
        self.source_box = QComboBox()
        for card_path in importer.cards():
            self.source_box.addItem(_card_name(card_path), card_path)
        for model, port in importer.cameras():
            self.source_box.addItem(f"{model}  (camera on USB)", _CAMERA + port)
        self.source_box.addItem("Other Folder…", _OTHER)
        self.source_box.activated.connect(self._source_chosen)
        top = QFormLayout()
        top.addRow("From:", self.source_box)
        layout.addLayout(top)
        self.read_label = QLabel()
        self.read_label.setWordWrap(True)
        layout.addWidget(self.read_label)
        self.days = QListWidget()
        self.days.setMinimumHeight(style.px(140))
        self.days.itemChanged.connect(lambda _item: self._update())
        layout.addWidget(self.days, 1)
        self.again_box = QCheckBox("Also photos imported before")
        self.again_box.setToolTip(
            "Photos already in the library, or imported from this card before, are left "
            "out unless this is ticked"
        )
        self.again_box.toggled.connect(lambda _on: self._fill_days())
        layout.addWidget(self.again_box)

        # To
        form = QFormLayout()
        self.destination_box = QComboBox()
        places = [folder.path for folder in folders if folder.connected]
        if choices["destination"] and choices["destination"] not in places:
            if os.path.isdir(choices["destination"]):
                places.insert(0, choices["destination"])
        for place in places:
            self.destination_box.addItem(place, place)
        self.destination_box.addItem("Other Folder…", _OTHER)
        self.destination_box.setCurrentIndex(
            max(0, self.destination_box.findData(choices["destination"]))
        )
        self._destination = self.destination_box.currentData()
        self.destination_box.activated.connect(self._destination_chosen)
        form.addRow("To:", self.destination_box)
        self.layout_box = QComboBox()
        for key, words in LAYOUTS:
            self.layout_box.addItem(words, key)
        self.layout_box.setCurrentIndex(max(0, self.layout_box.findData(choices["layout"])))
        self.layout_box.currentIndexChanged.connect(lambda _index: self._update())
        form.addRow("Folders:", self.layout_box)
        self.naming_box = QComboBox()
        for key, words in NAMINGS:
            self.naming_box.addItem(words, key)
        self.naming_box.setCurrentIndex(max(0, self.naming_box.findData(choices["naming"])))
        self.naming_box.currentIndexChanged.connect(lambda _index: self._update())
        form.addRow("Names:", self.naming_box)
        self.backup_box = QCheckBox("Also copy to")
        self.backup_box.setToolTip("A second copy of every photo, laid out the same: a backup")
        self.backup_box.setChecked(choices["backup_on"] and bool(choices["backup"]))
        self.backup_box.toggled.connect(lambda _on: self._update())
        self.backup_label = QLabel()
        self.backup_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._backup = choices["backup"] or None
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_backup)
        backup_row = QWidget()
        row = QHBoxLayout(backup_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.backup_box)
        row.addWidget(self.backup_label, 1)
        row.addWidget(choose)
        form.addRow("Backup:", backup_row)
        layout.addLayout(form)
        self.example = QLabel()
        self.example.setWordWrap(True)
        layout.addWidget(self.example)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        note = QLabel(
            "The photos on the card or camera are only read: nothing there is changed or "
            "deleted. Each copy is read back and checked against what was read."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.import_button = buttons.addButton("Import", QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.setDefault(True)
        buttons.accepted.connect(self._accepted)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(style.px(560), style.px(600))

        start = card if card is not None else self.source_box.itemData(0)
        if start != _OTHER and not _is_camera(start):
            self.set_source(start)
        elif _is_camera(start):
            # A camera is held while it is read: not before it is asked for
            self.read_label.setText(
                "No memory card is connected: choose the camera to read it, or a folder."
            )
        else:
            self.read_label.setText("No memory card is connected: choose a folder to import from.")
        self._update()

    # Choosing

    def set_source(self, path):
        """Read the photos under `path`: a card or a folder, or on a camera (its entry's
        data, "camera:" and its port)."""
        index = self.source_box.findData(path)
        if index < 0:
            self.source_box.insertItem(self.source_box.count() - 1, path, path)
            index = self.source_box.findData(path)
        self.source_box.setCurrentIndex(index)
        self._source = path
        self.reading = None
        self.days.clear()
        self.read_label.setText(f"Reading {self._source_name()}…")
        self._update()
        if _is_camera(path):
            self.importer.read_camera(self._on_read, self._on_read_progress)
        else:
            self.importer.read(path, self._on_read, self._on_read_progress)

    def set_destination(self, path):
        index = self.destination_box.findData(path)
        if index < 0:
            self.destination_box.insertItem(0, path, path)
            index = 0
        self.destination_box.setCurrentIndex(index)
        self._destination = path
        self._update()

    def set_backup(self, path):
        """Copy to `path` too, or not (None)."""
        if path:
            self._backup = path
        self.backup_box.setChecked(bool(path))
        self._update()

    def _source_chosen(self, index):
        if self.source_box.itemData(index) != _OTHER:
            self.set_source(self.source_box.itemData(index))
            return
        path = QFileDialog.getExistingDirectory(self, "Import From")
        if path:
            self.set_source(path)
        else:
            self.source_box.setCurrentIndex(max(0, self.source_box.findData(self._source)))

    def _destination_chosen(self, index):
        if self.destination_box.itemData(index) != _OTHER:
            self._destination = self.destination_box.itemData(index)
            self._update()
            return
        path = QFileDialog.getExistingDirectory(self, "Import To")
        if path:
            self.set_destination(path)
        else:
            self.destination_box.setCurrentIndex(
                max(0, self.destination_box.findData(self._destination))
            )

    def _choose_backup(self):
        path = QFileDialog.getExistingDirectory(self, "Back Up To", self._backup or "")
        if path:
            self.set_backup(path)

    # Reading

    def _source_name(self):
        """What the source is called to the user: the camera's model, else the path."""
        if _is_camera(self._source):
            index = self.source_box.findData(self._source)
            if index >= 0:
                return self.source_box.itemText(index).partition("  (")[0]
            return "the camera"
        return self._source

    def _on_read_progress(self, done, total):
        self.read_label.setText(f"Reading {self._source_name()}: {done} of {total} photos…")

    def _on_read(self, reading):
        if isinstance(reading, BaseException):
            name = self._source_name()
            if isinstance(reading, NotConnected) and not isinstance(reading, CameraBusy):
                self.read_label.setText(f"{name} is not connected.")
            else:
                self.read_label.setText(f"Could not read {name}: {reading}")
            return
        if reading is None:
            return
        self.reading = reading
        count = len(reading.candidates)
        before = sum(1 for c in reading.candidates if c.imported)
        text = f"{count} photo{'s' * (count != 1)}"
        if before:
            text += f", {before} of them imported before"
        if reading.failures:
            text += f". {len(reading.failures)} could not be read"
            self.read_label.setToolTip(
                "\n".join(f"{path}: {message}" for path, message in reading.failures[:20])
            )
        self.read_label.setText(text if count else f"No photos in {self._source_name()}")
        self._fill_days()

    def _fill_days(self):
        if self.reading is None:
            return
        again = self.again_box.isChecked()
        days = defaultdict(list)
        for candidate in self.reading.candidates:
            days[candidate.taken.date()].append(candidate)
        self.days.blockSignals(True)
        self.days.clear()
        for day in sorted(days, reverse=True):
            photos = days[day]
            new = [c for c in photos if again or not c.imported]
            words = f"{day:%A %-d %B %Y}  ·  {len(photos)} photo{'s' * (len(photos) != 1)}"
            if len(new) < len(photos):
                words += f", {len(photos) - len(new)} imported before"
            item = QListWidgetItem(words)
            item.setData(DAY_ROLE, day)
            if new:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.days.addItem(item)
        self.days.blockSignals(False)
        self._update()

    # What is chosen

    def chosen(self):
        """The photos to import: those of the days ticked, without those imported before
        unless asked for. As a Reading's candidates, in time order."""
        if self.reading is None:
            return []
        days = {
            self.days.item(row).data(DAY_ROLE)
            for row in range(self.days.count())
            if self.days.item(row).checkState() == Qt.CheckState.Checked
        }
        again = self.again_box.isChecked()
        chosen = [
            c
            for c in self.reading.candidates
            if c.taken.date() in days and (again or not c.imported)
        ]
        return sorted(chosen, key=lambda c: (c.taken, c.path))

    def destination(self):
        return self._destination if self._destination != _OTHER else None

    def layout_name(self):
        return self.layout_box.currentData()

    def naming(self):
        return self.naming_box.currentData()

    def backup(self):
        return self._backup if self.backup_box.isChecked() and self._backup else None

    def _update(self):
        self.backup_label.setText(self._backup or "No folder chosen")
        self.backup_box.setEnabled(bool(self._backup))
        chosen = self.chosen()
        destination = self.destination()
        if destination is not None:
            sample = chosen[0] if chosen else None
            if sample is not None:
                below = importing.subfolder(sample.taken, self.layout_name())
                name = importing.file_name(sample, self.naming())
                where = os.path.join(destination, below, name)
                self.example.setText(f"For example {where}")
            else:
                self.example.setText(f"Into {destination}")
        size = sum(c.size for c in chosen)
        free = self.importer.free_space(destination) if destination else None
        text = f"{len(chosen)} photo{'s' * (len(chosen) != 1)} to import, {_size(size)}"
        enough = free is None or size <= free
        if free is not None:
            text += f"  ·  {_size(free)} free there"
        if not enough:
            text += ": not enough space"
        self.summary.setText(text)
        colour = style.color("danger" if not enough else "textSecondary").name()
        self.summary.setStyleSheet(f"color: {colour};")
        self.import_button.setEnabled(bool(chosen) and destination is not None and enough)

    def _accepted(self):
        settings.set_import_choices(
            self.destination() or "",
            self.layout_name(),
            self.naming(),
            self._backup,
            self.backup_box.isChecked(),
        )
        self.accept()

    def done(self, result):
        self.importer.stop_reading()
        super().done(result)


def _is_camera(source):
    return isinstance(source, str) and source.startswith(_CAMERA)


def _card_name(path):
    return f"{os.path.basename(path.rstrip('/')) or path}  ({path})"


def _size(size):
    if size >= 1e9:
        return f"{size / 1e9:.1f} GB"
    return f"{size / 1e6:.0f} MB" if size >= 1e6 else f"{max(size, 0) / 1e3:.0f} kB"
