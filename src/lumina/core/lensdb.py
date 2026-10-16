"""The lensfun database: which lens took a photo, and how to correct it.

lensfun (https://lensfun.github.io/) keeps measured corrections for thousands of lenses
as XML (LGPL code, CC-BY-SA data). We do not link its library: the files are read here
and the few formulas are in `edit/lenscorrect.py`. The data is not shipped with Lumina;
it is the distribution's (`dnf install lensfun`, `/usr/share/lensfun`), or a newer copy
the user fetched with `lensfun-update-data` (`~/.local/share/lensfun`), or a folder
named by $LUMINA_LENSFUN.

A photo's camera and lens are matched from what exiftool read (`Shot`). The result is a
`LensProfile`: the numbers for that lens at that focal length and aperture, which the
edit then carries with it, so that it renders the same whatever the database says later.

Runs in the worker processes (a load parses a few megabytes of XML); no Qt.
"""

import difflib
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from lumina.core.edit.lenscorrect import LensProfile

# A lens whose name is at least this like the photo's is the lens
MATCH_RATIO = 0.85
# Lenses calibrated on a sensor this much smaller than the camera's cover its photos' corners
# only if they are not: the calibration must be of a frame at least as large
CROP_TOLERANCE = 1.01
# Where the focus was, in metres, when it is not known: far away
DEFAULT_DISTANCE = 1000.0
_WORDS = re.compile(r"[^a-z0-9]+")
# lensfun's mount names for the lenses that are not for a mount of their own
_RECTILINEAR = "rectilinear"


@dataclass(frozen=True)
class Shot:
    """What a photo says of how it was taken. Any may be None."""

    make: str | None = None
    model: str | None = None
    lens: str | None = None
    focal_length: float | None = None
    aperture: float | None = None
    distance: float | None = None


@dataclass
class Camera:
    maker: str
    models: list
    mount: str
    crop_factor: float


@dataclass
class Lens:
    maker: str
    model: str
    mounts: list
    crop_factor: float
    kind: str = _RECTILINEAR
    # Calibrations: (focal, ...) rows in lensfun's terms
    distortion: list = field(default_factory=list)  # (focal, model, terms)
    tca: list = field(default_factory=list)  # (focal, model, terms: vr, vb, cr, cb, br, bb)
    vignetting: list = field(default_factory=list)  # (focal, aperture, distance, terms)

    def focal_range(self):
        focals = [row[0] for rows in (self.distortion, self.tca, self.vignetting) for row in rows]
        return (min(focals), max(focals)) if focals else None


def _key(text):
    return _WORDS.sub("", (text or "").lower())


def data_dirs():
    """The folders of lensfun XML there are, the most wanted first."""
    home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    roots = []
    if os.environ.get("LUMINA_LENSFUN"):
        roots.append(Path(os.environ["LUMINA_LENSFUN"]))
    roots += [
        home / "lensfun" / "updates",
        home / "lensfun",
        Path("/var/lib/lensfun-updates"),
        Path("/usr/local/share/lensfun"),
        Path("/usr/share/lensfun"),
    ]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        # The newest format first: a root holds version_1, version_2... or the files
        versions = sorted(root.glob("version_*"), reverse=True)
        for folder in [*versions, root]:
            if folder.is_dir() and any(folder.glob("*.xml")) and folder not in found:
                found.append(folder)
    return found


