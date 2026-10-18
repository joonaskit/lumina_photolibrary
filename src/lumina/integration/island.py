"""Showing what Lumina does in the Quickshell Island, through its activities API
(`docs/ACTIVITIES_API.md` in the shell's repo), as filehouse shows its file operations.

Two parts. `IslandClient` talks D-Bus and knows nothing of jobs. `Activities` keeps the
island showing what it is told, at most a few times a second, and puts it back when the
island restarts. What a job looks like there is the UI's (`ui/job_reports.py`).

Without an island nothing here does anything, and nothing here can stop a job: every
call is asynchronous and every failure is only logged.
"""

import logging
import time
from enum import Enum

from PySide6.QtCore import SLOT, QObject, QTimer, Signal, Slot
from PySide6.QtDBus import (
    QDBus,
    QDBusConnection,
    QDBusMessage,
    QDBusPendingCallWatcher,
    QDBusServiceWatcher,
)

log = logging.getLogger(__name__)

BUS_NAME = "org.quickshell.IslandActivities"
OBJECT_PATH = "/org/quickshell/IslandActivities"
INTERFACE = "org.quickshell.IslandActivities1"
# The only version of the API this client knows
VERSION = 1
_ERROR_PREFIX = f"{BUS_NAME}.Error."

_CALL_TIMEOUT_MS = 2000
# At the app's exit, how long each last word may take
_CLOSE_TIMEOUT_MS = 500
# The API asks for a few progress updates a second at most
_UPDATE_INTERVAL_MS = 250
_RETRY_MS = 1000
_MAX_RETRIES = 5

RUNNING, DONE, CANCELLED, FAILED = "running", "done", "cancelled", "failed"


def error_code(name):
    """The API's code (`NotFound`, `RateLimited`) for a D-Bus error name, or "Failed"."""
    return name.removeprefix(_ERROR_PREFIX) if name.startswith(_ERROR_PREFIX) else "Failed"


def arguments(fields):
    """The fields as QtDBus sends them. PySide6 cannot send `a(ss)` or `aa{ss}`, so the
    actions go as an array of variants each holding `a{sv}` with `id` and `label`;
    the island's bridge unwraps the variants into the same list."""
    fields = dict(fields)
    if "actions" in fields:
        fields["actions"] = [{"id": key, "label": label} for key, label in fields["actions"]]
    return fields


class IslandClient(QObject):
    """The activities API over D-Bus, on the main thread. `available` is true while an
    island with the right API version is on the bus."""

    available_changed = Signal(bool)
    # (activity id, action): sent to the whole bus, so most are not ours
    action_invoked = Signal(str, str)
    dismissed = Signal(str)

    def __init__(self, connection=None, parent=None):
        super().__init__(parent)
        self._connection = connection or QDBusConnection.sessionBus()
        self._owner = None
        self._watcher = None
        self.available = False

    def start(self):
        bus = self._connection
        if not bus.isConnected():
            log.debug("no session bus, so no island")
            return
        for signal, slot in (("ActionInvoked", "_on_action"), ("Dismissed", "_on_dismissed")):
            bus.connect(
                BUS_NAME, OBJECT_PATH, INTERFACE, signal, self, SLOT(f"{slot}(QDBusMessage)")
            )
        self._watcher = QDBusServiceWatcher(
            BUS_NAME, bus, QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange, self
        )
        self._watcher.serviceOwnerChanged.connect(self._on_owner_changed)
        # Whether it is there already
        message = QDBusMessage.createMethodCall(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner"
        )
        message.setArguments([BUS_NAME])
        self._send(message, self._on_owner)

    def show(self, activity_id, fields, done=None):
        self._call("Show", [activity_id, arguments(fields)], done)

    def update(self, activity_id, fields, done=None):
        self._call("Update", [activity_id, arguments(fields)], done)

    def dismiss(self, activity_id, done=None):
        self._call("Dismiss", [activity_id], done)

    def update_now(self, activity_id, fields):
        """Update, waiting for the reply: for the app's exit, which would not wait."""
        if self.available:
            message = self._message("Update", [activity_id, arguments(fields)])
            self._connection.call(message, QDBus.CallMode.Block, _CLOSE_TIMEOUT_MS)

    def _on_owner(self, reply):
        if reply.type() == QDBusMessage.MessageType.ReplyMessage:
            self._on_owner_changed(BUS_NAME, "", reply.arguments()[0])

    def _on_owner_changed(self, _name, _old, owner):
        # A restarted island is a new owner, and has forgotten everything
        self._set_available(False)
        self._owner = owner or None
        if not owner:
            return
        message = QDBusMessage.createMethodCall(
            BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get"
        )
        message.setArguments([INTERFACE, "Version"])
        self._send(message, lambda reply: self._on_version(reply, owner))

    def _on_version(self, reply, owner):
        if owner != self._owner:
            return
        if reply.type() != QDBusMessage.MessageType.ReplyMessage:
            log.debug("could not read the island's API version: %s", reply.errorMessage())
            return
        version = reply.arguments()[0].variant()
        if version != VERSION:
            # Not an error: this client just stays silent
            log.debug("island speaks API version %s, this client version %s", version, VERSION)
            return
        self._set_available(True)

    def _set_available(self, available):
        if available != self.available:
            self.available = available
            self.available_changed.emit(available)

    @Slot(QDBusMessage)
    def _on_action(self, message):
        activity_id, action = message.arguments()
        self.action_invoked.emit(activity_id, action)

    @Slot(QDBusMessage)
    def _on_dismissed(self, message):
        self.dismissed.emit(message.arguments()[0])

    def _message(self, method, args):
        message = QDBusMessage.createMethodCall(BUS_NAME, OBJECT_PATH, INTERFACE, method)
        message.setArguments(args)
        return message

    def _call(self, method, args, done):
        if not self.available:
            return

        def replied(reply):
            code = None
            if reply.type() != QDBusMessage.MessageType.ReplyMessage:
                code = error_code(reply.errorName())
                log.debug("island refused %s for %s: %s", method, args[0], reply.errorMessage())
            if done is not None:
                done(code)

        self._send(self._message(method, args), replied)

    def _send(self, message, replied):
        watcher = QDBusPendingCallWatcher(
            self._connection.asyncCall(message, _CALL_TIMEOUT_MS), self
        )

        def finished(call):
            call.deleteLater()
            replied(call.reply())

        watcher.finished.connect(finished)


