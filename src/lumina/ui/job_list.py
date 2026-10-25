"""Every job that runs or waits, in one place (#132): a button in the status bar that says
how many, and the list it opens, with each job's progress and Cancel.

The list is fed by `JobReports.tell`, the same JobStatus the island and the notifications
get, so a service is wired once. A job joins it when it starts; one that ends cleanly or is
cancelled leaves it; one that ends with failures stays, naming its files, until it is
dismissed (the list is not kept across a restart: the failures stay in the library's jobs).
"""

from dataclasses import dataclass

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumina.core.dbtypes import CANCELLED, FAILED_JOB, QUEUED, UNFINISHED
from lumina.integration import style

# The files of a failed job the list names
NAMED = 3


@dataclass
class Entry:
    key: str
    # The job_reports.Source of its service: its words, Cancel and whether it is alone
    source: object
    status: object

    def waiting(self):
        """Not started: its drive or card is not there."""
        return self.status.state == QUEUED and bool(self.status.message)

    def ended(self):
        return self.status.state not in UNFINISHED


class JobList(QObject):
    # An entry came, changed or went
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = {}

    def update(self, key, source, status):
        """A job's JobStatus: it runs, waits or has ended."""
        keep = status.state in UNFINISHED or (
            status.state != CANCELLED and (status.state == FAILED_JOB or bool(status.failures))
        )
        if keep:
            self._entries[key] = Entry(key, source, status)
        elif self._entries.pop(key, None) is None:
            return
        self.changed.emit()

    def entries(self):
        """Running jobs first, then those waiting, then those that ended with failures; each
        in the order they came."""

        def rank(entry):
            return 2 if entry.ended() else 1 if entry.waiting() else 0

        return sorted(self._entries.values(), key=rank)

    def active(self):
        return [entry for entry in self._entries.values() if not entry.ended()]

    def failed(self):
        return [entry for entry in self._entries.values() if entry.ended()]

    def cancel(self, key):
        """Stop the job's service's running job and drop those waiting behind it."""
        entry = self._entries.get(key)
        if entry is not None and not entry.ended():
            entry.source.cancel()

    def dismiss(self, key):
        entry = self._entries.get(key)
        if entry is not None and entry.ended():
            del self._entries[key]
            self.changed.emit()

    def clear(self):
        """Dismiss every job that ended."""
        gone = [key for key, entry in self._entries.items() if entry.ended()]
        for key in gone:
            del self._entries[key]
        if gone:
            self.changed.emit()


def summary(jobs):
    """What the status bar's button says: how many jobs run, or the one that has no status
    bar widget of its own, and how many failed; "" when there is nothing to say."""
    active, failed = jobs.active(), jobs.failed()
    parts = []
    if len(active) > 1:
        parts.append(f"{len(active)} jobs")
    elif active and active[0].source.alone:
        parts.append(active[0].source.heading(active[0].status) + "…")
    if failed:
        parts.append(f"{len(failed)} failed")
    return "  ·  ".join(parts)


class JobsButton(QToolButton):
    """In the status bar: shown when there is something to say, opens the list."""

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.panel = JobsPanel(jobs, self)
        self.setAutoRaise(True)
        self.setToolTip("Show the jobs that run or wait")
        self.clicked.connect(self.panel.open)
        jobs.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        text = summary(self.jobs)
        self.setText(text)
        self.setVisible(bool(text))


class JobsPanel(QFrame):
    """The list: a popup over the button, with a row for each job."""

    def __init__(self, jobs, button):
        super().__init__(button, Qt.WindowType.Popup)
        self.jobs = jobs
        self.button = button
        self._rows = {}
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        self.rows = QVBoxLayout()
        self.empty = QLabel("No jobs running")
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Dismiss the jobs that ended with failures")
        self.clear_button.clicked.connect(jobs.clear)
        layout.addLayout(self.rows)
        layout.addWidget(self.empty)
        layout.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignRight)
        style.follow_density(
            self,
            lambda: (
                layout.setContentsMargins(*(style.gap(12),) * 4),
                layout.setSpacing(style.gap(8)),
                self.rows.setSpacing(style.gap(12)),
                self.setMinimumWidth(style.px(420)),
            ),
        )
        jobs.changed.connect(self.refresh)
        self.refresh()

    def open(self):
        self.refresh()
        self.adjustSize()
        corner = self.button.mapToGlobal(QPoint(self.button.width(), 0))
        self.move(max(0, corner.x() - self.width()), max(0, corner.y() - self.height()))
        self.show()

    def refresh(self):
        entries = self.jobs.entries()
        keys = {entry.key for entry in entries}
        for key in [key for key in self._rows if key not in keys]:
            self._rows.pop(key).deleteLater()
        for place, entry in enumerate(entries):
            row = self._rows.get(entry.key)
            if row is None:
                row = self._rows[entry.key] = _Row(entry.key, self.jobs)
            row.show_entry(entry)
            # Moves a row already there
            self.rows.insertWidget(place, row)
        self.empty.setVisible(not entries)
        self.clear_button.setVisible(bool(self.jobs.failed()))
        if self.isVisible():
            self.adjustSize()


class _Row(QWidget):
    def __init__(self, key, jobs):
        super().__init__()
        self.key = key
        self.jobs = jobs
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.action = QPushButton()
        top.addWidget(self.title, 1)
        top.addWidget(self.action)
        self.line = QLabel()
        self.line.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.files = QLabel()
        self.files.setWordWrap(True)
        for widget in (self.line, self.bar, self.files):
            layout.addWidget(widget)
        layout.insertLayout(0, top)
        self.action.clicked.connect(self._act)
        style.follow_density(self, lambda: (layout.setSpacing(style.gap(4)),))
        self.bar.setFixedHeight(style.px(6))
        self._ended = False

    def _act(self):
        if self._ended:
            self.jobs.dismiss(self.key)
        else:
            self.jobs.cancel(self.key)

    def show_entry(self, entry):
        status, source = entry.status, entry.source
        self._ended = entry.ended()
        if self._ended:
            self.title.setText(source.words(status))
            self.line.setVisible(False)
            self.bar.setVisible(False)
            names = [f"{path}: {message}" for path, message in status.failures[:NAMED]]
            if len(status.failures) > NAMED:
                names.append(f"and {len(status.failures) - NAMED} more")
            text = status.message if status.state == FAILED_JOB and status.message else ""
            self.files.setText("\n".join(([text] if text else []) + names))
            self.files.setVisible(bool(self.files.text()))
            colour = style.color("danger").name()
            self.title.setStyleSheet(f"color: {colour};")
            self.action.setText("Dismiss")
            self.action.setToolTip("Remove it from the list")
            return
        self.title.setStyleSheet("")
        self.title.setText(source.heading(status))
        self.files.setVisible(False)
        self.line.setVisible(True)
        if entry.waiting():
            self.line.setText(status.message)
            self.bar.setVisible(False)
        else:
            self.line.setText(source.detail(status) or source.words(status))
            self.bar.setVisible(True)
            if status.total:
                self.bar.setRange(0, status.total)
                self.bar.setValue(min(status.done, status.total))
            else:
                self.bar.setRange(0, 0)
        self.action.setText("Cancel")
        self.action.setToolTip(
            f"Stops this and the {status.queued} waiting" if status.queued else "Stop it"
        )