class Database:
    def __init__(self, cameras, lenses, compatible):
        self.cameras = cameras
        self.lenses = lenses
        # mount -> the mounts whose lenses it takes (its own included)
        self._compatible = compatible

    def __len__(self):
        return len(self.lenses)

    def camera(self, make, model):
        """The camera exiftool called `make` `model`, or None."""
        wanted = _key(model)
        if not wanted:
            return None
        maker = _key(make)
        best = None
        for camera in self.cameras:
            names = {_key(name) for name in camera.models}
            names |= {_key(camera.maker + name) for name in camera.models}
            if wanted not in names:
                continue
            if best is None or (maker and maker in _key(camera.maker)):
                best = camera
        return best

    def mounts_for(self, camera):
        return self._compatible.get(camera.mount, {camera.mount})

    def lenses_for(self, camera):
        """The lenses that fit `camera` and that a correction is known for, by name."""
        mounts = self.mounts_for(camera)
        return sorted(
            (
                lens
                for lens in self.lenses
                if lens.kind == _RECTILINEAR
                and lens.crop_factor <= camera.crop_factor * CROP_TOLERANCE
                and mounts & set(lens.mounts)
                and (lens.distortion or lens.tca or lens.vignetting)
            ),
            key=lambda lens: lens.model.lower(),
        )

    def find(self, shot):
        """(camera, lens) the photo `shot` was taken with, or None when the camera or the
        lens is not known."""
        camera = self.camera(shot.make, shot.model)
        if camera is None or not shot.lens:
            return None
        wanted = _key(shot.lens)
        best, best_score = None, 0.0
        for lens in self.lenses_for(camera):
            names = {_key(lens.model), _key(lens.model.removeprefix(lens.maker).strip())}
            ratio = max(difflib.SequenceMatcher(None, wanted, name).ratio() for name in names)
            if ratio < MATCH_RATIO:
                continue
            span = lens.focal_range()
            if (
                shot.focal_length
                and span
                and not span[0] * 0.95 <= shot.focal_length <= span[1] * 1.05
            ):
                continue
            # The best name; then the calibration on the sensor nearest the camera's
            score = ratio - 0.01 * abs(camera.crop_factor - lens.crop_factor)
            if score > best_score:
                best, best_score = lens, score
        return (camera, best) if best is not None else None

    def lens_named(self, camera, name):
        for lens in self.lenses_for(camera):
            if lens.model == name:
                return lens
        return None

    def profile(self, camera, lens, shot):
        """The `LensProfile` of `lens` on `camera` for the focal length and aperture of
        `shot`."""
        focal = shot.focal_length or (lens.focal_range() or (0.0, 0.0))[0]
        aperture = shot.aperture or 0.0
        distance = shot.distance or DEFAULT_DISTANCE
        return LensProfile(
            name=lens.model,
            scale=lens.crop_factor / camera.crop_factor,
            distortion=_distortion(lens.distortion, focal),
            tca=_tca(lens.tca, focal),
            vignetting=_vignetting(lens.vignetting, focal, aperture, distance),
        )


# Interpolation: between the focal lengths a lens was measured at


def _bracket(rows, focal):
    """The rows (sorted by focal) either side of `focal`, and the share of the way
    between them; the nearest one alone beyond the ends."""
    rows = sorted(rows, key=lambda row: row[0])
    if focal <= rows[0][0]:
        return rows[0], rows[0], 0.0
    if focal >= rows[-1][0]:
        return rows[-1], rows[-1], 0.0
    for low, high in zip(rows, rows[1:], strict=False):
        if low[0] <= focal <= high[0]:
            span = high[0] - low[0]
            return low, high, (focal - low[0]) / span if span else 0.0
    return rows[-1], rows[-1], 0.0


def _mix(low, high, share):
    return tuple(a + (b - a) * share for a, b in zip(low, high, strict=True))


def _distortion(rows, focal):
    if not rows:
        return None
    # One model at a time: the one the lens has most of
    models = [row[1] for row in rows]
    chosen = max(set(models), key=models.count)
    low, high, share = _bracket([row for row in rows if row[1] == chosen], focal)
    return (chosen, *_mix(low[2], high[2], share))


def _tca(rows, focal):
    if not rows:
        return None
    low, high, share = _bracket(rows, focal)
    return _mix(low[2], high[2], share)


def _vignetting(rows, focal, aperture, distance):
    """The vignetting terms at `focal`, `aperture` and `distance`: from the measurements at
    the distance nearest (in 1/m) to where the focus was, the two nearest in stops of focal
    length and aperture, mixed by how near they are."""
    if not rows:
        return None
    wanted = 1 / max(distance, 0.01)
    nearest = min(abs(1 / max(row[2], 0.01) - wanted) for row in rows)
    rows = [row for row in rows if abs(1 / max(row[2], 0.01) - wanted) <= nearest + 1e-9]

    def far(row):
        d_focal = math.log2(max(row[0], 0.1) / max(focal, 0.1))
        d_aperture = 2 * math.log2(max(row[1], 0.1) / max(aperture, 0.1)) if aperture else 0.0
        return math.hypot(d_focal, d_aperture)

    near = sorted(rows, key=far)[:2]
    first = far(near[0])
    if first < 1e-6 or len(near) == 1:
        return tuple(near[0][3])
    share = first / (first + far(near[1]))
    return _mix(near[0][3], near[1][3], share)


# Reading the files


