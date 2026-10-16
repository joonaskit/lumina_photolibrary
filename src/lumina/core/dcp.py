"""Camera profiles: DCP files (Adobe's DNG camera profiles), read, written and found.

A DCP is a small TIFF-like file (magic "IIRC", or "MMCR" big-endian) of DNG tags: colour
matrices from XYZ to the camera for one or two illuminants, forward matrices from the
white-balanced camera to XYZ (D50), hue/saturation maps and a look table (hue shift,
saturation and value scale on a grid of hue, saturation and value), a tone curve, and an
exposure offset. How they are applied is edit/colour_profile.py.

Profiles cannot be shipped (Adobe's are Adobe's, RawTherapee's are its own), so they are
found where the user has them: RawTherapee's folder, Lumina's own (`profiles/user` under
$XDG_DATA_HOME/lumina, where the profiles Lumina fits are written) and folders the user
names. An edit does not name a file there: the profile it uses is copied into a store,
named by its content (`store`), so the edit renders the same if the file is changed, moved
or deleted.

No Qt.
"""

import hashlib
import json
import os
import re
import struct
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from lumina.core.errors import PhotoError

# Tags
UNIQUE_CAMERA_MODEL = 50708
COLOR_MATRIX_1, COLOR_MATRIX_2 = 50721, 50722
CALIBRATION_ILLUMINANT_1, CALIBRATION_ILLUMINANT_2 = 50778, 50779
PROFILE_NAME = 50936
HUE_SAT_MAP_DIMS, HUE_SAT_MAP_DATA_1, HUE_SAT_MAP_DATA_2 = 50937, 50938, 50939
PROFILE_TONE_CURVE = 50940
PROFILE_EMBED_POLICY = 50941
PROFILE_COPYRIGHT = 50942
FORWARD_MATRIX_1, FORWARD_MATRIX_2 = 50964, 50965
LOOK_TABLE_DIMS, LOOK_TABLE_DATA = 50981, 50982
HUE_SAT_MAP_ENCODING, LOOK_TABLE_ENCODING = 51107, 51108
BASELINE_EXPOSURE_OFFSET = 51109

# The colour temperature of the illuminants a profile can be calibrated for (EXIF's
# LightSource codes), in kelvin
ILLUMINANTS = {
    1: 5500.0,  # daylight
    2: 4150.0,  # fluorescent
    3: 2856.0,  # tungsten
    4: 5500.0,  # flash
    9: 5500.0,  # fine weather
    10: 6500.0,  # cloudy
    11: 7500.0,  # shade
    12: 6430.0,  # daylight fluorescent
    13: 5000.0,  # day white fluorescent
    14: 4150.0,  # cool white fluorescent
    15: 3450.0,  # white fluorescent
    16: 2940.0,  # warm white fluorescent
    17: 2856.0,  # standard light A
    18: 4874.0,  # standard light B
    19: 6774.0,  # standard light C
    20: 5503.0,  # D55
    21: 6504.0,  # D65
    22: 7504.0,  # D75
    23: 5003.0,  # D50
    24: 3200.0,  # ISO studio tungsten
}
D65 = 21

_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
_CODES = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d"}
_FILE_SUFFIX = ".dcp"
_WORDS = re.compile(r"[^a-z0-9]+")


class ProfileError(PhotoError):
    """A profile file that cannot be read, named."""


@dataclass(frozen=True, eq=False)
class Table:
    """A hue/saturation map or look table: `dims` (hues, saturations, values) and
    `data` (values x hues x saturations x 3: hue shift in degrees, saturation scale,
    value scale), the value axis sRGB-encoded with `encoding` 1."""

    dims: tuple
    data: np.ndarray
    encoding: int = 0


@dataclass(frozen=True, eq=False)
class Profile:
    name: str
    camera: str
    # XYZ -> camera, one or two (3 x 3); their illuminants (codes of ILLUMINANTS)
    color_matrices: tuple
    illuminants: tuple
    # White-balanced camera -> XYZ (D50), none or as many as the colour matrices
    forward_matrices: tuple = ()
    # None, one or two (one per illuminant)
    hue_sat_maps: tuple = ()
    look_table: Table | None = None
    # N x 2 (x, y) in [0, 1] on linear values; None for none
    tone_curve: np.ndarray | None = None
    exposure_offset: float = 0.0
    copyright: str = ""
    embed_policy: int = 0
    # Kept by read(): what it was read from
    path: str | None = field(default=None, compare=False)


# Reading


def _values(data, order, kind, count, offset):
    size = _SIZES[kind] * count
    raw = data[offset : offset + size]
    if len(raw) < size:
        raise ValueError("a tag's data is past the end of the file")
    if kind == 2:
        return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    if kind in (5, 10):
        code = "I" if kind == 5 else "i"
        pairs = struct.unpack(f"{order}{2 * count}{code}", raw)
        return [n / d if d else 0.0 for n, d in zip(pairs[0::2], pairs[1::2], strict=True)]
    if kind in (7,):
        return raw
    return list(struct.unpack(f"{order}{count}{_CODES[kind]}", raw))


