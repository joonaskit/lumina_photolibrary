"""Shooting tethered with a camera on USB (#58), without Qt: a session that downloads
each frame the camera reports into the session's folder, run in a process of its own
(qt_models/tether.py) and spoken to through two queues.

What the author's EOS 90D does (docs/history.md has the rest): a frame from its own button or
from the app comes as a file event, from the card's folder ("IMG_6353.CR3") when the
capture target is the memory card, or as "capt0000.cr3" in the camera's memory, never
written to the card, when it is the camera's memory. So:

- **Noted before fetched**: each frame the camera reports gets a row (`tether_items`)
  before it is downloaded, so a crash or an unplug in the middle never loses track of
  it. At the next connection a frame still to fetch is fetched again from the card; one
  that was only in the camera's memory is named as lost. That is why keeping frames on
  the card as well is the default.
- **Written as an import is**: to a hidden part beside where it goes, synced, read back
  and compared, then put in place under the first free name by an exclusive create
  (`derived.place`), never over a file. The name is the time in the photo and the
  camera's name (20261017-110312-IMG_6353.CR3, as an import's dated names): the
  camera's own names start again every session and wrap. Only once the row says where
  it went is a frame in the camera's memory let go of; nothing on the card is touched.
- **One owner**: only one program can have the camera. A camera another program has is
  told with that program's name and how to let it go, found in /proc; nothing is
  stopped. An unplugged camera is waited for, and a session goes on when it is back.

Live view (#59) is the session's too, as only one process can have the camera: while
it is on, the session asks the camera for a frame between its events and its commands,
and puts each on a queue of its own. Not more than two are on their way at a time: a
frame is asked for only once the app has said it has the one before the last
(`SHOWN`), so frames the app had no time for are never taken, nothing queues up, and
the newest frame is always the one shown.

`serve()` is the session; `GPhotoCamera` the camera through python-gphoto2 (libgphoto2),
which is imported only when a camera is opened, so the rest of the app runs without it.
"""

import hashlib
import os
import queue
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime

from lumina.core import capture_time, derived, live
from lumina.core import db as dbm
from lumina.core.errors import PhotoError

# Where the camera keeps a frame: on its card and on the computer, or on the computer only
CARD, COMPUTER = "card", "computer"
# The settings shown and set, by libgphoto2's names, in the order shown; a camera without
# one leaves it out
SETTINGS = ("iso", "shutterspeed", "aperture", "whitebalance", "imageformat", "drivemode")
# What the camera tells and is not set
READINGS = ("batterylevel", "availableshots")
# The capture target for each, by libgphoto2's choices
_TARGETS = {CARD: "Memory card", COMPUTER: "Internal RAM"}

# The commands a session takes, as tuples: (STOP,), (CAPTURE, focus first), (SET, name,
# value), (FETCH, [item ids]) to fetch failed frames from the card again
STOP, CAPTURE, SET, FETCH = "stop", "capture", "set", "fetch"
# And for live view: (LIVE, a number for this start of it, or None to stop it), (SHOWN,
# the number of the newest frame the app has), (ZOOM, times), (POINT, x, y, focus there)
# with x and y as shares of the whole frame; (FOCUS,) to focus without taking a picture
LIVE, SHOWN, ZOOM, POINT, FOCUS = "live", "shown", "zoom", "point", "focus"
# What it tells, as tuples:
#   (WAITING, why): no camera, or one that can't be used yet (BUSY: another program has it)
#   (CONNECTED, CameraInfo, {name: Setting}), (SETTINGS, {name: Setting})
#   (DISCONNECTED, why)
#   (TAKEN, item id, the camera's name for it): reported, being fetched
#   (SAVED, item id, path), (FAILED, item id, name, why)
#   (ERROR, why): a command that could not be done
#   (LIVE_TOLD, LiveState): live view started, stopped (and why) or changed
#   (ENDED,)
# and live view's frames, LiveFrame, on a queue of their own
WAITING, BUSY, CONNECTED, SETTINGS_TOLD, DISCONNECTED = (
    "waiting",
    "busy",
    "connected",
    "settings",
    "disconnected",
)
TAKEN, SAVED, FAILED, ERROR, ENDED = "taken", "saved", "failed", "error", "ended"
LIVE_TOLD = "live view"