def _text(element, tag):
    """The text of the first `tag` under `element` that is not a translation."""
    for child in element.findall(tag):
        if child.get("lang") is None and child.text:
            return child.text.strip()
    child = element.find(tag)
    return child.text.strip() if child is not None and child.text else ""


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _terms(element, names):
    values = [_float(element.get(name), 0.0) for name in names]
    return tuple(values)


_DISTORTION_TERMS = {"ptlens": ("a", "b", "c"), "poly3": ("k1",), "poly5": ("k1", "k2")}


def _read_lens(element):
    lens = Lens(
        maker=_text(element, "maker"),
        model=_text(element, "model"),
        mounts=[m.text.strip() for m in element.findall("mount") if m.text],
        crop_factor=_float(_text(element, "cropfactor"), 1.0),
        kind=_text(element, "type") or _RECTILINEAR,
    )
    calibration = element.find("calibration")
    if calibration is None:
        return lens
    for row in calibration.findall("distortion"):
        model, focal = row.get("model"), _float(row.get("focal"))
        if model in _DISTORTION_TERMS and focal is not None:
            lens.distortion.append((focal, model, _terms(row, _DISTORTION_TERMS[model])))
    for row in calibration.findall("tca"):
        focal = _float(row.get("focal"))
        if focal is None:
            continue
        if row.get("model") == "poly3":
            lens.tca.append((focal, "poly3", _terms(row, ("vr", "vb", "cr", "cb", "br", "bb"))))
        elif row.get("model") == "linear":
            kr, kb = _float(row.get("kr"), 1.0), _float(row.get("kb"), 1.0)
            lens.tca.append((focal, "poly3", (kr, kb, 0.0, 0.0, 0.0, 0.0)))
    for row in calibration.findall("vignetting"):
        if row.get("model") != "pa":
            continue
        focal, aperture = _float(row.get("focal")), _float(row.get("aperture"))
        if focal is None or aperture is None:
            continue
        distance = _float(row.get("distance"), DEFAULT_DISTANCE)
        lens.vignetting.append((focal, aperture, distance, _terms(row, ("k1", "k2", "k3"))))
    return lens


def read_files(paths):
    """A `Database` from lensfun XML files (a file that is not readable is left out). The
    earlier a file is in `paths`, the more it counts: a camera or lens in two is the
    earlier one's."""
    cameras, lenses, compatible = {}, {}, {}
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        for element in root.findall("mount"):
            name = _text(element, "name")
            if name:
                compatible.setdefault(name, {name}).update(
                    c.text.strip() for c in element.findall("compat") if c.text
                )
        for element in root.findall("camera"):
            models = [m.text.strip() for m in element.findall("model") if m.text]
            camera = Camera(
                _text(element, "maker"),
                models,
                _text(element, "mount"),
                _float(_text(element, "cropfactor"), 1.0),
            )
            cameras.setdefault((_key(camera.maker), _key(models[0] if models else "")), camera)
        for element in root.findall("lens"):
            lens = _read_lens(element)
            if lens.model:
                lenses.setdefault((_key(lens.maker), _key(lens.model), lens.crop_factor), lens)
    return Database(list(cameras.values()), list(lenses.values()), compatible)


@lru_cache(maxsize=2)
def _load(folders):
    paths = [path for folder in folders for path in sorted(Path(folder).glob("*.xml"))]
    return read_files(paths)


def load(folders=None):
    """The database in `folders` (default: where lensfun's data is found), read once per
    process. Empty when there is none."""
    folders = tuple(str(folder) for folder in (data_dirs() if folders is None else folders))
    return _load(folders)


def match(shot, name=None, database=None):
    """The `LensProfile` for the lens that took `shot`, or the lens called `name` (as
    `lens_names` gave it) on the same camera; None when the camera or the lens is not in
    the database."""
    database = database if database is not None else load()
    camera = database.camera(shot.make, shot.model)
    if camera is None:
        return None
    if name is not None:
        lens = database.lens_named(camera, name)
        return database.profile(camera, lens, shot) if lens is not None else None
    found = database.find(shot)
    return database.profile(*found, shot) if found is not None else None


def lens_names(shot, database=None):
    """The names of the lenses that fit the camera of `shot`: to choose another lens from."""
    database = database if database is not None else load()
    camera = database.camera(shot.make, shot.model)
    return [lens.model for lens in database.lenses_for(camera)] if camera is not None else []
