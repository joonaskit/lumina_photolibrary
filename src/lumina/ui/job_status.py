"""The running job in the status bar: what it does, how far, and Cancel. When it
ends, how it ended, for a while."""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QToolButton, QWidget

from lumina.core.dbtypes import (
    CANCELLED,
    COPYING,
    DECODING,
    DETAILS,
    DONE,
    EXPORTING,
    FAILED_JOB,
    FINDING,
    HDR,
    MERGING,
    PANORAMA,
    PLANNING,
    QUEUED,
    RUNNING,
    SIDECARS,
    STITCHING,
)
from lumina.integration import style

# How long the end of a job stays in the status bar
_KEEP_MS = 10000


class JobStatus(QWidget):
    """A scan's, by default; a merge's with `describe_merge` and the merge's phases."""

    cancel_requested = Signal()

    def __init__(
        self,
        parent=None,
        words=None,
        counted=(DETAILS,),
        cancel_tip="Stop scanning",
    ):
        super().__init__(parent)
        # The status in words, and the phases whose done and total are a bar's
        self._words = words or describe
        self._counted = counted
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, style.px(8), 0)
        layout.setSpacing(style.px(8))
        self.label = QLabel()
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(style.px(140), style.px(6))
        self.cancel = QToolButton()
        self.cancel.setText("Cancel")
        self.cancel.setToolTip(cancel_tip)
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
        running = status.state in (QUEUED, RUNNING)
        self.label.setText(self._words(status))
        tooltip = "\n".join(f"{path}: {message}" for path, message in status.failures)
        self.label.setToolTip(tooltip)
        failed = status.state == FAILED_JOB or (not running and status.failures)
        colour = style.color("danger" if failed else "textSecondary").name()
        self.label.setStyleSheet(f"color: {colour};")
        self.bar.setVisible(running)
        self.cancel.setVisible(running)
        if running:
            self._hide.stop()
            if status.phase in self._counted and status.total:
                self.bar.setRange(0, status.total)
                self.bar.setValue(status.done)
            else:
                # Finding: how many there are is not known until the end; or waiting
                self.bar.setRange(0, 0)
        else:
            self._hide.start()
        self.show()


def time_left(seconds):
    """A time to wait in words, rounded: "a minute", "25 minutes", "2 h 10 min"."""
    minutes = round(seconds / 60)
    if minutes < 2:
        return "a minute"
    if minutes < 60:
        return f"{minutes} minutes"
    hours, minutes = divmod(round(minutes / 5) * 5, 60)
    if hours >= 10:
        return f"{hours + (minutes >= 30)} h"
    return f"{hours} h" if minutes == 0 else f"{hours} h {minutes} min"


def describe(status):
    """A scan's JobStatus in words, for the status bar."""
    name = status.name
    more = f"  ·  {status.queued} more waiting" if status.queued else ""
    if status.state == QUEUED or status.phase is None:
        return f"Waiting to scan {name}{more}"
    if status.state == RUNNING:
        if status.phase == FINDING:
            return f"Scanning {name}: {status.done} photos found{more}"
        if status.phase == SIDECARS:
            return f"Reading sidecars in {name}{more}"
        left = f"  ·  about {time_left(status.eta)} left" if status.eta is not None else ""
        return f"Reading details in {name}: {status.done} of {status.total}{left}{more}"
    ended = {
        DONE: "Scanned",
        CANCELLED: "Stopped scanning",
        FAILED_JOB: "Could not scan",
    }.get(status.state, "Scanned")
    text = f"{ended} {name}"
    if status.message:
        text += f": {status.message}"
    return text + more


def merge_status():
    """A JobStatus for merges."""
    return JobStatus(
        words=describe_merge,
        counted=(DECODING, MERGING, STITCHING),
        cancel_tip="Stop merging",
    )


# Per kind of merge: (to do it, doing it, done it, what planning is)
_WORDS = {
    HDR: ("merge", "Merging", "Merged", "aligning"),
    PANORAMA: ("stitch", "Stitching", "Stitched", "finding how the photos fit"),
}


def describe_merge(status):
    """A merge's JobStatus (an HDR merge's or a panorama's) in words, for the status bar."""
    name = status.name
    verb, doing, done, planning = _WORDS.get(status.kind, _WORDS[HDR])
    more = f"  ·  {status.queued} more waiting" if status.queued else ""
    if status.state == QUEUED or status.phase is None:
        return f"Waiting to {verb} {name}{more}"
    if status.state == RUNNING:
        if status.phase == DECODING:
            return f"{doing} {name}: reading photo {status.done} of {status.total}{more}"
        if status.phase == PLANNING:
            return f"{doing} {name}: {planning}{more}"
        share = round(100 * status.done / status.total) if status.total else 0
        return f"{doing} {name}: {share} %{more}"
    if status.state == DONE:
        return (status.message or f"{done} {name}") + more
    if status.state == CANCELLED:
        return f"Stopped {doing.lower()} {name}{more}"
    return (status.message or f"Could not {verb} {name}") + more


def import_status():
    """A JobStatus for imports."""
    return JobStatus(words=describe_import, counted=(COPYING,), cancel_tip="Stop importing")


def describe_import(status):
    """An import's JobStatus in words, for the status bar."""
    name = status.name
    more = f"  ·  {status.queued} more waiting" if status.queued else ""
    if status.state == QUEUED or (status.state == RUNNING and not status.phase):
        return (status.message or f"Waiting to import into {name}") + more
    if status.state == RUNNING:
        return f"Importing into {name}: {status.done} of {status.total}{more}"
    if status.state == CANCELLED:
        return f"Stopped importing into {name}{more}"
    if status.state == DONE:
        return f"{status.message or 'Imported'} into {name}{more}"
    return (status.message or f"Could not import into {name}") + more


def export_status():
    """A JobStatus for exports."""
    return JobStatus(words=describe_export, counted=(EXPORTING,), cancel_tip="Stop exporting")


def describe_export(status):
    """An export's JobStatus in words, for the status bar."""
    name = status.name
    more = f"  ·  {status.queued} more waiting" if status.queued else ""
    if status.state == QUEUED or (status.state == RUNNING and not status.phase):
        return (status.message or f"Waiting to export to {name}") + more
    if status.state == RUNNING:
        return f"Exporting to {name}: {status.done} of {status.total}{more}"
    if status.state == CANCELLED:
        return f"Stopped exporting to {name}{more}"
    if status.state == DONE:
        return f"{status.message or 'Exported'} to {name}{more}"
    return (status.message or f"Could not export to {name}") + more
