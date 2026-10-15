"""Photo metadata through exiftool: capture time, camera, lens, exposure, GPS, orientation.

One exiftool process is started with `-stay_open` and fed batches of files on its
stdin, so a library of a hundred thousand photos is not a hundred thousand Perl
starts. exiftool only reads here: no option that writes is ever passed.

An `ExifTool` is for one thread at a time; it locks around each batch.
"""

import json
import os
import re
import selectors
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from lumina.core.errors import PhotoError

# Asked for by name. "Composite:" picks exiftool's signed decimal GPS position over
# the unsigned EXIF value and its separate N/S, E/W reference. With tags named,
# exiftool reports a file it cannot read ("File is empty") only when "Error" is
# among them
_TAGS = (
    "Error",
    "Warning",
    "FileType",
    "SubSecDateTimeOriginal",
    "DateTimeOriginal",
    "CreateDate",
    "OffsetTimeOriginal",
    "Make",
    "Model",
    "LensModel",
    "Lens",
    "FocalLength",
    "FNumber",
    "ExposureTime",
    "ISO",
    "Orientation",
    "Composite:GPSLatitude",
    "Composite:GPSLongitude",
)
# Seconds a batch may take before the process is taken for hung, and per file on top
_BATCH_TIMEOUT = 30.0
_FILE_TIMEOUT = 2.0
_READ = 65536

_DATE = re.compile(
    r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?"
)


class MetadataUnavailable(Exception):
    """exiftool is missing, would not start, or stopped answering."""


@dataclass(frozen=True)
class Metadata:
    # Aware when the file says its offset from UTC, naive (camera's local time) if not
    captured: datetime | None = None
    make: str | None = None
    model: str | None = None
    lens: str | None = None
    # Millimetres, the f-number, seconds
    focal_length: float | None = None
    aperture: float | None = None
    exposure_time: float | None = None
    iso: int | None = None
    # EXIF orientation, 1 to 8
    orientation: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    # What exiftool found the file to be ("JPEG", "CR3", "TXT"), whatever its name says
    file_type: str | None = None
    # What exiftool found wrong but could read past ("JPEG format error")
    warning: str | None = None


# How far down the CPU priority goes (nice) for a process that should not be noticed
_NICE = 10