# How long one wait for the camera's events is, how often a camera that is not there is
# looked for, and how often the readings (battery, shots left) are read again, in seconds
_POLL = 0.1
_RETRY = 1.0
_READINGS_EVERY = 15.0
_EVENTS_AT_A_TIME = 50
# A camera that says it is busy (writing a burst, a menu or playback open on it, waking)
# is asked again for this long, in seconds, this often
_BUSY_FOR = 3.0
_BUSY_STEP = 0.1
# How long a focus without a picture is held, in seconds: a Canon holds what it focused on
# until let go of, and focuses again only after that
_FOCUS_HELD = 2.0
# Live view: frames on their way at most, and how long frames may fail before it stops
# (a camera too hot stops its live view itself), in seconds
_IN_FLIGHT = 2
_LIVE_FAILS_FOR = 3.0


class CameraError(Exception):
    """A camera that can't be used: `kind` is NOT_FOUND, BUSY (another program has it,
    `port` says which device), GONE (unplugged) or None (it refused)."""

    def __init__(self, message, kind=None, port=None):
        super().__init__(message)
        self.kind = kind
        self.port = port


NOT_FOUND, GONE = "not found", "gone"


@dataclass(frozen=True)
class CameraInfo:
    model: str
    serial: str
    port: str


@dataclass(frozen=True)
class Setting:
    name: str
    label: str
    value: str
    choices: tuple = ()
    readonly: bool = False


@dataclass(frozen=True)
class LiveView:
    """What the camera's live view can do: magnify (the times, 1 among them), move what is
    magnified and the focus point, and focus there."""

    zooms: tuple = (1,)
    point: bool = False
    focus: bool = False
    # Whether live view goes on through a capture, or is stopped for it
    captures: bool = True


@dataclass(frozen=True)
class LiveState:
    """Live view: `start` the number of this start of it (None: off, `why` says why when
    it was not asked to), magnified `zoom` times around `point`."""

    start: int | None
    why: str | None = None
    view: LiveView = field(default_factory=LiveView)
    zoom: int = 1
    point: tuple = (0.5, 0.5)

    @property
    def on(self):
        return self.start is not None


@dataclass(frozen=True)
class LiveFrame:
    """A live view frame: its number (rising), the start of live view it is of, the JPEG,
    its Look, and how it was magnified."""

    number: int
    start: int
    jpeg: bytes
    look: live.Look
    zoom: int = 1
    point: tuple = (0.5, 0.5)


@dataclass(frozen=True)
class Frame:
    """A file the camera has: its folder and name there."""

    folder: str
    name: str

    @property
    def on_card(self):
        # The card's folders are under its storage ("/store_00020001/DCIM/100CANON"); the
        # camera's memory is the root
        return self.folder.strip("/") != ""


@dataclass(frozen=True)
class CameraFile:
    """A file on the camera's storage, for an import: where it is, its size and its time
    there (the camera's clock, as a Unix time)."""

    folder: str
    name: str
    size: int
    mtime: float = 0.0

    @property
    def path(self):
        return f"{self.folder.rstrip('/')}/{self.name}"


@dataclass(frozen=True)
class Session:
    """What a camera session needs: its row's id, where its frames go and where frames are
    kept. `folders` is where each session with frames still to fetch puts them, by id,
    this one's among them."""

    id: int
    where: str
    keep: str = CARD
    folders: dict = field(default_factory=dict)


# The camera through libgphoto2


