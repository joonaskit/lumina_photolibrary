"""Giving photos keywords, and taking them off: a field with completion over the
keywords there are, and the list of what the chosen photos have."""

from PySide6.QtCore import QStringListModel, Qt, Signal
from PySide6.QtWidgets import (
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from lumina.integration import style

PATH_ROLE = Qt.ItemDataRole.UserRole + 1


class KeywordsDialog(QDialog):
    """Changes are made as they are asked for (and can be undone from the window), so
    this only says what was asked: the window answers with `set_state`."""

    # The keywords typed, as text: several are separated by commas
    add_requested = Signal(str)
    # The paths of the keywords chosen in the list
    remove_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keywords")
        self.setMinimumWidth(style.px(380))
        layout = QVBoxLayout(self)
        layout.setSpacing(style.gap(10))
        self.heading = QLabel()
        layout.addWidget(self.heading)
        self.field = QLineEdit()
        self.field.setPlaceholderText("Add keywords, separated by commas")
        self.field.setToolTip("Places > Finland > Helsinki makes a keyword inside another")
        self._names = QStringListModel(self)
        self._completer = QCompleter(self._names, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.field.setCompleter(self._completer)
        self.field.returnPressed.connect(self._add)
        layout.addWidget(self.field)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.setMinimumHeight(style.px(140))
        self.list.itemSelectionChanged.connect(self._update_remove)
        layout.addWidget(self.list, 1)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Take the chosen keywords off the photos (Delete)")
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(self._remove)
        layout.addWidget(self.remove_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).setAutoDefault(False)
        layout.addWidget(buttons)
        self._update_remove()

    def set_state(self, heading, known, current, total):
        """`heading`: what the photos are. `known`: every keyword path, to complete.
        `current`: {path: how many of the `total` photos have it}."""
        self.heading.setText(heading)
        self._names.setStringList(list(known))
        chosen = {item.data(PATH_ROLE) for item in self.list.selectedItems()}
        self.list.clear()
        for path in sorted(current, key=str.casefold):
            count = current[path]
            text = path if count == total else f"{path}  ({count} of {total})"
            item = QListWidgetItem(text)
            item.setData(PATH_ROLE, path)
            self.list.addItem(item)
            item.setSelected(path in chosen)
        self._update_remove()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete and self.list.hasFocus():
            self._remove()
            return
        super().keyPressEvent(event)

    def _add(self):
        text = self.field.text().strip()
        if text:
            self.field.clear()
            self.add_requested.emit(text)

    def _remove(self):
        paths = [item.data(PATH_ROLE) for item in self.list.selectedItems()]
        if paths:
            self.remove_requested.emit(paths)

    def _update_remove(self):
        self.remove_button.setEnabled(bool(self.list.selectedItems()))
