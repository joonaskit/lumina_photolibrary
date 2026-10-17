"""Stand-ins for the tests: an exiftool that answers at once and can be told to fail."""

import time
from datetime import datetime

from lumina.core.errors import PhotoError
from lumina.core.metadata import Metadata, MetadataUnavailable


class FakeExifTool:
    """Says every photo is from a 90D taken on 2022-05-09, except: a name in `models`
    gets that camera, a name in `warnings` that warning, a path in `broken` fails, and
    after `dies_after` batches it stops. The file type is the name's extension.
    `delay` seconds per batch make a scan slow enough to stop in the middle."""

    calls = []
    models = {}
    warnings = {}
    delay = 0.0
    # Whether a scan asked for the lowest priority, as the last one did
    gentle = False

    def __init__(self, broken=(), dies_after=None):
        self.broken = set(map(str, broken))
        self.dies_after = dies_after

    def read_many(self, paths):
        FakeExifTool.calls.append(list(paths))
        if FakeExifTool.delay:
            time.sleep(FakeExifTool.delay)
        if self.dies_after is not None and len(FakeExifTool.calls) > self.dies_after:
            raise MetadataUnavailable("exiftool stopped")
        return {p: self._one(p) for p in paths}

    def _one(self, path):
        if path in self.broken:
            return PhotoError(path, "File format error")
        name = path.rsplit("/", 1)[-1]
        model = FakeExifTool.models.get(name, "Canon EOS 90D")
        return Metadata(
            captured=datetime(2022, 5, 9, 21, 3),
            model=model,
            lens="EF-S55-250mm",
            file_type=name.rsplit(".", 1)[-1].upper(),
            warning=FakeExifTool.warnings.get(name),
        )

    def set_gentle(self, on=True):
        FakeExifTool.gentle = on

    def close(self):
        pass

    @classmethod
    def reset(cls):
        cls.calls, cls.models, cls.warnings, cls.delay, cls.gentle = [], {}, {}, 0.0, False


def colour_vector(colour):
    """A unit vector from an RGB colour, in the model's dimensions: photos of one
    colour are alike, and a query is a colour."""
    import numpy as np

    from lumina.core import embed

    vector = np.zeros(embed.DIMENSIONS, np.float32)
    vector[:3] = colour
    vector[3] = 1
    return vector / np.linalg.norm(vector)


class FakeEncoder:
    """encode(paths) as core/indexing.py takes it, without the model: a photo's vector
    is its mean colour. `calls` records the batches; a path in `crashes` makes a call
    that has it raise, as a worker a decoder crashed in does; `broken` raises always."""

    def __init__(self, crashes=(), broken=False):
        self.calls = []
        self.crashes = set(map(str, crashes))
        self.broken = broken

    def __call__(self, paths):
        from lumina.core import embed, images

        self.calls.append(list(paths))
        if self.broken:
            raise RuntimeError("the model could not be loaded")
        if self.crashes & set(paths):
            raise RuntimeError("A process in the process pool was terminated abruptly")
        results = []
        for path in paths:
            try:
                pixels = images.load_pixels(path, 16)
            except PhotoError as error:
                results.append(error)
                continue
            data = pixels.data
            colour = [sum(data[i::3]) / (len(data) / 3) / 255 for i in range(3)]
            results.append(embed.to_blob(colour_vector(colour)))
        return results


