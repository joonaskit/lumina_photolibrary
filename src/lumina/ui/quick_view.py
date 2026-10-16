"""A photo opened from the file manager whose folder is not in the library: shown in its
own window, with nothing kept for it (no flags, rating or edit), and a question whether
to add its folder. Nothing happens to the library unless the user says yes."""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from lumina.integration import style
from lumina.qt_models.previews import PreviewLoader
from lumina.ui.image_view import ImageView

# The longest side of the image loaded, as the viewer's
_MAX_SIDE = 3840


class QuickView(QWidget):
    # The folder to add to the library
    add_requested = Signal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.folder = os.path.dirname(path)
        self.setWindowTitle(f"{os.path.basename(path)} — Lumina")
        self.setWindowIcon(style.app_icon())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(style.px(1100), style.px(760))

        self.view = ImageView(zoomable=True)
        self.view.set_message("Loading…")
        self.question = QLabel(
            f"“{os.path.basename(self.folder) or self.folder}” is not in your library."
        )
        self.question.setWordWrap(True)
        self.add_button = QPushButton("Add Folder to Library")
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(lambda: self.add_requested.emit(self.folder))
        self.dismiss_button = QPushButton("Not Now")
        self.dismiss_button.clicked.connect(self.dismiss)
        self.banner = QWidget()
        row = QHBoxLayout(self.banner)
        row.addWidget(self.question, 1)
        row.addWidget(self.dismiss_button)
        row.addWidget(self.add_button)
        column = QVBoxLayout(self)
        column.addWidget(self.banner)
        column.addWidget(self.view, 1)

        def space():
            column.setContentsMargins(style.gap(8), style.gap(8), style.gap(8), style.gap(8))
            column.setSpacing(style.gap(8))
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(style.gap(8))

        style.follow_density(self, space)

        self.previews = PreviewLoader(self)
        self.previews.ready.connect(self._ready)
        self.previews.failed.connect(self._failed)
        self.previews.request(path, _MAX_SIDE)

    def dismiss(self):
        """Look at the photo without being asked again."""
        self.banner.hide()
        self.view.setFocus()

    def _ready(self, _path, image, full):
        self.view.set_image(image, full)

    def _failed(self, _path, message):
        self.view.set_message(f"Cannot show {os.path.basename(self.path)}\n{message}")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)