class GPhotoCamera:
    """The first camera found on USB, through python-gphoto2."""

    def __init__(self):
        self._camera = None
        self.camera_info = None
        # A half press held to focus, and until when
        self._held = False
        self._held_until = 0.0

    def open(self):
        """Raises CameraError: NOT_FOUND with none, BUSY when another program has it."""
        try:
            import gphoto2 as gp
        except ImportError as error:
            raise CameraError(
                "Shooting tethered needs python-gphoto2: pip install 'lumina[tether]'"
            ) from error
        self._gp = gp
        found = gp.Camera.autodetect()
        cameras = [(found.get_name(i), found.get_value(i)) for i in range(len(found))]
        cameras = [(model, port) for model, port in cameras if port.startswith("usb:")]
        if not cameras:
            raise CameraError("No camera is connected", NOT_FOUND)
        model, port = cameras[0]
        camera = gp.Camera()
        ports = gp.PortInfoList()
        ports.load()
        camera.set_port_info(ports[ports.lookup_path(port)])
        try:
            camera.init()
        except gp.GPhoto2Error as error:
            if error.code == gp.GP_ERROR_IO_USB_CLAIM:
                raise CameraError(str(error), BUSY, port) from error
            raise CameraError(f"{model}: {error}") from error
        self._camera = camera
        serial = self._value("serialnumber") or port
        self.camera_info = CameraInfo(model, serial, port)
        return self.camera_info

    def close(self):
        if self._camera is not None:
            # A capture cut short must not leave the button held
            self._end_focus()
            self._let_go(patient=False)
            try:
                self._camera.exit()
            except self._gp.GPhoto2Error:
                pass
            self._camera = None

    def _let_go(self, patient=True):
        """Let go of the shutter button's full and half press (Canon); quietly, as a camera
        whose button is not held may refuse it. With `patient`, while it is still taking
        the picture, it is asked again."""
        if self._value("eosremoterelease") is None:
            return
        for stage in ("Release Full", "Release Half"):
            try:
                self.set("eosremoterelease", stage, patient)
            except CameraError:
                pass

    def settings(self):
        """{name: Setting} of SETTINGS and READINGS the camera has."""
        gp = self._gp
        try:
            config = self._patiently(self._camera.get_config)
        except gp.GPhoto2Error as error:
            raise self._error(error) from error
        found = {}
        for name in (*SETTINGS, *READINGS):
            try:
                widget = config.get_child_by_name(name)
            except gp.GPhoto2Error:
                continue
            choices = ()
            if widget.get_type() in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
                choices = tuple(widget.get_choice(i) for i in range(widget.count_choices()))
            value = widget.get_value()
            found[name] = Setting(
                name,
                widget.get_label(),
                str(value) if value is not None else "",
                choices,
                bool(widget.get_readonly()) or name in READINGS,
            )
        return found

    def set(self, name, value, patient=True):
        """Set `name` to `value`; with `patient`, asked again while the camera is busy."""
        gp = self._gp

        def put():
            config = self._camera.get_config()
            config.get_child_by_name(name).set_value(value)
            self._camera.set_config(config)

        try:
            self._patiently(put) if patient else put()
        except gp.GPhoto2Error as error:
            raise self._error(error) from error

    def wait(self, seconds):
        """The frames the camera told of within `seconds`, at the first event or the end."""
        gp = self._gp
        if self._held and time.monotonic() >= self._held_until:
            self._end_focus()
        frames = []
        timeout = max(1, int(seconds * 1000))
        try:
            # Not more events than this at a time: a camera tells of every change of its
            # settings, and the commands are not to wait for them
            for _event in range(_EVENTS_AT_A_TIME):
                try:
                    kind, data = self._camera.wait_for_event(timeout)
                except gp.GPhoto2Error as error:
                    if error.code != gp.GP_ERROR_CAMERA_BUSY:
                        raise
                    # Busy with something of its own: its events come once it is done
                    time.sleep(seconds)
                    return frames
                if kind == gp.GP_EVENT_TIMEOUT:
                    return frames
                if kind == gp.GP_EVENT_FILE_ADDED:
                    frames.append(Frame(data.folder, data.name))
                # More may be waiting: without waiting for them
                timeout = 1
            return frames
        except gp.GPhoto2Error as error:
            raise self._error(error) from error

    def fetch(self, frame):
        """The bytes of `frame`."""
        gp = self._gp
        try:
            file = self._patiently(
                self._camera.file_get, frame.folder, frame.name, gp.GP_FILE_TYPE_NORMAL
            )
            return bytes(file.get_data_and_size())
        except gp.GPhoto2Error as error:
            raise self._error(error) from error

    def list_files(self, should_stop=lambda: False):
        """Every file on the camera's storage (all its cards), as CameraFiles in folder
        order, with the size and time each has there. Only reads."""
        gp = self._gp
        found = []
        pending = ["/"]
        try:
            while pending:
                if should_stop():
                    break
                folder = pending.pop()
                subfolders = self._patiently(self._camera.folder_list_folders, folder)
                names = self._patiently(self._camera.folder_list_files, folder)
                for number in range(len(names)):
                    name = names.get_name(number)
                    info = self._patiently(self._camera.file_get_info, folder, name)
                    found.append(CameraFile(folder, name, info.file.size, info.file.mtime))
                pending.extend(
                    reversed(
                        [
                            f"{folder.rstrip('/')}/{subfolders.get_name(number)}"
                            for number in range(len(subfolders))
                        ]
                    )
                )
        except gp.GPhoto2Error as error:
            raise self._error(error) from error
        return found

    def info(self, path):
        """The CameraFile at `path` on the camera."""
        gp = self._gp
        folder, _slash, name = path.rpartition("/")
        try:
            info = self._patiently(self._camera.file_get_info, folder or "/", name)
        except gp.GPhoto2Error as error:
            raise self._error(error) from error
        return CameraFile(folder or "/", name, info.file.size, info.file.mtime)

    def read_range(self, path, offset, length):
        """`length` bytes of the file at `path` from `offset` (fewer at its end), without
        the rest of it being sent."""
        gp = self._gp
        folder, _slash, name = path.rpartition("/")
        buffer = bytearray(length)
        try:
            got = self._patiently(
                self._camera.file_read,
                folder or "/",
                name,
                gp.GP_FILE_TYPE_NORMAL,
                offset,
                memoryview(buffer),
            )
        except gp.GPhoto2Error as error:
            raise self._error(error) from error
        return bytes(buffer[:got])

    def release(self, frame):
        """Let go of a frame in the camera's memory, once it is saved."""
        gp = self._gp
        try:
            self._patiently(self._camera.file_delete, frame.folder, frame.name)
        except gp.GPhoto2Error as error:
            raise self._error(error) from error

    def trigger(self, focus):
        """Take a picture, focusing first or not. The frame comes as an event."""
        gp = self._gp
        self._end_focus()
        try:
            if self._value("eosremoterelease") is not None:
                # Canon: press the button all the way, with autofocus or without, and let go
                # of both of its stages, as a finger does. Let go of only the full press, the
                # half press stays held: the 90D's screen goes dark and it refuses every
                # command (`-110 I/O in progress`) until let go of or turned off and on
                self.set("eosremoterelease", "Press Full AF" if focus else "Press Full MF")
                self._let_go()
            else:
                self._patiently(self._camera.trigger_capture)
        except gp.GPhoto2Error as error:
            raise self._error(error) from error

    # Live view

    def live_view(self):
        """What its live view can do. Canon (EOS): magnified 5 and 10 times, around a point
        that is also where it focuses (for the models whose frame is known), and it takes
        pictures in live view; others: frames only, and out of live view for a picture."""
        canon = self._value("eosremoterelease") is not None
        zooms = (1, 5, 10) if canon and self._value("eoszoom") is not None else (1,)
        point = (
            canon
            and self._value("eoszoomposition") is not None
            and self.camera_info.model in _POINT_FRAMES
        )
        focus = canon or self._value("autofocusdrive") is not None
        return LiveView(zooms, point, focus, captures=canon)

    def live(self, on):
        """Start or stop its live view: a camera with a viewfinder setting (Canon, Nikon)
        through it; others start with the first frame and stop on their own."""
        self._end_focus()
        if self._value("viewfinder") is None:
            return
        try:
            self.set("viewfinder", 1 if on else 0)
        except CameraError:
            if on:
                raise

    def preview(self):
        """The newest live view frame: a JPEG."""
        gp = self._gp
        try:
            file = self._camera.capture_preview()
            return bytes(file.get_data_and_size())
        except gp.GPhoto2Error as error:
            raise self._error(error) from error

    def zoom(self, times):
        """Magnify `times` times, around the point."""
        self._no_face_tracking()
        self.set("eoszoom", str(times))

    def point(self, x, y):
        """Move what is magnified, and where it focuses, to (x, y) as shares of the frame;
        returns where it went."""
        self._no_face_tracking()
        position, placed = canon_position((x, y), _POINT_FRAMES[self.camera_info.model])
        self.set("eoszoomposition", f"{position[0]},{position[1]}")
        return placed

    def autofocus(self):
        """Focus without taking a picture, as a half press of the button does: in live view
        at its point. It returns at once, the camera focuses on its own, and the focus is
        let go of a while later (`wait`), or before anything else is done.

        Tried on the 90D: `autofocusdrive` focuses in live view only (out of it, "-1
        Unspecified error"), and only once, even set to 0 between. A half press of the
        remote release focuses in live view and out of it, every time once let go of."""
        self._end_focus()
        if self._value("eosremoterelease") is not None:
            self.set("eosremoterelease", "Press Half AF")
            self._held = True
            self._held_until = time.monotonic() + _FOCUS_HELD
        else:
            self.set("autofocusdrive", 1)

    def _end_focus(self):
        """Let go of a focus held; quietly, as a camera may refuse it."""
        held, self._held = self._held, False
        if held:
            try:
                self.set("eosremoterelease", "Release Half")
            except CameraError:
                pass

    def _no_face_tracking(self):
        # Tried on the 90D: with face tracking it answers magnifying with "busy"
        if self._value("afmethod") == "LiveFace":
            raise CameraError(
                "The camera can't magnify or move its focus point with face tracking AF: set "
                "its AF method to 1-point or Spot AF"
            )

    def _patiently(self, call, *args):
        """`call(*args)`, asked again while the camera says it is busy, for a while."""
        gp = self._gp
        end = time.monotonic() + _BUSY_FOR
        while True:
            try:
                return call(*args)
            except gp.GPhoto2Error as error:
                if error.code != gp.GP_ERROR_CAMERA_BUSY or time.monotonic() >= end:
                    raise
            time.sleep(_BUSY_STEP)

    def _value(self, name):
        try:
            return self._camera.get_single_config(name).get_value()
        except self._gp.GPhoto2Error:
            return None

    def _error(self, error):
        gp = self._gp
        gone = (gp.GP_ERROR_IO_USB_FIND, gp.GP_ERROR_IO, gp.GP_ERROR_IO_READ, gp.GP_ERROR_IO_WRITE)
        if error.code == gp.GP_ERROR_CAMERA_BUSY:
            return CameraError(
                "The camera is busy: close a menu or playback on it, or wait for it to finish "
                "writing"
            )
        if error.code in gone or not self._still_there():
            return CameraError(f"The camera was disconnected ({error})", GONE)
        return CameraError(str(error))

    def _still_there(self):
        try:
            found = self._gp.Camera.autodetect()
        except self._gp.GPhoto2Error:
            return False
        port = self.camera_info.port if self.camera_info is not None else None
        return any(found.get_value(i) == port for i in range(len(found)))