def install_model(root):
    """The search model as is_installed sees it: every file at its size, sparse, so
    nothing the size of the real one is written."""
    import os

    from lumina.core import embed, models

    folder = models.directory(embed.MODEL, root)
    for f in embed.MODEL.files:
        path = os.path.join(folder, f.path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(f.size)


# What the fake text encoder understands: a colour by name
COLOURS = {"red": (0.8, 0.15, 0.15), "green": (0.04, 0.8, 0.04), "blue": (0.1, 0.1, 0.8)}


def encode_colour_text(text):
    return colour_vector(COLOURS.get(text.lower(), (0.5, 0.5, 0.5)))


class FakeCamera:
    """A camera on USB as core/tether.py sees it, kept in the folder `root`, so a test
    drives it from outside the process that has it (`shoot`, `unplug`, `make_busy`). Its
    card is `root/card/DCIM/100CANON`, its memory `root/memory`; a shot goes where the
    session set the capture target, and is told as an event once."""

    CARD_FOLDER = "/store_00020001/DCIM/100CANON"

    def __init__(self, root):
        import os

        self.root = os.fspath(root)
        self._seen = 0
        self._open = False

    # Driven by the test

    @classmethod
    def shoot(cls, root, data, name=None):
        """A frame, as from the camera's own button. Returns its name."""
        import os

        root = os.fspath(root)
        card = cls._target(root) == "Memory card"
        folder = (
            os.path.join(root, "card", "DCIM", "100CANON") if card else os.path.join(root, "memory")
        )
        os.makedirs(folder, exist_ok=True)
        count = len(cls._lines(root, "events"))
        name = name or (f"IMG_{6000 + count:04d}.CR3" if card else f"capt{count:04d}.cr3")
        with open(os.path.join(folder, name), "wb") as file:
            file.write(data)
        with open(os.path.join(root, "events"), "a") as file:
            file.write(f"{cls.CARD_FOLDER if card else '/'}|{name}\n")
        return name

    @staticmethod
    def unplug(root, unplugged=True):
        FakeCamera._flag(root, "unplugged", unplugged)

    @staticmethod
    def make_busy(root, busy=True):
        FakeCamera._flag(root, "busy", busy)

    @staticmethod
    def fail_fetching(root, failing=True):
        FakeCamera._flag(root, "fail-fetch", failing)

    @staticmethod
    def unplug_while_fetching(root):
        """The next fetch finds the camera unplugged."""
        FakeCamera._flag(root, "unplug-on-fetch", True)

    @staticmethod
    def fail_previews(root, failing=True):
        """Live view frames fail, as from a camera too hot."""
        FakeCamera._flag(root, "fail-preview", failing)

    @staticmethod
    def stop_in_live_view(root, stops=True):
        """A camera that can't take a picture in live view."""
        FakeCamera._flag(root, "no-live-capture", stops)

    @classmethod
    def log(cls, root, name):
        """What the session did: "target", "set", "triggers", "released", "live",
        "previews", "zoom", "point" or "focus" lines."""
        return cls._lines(root, name)

    # As core/tether.py uses it

    def open(self):
        from lumina.core import tether

        if self._has("unplugged"):
            raise tether.CameraError("No camera is connected", tether.NOT_FOUND)
        if self._has("busy"):
            raise tether.CameraError("Could not claim the USB device", tether.BUSY, "usb:001,002")
        self._open = True
        # Only what comes after it was opened is told
        self._seen = len(self._lines(self.root, "events"))
        return tether.CameraInfo("Fake EOS", "FAKE-1", "usb:001,002")

    def close(self):
        self._open = False

    def settings(self):
        from lumina.core import tether

        self._check()
        values = {"iso": "100", "shutterspeed": "1/125", "batterylevel": "56%"}
        for line in self._lines(self.root, "set"):
            name, value = line.split("=", 1)
            values[name] = value
        return {
            "iso": tether.Setting("iso", "ISO Speed", values["iso"], ("Auto", "100", "200", "400")),
            "shutterspeed": tether.Setting(
                "shutterspeed", "Shutter Speed", values["shutterspeed"], ("1/60", "1/125")
            ),
            "batterylevel": tether.Setting(
                "batterylevel", "Battery Level", values["batterylevel"], (), True
            ),
        }

    def set(self, name, value):
        from lumina.core import tether

        self._check()
        if name == "capturetarget":
            self._write("target", value, mode="w")
            return
        setting = self.settings().get(name)
        if setting is None or (setting.choices and value not in setting.choices):
            raise tether.CameraError(f"{value} is not a value of {name}")
        self._write("set", f"{name}={value}")

    def wait(self, seconds):
        import time

        from lumina.core import tether

        self._check()
        lines = self._lines(self.root, "events")
        if len(lines) <= self._seen:
            time.sleep(seconds)
            return []
        frames = [tether.Frame(*line.split("|", 1)) for line in lines[self._seen :]]
        self._seen = len(lines)
        return frames

    def fetch(self, frame):
        from lumina.core import tether

        self._check()
        if self._has("unplug-on-fetch"):
            FakeCamera._flag(self.root, "unplug-on-fetch", False)
            FakeCamera.unplug(self.root)
            raise tether.CameraError("The camera was disconnected", tether.GONE)
        if self._has("fail-fetch"):
            raise tether.CameraError("I/O problem reading the file")
        try:
            with open(self._file(frame), "rb") as file:
                return file.read()
        except OSError as error:
            raise tether.CameraError(f"{frame.name}: not found ({error})") from error

    def release(self, frame):
        import os

        self._check()
        os.remove(self._file(frame))
        self._write("released", frame.name)

    def trigger(self, focus):
        from lumina.core import tether

        self._check()
        if self._has("live-on") and self._has("no-live-capture"):
            raise tether.CameraError("It can't take a picture in live view")
        self._write("triggers", "focus" if focus else "no focus")
        FakeCamera.shoot(self.root, b"shot from the app " + bytes(str(focus), "ascii"))

    # Live view

    def live_view(self):
        from lumina.core import tether

        return tether.LiveView((1, 5, 10), True, True, not self._has("no-live-capture"))

    def live(self, on):
        self._check()
        FakeCamera._flag(self.root, "live-on", on)
        self._write("live", "on" if on else "off")

    def preview(self):
        import time

        from lumina.core import tether

        self._check()
        if not self._has("live-on"):
            raise tether.CameraError("Live view is off")
        if self._has("fail-preview"):
            time.sleep(0.005)
            raise tether.CameraError("Could not get the live view frame")
        count = len(self._lines(self.root, "previews"))
        self._write("previews", str(count + 1))
        time.sleep(0.005)
        return live_jpeg(count % 256)

    def zoom(self, times):
        self._check()
        self._write("zoom", str(times))

    def point(self, x, y):
        self._check()
        self._write("point", f"{x:.2f},{y:.2f}")
        return (x, y)

    def autofocus(self):
        self._check()
        self._write("focus", "at the point")

    # Inside

    def _check(self):
        from lumina.core import tether

        if self._has("unplugged"):
            raise tether.CameraError("The camera was disconnected", tether.GONE)

    def _file(self, frame):
        import os

        if frame.on_card:
            return os.path.join(self.root, "card", "DCIM", "100CANON", frame.name)
        return os.path.join(self.root, "memory", frame.name)

    def _has(self, name):
        import os

        return os.path.exists(os.path.join(self.root, name))

    def _write(self, name, line, mode="a"):
        import os

        with open(os.path.join(self.root, name), mode) as file:
            file.write(f"{line}\n")

    @staticmethod
    def _target(root):
        lines = FakeCamera._lines(root, "target")
        return lines[-1] if lines else "Internal RAM"

    @staticmethod
    def _lines(root, name):
        import os

        try:
            with open(os.path.join(os.fspath(root), name)) as file:
                text = file.read()
        except FileNotFoundError:
            return []
        # Only whole lines: one may be being written
        return text.split("\n")[:-1]

    @staticmethod
    def _flag(root, name, on):
        import os

        path = os.path.join(os.fspath(root), name)
        if on:
            open(path, "w").close()
        elif os.path.exists(path):
            os.remove(path)


def live_jpeg(level=128, size=(96, 64)):
    """A grey live view frame as a camera sends it: a JPEG."""
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, (level, level, level)).save(out, "JPEG", quality=90)
    return out.getvalue()
