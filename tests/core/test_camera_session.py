"""Shooting tethered (#58): every frame the camera tells of is noted, fetched, written
safely into the session's folder and never lost track of, through unplugs and crashes."""

import os
import queue
import threading
import time

import pytest
from fakes import FakeCamera
from photos import make_jpeg_with_exif

from lumina.core import db as dbm
from lumina.core import tether
from lumina.core.db import Library
from lumina.core.errors import PhotoError

TIMEOUT = 10.0
TIMING = {"poll": 0.02, "retry": 0.05, "readings_every": 60.0}


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


@pytest.fixture
def camera(tmp_path):
    root = tmp_path / "camera"
    root.mkdir()
    return root


@pytest.fixture
def where(tmp_path):
    return tmp_path / "Photos" / "2026" / "2026-10-17" / "Studio"


def photo_bytes(tmp_path, name="exif.jpg"):
    path = make_jpeg_with_exif(tmp_path / name)
    return path.read_bytes()


class Running:
    """A session on a thread: its commands and what it tells, read with `until`."""

    def __init__(self, library, session, camera_root):
        self.commands, self.events = queue.SimpleQueue(), queue.SimpleQueue()
        self.told = []
        self.thread = threading.Thread(
            target=tether.serve,
            args=(library.path, session, self.commands, self.events),
            kwargs={"camera": lambda: FakeCamera(camera_root), **TIMING},
        )
        self.thread.start()

    def until(self, kind, timeout=TIMEOUT):
        """The next event of `kind`, the ones before it kept in `told`."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                event = self.events.get(timeout=0.05)
            except queue.Empty:
                continue
            self.told.append(event)
            if event[0] == kind:
                return event
        raise AssertionError(f"no {kind} in {self.told}")

    def stop(self):
        self.commands.put((tether.STOP,))
        self.until(tether.ENDED)
        self.thread.join(TIMEOUT)


@pytest.fixture
def start(library, camera, where):
    running = []

    def start(keep=tether.CARD, session_id=None, folders=None):
        if session_id is None:
            session_id = library.add_tether_session("Studio", "uuid:X", "Photos/Studio", keep)
        folders = {session_id: str(where), **(folders or {})}
        session = tether.Session(session_id, str(where), keep, folders)
        run = Running(library, session, camera)
        running.append(run)
        return run, session_id

    yield start
    for run in running:
        if run.thread.is_alive():
            run.stop()


def test_a_frame_on_the_card_is_saved_by_its_time_and_left_on_the_card(
    library, camera, where, start, tmp_path
):
    run, session = start(tether.CARD)
    run.until(tether.CONNECTED)
    assert FakeCamera.log(camera, "target") == ["Memory card"]

    name = FakeCamera.shoot(camera, photo_bytes(tmp_path))
    _kind, item, told_name = run.until(tether.TAKEN)
    _kind, saved_item, path = run.until(tether.SAVED)

    assert told_name == name == "IMG_6000.CR3" and saved_item == item
    # 2024:06:01 18:30:05 in the photo
    assert os.path.basename(path) == "20240601-183005-IMG_6000.CR3"
    assert os.path.dirname(path) == str(where)
    with open(path, "rb") as file:
        assert file.read() == photo_bytes(tmp_path)
    stored = library.tether_item(item)
    assert (stored.state, stored.name, stored.on_card) == (
        dbm.TETHER_SAVED,
        "20240601-183005-IMG_6000.CR3",
        True,
    )
    assert stored.session_id == session and stored.camera == "FAKE-1"
    # Nothing on the card is touched, and no part is left
    assert (camera / "card" / "DCIM" / "100CANON" / name).exists()
    assert FakeCamera.log(camera, "released") == []
    assert not [n for n in os.listdir(where) if n.startswith(".")]


def test_a_frame_in_the_cameras_memory_is_let_go_of_once_saved(library, camera, start, tmp_path):
    run, _session = start(tether.COMPUTER)
    run.until(tether.CONNECTED)
    assert FakeCamera.log(camera, "target") == ["Internal RAM"]

    name = FakeCamera.shoot(camera, photo_bytes(tmp_path))
    _kind, item, path = run.until(tether.SAVED)

    assert name == "capt0000.cr3"
    assert os.path.basename(path) == "20240601-183005-capt0000.cr3"
    assert library.tether_item(item).on_card is False
    deadline = time.monotonic() + TIMEOUT
    while FakeCamera.log(camera, "released") != [name] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert FakeCamera.log(camera, "released") == [name]


def test_a_burst_is_saved_frame_by_frame_and_never_over_a_file(camera, where, start, tmp_path):
    where.mkdir(parents=True)
    # A file there already, with the name the first frame would take
    (where / "20240601-183005-IMG_6000.CR3").write_bytes(b"someone else's")
    run, _session = start(tether.CARD)
    run.until(tether.CONNECTED)

    data = photo_bytes(tmp_path)
    for _ in range(3):
        FakeCamera.shoot(camera, data)
    paths = [run.until(tether.SAVED)[2] for _ in range(3)]

    assert [os.path.basename(p) for p in paths] == [
        "20240601-183005-IMG_6000-2.CR3",
        "20240601-183005-IMG_6001.CR3",
        "20240601-183005-IMG_6002.CR3",
    ]
    assert (where / "20240601-183005-IMG_6000.CR3").read_bytes() == b"someone else's"


def test_a_frame_without_a_time_in_it_is_named_by_the_time_now(camera, start):
    run, _session = start()
    run.until(tether.CONNECTED)

    FakeCamera.shoot(camera, b"no time in these bytes")
    path = run.until(tether.SAVED)[2]

    assert os.path.basename(path).endswith("-IMG_6000.CR3")
    assert os.path.basename(path)[:8] == time.strftime("%Y%m%d")


def test_a_frame_that_cannot_be_fetched_is_named_and_fetched_again_from_the_card(
    library, camera, start, tmp_path
):
    run, session = start(tether.CARD)
    run.until(tether.CONNECTED)
    FakeCamera.fail_fetching(camera)

    FakeCamera.shoot(camera, photo_bytes(tmp_path))
    _kind, item, name, why = run.until(tether.FAILED)

    assert name == "IMG_6000.CR3" and "I/O problem" in why
    assert library.tether_items(session, dbm.TETHER_FAILED)[0].id == item

    FakeCamera.fail_fetching(camera, False)
    run.commands.put((tether.FETCH, [item]))
    _kind, again, path = run.until(tether.SAVED)

    assert again == item and os.path.exists(path)
    assert library.tether_item(item).state == dbm.TETHER_SAVED


def test_unplugged_while_fetching_the_frame_is_fetched_when_the_camera_is_back(
    library, camera, start, tmp_path
):
    run, _session = start(tether.CARD)
    run.until(tether.CONNECTED)
    FakeCamera.unplug_while_fetching(camera)

    FakeCamera.shoot(camera, photo_bytes(tmp_path))
    _kind, item, _name = run.until(tether.TAKEN)
    run.until(tether.DISCONNECTED)
    # Still to fetch, not given up
    assert library.tether_item(item).state == dbm.TETHER_PENDING
    run.until(tether.WAITING)

    FakeCamera.unplug(camera, False)
    run.until(tether.CONNECTED)
    _kind, saved, path = run.until(tether.SAVED)

    assert saved == item and os.path.exists(path)


def test_a_frame_only_in_the_cameras_memory_when_unplugged_is_named_as_lost(
    library, camera, start, tmp_path
):
    run, _session = start(tether.COMPUTER)
    run.until(tether.CONNECTED)
    FakeCamera.unplug_while_fetching(camera)

    FakeCamera.shoot(camera, photo_bytes(tmp_path))
    _kind, item, _name = run.until(tether.TAKEN)
    run.until(tether.DISCONNECTED)
    FakeCamera.unplug(camera, False)
    _kind, lost, name, why = run.until(tether.FAILED)

    assert lost == item and name == "capt0000.cr3"
    assert "only in the camera's memory" in why
    assert library.tether_item(item).state == dbm.TETHER_FAILED


def test_after_a_crash_a_frame_still_to_fetch_is_fetched_into_its_own_session(
    library, camera, where, start, tmp_path
):
    # A session the app crashed in: its frame was noted and never fetched
    earlier = library.add_tether_session("Before", "uuid:X", "Photos/Before", tether.CARD)
    data = photo_bytes(tmp_path)
    # Shot onto the card before this session opened the camera: not told again
    (camera / "target").write_text("Memory card\n")
    FakeCamera.shoot(camera, data)
    item = library.add_tether_item(earlier, "FAKE-1", FakeCamera.CARD_FOLDER, "IMG_6000.CR3", True)
    before = tmp_path / "Photos" / "Before"

    run, _session = start(tether.CARD, folders={earlier: str(before)})
    run.until(tether.CONNECTED)
    _kind, saved, path = run.until(tether.SAVED)

    assert saved == item
    assert os.path.dirname(path) == str(before)
    assert library.tether_item(item).state == dbm.TETHER_SAVED


def test_waiting_for_a_camera_until_one_is_plugged_in(camera, start):
    FakeCamera.unplug(camera)
    run, _session = start()

    _kind, why = run.until(tether.WAITING)
    assert why == "Connect the camera by USB and turn it on"
    FakeCamera.unplug(camera, False)

    _kind, info, settings = run.until(tether.CONNECTED)
    assert info.model == "Fake EOS"
    assert settings["iso"].value == "100" and settings["batterylevel"].readonly
    # Told once, not every time it looked
    assert [event[0] for event in run.told].count(tether.WAITING) == 1


def test_a_camera_another_program_has_is_told_with_who_and_waited_for(camera, start):
    FakeCamera.make_busy(camera)
    run, _session = start()

    _kind, why = run.until(tether.BUSY)
    assert "Another program is using the camera" in why
    FakeCamera.make_busy(camera, False)

    run.until(tether.CONNECTED)


def test_settings_are_set_and_a_value_the_camera_has_not_is_refused(camera, start):
    run, _session = start()
    run.until(tether.CONNECTED)

    run.commands.put((tether.SET, "iso", "400"))
    _kind, settings = run.until(tether.SETTINGS_TOLD)
    assert settings["iso"].value == "400"

    run.commands.put((tether.SET, "iso", "3"))
    _kind, why = run.until(tether.ERROR)
    assert "3 is not a value of iso" in why


def test_capture_from_the_app_comes_as_a_frame(camera, start):
    run, _session = start()
    run.until(tether.CONNECTED)

    run.commands.put((tether.CAPTURE, True))
    path = run.until(tether.SAVED)[2]
    run.commands.put((tether.CAPTURE, False))
    run.until(tether.SAVED)

    assert FakeCamera.log(camera, "triggers") == ["focus", "no focus"]
    assert os.path.exists(path)


def test_a_command_with_no_camera_is_told_and_stop_ends_the_session(camera, start):
    FakeCamera.unplug(camera)
    run, _session = start()
    run.until(tether.WAITING)

    run.commands.put((tether.CAPTURE, False))
    _kind, why = run.until(tether.ERROR)
    assert why == "The camera is not connected"
    run.stop()

    assert not run.thread.is_alive()


# Writing


def test_a_copy_that_does_not_read_back_the_same_is_refused_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(tether, "_hash", lambda path: "different")

    with pytest.raises(PhotoError, match="not the same"):
        tether.save(str(tmp_path), 7, "IMG_0001.CR3", b"frame")

    assert os.listdir(tmp_path) == []


def test_saving_again_what_is_there_already_takes_that_file(tmp_path):
    first = tether.save(str(tmp_path), 1, "IMG_0001.CR3", b"frame")

    again = tether.save(str(tmp_path), 1, "IMG_0001.CR3", b"frame")

    assert again == first and sorted(os.listdir(tmp_path)) == [os.path.basename(first)]


# Who has the camera


def proc_tree(root, processes):
    """A /proc of {pid: (name, [what its descriptors are])}."""
    for pid, (name, targets) in processes.items():
        fd = root / str(pid) / "fd"
        fd.mkdir(parents=True)
        (root / str(pid) / "comm").write_text(name + "\n")
        for number, target in enumerate(targets):
            os.symlink(target, fd / str(number))
    return root


def test_who_has_the_camera_is_found_by_its_usb_device(tmp_path):
    proc = proc_tree(
        tmp_path / "proc",
        {
            10: ("kioworker", ["/dev/bus/usb/003/003", "/dev/null"]),
            11: ("bash", ["/dev/pts/1"]),
            12: ("lumina", ["/dev/bus/usb/003/003"]),
        },
    )

    found = tether.holders("usb:003,003", str(proc), own=12)

    assert found == ["kioworker"]
    assert tether.busy_text(found).startswith("KDE has the camera open")
    assert tether.busy_text(["gvfsd-gphoto2"]).startswith("The desktop's file access (GVFS)")
    assert tether.busy_text(["darktable"]) == (
        "darktable is using the camera: close it to shoot with Lumina."
    )
    assert tether.holders("ptpip:10.0.0.2", str(proc)) == []