# The frame a Canon places what it magnifies in: its sensor's pixels, by model (measured
# on the 90D by finding magnified frames in the whole one). A model not here is
# magnified around where its own point is.
_POINT_FRAMES = {"Canon EOS 90D": (6960, 4640)}
# The share of the frame across a Canon's magnifying frame: what it shows magnified 5
# times (10 times, the middle of it)
_POINT_SHARE = 1 / 5


def canon_position(point, size):
    """Where a Canon's `eoszoomposition` goes for it to magnify around `point` (x, y as
    shares of the frame), and the point that makes, in the frame `size` (its pixels).
    Tried on the 90D: the position is the top left corner of its magnifying frame, a fifth
    of the frame across, kept inside the frame, whatever the magnification; it stays when
    the magnification changes, and reads back as 0,0 always."""
    half = _POINT_SHARE / 2
    left = min(max(point[0] - half, 0.0), 1 - _POINT_SHARE)
    top = min(max(point[1] - half, 0.0), 1 - _POINT_SHARE)
    return (round(left * size[0]), round(top * size[1])), (left + half, top + half)


def detect():
    """[(model, port)] of the cameras on USB that libgphoto2 knows, without opening them:
    empty without python-gphoto2 or when it can't look."""
    try:
        import gphoto2 as gp

        found = gp.Camera.autodetect()
        cameras = [(found.get_name(i), found.get_value(i)) for i in range(len(found))]
    except Exception:
        return []
    return [(model, port) for model, port in cameras if port.startswith("usb:")]