def _tags(data):
    """{tag: values} of the file's first IFD."""
    if data[:4] == b"IIRC":
        order = "<"
    elif data[:4] == b"MMCR":
        order = ">"
    else:
        raise ValueError("not a camera profile (DCP)")
    (first,) = struct.unpack(f"{order}I", data[4:8])
    (count,) = struct.unpack(f"{order}H", data[first : first + 2])
    tags = {}
    for i in range(count):
        start = first + 2 + 12 * i
        tag, kind, number, value = struct.unpack(f"{order}HHII", data[start : start + 12])
        if kind not in _SIZES:
            continue
        inline = _SIZES[kind] * number <= 4
        offset = start + 8 if inline else value
        tags[tag] = _values(data, order, kind, number, offset)
    return tags


def _matrix(values):
    if values is None:
        return None
    if len(values) != 9:
        raise ValueError("a colour matrix that is not 3 x 3")
    matrix = np.array(values, dtype=np.float64).reshape(3, 3)
    if not np.isfinite(matrix).all():
        raise ValueError("a colour matrix that is not numbers")
    return matrix


def _table(dims, data, encoding):
    if dims is None or data is None:
        return None
    hues, sats, vals = (int(d) for d in dims)
    if hues < 1 or sats < 2 or vals < 1 or len(data) != hues * sats * vals * 3:
        raise ValueError("a hue/saturation table of the wrong size")
    array = np.array(data, dtype=np.float32).reshape(vals, hues, sats, 3)
    if not np.isfinite(array).all():
        raise ValueError("a hue/saturation table that is not numbers")
    return Table((hues, sats, vals), array, int(encoding[0]) if encoding else 0)


def parse(data, path=None):
    """A `Profile` from the bytes of a DCP. Raises ValueError for what is not one."""
    try:
        tags = _tags(data)
    except struct.error as error:
        raise ValueError(f"cut short ({error})") from error
    first = _matrix(tags.get(COLOR_MATRIX_1))
    if first is None:
        raise ValueError("no colour matrix")
    matrices = [first]
    illuminants = [int((tags.get(CALIBRATION_ILLUMINANT_1) or [D65])[0])]
    second = _matrix(tags.get(COLOR_MATRIX_2))
    if second is not None and CALIBRATION_ILLUMINANT_2 in tags:
        matrices.append(second)
        illuminants.append(int(tags[CALIBRATION_ILLUMINANT_2][0]))
    forward = [_matrix(tags.get(FORWARD_MATRIX_1)), _matrix(tags.get(FORWARD_MATRIX_2))]
    forward = tuple(m for m in forward[: len(matrices)] if m is not None)
    if len(forward) != len(matrices):
        # One for each illuminant, or none at all
        forward = ()
    encoding = tags.get(HUE_SAT_MAP_ENCODING)
    maps = [
        _table(tags.get(HUE_SAT_MAP_DIMS), tags.get(HUE_SAT_MAP_DATA_1), encoding),
        _table(tags.get(HUE_SAT_MAP_DIMS), tags.get(HUE_SAT_MAP_DATA_2), encoding),
    ]
    maps = tuple(m for m in maps[: len(matrices)] if m is not None)
    curve = tags.get(PROFILE_TONE_CURVE)
    if curve is not None:
        curve = np.array(curve, dtype=np.float64).reshape(-1, 2)
        if len(curve) < 2 or not np.isfinite(curve).all() or (np.diff(curve[:, 0]) < 0).any():
            curve = None
    offset = tags.get(BASELINE_EXPOSURE_OFFSET)
    return Profile(
        name=tags.get(PROFILE_NAME) or tags.get(UNIQUE_CAMERA_MODEL) or "Profile",
        camera=tags.get(UNIQUE_CAMERA_MODEL) or "",
        color_matrices=tuple(matrices),
        illuminants=tuple(illuminants),
        forward_matrices=forward,
        hue_sat_maps=maps,
        look_table=_table(
            tags.get(LOOK_TABLE_DIMS), tags.get(LOOK_TABLE_DATA), tags.get(LOOK_TABLE_ENCODING)
        ),
        tone_curve=curve,
        exposure_offset=float(offset[0]) if offset else 0.0,
        copyright=tags.get(PROFILE_COPYRIGHT) or "",
        embed_policy=int((tags.get(PROFILE_EMBED_POLICY) or [0])[0]),
        path=str(path) if path is not None else None,
    )


def read(path):
    """The profile in the file at `path`. Raises ProfileError naming it."""
    try:
        data = Path(path).read_bytes()
        return parse(data, path)
    except (OSError, ValueError) as error:
        raise ProfileError(path, f"cannot read the camera profile ({error})") from error


