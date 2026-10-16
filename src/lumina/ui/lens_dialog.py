"""Choosing the lens to correct a photo as, from those that fit its camera."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QVBoxLayout,
)

from lumina.integration import style


class LensDialog(QDialog):
    def __init__(self, names, current=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Lens")
        self.resize(style.px(460), style.px(420))
        layout = QVBoxLayout(self)
        layout.setSpacing(style.px(10))
        layout.addWidget(QLabel("Lenses that fit this camera:"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter, e.g. 24-70")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        layout.addWidget(self.filter)
        self.list = QListWidget()
        self.list.addItems(names)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.list.currentItemChanged.connect(lambda *_a: self._enable())
        layout.addWidget(self.list, 1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for row in range(self.list.count()):
            if self.list.item(row).text() == current:
                self.list.setCurrentRow(row)
        self._enable()

    def _filter(self, text):
        words = text.lower().split()
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setHidden(not all(word in item.text().lower() for word in words))

    def _enable(self):
        item = self.list.currentItem()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            item is not None and not item.isHidden()
        )

    def chosen(self):
        """The name of the lens picked, or None."""
        item = self.list.currentItem()
        return item.text() if item is not None else None

    def keyPressEvent(self, event):
        # Enter in the filter picks the first lens left, not the dialog's default button
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.filter.hasFocus():
            for row in range(self.list.count()):
                if not self.list.item(row).isHidden():
                    self.list.setCurrentRow(row)
                    break
            return
        super().keyPressEvent(event)