def available():
    """Whether python-gphoto2 is there to speak to a camera."""
    import importlib.util

    return importlib.util.find_spec("gphoto2") is not None


# Who has the camera


# Programs that take a camera, and how to let it go
_HOLDERS = (
    (
        ("gvfsd-gphoto2", "gvfs-gphoto2-vo"),
        "The desktop's file access (GVFS) has the camera: unmount it in the file manager, "
        "or run “gio mount -s gphoto2”.",
    ),
    (
        ("kioworker", "kio_camera", "kiod6", "kiod5", "kdeinit5", "solid-hardware"),
        "KDE has the camera open (a Dolphin window or the device notifier showing it): "
        "close that, or unplug the camera and plug it in again without opening it.",
    ),
)


def holders(port, proc="/proc", own=None):
    """The names of the processes that have the USB device of `port` ("usb:003,003")
    open, among those /proc shows (this user's), this one left out."""
    match = re.fullmatch(r"usb:(\d+),(\d+)", port or "")
    if match is None:
        return []
    device = f"/dev/bus/usb/{int(match[1]):03d}/{int(match[2]):03d}"
    own = os.getpid() if own is None else own
    names = []
    try:
        pids = [name for name in os.listdir(proc) if name.isdigit() and int(name) != own]
    except OSError:
        return []
    for pid in pids:
        try:
            fds = os.listdir(os.path.join(proc, pid, "fd"))
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(proc, pid, "fd", fd))
            except OSError:
                continue
            if target == device:
                try:
                    with open(os.path.join(proc, pid, "comm")) as file:
                        names.append(file.read().strip())
                except OSError:
                    names.append(f"process {pid}")
                break
    return sorted(set(names))