def peek(path):
    """(camera, name) of the profile at `path` without its tables, or None for a file
    that is not a profile."""
    names = {}
    try:
        with open(path, "rb") as file:
            head = file.read(8)
            order = {b"IIRC": "<", b"MMCR": ">"}.get(head[:4])
            if order is None:
                return None
            (first,) = struct.unpack(f"{order}I", head[4:8])
            file.seek(first)
            (count,) = struct.unpack(f"{order}H", file.read(2))
            entries = file.read(12 * count)
            for i in range(count):
                tag, kind, number, value = struct.unpack(
                    f"{order}HHII", entries[12 * i : 12 * i + 12]
                )
                if tag in (UNIQUE_CAMERA_MODEL, PROFILE_NAME) and kind == 2:
                    if number <= 4:
                        raw = entries[12 * i + 8 : 12 * i + 8 + number]
                    else:
                        file.seek(value)
                        raw = file.read(number)
                    names[tag] = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    except (OSError, struct.error):
        return None
    return names.get(UNIQUE_CAMERA_MODEL, ""), names.get(PROFILE_NAME) or Path(path).stem


# Writing


def _entry(order, tag, kind, values):
    if kind == 2:
        raw = values.encode() + b"\0"
        count = len(raw)
    elif kind == 10:
        pairs = []
        for value in values:
            pairs += [round(value * 10000), 10000]
        raw = struct.pack(f"{order}{len(pairs)}i", *pairs)
        count = len(values)
    else:
        raw = struct.pack(f"{order}{len(values)}{_CODES[kind]}", *values)
        count = len(values)
    return tag, kind, count, raw


def serialize(profile):
    """The bytes of a DCP holding `profile`."""
    order = "<"
    entries = [
        _entry(order, UNIQUE_CAMERA_MODEL, 2, profile.camera),
        _entry(order, PROFILE_NAME, 2, profile.name),
        _entry(order, PROFILE_EMBED_POLICY, 4, [profile.embed_policy]),
    ]
    if profile.copyright:
        entries.append(_entry(order, PROFILE_COPYRIGHT, 2, profile.copyright))
    tags_by_index = (
        (COLOR_MATRIX_1, FORWARD_MATRIX_1, CALIBRATION_ILLUMINANT_1, HUE_SAT_MAP_DATA_1),
        (COLOR_MATRIX_2, FORWARD_MATRIX_2, CALIBRATION_ILLUMINANT_2, HUE_SAT_MAP_DATA_2),
    )
    for index, matrix in enumerate(profile.color_matrices):
        colour, forward, illuminant, hue_sat = tags_by_index[index]
        entries.append(_entry(order, colour, 10, list(np.ravel(matrix))))
        entries.append(_entry(order, illuminant, 3, [profile.illuminants[index]]))
        if profile.forward_matrices:
            forward_values = list(np.ravel(profile.forward_matrices[index]))
            entries.append(_entry(order, forward, 10, forward_values))
        if profile.hue_sat_maps:
            table = profile.hue_sat_maps[index]
            entries.append(_entry(order, hue_sat, 11, list(np.ravel(table.data))))
    if profile.hue_sat_maps:
        table = profile.hue_sat_maps[0]
        entries.append(_entry(order, HUE_SAT_MAP_DIMS, 4, list(table.dims)))
        entries.append(_entry(order, HUE_SAT_MAP_ENCODING, 4, [table.encoding]))
    if profile.look_table is not None:
        table = profile.look_table
        entries.append(_entry(order, LOOK_TABLE_DIMS, 4, list(table.dims)))
        entries.append(_entry(order, LOOK_TABLE_DATA, 11, list(np.ravel(table.data))))
        entries.append(_entry(order, LOOK_TABLE_ENCODING, 4, [table.encoding]))
    if profile.tone_curve is not None:
        entries.append(_entry(order, PROFILE_TONE_CURVE, 11, list(np.ravel(profile.tone_curve))))
    if profile.exposure_offset:
        entries.append(_entry(order, BASELINE_EXPOSURE_OFFSET, 10, [profile.exposure_offset]))
    entries.sort(key=lambda entry: entry[0])
    header = 8
    ifd_size = 2 + 12 * len(entries) + 4
    data_at = header + ifd_size
    body = bytearray()
    ifd = bytearray(struct.pack(f"{order}H", len(entries)))
    for tag, kind, count, raw in entries:
        if len(raw) <= 4:
            ifd += struct.pack(f"{order}HHI", tag, kind, count) + raw.ljust(4, b"\0")
        else:
            if len(body) % 2:
                body += b"\0"
            ifd += struct.pack(f"{order}HHII", tag, kind, count, data_at + len(body))
            body += raw
    ifd += b"\0\0\0\0"
    return b"IIRC" + struct.pack(f"{order}I", header) + bytes(ifd) + bytes(body)


