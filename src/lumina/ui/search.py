"""Search in the window: the field above the grid, and its progress in the status bar.

The field searches as it is typed in, a moment after the last key, or at once on
Enter. "Like this photo" shows as a chip in front of it, with a button to end it.
Downloading the model and indexing show in the status bar, each with its Cancel.
"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QToolButton,
    QWidget,
)

from lumina.integration import style
from lumina.qt_models import library

# How long after the last key a search starts
_TYPING_MS = 350
# How long the end of indexing stays in the status bar
_KEEP_MS = 10000


class SearchField(QWidget):
    # The text to search for, "" for none
    searched = Signal(str)
    # The chip's button: stop showing photos like one
    similar_cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(style.px(6))
        self.chip = QToolButton()
        self.chip.setObjectName("chip")
        self.chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.chip.setFixedHeight(style.px(style.CONTROL_HEIGHT - 4))
        self.chip.clicked.connect(self.similar_cleared)
        self.chip.hide()
        self.edit = QLineEdit()
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumWidth(style.px(160))
        self.edit.setMaximumWidth(style.px(360))
        # As tall and as round as the toolbar's cards beside it
        self.edit.setFixedHeight(style.px(style.ROW_HEIGHT))
        self.edit.setObjectName("row")
        self.edit.setPlaceholderText("Search photos, in any language")
        self.edit.setToolTip("Describe what is in the photo: “sunset over the sea”, “majakka”")
        self._glass = QAction(self.edit)
        self.edit.addAction(self._glass, QLineEdit.ActionPosition.LeadingPosition)
        layout.addWidget(self.chip)
        layout.addWidget(self.edit)
        self._typing = QTimer(self)
        self._typing.setSingleShot(True)
        self._typing.setInterval(_TYPING_MS)
        self._typing.timeout.connect(self._search)
        self.edit.textEdited.connect(lambda _text: self._typing.start())
        # Enter searches again even for the same text
        self.edit.returnPressed.connect(lambda: self._search(again=True))
        # The clear button sets the text without textEdited
        self.edit.textChanged.connect(lambda text: text or self._clearing or self._search())
        self._clearing = False
        self._sent = ""
        self.draw_icons()

    def draw_icons(self):
        self._glass.setIcon(style.icon("edit-find", "system-search", colour="textSecondary"))
        self.chip.setIcon(style.icon("window-close", "edit-clear", colour="textSecondary"))

    def text(self):
        return self.edit.text().strip()

    def focus(self):
        self.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.edit.selectAll()

    def clear(self):
        """Empty the field without searching: the window ends the search itself."""
        self._typing.stop()
        # Not by blocking signals: the line edit hides its clear button on textChanged
        self._clearing = True
        self.edit.clear()
        self._clearing = False
        self._sent = ""

    def show_similar(self, name):
        """Show "Like <name>" in front of the field; None hides it."""
        self.chip.setVisible(name is not None)
        if name is not None:
            metrics = self.chip.fontMetrics()
            shown = metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, style.px(160))
            self.chip.setText(f"Like {shown}")
            self.chip.setToolTip(f"Photos like {name}. Click to show all photos again")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and (self.edit.text() or self.chip.isVisible()):
            self.clear()
            self.searched.emit("")
            self.similar_cleared.emit()
            return
        super().keyPressEvent(event)

    def _search(self, again=False):
        self._typing.stop()
        text = self.text()
        if text != self._sent or again:
            self._sent = text
            self.searched.emit(text)


class SearchStatus(QWidget):
    """Downloading the model or indexing, in the status bar, with Cancel."""

    cancel_download = Signal()
    cancel_indexing = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, style.px(8), 0)
        layout.setSpacing(style.px(8))
        self.label = QLabel()
        self.label.setObjectName("secondary")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(style.px(140), style.px(6))
        self.cancel = QToolButton()
        self.cancel.setText("Cancel")
        self.cancel.setAutoRaise(True)
        self.cancel.clicked.connect(self._cancel)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        layout.addWidget(self.cancel)
        self._downloading = False
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.setInterval(_KEEP_MS)
        self._hide.timeout.connect(self.hide)
        self.hide()

    def show_download(self, done, total):
        self._downloading = True
        self._hide.stop()
        self.label.setText(f"Downloading the search model: {_mb(done)} of {_mb(total)} MB")
        self.label.setToolTip("")
        self.bar.setRange(0, 1000)
        self.bar.setValue(round(1000 * done / total) if total else 0)
        self.bar.show()
        self.cancel.setToolTip("Stop the download; it continues where it stopped next time")
        self.cancel.show()
        self.show()

    def download_ended(self, error=None):
        self._downloading = False
        self.bar.hide()
        self.cancel.hide()
        if error:
            self.label.setText("The search model could not be downloaded")
            self.label.setToolTip(error)
            self._hide.start()
        else:
            self.hide()

    def show_indexing(self, status):
        if self._downloading:
            return
        running = status.state in (library.QUEUED, library.RUNNING)
        self.label.setText(describe_indexing(status))
        self.label.setToolTip("\n".join(f"{path}: {message}" for path, message in status.failures))
        self.bar.setVisible(running)
        self.cancel.setVisible(running)
        self.cancel.setToolTip("Stop indexing until the next start or scan")
        if running:
            self._hide.stop()
            if status.total:
                self.bar.setRange(0, status.total)
                self.bar.setValue(status.done)
            else:
                self.bar.setRange(0, 0)
        else:
            self._hide.start()
        if not status.total and not status.failures:
            # Nothing to do: no need to say so
            self.hide()
            return
        self.show()

    def _cancel(self):
        if self._downloading:
            self.cancel_download.emit()
        else:
            self.cancel_indexing.emit()


def describe_indexing(status):
    if status.state in (library.QUEUED, library.RUNNING):
        return f"Indexing for search: {status.done:,} of {status.total:,}".replace(",", " ")
    if status.state == library.CANCELLED:
        return "Stopped indexing for search"
    if status.state == library.FAILED:
        return f"Could not index for search: {status.message}"
    return status.message or "Indexed for search"


def _mb(size):
    return f"{size / 1_000_000:,.0f}".replace(",", " ")