def busy_text(names):
    """Why the camera can't be used, from the names of the programs that have it."""
    for programs, text in _HOLDERS:
        if any(name.startswith(programs) for name in names):
            return text
    if names:
        return f"{', '.join(names)} is using the camera: close it to shoot with Lumina."
    return "Another program is using the camera: close it to shoot with Lumina."


# Writing a frame


def save(where, item_id, camera_name, data):
    """Put the frame `data` (its name on the camera `camera_name`) in the folder `where`,
    never over a file, and return its path. Raises PhotoError naming it when it can't be
    written or does not read back the same."""
    os.makedirs(where, exist_ok=True)
    part = os.path.join(where, f".lumina-tether-{item_id}.part")
    wanted = hashlib.blake2b(data).hexdigest()
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        if _hash(part) != wanted:
            raise PhotoError(camera_name, "the copy is not the same as the frame the camera sent")
        taken = capture_time.read(part) or datetime.now()
        stem, suffix = os.path.splitext(camera_name)
        return derived.place(
            part,
            where,
            f"{taken:%Y%m%d-%H%M%S}-{stem}",
            suffix,
            same=lambda path: os.path.getsize(path) == len(data) and _hash(path) == wanted,
        )
    except OSError as error:
        derived.remove(part)
        raise PhotoError(os.path.join(where, camera_name), error.strerror or str(error)) from error
    except BaseException:
        derived.remove(part)
        raise