def write_new(path, profile):
    """Write `profile` to `path`, which must not exist yet: a file there, or one that
    appears meanwhile, is never written over (FileExistsError). Written beside it first and
    put in place by an exclusive link, so a crash leaves no half-written profile."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with open(part, "xb") as file:
            file.write(serialize(profile))
            file.flush()
            os.fsync(file.fileno())
        os.link(part, path)
    finally:
        part.unlink(missing_ok=True)
    return path


def free_name(folder, name):
    """A file name in `folder` for a profile called `name` that is not taken."""
    stem = re.sub(r"[^\w .,()+-]+", "", name).strip() or "Profile"
    candidate = Path(folder) / f"{stem}{_FILE_SUFFIX}"
    number = 2
    while candidate.exists():
        candidate = Path(folder) / f"{stem} {number}{_FILE_SUFFIX}"
        number += 1
    return candidate


# Where profiles are


def lumina_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "lumina" / "profiles"


def user_dir():
    """Where the profiles Lumina makes are written, and the user's own can be put."""
    return lumina_dir() / "user"


def store_dir():
    """The copies of the profiles edits use, by content."""
    return lumina_dir() / "store"


def default_folders():
    """The folders profiles are looked for in, besides those the user names."""
    folders = [user_dir()]
    if os.environ.get("LUMINA_PROFILES"):
        folders.insert(0, Path(os.environ["LUMINA_PROFILES"]))
    folders += [
        Path("/usr/share/rawtherapee/dcpprofiles"),
        Path("/usr/local/share/rawtherapee/dcpprofiles"),
        Path.home() / ".config" / "RawTherapee" / "dcpprofiles",
    ]
    return folders


def _key(text):
    return _WORDS.sub("", (text or "").lower())


def _aliases(folder):
    """RawTherapee's names of the same camera sold under others: {key: {keys}}."""
    found = {}
    try:
        data = json.loads((Path(folder) / "camera_model_aliases.json").read_text())
    except (OSError, ValueError):
        return found
    if isinstance(data, dict):
        for model, others in data.items():
            if isinstance(others, list):
                keys = {_key(model), *(_key(o) for o in others if isinstance(o, str))}
                for key in keys:
                    found.setdefault(key, set()).update(keys)
    return found


@dataclass(frozen=True)
class Found:
    path: str
    camera: str
    name: str


@lru_cache(maxsize=8)
def _scan(folders):
    found, aliases = [], {}
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for key, keys in _aliases(folder).items():
            aliases.setdefault(key, set()).update(keys)
        for path in sorted(folder.glob("*")):
            if path.suffix.lower() != _FILE_SUFFIX or not path.is_file():
                continue
            seen = peek(path)
            if seen is not None:
                found.append(Found(str(path), *seen))
    return tuple(found), {k: frozenset(v) for k, v in aliases.items()}


def profiles_for(make, model, folders=None):
    """The profiles for the camera exiftool called `make` `model`, in `folders` (default:
    `default_folders()`), the earlier folders first."""
    folders = tuple(str(f) for f in (default_folders() if folders is None else folders))
    found, aliases = _scan(folders)
    wanted = {_key(model), _key(f"{make or ''} {model or ''}")} - {""}
    for key in list(wanted):
        wanted |= aliases.get(key, set())
    return [profile for profile in found if _key(profile.camera) in wanted]


def forget_scans():
    """Look at the folders again next time (a profile was added)."""
    _scan.cache_clear()


# The store


def content_id(data):
    return hashlib.sha256(data).hexdigest()[:16]


def store(path, root=None):
    """Copy the profile at `path` into the store; returns (its id, the Profile). The same
    content is stored once. Raises ProfileError naming it."""
    try:
        data = Path(path).read_bytes()
        profile = parse(data, path)
    except (OSError, ValueError) as error:
        raise ProfileError(path, f"cannot read the camera profile ({error})") from error
    ident = content_id(data)
    target = Path(root or store_dir()) / f"{ident}{_FILE_SUFFIX}"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(f".{target.name}.{os.getpid()}.part")
        try:
            with open(part, "xb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(part, target)
            except FileExistsError:
                # Stored meanwhile by another process: the same content
                pass
        finally:
            part.unlink(missing_ok=True)
    return ident, profile


@lru_cache(maxsize=8)
def _stored(ident, root):
    return read(Path(root) / f"{ident}{_FILE_SUFFIX}")


def stored(ident, root=None):
    """The profile stored as `ident`, or None when it is not in the store."""
    if not re.fullmatch(r"[0-9a-f]{16}", ident or ""):
        return None
    try:
        return _stored(ident, str(root or store_dir()))
    except ProfileError:
        return None