def lower_priority(pid):
    """Make the process `pid` yield to the rest of the desktop: a lower CPU priority, and
    the disk's idle class (it is served when nothing else wants the disk). Best effort:
    where the system has no such thing, it is as it was."""
    try:
        os.setpriority(os.PRIO_PROCESS, pid, _NICE)
    except (OSError, AttributeError):
        pass
    ionice = shutil.which("ionice")
    if ionice:
        try:
            subprocess.run(
                [ionice, "-c", "3", "-p", str(pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


class ExifTool:
    def __init__(self, executable=None):
        self.executable = executable or shutil.which("exiftool")
        if not self.executable:
            raise MetadataUnavailable("exiftool is not installed")
        self._gentle = False
        self._process = None
        self._buffer = b""
        self._counter = 0
        self._lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def read(self, path):
        """The metadata of one file. Raises PhotoError naming it."""
        result = self.read_many([path])[os.fspath(path)]
        if isinstance(result, PhotoError):
            raise result
        return result

    def read_many(self, paths):
        """{path: Metadata or PhotoError} for every path given, keyed as given.
        Raises MetadataUnavailable when exiftool itself fails."""
        paths = [os.fspath(path) for path in paths]
        results, wanted = {}, {}
        for path in paths:
            # Absolute, so a name starting with "-" is never read as an option
            wanted.setdefault(os.path.abspath(path), []).append(path)
        if not wanted:
            return results
        with self._lock:
            entries = self._run(list(wanted))
        by_source = {entry.get("SourceFile"): entry for entry in entries}
        for absolute, given in wanted.items():
            entry = by_source.get(absolute)
            if entry is None:
                result = PhotoError(absolute, "not found or not readable")
            elif "Error" in entry:
                result = PhotoError(absolute, str(entry["Error"]))
            else:
                result = _metadata(entry)
            for path in given:
                results[path] = result
        return results

    def close(self):
        with self._lock:
            self._stop()

    def set_gentle(self, on=True):
        """Run exiftool at the lowest CPU and disk priority, so that reading a large
        library does not slow the desktop down: now, and in a process started later."""
        with self._lock:
            self._gentle = on
            if on and self._process is not None:
                lower_priority(self._process.pid)

    # The process

    def _start(self):
        try:
            self._process = subprocess.Popen(
                [
                    self.executable,
                    "-stay_open",
                    "True",
                    "-@",
                    "-",
                    "-common_args",
                    "-json",
                    "-n",
                    "-q",
                    "-charset",
                    "filename=utf8",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            self._process = None
            raise MetadataUnavailable(f"cannot start exiftool ({error})") from error
        self._buffer = b""
        if self._gentle:
            lower_priority(self._process.pid)

    def _stop(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.write(b"-stay_open\nFalse\n")
            process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        process.stdout.close()

    def _run(self, paths):
        """One batch, started over once if the process had died since the last."""
        for attempt in (1, 2):
            if self._process is None or self._process.poll() is not None:
                self._start()
            try:
                return self._ask(paths)
            except (BrokenPipeError, EOFError) as error:
                self._kill()
                if attempt == 2:
                    raise MetadataUnavailable(f"exiftool stopped ({error})") from error
        raise AssertionError("unreachable")

    def _ask(self, paths):
        self._counter += 1
        marker = f"{{ready{self._counter}}}".encode()
        lines = [f"-{tag}" for tag in _TAGS] + [_argument(path) for path in paths]
        lines.append(f"-execute{self._counter}")
        self._process.stdin.write(("\n".join(lines) + "\n").encode())
        self._process.stdin.flush()
        output = self._read_until(marker, _BATCH_TIMEOUT + _FILE_TIMEOUT * len(paths))
        output = output.strip()
        if not output:
            # None of the files could be read: exiftool prints nothing at all
            return []
        try:
            entries = json.loads(output)
        except ValueError as error:
            self._kill()
            raise MetadataUnavailable(f"exiftool answered something else ({error})") from error
        return [entry for entry in entries if isinstance(entry, dict)]

    def _read_until(self, marker, timeout):
        deadline = time.monotonic() + timeout
        stdout = self._process.stdout
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)
            while True:
                # The marker ends its own line
                end = self._buffer.find(marker + b"\n")
                if end == -1 and self._buffer.endswith(marker):
                    end = len(self._buffer) - len(marker)
                if end != -1:
                    output = self._buffer[:end]
                    self._buffer = self._buffer[end + len(marker) :].lstrip(b"\r\n")
                    return output.decode("utf-8", "replace")
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    self._kill()
                    raise MetadataUnavailable(f"exiftool did not answer in {timeout:.0f} s")
                chunk = os.read(stdout.fileno(), _READ)
                if not chunk:
                    raise EOFError("exiftool closed its output")
                self._buffer += chunk

    def _kill(self):
        process, self._process = self._process, None
        if process is not None:
            process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass


def _argument(path):
    """A line of exiftool's argument file. exiftool strips each line, and a line can't
    hold a newline: a name with either is written as a C string, which it unescapes."""
    if path == path.strip() and "\n" not in path and "\r" not in path:
        return path
    escaped = path.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    escaped = escaped.replace("\t", "\\t")
    return f"#[CSTR]{escaped}"


def _metadata(entry):
    return Metadata(
        captured=_captured(entry),
        make=_text(entry.get("Make")),
        model=_text(entry.get("Model")),
        lens=_text(entry.get("LensModel")) or _text(entry.get("Lens")),
        focal_length=_number(entry.get("FocalLength")),
        aperture=_number(entry.get("FNumber")),
        exposure_time=_number(entry.get("ExposureTime")),
        iso=_integer(entry.get("ISO")),
        orientation=_integer(entry.get("Orientation")),
        latitude=_number(entry.get("GPSLatitude")),
        longitude=_number(entry.get("GPSLongitude")),
        file_type=_text(entry.get("FileType")),
        warning=_text(entry.get("Warning")),
    )


def _captured(entry):
    for name in ("SubSecDateTimeOriginal", "DateTimeOriginal", "CreateDate"):
        moment = parse_date(entry.get(name), entry.get("OffsetTimeOriginal"))
        if moment is not None:
            return moment
    return None


def parse_date(value, offset=None):
    """An exiftool date ("2024:06:01 12:34:56.5+03:00") as a datetime, or None for
    nothing or the all-zero date cameras write before their clock is set. `offset`
    ("+03:00") is used when the date has none of its own."""
    if not isinstance(value, str):
        return None
    match = _DATE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    fraction, zone = match.group(7), match.group(8) or _zone_text(offset)
    try:
        moment = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    if fraction:
        moment = moment.replace(microsecond=round(float(fraction) * 1_000_000) % 1_000_000)
    tz = _zone(zone)
    return moment.replace(tzinfo=tz) if tz is not None else moment


def _zone_text(offset):
    return offset.strip() if isinstance(offset, str) else None


def _zone(text):
    if not text:
        return None
    if text == "Z":
        return timezone.utc
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
    return timezone(sign * delta) if delta < timedelta(hours=24) else None


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _integer(value):
    number = _number(value)
    return round(number) if number is not None else None
