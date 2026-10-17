"""Photos that look alike or are copies of one file, across the library (#61): groups
to stack, or to thumbs-down all but the best. Nothing here deletes a photo."""

import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from lumina.integration import style
from lumina.qt_models.thumbnails import Thumbnails

# The slider's range, in percent: from clearly alike to the same picture
LOWEST, HIGHEST = 80, 100
_IDS = Qt.ItemDataRole.UserRole + 1


class SimilarDialog(QDialog):
    # The similarity wanted, 0 to 1: look again
    search_requested = Signal(float)
    # The groups chosen, each a list of photo ids, the best first
    stack_requested = Signal(object)
    # The ids of the photos other than the best of each group chosen
    reject_requested = Signal(object)

    def __init__(self, resolve, similarity, parent=None):
        """`resolve(photo)` is where the photo is now, or None when it can't be read."""
        super().__init__(parent)
        self.setWindowTitle("Find Duplicates")
        self.resize(style.px(560), style.px(640))
        self._resolve = resolve
        self._thumbnails = Thumbnails(self)
        self._thumbnails.changed.connect(self._thumbnail_made)
        # Thumbnail key -> its item
        self._items = {}
        layout = QVBoxLayout(self)
        layout.setSpacing(style.gap(10))
        row = QHBoxLayout()
        row.addWidget(QLabel("Looks alike:"))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(LOWEST, HIGHEST)
        self.slider.setValue(round(similarity * 100))
        self.slider.setToolTip("Lower finds more photos that only resemble each other")
        self.slider.setTracking(False)
        self.slider.valueChanged.connect(self._ask)
        self.slider.sliderMoved.connect(self._show_value)
        row.addWidget(self.slider, 1)
        self.value = QLabel()
        row.addWidget(self.value)
        layout.addLayout(row)
        self.summary = QLabel("Looking…")
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(style.px(64), style.px(64)))
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        self.stack_button = QPushButton("Stack")
        self.stack_button.setToolTip("Put each group chosen in a stack, the best on top")
        self.stack_button.clicked.connect(lambda: self.stack_requested.emit(self._chosen()))
        self.reject_button = QPushButton("Thumbs Down the Others")
        self.reject_button.setToolTip(
            "Give every photo of each group chosen but the best a thumbs down. No photo is deleted"
        )
        self.reject_button.clicked.connect(self._reject)
        for button in (self.stack_button, self.reject_button):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        buttons.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._show_value(self.slider.value())
        self._update_buttons()

    def similarity(self):
        return self.slider.value() / 100

    def _show_value(self, percent):
        self.value.setText(f"{percent}%")

    def _ask(self, percent):
        self._show_value(percent)
        self.summary.setText("Looking…")
        self.search_requested.emit(percent / 100)

    def searching(self):
        self.summary.setText("Looking…")

    def failed(self, message):
        self.summary.setText(f"Could not look: {message}")

    def set_groups(self, groups):
        """Show `groups`: [(exact, [photo, ...])], the best photo of each first."""
        self._thumbnails.clear()
        self._items = {}
        self.tree.clear()
        extra = 0
        for exact, photos in groups:
            extra += len(photos) - 1
            head = QTreeWidgetItem(
                [f"{len(photos)} photos, " + ("the same file" if exact else "alike")]
            )
            head.setData(0, _IDS, [photo.id for photo in photos])
            self.tree.addTopLevelItem(head)
            for place, photo in enumerate(photos):
                name = os.path.basename(photo.path)
                child = QTreeWidgetItem(
                    [f"{name}   {os.path.dirname(photo.path)}" + ("   (best)" if not place else "")]
                )
                child.setToolTip(0, photo.path)
                head.addChild(child)
                self._items[self._key(photo)] = (child, photo)
                self._set_icon(child, photo)
            head.setExpanded(True)
        if groups:
            self.summary.setText(
                f"{len(groups)} group{'s' if len(groups) != 1 else ''}, "
                f"{extra} photo{'s' if extra != 1 else ''} besides the best of each"
            )
        else:
            self.summary.setText("No photos that look alike at this setting")
        self._update_buttons()

    def _key(self, photo):
        return ("similar", photo.id, photo.edit)

    def _set_icon(self, item, photo):
        path = self._resolve(photo)
        item.setIcon(0, self._thumbnails.icon(self._key(photo), path, photo.content_id, photo.edit))

    def _thumbnail_made(self, key):
        found = self._items.get(key)
        if found is not None:
            self._set_icon(*found)

    def _chosen(self):
        """The groups chosen (a chosen photo chooses its group), each as photo ids, the
        best first."""
        heads = {}
        for item in self.tree.selectedItems():
            head = item.parent() or item
            heads[self.tree.indexOfTopLevelItem(head)] = head
        return [heads[number].data(0, _IDS) for number in sorted(heads)]

    def _reject(self):
        self.reject_requested.emit([i for group in self._chosen() for i in group[1:]])

    def _update_buttons(self):
        chosen = bool(self.tree.selectedItems())
        self.stack_button.setEnabled(chosen)
        self.reject_button.setEnabled(chosen)
