"""Choosing where and how to export the photo being edited."""

import os

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style
from lumina.qt_models.develop import FORMATS, ExportOptions, suggested_name
from lumina.ui import settings

_FORMAT_NAMES = (("jpeg", "JPEG"), ("png", "PNG"), ("tiff", "TIFF"))


class ExportDialog(QDialog):
    def __init__(self, source_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Photo")
        self.source_path = source_path
        choices = settings.export_choices()
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(style.px(10))
        layout.addLayout(form)

        self.format = QComboBox()
        for key, name in _FORMAT_NAMES:
            self.format.addItem(name, key)
        self.format.setCurrentIndex(max(0, self.format.findData(choices.get("format", "jpeg"))))
        form.addRow("Format", self.format)

        self.bits = QComboBox()
        self.bits.addItem("8 bits", 8)
        self.bits.addItem("16 bits", 16)
        self.bits.setCurrentIndex(max(0, self.bits.findData(int(choices.get("bits", 8)))))
        form.addRow("Depth", self.bits)

        self.quality = QSpinBox()
        self.quality.setRange(50, 100)
        self.quality.setValue(int(choices.get("quality", 92)))
        form.addRow("Quality", self.quality)

        size = QWidget()
        size_row = QHBoxLayout(size)
        size_row.setContentsMargins(0, 0, 0, 0)
        self.size_mode = QComboBox()
        self.size_mode.addItem("Full size", False)
        self.size_mode.addItem("Long side", True)
        self.long_side = QSpinBox()
        self.long_side.setRange(256, 20000)
        self.long_side.setSingleStep(256)
        self.long_side.setSuffix(" px")
        self.long_side.setValue(int(choices.get("long_side", 2048)))
        self.size_mode.setCurrentIndex(1 if choices.get("resize") in (True, "true") else 0)
        size_row.addWidget(self.size_mode)
        size_row.addWidget(self.long_side)
        form.addRow("Size", size)

        folder = QWidget()
        folder_row = QHBoxLayout(folder)
        folder_row.setContentsMargins(0, 0, 0, 0)
        self.folder = QLineEdit(choices.get("folder") or _default_folder())
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(choose)
        form.addRow("Folder", folder)

        self.name = QLineEdit()
        form.addRow("Name", self.name)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(style.px(460))

        self.format.currentIndexChanged.connect(self._on_format)
        self.size_mode.currentIndexChanged.connect(self._update_enabled)
        self.folder.textChanged.connect(lambda _t: self._suggest_name())
        self._on_format()

    def _on_format(self):
        allowed = FORMATS[self.format.currentData()]
        for index in range(self.bits.count()):
            item = self.bits.model().item(index)
            item.setEnabled(self.bits.itemData(index) in allowed)
        if self.bits.currentData() not in allowed:
            self.bits.setCurrentIndex(self.bits.findData(allowed[0]))
        self._update_enabled()
        self._suggest_name()

    def _update_enabled(self):
        self.bits.setEnabled(len(FORMATS[self.format.currentData()]) > 1)
        self.quality.setEnabled(self.format.currentData() == "jpeg")
        self.long_side.setEnabled(bool(self.size_mode.currentData()))

    def _suggest_name(self):
        name = suggested_name(self.source_path, self.options())
        # Never the original's own name in its own folder: export does not write there
        if os.path.realpath(os.path.join(self.folder.text(), name)) == os.path.realpath(
            self.source_path
        ):
            stem, extension = os.path.splitext(name)
            name = f"{stem}-edit{extension}"
        self.name.setText(name)

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Export to Folder", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def options(self):
        return ExportOptions(
            format=self.format.currentData(),
            bits=self.bits.currentData(),
            quality=self.quality.value(),
            max_side=self.long_side.value() if self.size_mode.currentData() else None,
        )

    def destination(self):
        return os.path.join(os.path.expanduser(self.folder.text()), self.name.text().strip())

    def accept(self):
        settings.set_export_choices(
            {
                "format": self.format.currentData(),
                "bits": self.bits.currentData(),
                "quality": self.quality.value(),
                "resize": bool(self.size_mode.currentData()),
                "long_side": self.long_side.value(),
                "folder": self.folder.text(),
            }
        )
        super().accept()


def _default_folder():
    pictures = os.path.expanduser("~/Pictures")
    return pictures if os.path.isdir(pictures) else os.path.expanduser("~")