def _hash(path):
    digest = hashlib.blake2b()
    with open(path, "rb") as file:
        if hasattr(os, "posix_fadvise"):
            # What is on the disk, not what is still in memory from writing it
            os.posix_fadvise(file.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


# The session


def serve(library_path, session, commands, events, camera=GPhotoCamera, frames=None, **timing):
    """Run the camera session `session` (a Session) until told to stop: in a process of
    its own, with a connection of its own. `commands` and `events` are queues of the
    tuples above, `frames` one for live view's frames (None: no live view); `camera`
    makes a camera (GPhotoCamera's methods)."""
    with dbm.Library(library_path) as library:
        _Session(library, session, commands, events, camera, frames, **timing).run()


class _Session:
    def __init__(
        self,
        library,
        session,
        commands,
        events,
        camera,
        frames=None,
        poll=_POLL,
        retry=_RETRY,
        readings_every=_READINGS_EVERY,
        live_fails_for=_LIVE_FAILS_FOR,
        proc="/proc",
    ):
        self.library = library
        self.session = session
        self.commands = commands
        self.events = events
        self.make_camera = camera
        self.poll = poll
        self.retry = retry
        self.readings_every = readings_every
        self.proc = proc
        self.frames = frames
        self.live_fails_for = live_fails_for
        self.camera = None
        self.camera_live = False
        self.stopping = False
        self._told = None
        self._read_at = 0.0
        # Live view: as wanted, kept through an unplug; the newest frame sent and the
        # newest the app has; since when frames have failed
        self.live = LiveState(None)
        self._view = LiveView()
        self._sent = self._shown = 0
        self._failing_since = None

    def run(self):
        try:
            while not self.stopping:
                try:
                    if self.camera is None:
                        self._connect()
                        if self.camera is None:
                            self._take_commands(self.retry)
                            continue
                    self._take_commands(0)
                    if self.stopping:
                        break
                    # In live view not waiting for events: they are looked for between frames
                    for frame in self.camera.wait(0 if self.live.on else self.poll):
                        self._take(frame)
                    if time.monotonic() - self._read_at >= self.readings_every:
                        self._read_settings()
                    if self.live.on:
                        self._show()
                except CameraError as error:
                    if error.kind == GONE:
                        self._lost(str(error))
                    else:
                        self._tell(ERROR, str(error))
        except Exception as error:
            self._tell(ERROR, f"The camera session stopped: {type(error).__name__}: {error}")
            raise
        finally:
            if self.camera is not None:
                if self.camera_live:
                    try:
                        self._live_off()
                    except CameraError:
                        pass
                self.camera.close()
            self.events.put((ENDED,))

    def _tell(self, *event):
        self.events.put(event)

    def _say(self, kind, text):
        # Waiting is told when it changes, not every second
        if self._told != (kind, text):
            self._told = (kind, text)
            self._tell(kind, text)

    def _connect(self):
        camera = self.make_camera()
        try:
            info = camera.open()
        except CameraError as error:
            camera.close()
            if error.kind == BUSY:
                self._say(BUSY, busy_text(holders(error.port, self.proc)))
            elif error.kind == NOT_FOUND:
                self._say(WAITING, "Connect the camera by USB and turn it on")
            else:
                self._say(WAITING, str(error))
            return
        self.camera = camera
        self.info = info
        self._told = None
        try:
            camera.set("capturetarget", _TARGETS[self.session.keep])
        except CameraError as error:
            if error.kind == GONE:
                raise
            self._tell(ERROR, f"Where the camera keeps its frames could not be chosen: {error}")
        settings = camera.settings()
        self._read_at = time.monotonic()
        self._view = camera.live_view()
        self._tell(CONNECTED, info, settings)
        self._resume()
        if self.live.on:
            # Live view was on when the camera was unplugged: on again
            self._live_on(self.live.start)
        else:
            self._tell(LIVE_TOLD, replace(self.live, view=self._view))

    def _lost(self, why):
        if self.camera is not None:
            self.camera.close()
        self.camera = None
        self.camera_live = False
        self._told = (DISCONNECTED, why)
        self._tell(DISCONNECTED, why)

    def _read_settings(self):
        self._read_at = time.monotonic()
        self._tell(SETTINGS_TOLD, self.camera.settings())

    def _take_commands(self, wait):
        """Do the commands there are; with `wait`, wait that long for the first."""
        while True:
            try:
                command = self.commands.get(timeout=wait) if wait else self.commands.get_nowait()
            except queue.Empty:
                return
            wait = 0
            kind = command[0]
            if kind == STOP:
                self.stopping = True
                return
            if kind == SHOWN:
                self._shown = max(self._shown, command[1])
                continue
            if kind == LIVE and self.camera is None:
                # On, or not, when the camera is there
                self.live = LiveState(command[1], view=self._view)
                continue
            if self.camera is None:
                self._tell(ERROR, "The camera is not connected")
                continue
            try:
                if kind == CAPTURE:
                    self._capture(command[1])
                elif kind == LIVE:
                    if command[1] is None:
                        self._live_off()
                    else:
                        self._live_on(command[1])
                elif kind == ZOOM:
                    self._zoom(command[1])
                elif kind == POINT:
                    self._point(*command[1:])
                elif kind == FOCUS:
                    self._focus()
                elif kind == SET:
                    self.camera.set(command[1], command[2])
                    self._read_settings()
                elif kind == FETCH:
                    for item_id in command[1]:
                        self._fetch_again(item_id)
            except CameraError as error:
                if error.kind == GONE:
                    raise
                self._tell(ERROR, str(error))

    def _capture(self, focus):
        if self.live.on and not self._view.captures:
            # A camera that can't take a picture in live view: out of it for the picture
            self.camera.live(False)
            try:
                self.camera.trigger(focus)
            finally:
                self.camera.live(True)
        else:
            self.camera.trigger(focus)

    # Live view

    def _live_on(self, start):
        if self.frames is None:
            raise CameraError("Live view is not available here")
        if not self.camera_live:
            self.camera.live(True)
            self.camera_live = True
        # The frames of an earlier start count as shown: they are dropped
        self._shown = self._sent
        self._failing_since = None
        # A camera starts its live view unmagnified
        self.live = replace(self.live, start=start, why=None, view=self._view, zoom=1)
        self._tell(LIVE_TOLD, self.live)

    def _live_off(self, why=None):
        try:
            if self.camera is not None:
                self.camera.live(False)
        finally:
            self.camera_live = False
            self.live = LiveState(None, why, self._view)
            self._tell(LIVE_TOLD, self.live)

    def _zoom(self, zoom):
        if not self.live.on:
            raise CameraError("Live view is off")
        if zoom not in self._view.zooms:
            raise CameraError(f"The camera can't magnify {zoom} times")
        self.camera.zoom(zoom)
        self.live = replace(self.live, zoom=zoom)
        self._tell(LIVE_TOLD, self.live)

    def _point(self, x, y, focus):
        if not self.live.on:
            raise CameraError("Live view is off")
        if not self._view.point:
            raise CameraError("The camera's focus point can't be moved from here")
        point = self.camera.point(min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0))
        self.live = replace(self.live, point=point)
        self._tell(LIVE_TOLD, self.live)
        if focus:
            self._focus()

    def _focus(self):
        """Focus, in live view or not, at the camera's point."""
        if not self._view.focus:
            raise CameraError("The camera can't be focused from here")
        self.camera.autofocus()

    def _show(self):
        """The next frame, once the app has the one before the last; waiting a moment for
        it to say so otherwise."""
        if self._sent - self._shown >= _IN_FLIGHT:
            self._take_commands(self.poll / 4)
            return
        try:
            jpeg = self.camera.preview()
            look = live.examine(jpeg)
        except CameraError as error:
            if error.kind == GONE:
                raise
            self._failed_frame(str(error))
            return
        except ValueError as error:
            self._failed_frame(str(error))
            return
        self._failing_since = None
        self._sent += 1
        self.frames.put(
            LiveFrame(self._sent, self.live.start, jpeg, look, self.live.zoom, self.live.point)
        )

    def _failed_frame(self, why):
        """A frame that did not come: live view stops after a few seconds of them (a
        camera too hot, a menu open on it)."""
        now = time.monotonic()
        if self._failing_since is None:
            self._failing_since = now
        elif now - self._failing_since >= self.live_fails_for:
            self._live_off(f"The camera stopped sending live view ({why})")
            return
        time.sleep(self.poll)

    # Frames

    def _take(self, frame):
        item = self.library.add_tether_item(
            self.session.id, self.info.serial, frame.folder, frame.name, frame.on_card
        )
        self._tell(TAKEN, item, frame.name)
        self._download(item, frame, self.session.where)

    def _download(self, item, frame, where):
        """Fetch, write and note one frame. A camera unplugged meanwhile leaves it to
        fetch (CameraError GONE is raised)."""
        try:
            data = self.camera.fetch(frame)
        except CameraError as error:
            if error.kind == GONE:
                raise
            self._failed(item, frame.name, str(error))
            return
        try:
            path = save(where, item, frame.name, data)
        except PhotoError as error:
            self._failed(item, frame.name, error.message)
            return
        self.library.set_tether_item(item, dbm.TETHER_SAVED, name=os.path.basename(path))
        self._tell(SAVED, item, path)
        if not frame.on_card:
            try:
                self.camera.release(frame)
            except CameraError as error:
                if error.kind == GONE:
                    raise

    def _failed(self, item, name, why):
        self.library.set_tether_item(item, dbm.TETHER_FAILED, message=why)
        self._tell(FAILED, item, name, why)

    def _resume(self):
        """Frames this camera reported that were not saved (a crash, an unplug): those
        on the card fetched again, those that were only in its memory named as lost."""
        for item in self.library.pending_tether_items(self.info.serial):
            frame = Frame(item.camera_folder, item.camera_name)
            where = self.session.folders.get(item.session_id)
            if not frame.on_card:
                self._failed(
                    item.id,
                    frame.name,
                    "It was only in the camera's memory, and the camera was disconnected "
                    "before it was saved",
                )
            elif where is None:
                self._failed(item.id, frame.name, "Its session's folder is not connected")
            else:
                self._tell(TAKEN, item.id, frame.name)
                self._download(item.id, frame, where)

    def _fetch_again(self, item_id):
        item = self.library.tether_item(item_id)
        if item is None or item.state == dbm.TETHER_SAVED:
            return
        frame = Frame(item.camera_folder, item.camera_name)
        where = self.session.folders.get(item.session_id)
        if not frame.on_card:
            self._failed(item_id, frame.name, "It was only in the camera's memory")
            return
        if where is None:
            self._failed(item_id, frame.name, "Its session's folder is not connected")
            return
        if item.camera != self.info.serial:
            self._failed(item_id, frame.name, "It is on another camera's card")
            return
        self.library.set_tether_item(item_id, dbm.TETHER_PENDING)
        self._tell(TAKEN, item_id, frame.name)
        self._download(item_id, frame, where)
