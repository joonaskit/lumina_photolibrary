"""Desktop notifications: what gdbus is asked to send, and the click on one of ours."""

from PySide6.QtDBus import QDBusMessage

from lumina.integration import notifications
from lumina.integration.notifications import Notifier


class FakeConnection:
    def isConnected(self):
        return True

    def connect(self, *_args):
        return True


def signal(*args):
    message = QDBusMessage.createSignal("/org/freedesktop/Notifications", "x", "y")
    message.setArguments(list(args))
    return message


def test_the_arguments_are_typed_and_the_text_quoted():
    args = notifications.notify_arguments('Scanned "Holiday" \\ ä', "a.cr3: bad\nb.cr3", True)

    assert args[:8] == [
        "call",
        "--session",
        "--dest",
        "org.freedesktop.Notifications",
        "--object-path",
        "/org/freedesktop/Notifications",
        "--method",
        "org.freedesktop.Notifications.Notify",
    ]
    app, replaces, icon, summary, body, actions, hints, timeout = args[8:]
    assert (app, replaces, icon, timeout) == ('"Lumina"', "uint32 0", '"lumina"', "int32 -1")
    assert summary == '"Scanned \\"Holiday\\" \\\\ ä"'
    assert body == '"a.cr3: bad\\nb.cr3"'
    assert actions == '@as ["default", "Show"]'
    assert hints == '@a{sv} {"desktop-entry": <"lumina">, "urgency": <byte 2>}'
    assert "<byte 1>" in notifications.notify_arguments("Scanned", "", False)[14]


def test_the_id_is_read_from_the_reply():
    assert notifications.notification_id("(uint32 17,)\n") == 17
    assert notifications.notification_id("") is None


def test_a_click_on_one_of_ours_brings_the_window(qtbot):
    sent = []
    notifier = Notifier(FakeConnection(), run=lambda _owner, args, done: sent.append(done))
    notifier.start()
    notifier.notify("Scanned Holiday")
    [done] = sent
    done("(uint32 7,)\n")
    told = []
    notifier.activated.connect(lambda: told.append(True))

    # Another app's notification, and a button that is not the click
    notifier._on_action(signal(8, "default"))
    notifier._on_action(signal(7, "other"))
    assert told == []
    notifier._on_action(signal(7, "default"))
    assert told == [True]
    # Closed: its id may be another app's next
    notifier._on_closed(signal(7, 2))
    notifier._on_action(signal(7, "default"))
    assert told == [True]


def test_without_gdbus_nothing_is_sent(qapp, monkeypatch):
    monkeypatch.setattr(notifications.shutil, "which", lambda _name: None)
    notifier = Notifier(FakeConnection())

    notifier.notify("Scanned Holiday")
    notifier._on_sent(None)

    assert notifier._shown == set()
