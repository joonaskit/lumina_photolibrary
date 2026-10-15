"""The app's settings, in one window. A change is made as it is picked: there is no
OK to press, only Close. What is chosen is kept in `ui/settings.py`."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from lumina.integration import style
from lumina.ui import settings

_DENSITIES = (("comfortable", "Comfortable"), ("compact", "Compact"))


class SettingsDialog(QDialog):
    """`search` is the SearchService; without one there is no Search section."""

    def __init__(self, search=None, parent=None):
        super().__init__(parent)
        self.search = search
        self.setWindowTitle("Settings")
        self.setMinimumWidth(style.px(460))

        layout = QVBoxLayout(self)
        style.follow_density(self, lambda: layout.setSpacing(style.gap(10)))

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

        if search is not None:
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

        layout.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)

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
        if self.search is not None:
            self.search.model_changed.disconnect(self._show_model)
        super().done(result)