class _Phase(Enum):
    UNSENT = "unsent"  # the island doesn't have it (yet, or any more)
    SHOWING = "showing"  # Show was sent and its reply hasn't come
    SHOWN = "shown"
    GONE = "gone"  # dismissed or timed out in the island: leave it alone


class _Activity:
    def __init__(self, activity_id, fields):
        self.id = activity_id
        # What it should show now: every field, `state` among them
        self.fields = fields
        self.phase = _Phase.UNSENT
        self.sent = {}  # the fields the island has, as far as we know
        self.last_sent = 0.0
        self.timer = None
        self.retries = 0
        self.made_room = False

    @property
    def final(self):
        return self.fields.get("state", RUNNING) != RUNNING


class Activities(QObject):
    """The activities the island should show, kept there through its limits and restarts.
    Told every change with `set`; the island has them a few times a second at most, and
    an end at once."""

    # (activity id, action) for one of ours
    action_invoked = Signal(str, str)

    def __init__(self, client, parent=None, interval_ms=_UPDATE_INTERVAL_MS, clock=time.monotonic):
        super().__init__(parent)
        self._client = client
        self._interval = interval_ms / 1000
        self._clock = clock
        self._activities = {}  # id -> _Activity, in the order first set
        client.available_changed.connect(self._on_available_changed)
        client.action_invoked.connect(self._on_action)
        client.dismissed.connect(self._on_dismissed)

    @property
    def available(self):
        return self._client.available

    def set(self, activity_id, fields):
        """What `activity_id` shows now: all its fields. One the user dismissed is left
        dismissed."""
        activity = self._activities.get(activity_id)
        if activity is None:
            activity = self._activities[activity_id] = _Activity(activity_id, fields)
        elif activity.phase is _Phase.GONE:
            if fields.get("state", RUNNING) != RUNNING:
                # Its end, which the user did not wait for
                del self._activities[activity_id]
            return
        activity.fields = fields
        if activity.final and not self._client.available:
            # Not worth showing late, once there is an island
            del self._activities[activity_id]
            return
        self._publish(activity)

    def drop(self, activity_id):
        """Not shown any more: a failure still in the island is dismissed."""
        activity = self._activities.pop(activity_id, None)
        if activity is None:
            return
        self._stop_timer(activity)
        if activity.phase in (_Phase.SHOWING, _Phase.SHOWN) and activity.final:
            self._client.dismiss(activity_id)

    def close(self, fields):
        """For the app's exit: what is running in the island is told `fields` (a
        cancelled state, say), and waited for, since the process ends next."""
        for activity in self._activities.values():
            self._stop_timer(activity)
            if activity.phase is _Phase.SHOWN and not activity.final:
                self._client.update_now(activity.id, fields)
        self._activities.clear()

    # Publishing

    def _publish(self, activity, now=False):
        """Bring the island up to date, at most a few times a second."""
        if not self._client.available or activity.phase in (_Phase.SHOWING, _Phase.GONE):
            # While a Show is on its way, its reply publishes what changed since
            return
        if activity.phase is _Phase.SHOWN and not activity.final and not now:
            # An end is never held back; progress is
            wait = self._interval - (self._clock() - activity.last_sent)
            if wait > 0:
                self._later(activity, wait * 1000)
                return
        self._stop_timer(activity)
        fields = activity.fields
        activity.last_sent = self._clock()
        if activity.phase is _Phase.UNSENT:
            activity.phase = _Phase.SHOWING
            activity.sent = dict(fields)
            self._client.show(activity.id, fields, lambda code: self._on_shown(activity, code))
            return
        sent = activity.sent
        changed = {name: value for name, value in fields.items() if sent.get(name) != value}
        if changed:
            activity.sent.update(changed)
            self._client.update(
                activity.id, changed, lambda code: self._on_updated(activity, changed, code)
            )
        else:
            self._settled(activity)

    def _on_shown(self, activity, code):
        if activity.phase is not _Phase.SHOWING:
            return
        if code is None:
            activity.phase = _Phase.SHOWN
            activity.retries = 0
            # Whatever changed while the Show was on its way
            self._publish(activity, now=True)
            return
        activity.phase = _Phase.UNSENT
        activity.sent = {}
        if code == "LimitExceeded" and not activity.made_room:
            # Too many activities, most likely old failures still showing: drop the
            # oldest of ours from the island and try once more
            activity.made_room = True
            if self._dismiss_oldest_failure():
                self._publish(activity, now=True)
                return
        self._retry(activity)

    def _on_updated(self, activity, changed, code):
        if code is None:
            activity.retries = 0
            self._settled(activity)
            return
        # The island doesn't have these values after all
        for name in changed:
            activity.sent.pop(name, None)
        if code in ("NotFound", "InvalidState"):
            # Dismissed or timed out over there: stop updating it
            activity.phase = _Phase.GONE
            self._stop_timer(activity)
        elif activity.phase is _Phase.SHOWN:
            # RateLimited and the like: the next update carries newer values anyway
            self._retry(activity)

    def _settled(self, activity):
        """The island has all of it. A done or cancelled one leaves the island by
        itself, so there is nothing more to keep; a failure stays until dismissed."""
        if activity.final and activity.fields.get("state") != FAILED:
            if self._activities.get(activity.id) is activity:
                del self._activities[activity.id]

    def _retry(self, activity):
        """Try again in a while. An end must get through; progress need not."""
        if activity.retries >= _MAX_RETRIES:
            return
        activity.retries += 1
        self._later(activity, _RETRY_MS, replace=True)

    def _dismiss_oldest_failure(self):
        for activity in self._activities.values():
            if activity.fields.get("state") == FAILED and activity.phase is _Phase.SHOWN:
                activity.phase = _Phase.GONE
                self._client.dismiss(activity.id)
                del self._activities[activity.id]
                return True
        return False

    def _later(self, activity, msecs, replace=False):
        if activity.timer is not None:
            if not replace:
                return
            self._stop_timer(activity)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._on_timer(activity))
        activity.timer = timer
        timer.start(max(1, round(msecs)))

    def _on_timer(self, activity):
        self._stop_timer(activity)
        if self._activities.get(activity.id) is activity:
            self._publish(activity, now=True)

    @staticmethod
    def _stop_timer(activity):
        if activity.timer is not None:
            activity.timer.stop()
            activity.timer.deleteLater()
            activity.timer = None

    # The island

    def _on_available_changed(self, available):
        if not available:
            # Gone, or restarted: it knows nothing of ours
            for activity in self._activities.values():
                self._stop_timer(activity)
                activity.phase = _Phase.UNSENT
                activity.sent = {}
                activity.retries = 0
                activity.made_room = False
            return
        # What was over while it was away is not worth showing late
        for activity in list(self._activities.values()):
            if activity.final:
                del self._activities[activity.id]
            else:
                self._publish(activity, now=True)

    def _on_action(self, activity_id, action):
        # Signals go to the whole bus and ids are only unique per sender
        if activity_id in self._activities:
            self.action_invoked.emit(activity_id, action)

    def _on_dismissed(self, activity_id):
        activity = self._activities.get(activity_id)
        if activity is None:
            return
        self._stop_timer(activity)
        if activity.final:
            del self._activities[activity_id]
        else:
            activity.phase = _Phase.GONE
