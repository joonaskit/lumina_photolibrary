"""Desktop notifications (the freedesktop notifications service), for a job that ended
while the window was not looked at. Knows nothing of jobs: when one is worth a
notification is the UI's (`ui/job_reports.py`).

PySide6 cannot send `Notify`'s unsigned arguments over QtDBus, so a notification is sent
with `gdbus call` (GLib's, there on any desktop that has the service); the reply and the
user's click come back over QtDBus. Without the service, or gdbus, nothing here does
anything, and nothing waits: every failure is only logged.
"""

import json
import logging
import re
import shutil

from PySide6.QtCore import SLOT, QObject, QProcess, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusMessage

from lumina import APP_ID

log = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"
INTERFACE = "org.freedesktop.Notifications"

URGENCY_NORMAL, URGENCY_CRITICAL = 1, 2
# The action the service invokes when the notification itself is clicked
_DEFAULT_ACTION = "default"


def text(value):
    """A string as GVariant's text format has it, which takes JSON's escapes."""
    return json.dumps(value, ensure_ascii=False)


def notify_arguments(summary, body, urgent):
    """`gdbus call`'s arguments for one Notify, each typed in GVariant's text format."""
    hints = (
        f'{{"desktop-entry": <{text(APP_ID)}>, '
        f'"urgency": <byte {URGENCY_CRITICAL if urgent else URGENCY_NORMAL}>}}'
    )
    return [
        "call",
        "--session",
        "--dest",
        BUS_NAME,
        "--object-path",
        OBJECT_PATH,
        "--method",
        f"{INTERFACE}.Notify",
        text("Lumina"),
        "uint32 0",
        text(APP_ID),
        text(summary),
        text(body),
        f"@as [{text(_DEFAULT_ACTION)}, {text('Show')}]",
        f"@a{{sv}} {hints}",
        "int32 -1",
    ]


def notification_id(output):
    """The id in gdbus's printed reply, "(uint32 7,)", or None."""
    found = re.search(r"uint32 (\d+)", output)
    return int(found.group(1)) if found else None


def _gdbus(owner, args, done):
    """Run gdbus without waiting for it; `done(output)`, or `done(None)` when it failed."""
    process = QProcess(owner)

    def finished(code, _status):
        output = bytes(process.readAllStandardOutput()).decode(errors="replace")
        if code != 0:
            error = bytes(process.readAllStandardError()).decode(errors="replace").strip()
            log.debug("could not send a notification: %s", error)
        process.deleteLater()
        done(output if code == 0 else None)

    def failed(error):
        if error == QProcess.ProcessError.FailedToStart:
            log.debug("could not start gdbus for a notification")
            process.deleteLater()
            done(None)

    process.finished.connect(finished)
    process.errorOccurred.connect(failed)
    process.start("gdbus", args)


class Notifier(QObject):
    """Sends notifications; `activated` when the user clicks one of them."""

    activated = Signal()

    def __init__(self, connection=None, run=None, parent=None):
        """`run(owner, args, done)` runs gdbus; the tests replace it."""
        super().__init__(parent)
        self._connection = connection or QDBusConnection.sessionBus()
        self._run = run or (_gdbus if shutil.which("gdbus") else None)
        # The ids of ours the service shows
        self._shown = set()

    def start(self):
        bus = self._connection
        if not bus.isConnected():
            return
        for signal, slot in (("ActionInvoked", "_on_action"), ("NotificationClosed", "_on_closed")):
            bus.connect(
                BUS_NAME, OBJECT_PATH, INTERFACE, signal, self, SLOT(f"{slot}(QDBusMessage)")
            )

    def notify(self, summary, body="", urgent=False):
        if self._run is None or not self._connection.isConnected():
            return
        self._run(self, notify_arguments(summary, body, urgent), self._on_sent)

    def _on_sent(self, output):
        found = notification_id(output) if output is not None else None
        if found is not None:
            self._shown.add(found)

    @Slot(QDBusMessage)
    def _on_action(self, message):
        found, action = message.arguments()
        if found in self._shown and action == _DEFAULT_ACTION:
            self.activated.emit()

    @Slot(QDBusMessage)
    def _on_closed(self, message):
        self._shown.discard(message.arguments()[0])
