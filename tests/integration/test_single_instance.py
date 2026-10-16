"""A second start hands its paths to the first."""

import uuid

from lumina.integration.single_instance import SingleInstance


def name():
    return f"lumina-test-{uuid.uuid4().hex}"


def test_without_a_running_instance_nothing_is_forwarded(qapp):
    assert not SingleInstance(name()).forward(["/tmp/a.jpg"])


def test_the_running_instance_receives_the_paths(qtbot):
    socket = name()
    first = SingleInstance(socket)
    assert first.listen()
    try:
        second = SingleInstance(socket)
        with qtbot.waitSignal(first.opened, timeout=5000) as received:
            assert second.forward(["/tmp/a.jpg", "/tmp/My Photos/ä.cr3"])
        assert received.args == [["/tmp/a.jpg", "/tmp/My Photos/ä.cr3"]]
        with qtbot.waitSignal(first.opened, timeout=5000) as received:
            assert second.forward([])
        assert received.args == [[]]
    finally:
        first.close()


def test_a_stale_socket_is_taken_over(qapp):
    socket = name()
    first = SingleInstance(socket)
    assert first.listen()
    first.close()

    assert SingleInstance(socket).listen()
