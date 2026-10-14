"""The running job in the status bar: what it does, how far, and Cancel. When it
ends, how it ended, for a while."""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QToolButton, QWidget

from lumina.integration import style
from lumina.qt_models import library

# How long the end of a job stays in the status bar
_KEEP_MS = 10000


class JobStatus(QWidget):
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, style.px(8), 0)
        layout.setSpacing(style.px(8))
        self.label = QLabel()
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(style.px(140), style.px(6))
        self.cancel = QToolButton()
        self.cancel.setText("Cancel")
        self.cancel.setToolTip("Stop scanning")
        self.cancel.setAutoRaise(True)
        self.cancel.clicked.connect(self.cancel_requested)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        layout.addWidget(self.cancel)
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.setInterval(_KEEP_MS)
        self._hide.timeout.connect(self.hide)
        self.hide()

    def show_status(self, status):
        running = status.state in (library.QUEUED, library.RUNNING)
        self.label.setText(describe(status))
        tooltip = "\n".join(f"{path}: {message}" for path, message in status.failures)
        self.label.setToolTip(tooltip)
        failed = status.state == library.FAILED or (not running and status.failures)
        colour = style.color("danger" if failed else "textSecondary").name()
        self.label.setStyleSheet(f"color: {colour};")
        self.bar.setVisible(running)
        self.cancel.setVisible(running)
        if running:
            self._hide.stop()
            if status.phase == library.DETAILS and status.total:
                self.bar.setRange(0, status.total)
                self.bar.setValue(status.done)
            else:
                # Finding: how many there are is not known until the end
                self.bar.setRange(0, 0)
        else:
            self._hide.start()
        self.show()


def describe(status):
    """A JobStatus in words, for the status bar."""
    name = status.folder_name
    more = f"  ·  {status.queued} more waiting" if status.queued else ""
    if status.state == library.QUEUED or status.phase is None:
        return f"Waiting to scan {name}{more}"
    if status.state == library.RUNNING:
        if status.phase == library.FINDING:
            return f"Scanning {name}: {status.done} photos found{more}"
        return f"Reading details in {name}: {status.done} of {status.total}{more}"
    ended = {
        library.DONE: "Scanned",
        library.CANCELLED: "Stopped scanning",
        library.FAILED: "Could not scan",
    }.get(status.state, "Scanned")
    text = f"{ended} {name}"
    if status.message:
        text += f": {status.message}"
    return text + more
