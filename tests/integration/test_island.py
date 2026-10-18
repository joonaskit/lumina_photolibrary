"""The island's activities: kept up to date through its limits, refusals and restarts.
The D-Bus side is replaced by a client that records the calls and answers when told."""

from PySide6.QtCore import QObject, Signal

from lumina.integration import island
from lumina.integration.island import Activities


class FakeClient(QObject):
    available_changed = Signal(bool)
    action_invoked = Signal(str, str)
    dismissed = Signal(str)

    def __init__(self):
        super().__init__()
        self.available = True
        # (method, id, fields or None, reply callback), in the order sent
        self.calls = []
        self.waited = []

    def show(self, activity_id, fields, done=None):
        self.calls.append(("Show", activity_id, dict(fields), done))

    def update(self, activity_id, fields, done=None):
        self.calls.append(("Update", activity_id, dict(fields), done))

    def dismiss(self, activity_id, done=None):
        self.calls.append(("Dismiss", activity_id, None, done))

    def update_now(self, activity_id, fields):
        self.waited.append((activity_id, dict(fields)))

    def reply(self, code=None):
        """Answer the oldest call not answered yet."""
        for index, call in enumerate(self.calls):
            if call[3] is not None:
                self.calls[index] = (*call[:3], None)
                call[3](code)
                return call
        raise AssertionError("nothing to answer")

    def sent(self):
        return [call[:3] for call in self.calls]

    def restart(self):
        self.available_changed.emit(False)
        self.available_changed.emit(True)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def running(done, **more):
    return {"state": "running", "title": "Scanning Holiday", "progress": done / 10, **more}


def made(clock=None):
    client = FakeClient()
    return client, Activities(client, clock=clock or Clock())


def test_an_activity_is_shown_then_updated_with_what_changed(qapp):
    clock = Clock()
    client, activities = made(clock)

    activities.set("scan-1", running(1))
    client.reply()
    clock.now += 1
    activities.set("scan-1", running(1, text="1 of 10"))

    assert client.sent() == [
        ("Show", "scan-1", running(1)),
        ("Update", "scan-1", {"text": "1 of 10"}),
    ]


def test_what_changes_while_the_show_is_on_its_way_follows_its_reply(qapp):
    client, activities = made()

    activities.set("scan-1", running(1))
    activities.set("scan-1", running(2))
    assert len(client.calls) == 1
    client.reply()

    assert client.sent()[1] == ("Update", "scan-1", {"progress": 0.2})


def test_progress_is_held_back_to_a_few_updates_a_second_but_an_end_is_not(qtbot):
    clock = Clock()
    client, activities = made(clock)
    activities.set("scan-1", running(1))
    client.reply()

    clock.now += 0.1
    activities.set("scan-1", running(2))
    activities.set("scan-1", running(3))
    assert len(client.calls) == 1
    # The newest of them, once the interval is over
    clock.now += 0.2
    qtbot.waitUntil(lambda: len(client.calls) == 2)
    assert client.sent()[1] == ("Update", "scan-1", {"progress": 0.3})
    client.reply()

    clock.now += 0.01
    activities.set("scan-1", {**running(3), "state": "done"})
    assert client.sent()[2] == ("Update", "scan-1", {"state": "done"})


def test_one_dismissed_in_the_island_is_left_alone(qapp):
    client, activities = made()
    activities.set("scan-1", running(1))
    client.reply()

    client.dismissed.emit("scan-1")
    activities.set("scan-1", running(5))
    activities.set("scan-1", {**running(5), "state": "done"})

    assert len(client.calls) == 1


def test_one_gone_from_the_island_is_not_updated_any_more(qapp):
    clock = Clock()
    client, activities = made(clock)
    activities.set("scan-1", running(1))
    client.reply()
    clock.now += 1
    activities.set("scan-1", running(2))

    client.reply("NotFound")
    clock.now += 1
    activities.set("scan-1", running(3))

    assert len(client.calls) == 2


def test_too_many_makes_room_by_dismissing_our_oldest_failure(qapp):
    client, activities = made()
    activities.set("scan-1", {"state": "failed", "title": "Could not scan", "error": "x"})
    client.reply()

    activities.set("scan-2", running(1))
    client.reply("LimitExceeded")

    assert client.sent()[2:] == [
        ("Dismiss", "scan-1", None),
        ("Show", "scan-2", running(1)),
    ]


def test_a_restarted_island_is_shown_what_runs_and_not_what_ended(qapp):
    client, activities = made()
    activities.set("scan-1", running(1))
    client.reply()
    activities.set("scan-2", {"state": "failed", "title": "Could not scan", "error": "x"})
    client.reply()
    client.calls.clear()

    client.restart()

    assert client.sent() == [("Show", "scan-1", running(1))]


def test_nothing_is_kept_of_an_end_while_there_is_no_island(qapp):
    client, activities = made()
    client.available = False

    activities.set("scan-1", {"state": "failed", "title": "Could not scan", "error": "x"})
    client.available = True
    client.available_changed.emit(True)

    assert client.calls == []


def test_only_our_activities_actions_are_passed_on(qtbot):
    client, activities = made()
    activities.set("scan-1", running(1))
    client.reply()
    told = []
    activities.action_invoked.connect(lambda *args: told.append(args))

    # Another app's activity with an id that is not ours
    client.action_invoked.emit("copy-1", "cancel")
    client.action_invoked.emit("scan-1", "cancel")

    assert told == [("scan-1", "cancel")]


def test_a_failure_no_longer_wanted_is_dismissed(qapp):
    client, activities = made()
    activities.set("scan-1", {"state": "failed", "title": "Could not scan", "error": "x"})
    client.reply()

    activities.drop("scan-1")

    assert client.sent()[-1] == ("Dismiss", "scan-1", None)


def test_at_the_exit_what_runs_is_told_and_waited_for(qapp):
    client, activities = made()
    activities.set("scan-1", running(1))
    client.reply()
    activities.set("scan-2", {**running(1), "state": "done"})

    activities.close({"state": "cancelled", "text": "Lumina quit"})

    assert client.waited == [("scan-1", {"state": "cancelled", "text": "Lumina quit"})]


def test_the_actions_go_as_ids_and_labels_and_errors_by_code():
    assert island.arguments({"title": "t", "actions": [("cancel", "Cancel")]}) == {
        "title": "t",
        "actions": [{"id": "cancel", "label": "Cancel"}],
    }
    assert island.error_code("org.quickshell.IslandActivities.Error.RateLimited") == "RateLimited"
    assert island.error_code("org.freedesktop.DBus.Error.NoReply") == "Failed"
