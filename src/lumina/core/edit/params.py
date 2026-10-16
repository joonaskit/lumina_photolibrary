"""Edit parameters: what an edit is, as a small versioned JSON document.

The rendered image is `render(original, params)`, so an edit is only these numbers.
Each document carries its schema `version`; a change to the schema bumps it and adds
a migration to `_MIGRATIONS` that turns the version before into it, so every edit
ever stored keeps rendering the same. A document from a newer Lumina is refused,
never guessed at or written back.

Values out of range are clamped and values of the wrong kind fall back to the
default, field by field: a damaged edit still renders.
"""

import hashlib
import json
from dataclasses import dataclass, field, replace

from lumina.core.edit import curves
from lumina.core.edit.lenscorrect import Correction, LensProfile
from lumina.core.edit.perspective import AMOUNT as PERSPECTIVE
from lumina.core.edit.perspective import Perspective

VERSION = 11

# Ranges, in the units the pipeline uses
EXPOSURE = (-5.0, 5.0)  # stops
WARMTH = (-100.0, 100.0)  # see pipeline.white_balance_gains
BASIC = (-100.0, 100.0)  # contrast, highlights, shadows, whites, blacks: see basic.py
PRESENCE = (-100.0, 100.0)  # texture, clarity, dehaze: see detail.py and dehaze.py
SHARPEN_AMOUNT = (0.0, 150.0)  # see sharpen.py
SHARPEN_RADIUS = (0.5, 3.0)  # pixels at full size
DEFAULT_SHARPEN_RADIUS = 1.0
DEFAULT_SHARPEN_DETAIL = 25.0
DEFAULT_NOISE_DETAIL = 50.0
COLOUR = (-100.0, 100.0)  # vibrance, saturation: see colour.py
HSL = (-100.0, 100.0)  # a band's hue, saturation, luminance: see hsl.py
HUE = (0.0, 360.0)  # degrees: a grading wheel's hue
WHEEL = (0.0, 100.0)  # a grading wheel's saturation
GRADING = (-100.0, 100.0)  # a grading wheel's luminance, and the balance
BLENDING = (0.0, 100.0)  # how softly the three ranges meet: see grading.py
DEFAULT_BLENDING = 50.0
VIGNETTE = (-100.0, 100.0)  # negative darkens the corners, positive lightens them
PERCENT = (0.0, 100.0)  # vignette midpoint and feather, grain amount and size
DEFAULT_MIDPOINT = 50.0
DEFAULT_FEATHER = 50.0
DEFAULT_GRAIN_SIZE = 25.0
MATTE_BLACK = (0.0, 0.5)  # how far black is raised, as a share of white
MATTE_WHITE = (0.0, 0.5)  # how far white is lowered
DESATURATION = (0.0, 1.0)
STRAIGHTEN = (-45.0, 45.0)  # degrees, clockwise
LENS = (0.0, 100.0)  # how much of a lens correction to make: see lenscorrect.py
CHANNELS = ("rgb", "red", "green", "blue")
BASIC_FIELDS = ("contrast", "highlights", "shadows", "whites", "blacks")
PRESENCE_FIELDS = ("texture", "clarity", "dehaze")
COLOUR_FIELDS = ("vibrance", "saturation")
# From red round the hue circle: the HSL panel's eight bands
BANDS = ("red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta")
BAND_FIELDS = ("hue", "saturation", "luminance")
RANGES = ("shadows", "midtones", "highlights")
WHEEL_FIELDS = ("hue", "saturation", "luminance")


class NewerParams(Exception):
    """The edit was made by a newer Lumina: this one can't render it as meant."""


@dataclass(frozen=True)
class Basic:
    # Lightroom's Basic tone controls, -100 to 100 each
    contrast: float = 0.0
    highlights: float = 0.0
    shadows: float = 0.0
    whites: float = 0.0
    blacks: float = 0.0

    def is_identity(self):
        return self == Basic()


@dataclass(frozen=True)
class Presence:
    # How much the photo's small and medium detail stands out, and how clear the air is,
    # -100 to 100 each: texture (fine detail), clarity (local contrast at a larger size),
    # dehaze (less, or more, haze)
    texture: float = 0.0
    clarity: float = 0.0
    dehaze: float = 0.0

    def is_identity(self):
        return self == Presence()


@dataclass(frozen=True)
class Sharpening:
    # Unsharp mask on the luminance. Amount; radius in pixels of the full-size photo;
    # detail (0: halos held down, 100: the plain mask, which keeps fine detail); masking
    # (0: everywhere, 100: only on the strongest edges, not on noise and flat areas)
    amount: float = 0.0
    radius: float = DEFAULT_SHARPEN_RADIUS
    detail: float = DEFAULT_SHARPEN_DETAIL
    masking: float = 0.0

    def is_identity(self):
        # With no amount there is no sharpening, whatever the rest
        return self.amount == 0


@dataclass(frozen=True)
class NoiseReduction:
    # How much of the brightness noise and of the colour noise to take out, 0 to 100,
    # and how much fine detail the brightness noise reduction keeps, 0 to 100
    luminance: float = 0.0
    detail: float = DEFAULT_NOISE_DETAIL
    colour: float = 0.0

    def is_identity(self):
        return self.luminance == 0 and self.colour == 0


@dataclass(frozen=True)
class Colour:
    # How colourful the photo is, -100 to 100 each. Vibrance leaves colours that are
    # already strong, and skin tones, more alone than saturation does
    vibrance: float = 0.0
    saturation: float = 0.0

    def is_identity(self):
        return self == Colour()


@dataclass(frozen=True)
class Band:
    # One colour band of the HSL panel, -100 to 100 each: hue moves the colour towards
    # its neighbours on the hue circle, saturation and luminance as they say
    hue: float = 0.0
    saturation: float = 0.0
    luminance: float = 0.0

    def is_identity(self):
        return self == Band()


@dataclass(frozen=True)
class Hsl:
    red: Band = field(default_factory=Band)
    orange: Band = field(default_factory=Band)
    yellow: Band = field(default_factory=Band)
    green: Band = field(default_factory=Band)
    aqua: Band = field(default_factory=Band)
    blue: Band = field(default_factory=Band)
    purple: Band = field(default_factory=Band)
    magenta: Band = field(default_factory=Band)

    def is_identity(self):
        return self == Hsl()


@dataclass(frozen=True)
class Wheel:
    # A colour grading wheel: the tint's hue (degrees) and strength (0 to 100), and
    # how much lighter or darker its range gets (-100 to 100)
    hue: float = 0.0
    saturation: float = 0.0
    luminance: float = 0.0

    def is_identity(self):
        # With no strength there is no tint, whatever the hue
        return self.saturation == 0 and self.luminance == 0


@dataclass(frozen=True)
class Grading:
    shadows: Wheel = field(default_factory=Wheel)
    midtones: Wheel = field(default_factory=Wheel)
    highlights: Wheel = field(default_factory=Wheel)
    # How softly the ranges meet, and where: positive gives more of the photo to the
    # highlights, negative to the shadows
    blending: float = DEFAULT_BLENDING
    balance: float = 0.0

    def is_identity(self):
        return all(getattr(self, name).is_identity() for name in RANGES)


@dataclass(frozen=True)
class Vignette:
    # Darkening (negative) or lightening (positive) of the corners of the cropped photo.
    # Midpoint: how far from the centre it starts (higher is further out); feather: how
    # gradually it comes in
    amount: float = 0.0
    midpoint: float = DEFAULT_MIDPOINT
    feather: float = DEFAULT_FEATHER

    def is_identity(self):
        # With no amount there is no vignette, whatever the rest
        return self.amount == 0


@dataclass(frozen=True)
class Grain:
    # Film grain: how strong, and how coarse (relative to the size of the photo, so a
    # small render looks like a large one)
    amount: float = 0.0
    size: float = DEFAULT_GRAIN_SIZE

    def is_identity(self):
        return self.amount == 0


@dataclass(frozen=True)
class Curves:
    # Points (x, y) in [0, 1]: "rgb" for all channels, then each channel on its own
    rgb: tuple = curves.IDENTITY
    red: tuple = curves.IDENTITY
    green: tuple = curves.IDENTITY
    blue: tuple = curves.IDENTITY

    def __post_init__(self):
        # However they were given, as the same floats: equal curves, equal JSON
        for name in CHANNELS:
            object.__setattr__(self, name, curves.normalize(getattr(self, name)))


@dataclass(frozen=True)
class Matte:
    black: float = 0.0
    white: float = 0.0
    shadow_desaturation: float = 0.0


@dataclass(frozen=True)
class ColourProfile:
    # How a RAW file's camera colours become the photo's (colour_profile.py): "libraw"
    # (LibRaw's matrix, as before profiles), "standard" (Lumina's look) or a stored camera
    # profile's id (dcp.store); `name` is for showing it. A photo in sRGB has no use for it
    id: str = "libraw"
    name: str = ""


@dataclass(frozen=True)
class Lens:
    # The lens's corrections (lenscorrect.py): off, or on with the lens's `profile` (the
    # numbers for this focal length and aperture, kept here so the edit renders the same
    # whatever the lens database says later) and how much of each to make, 0 to 100
    enabled: bool = False
    distortion: float = 100.0
    vignetting: float = 100.0
    aberration: float = 100.0
    profile: LensProfile | None = None

    def is_identity(self):
        # Nothing to correct with, or nothing asked for
        return not self.enabled or self.profile is None

    def correction(self):
        """The `lenscorrect.Correction` to make, or None for none."""
        if self.is_identity():
            return None
        correction = Correction(
            self.profile, self.distortion / 100, self.vignetting / 100, self.aberration / 100
        )
        return None if correction.is_identity() else correction


@dataclass(frozen=True)
class Crop:
    # Of the photo after the quarter turns, as shares of its width and height
    left: float = 0.0
    top: float = 0.0
    right: float = 1.0
    bottom: float = 1.0

    def __post_init__(self):
        # Dragged and turned crops collect float noise: equal crops, equal JSON
        for name in ("left", "top", "right", "bottom"):
            object.__setattr__(self, name, round(float(getattr(self, name)), 6))


@dataclass(frozen=True)
class Params:
    exposure: float = 0.0
    temperature: float = 0.0
    tint: float = 0.0
    basic: Basic = field(default_factory=Basic)
    presence: Presence = field(default_factory=Presence)
    sharpening: Sharpening = field(default_factory=Sharpening)
    noise: NoiseReduction = field(default_factory=NoiseReduction)
    colour: Colour = field(default_factory=Colour)
    hsl: Hsl = field(default_factory=Hsl)
    grading: Grading = field(default_factory=Grading)
    curves: Curves = field(default_factory=Curves)
    matte: Matte = field(default_factory=Matte)
    vignette: Vignette = field(default_factory=Vignette)
    grain: Grain = field(default_factory=Grain)
    lens: Lens = field(default_factory=Lens)
    profile: ColourProfile = field(default_factory=ColourProfile)
    # Quarter turns clockwise, 0 to 3; then mirrored left to right and top to bottom;
    # then corrected for perspective (see perspective.py) and turned by `straighten` degrees
    # clockwise, scaled to fill the frame; then cropped
    rotate: int = 0
    flip_horizontal: bool = False
    flip_vertical: bool = False
    perspective: Perspective = field(default_factory=Perspective)
    straighten: float = 0.0
    crop: Crop = field(default_factory=Crop)

    def is_identity(self):
        return self == Params()

    def to_dict(self):
        return {
            "version": VERSION,
            "exposure": float(self.exposure),
            "white_balance": {"temperature": float(self.temperature), "tint": float(self.tint)},
            "basic": {name: float(getattr(self.basic, name)) for name in BASIC_FIELDS},
            "presence": {name: float(getattr(self.presence, name)) for name in PRESENCE_FIELDS},
            "sharpening": {
                "amount": float(self.sharpening.amount),
                "radius": float(self.sharpening.radius),
                "detail": float(self.sharpening.detail),
                "masking": float(self.sharpening.masking),
            },
            "noise": {
                "luminance": float(self.noise.luminance),
                "detail": float(self.noise.detail),
                "colour": float(self.noise.colour),
            },
            "colour": {name: float(getattr(self.colour, name)) for name in COLOUR_FIELDS},
            "hsl": {
                band: {name: float(getattr(getattr(self.hsl, band), name)) for name in BAND_FIELDS}
                for band in BANDS
            },
            "grading": {
                **{
                    name: {
                        field_: float(getattr(getattr(self.grading, name), field_))
                        for field_ in WHEEL_FIELDS
                    }
                    for name in RANGES
                },
                "blending": float(self.grading.blending),
                "balance": float(self.grading.balance),
            },
            "curves": {name: [list(p) for p in getattr(self.curves, name)] for name in CHANNELS},
            "matte": {
                "black": float(self.matte.black),
                "white": float(self.matte.white),
                "shadow_desaturation": float(self.matte.shadow_desaturation),
            },
            "vignette": {
                "amount": float(self.vignette.amount),
                "midpoint": float(self.vignette.midpoint),
                "feather": float(self.vignette.feather),
            },
            "grain": {"amount": float(self.grain.amount), "size": float(self.grain.size)},
            "lens": {
                "enabled": self.lens.enabled,
                "distortion": float(self.lens.distortion),
                "vignetting": float(self.lens.vignetting),
                "aberration": float(self.lens.aberration),
                "profile": None if self.lens.profile is None else self.lens.profile.to_dict(),
            },
            "profile": {"id": self.profile.id, "name": self.profile.name},
            "rotate": self.rotate,
            "flip": {"horizontal": self.flip_horizontal, "vertical": self.flip_vertical},
            "perspective": {
                "vertical": float(self.perspective.vertical),
                "horizontal": float(self.perspective.horizontal),
            },
            "straighten": float(self.straighten),
            "crop": {
                "left": float(self.crop.left),
                "top": float(self.crop.top),
                "right": float(self.crop.right),
                "bottom": float(self.crop.bottom),
            },
        }

    def to_json(self):
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    def fingerprint(self):
        """A short hash of what the edit looks like: for caches of rendered images."""
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

    def with_changes(self, **changes):
        return replace(self, **changes)

    # Turning and mirroring the result: the edit that shows what this one shows, turned
    # or mirrored as a whole, crop and all

    def turned(self, quarters):
        """This edit with the photo turned `quarters` quarter turns clockwise (negative:
        counter-clockwise), the crop turning with it."""
        params = self
        for _ in range(quarters % 4):
            c = params.crop
            params = replace(
                params,
                rotate=(params.rotate + 1) % 4,
                # Mirroring then turning is turning then mirroring the other way
                flip_horizontal=params.flip_vertical,
                flip_vertical=params.flip_horizontal,
                perspective=params.perspective.turned(),
                crop=Crop(1 - c.bottom, c.left, 1 - c.top, c.right),
            )
        return params

    def flipped(self, horizontal):
        """This edit mirrored left to right (`horizontal`) or top to bottom. The crop is
        mirrored, and straightening turns the other way."""
        c = self.crop
        if horizontal:
            return replace(
                self,
                flip_horizontal=not self.flip_horizontal,
                straighten=-self.straighten,
                perspective=self.perspective.flipped(True),
                crop=Crop(1 - c.right, c.top, 1 - c.left, c.bottom),
            )
        return replace(
            self,
            flip_vertical=not self.flip_vertical,
            straighten=-self.straighten,
            perspective=self.perspective.flipped(False),
            crop=Crop(c.left, 1 - c.bottom, c.right, 1 - c.top),
        )


def from_json(text):
    """Params from stored JSON. Raises NewerParams for a newer schema; anything else
    unreadable gives the defaults."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return Params()
    return from_dict(data)


def from_dict(data):
    if not isinstance(data, dict):
        return Params()
    data = migrate(data)
    wb = _section(data, "white_balance")
    basic = _section(data, "basic")
    presence = _section(data, "presence")
    sharpening = _section(data, "sharpening")
    noise = _section(data, "noise")
    colour = _section(data, "colour")
    hsl = _section(data, "hsl")
    grading = _section(data, "grading")
    vignette = _section(data, "vignette")
    grain = _section(data, "grain")
    lens = _section(data, "lens")
    profile = _section(data, "profile")
    curve_data = _section(data, "curves")
    matte = _section(data, "matte")
    crop = _section(data, "crop")
    left = _number(crop.get("left"), 0.0, (0.0, 1.0))
    top = _number(crop.get("top"), 0.0, (0.0, 1.0))
    right = _number(crop.get("right"), 1.0, (0.0, 1.0))
    bottom = _number(crop.get("bottom"), 1.0, (0.0, 1.0))
    if right - left < 0.01 or bottom - top < 0.01:
        # Nothing, or almost nothing, left: the crop is dropped, not the photo
        left, top, right, bottom = 0.0, 0.0, 1.0, 1.0
    rotate = data.get("rotate", 0)
    flip = _section(data, "flip")
    perspective = _section(data, "perspective")
    return Params(
        exposure=_number(data.get("exposure"), 0.0, EXPOSURE),
        temperature=_number(wb.get("temperature"), 0.0, WARMTH),
        tint=_number(wb.get("tint"), 0.0, WARMTH),
        basic=Basic(**{name: _number(basic.get(name), 0.0, BASIC) for name in BASIC_FIELDS}),
        presence=Presence(
            **{name: _number(presence.get(name), 0.0, PRESENCE) for name in PRESENCE_FIELDS}
        ),
        sharpening=Sharpening(
            amount=_number(sharpening.get("amount"), 0.0, SHARPEN_AMOUNT),
            radius=_number(sharpening.get("radius"), DEFAULT_SHARPEN_RADIUS, SHARPEN_RADIUS),
            detail=_number(sharpening.get("detail"), DEFAULT_SHARPEN_DETAIL, PERCENT),
            masking=_number(sharpening.get("masking"), 0.0, PERCENT),
        ),
        noise=NoiseReduction(
            luminance=_number(noise.get("luminance"), 0.0, PERCENT),
            detail=_number(noise.get("detail"), DEFAULT_NOISE_DETAIL, PERCENT),
            colour=_number(noise.get("colour"), 0.0, PERCENT),
        ),
        colour=Colour(**{name: _number(colour.get(name), 0.0, COLOUR) for name in COLOUR_FIELDS}),
        hsl=Hsl(
            **{
                band: Band(
                    **{
                        name: _number(_section(hsl, band).get(name), 0.0, HSL)
                        for name in BAND_FIELDS
                    }
                )
                for band in BANDS
            }
        ),
        grading=Grading(
            **{
                name: Wheel(
                    hue=_number(_section(grading, name).get("hue"), 0.0, HUE),
                    saturation=_number(_section(grading, name).get("saturation"), 0.0, WHEEL),
                    luminance=_number(_section(grading, name).get("luminance"), 0.0, GRADING),
                )
                for name in RANGES
            },
            blending=_number(grading.get("blending"), DEFAULT_BLENDING, BLENDING),
            balance=_number(grading.get("balance"), 0.0, GRADING),
        ),
        curves=Curves(**{name: curves.normalize(curve_data.get(name)) for name in CHANNELS}),
        matte=Matte(
            black=_number(matte.get("black"), 0.0, MATTE_BLACK),
            white=_number(matte.get("white"), 0.0, MATTE_WHITE),
            shadow_desaturation=_number(matte.get("shadow_desaturation"), 0.0, DESATURATION),
        ),
        vignette=Vignette(
            amount=_number(vignette.get("amount"), 0.0, VIGNETTE),
            midpoint=_number(vignette.get("midpoint"), DEFAULT_MIDPOINT, PERCENT),
            feather=_number(vignette.get("feather"), DEFAULT_FEATHER, PERCENT),
        ),
        grain=Grain(
            amount=_number(grain.get("amount"), 0.0, PERCENT),
            size=_number(grain.get("size"), DEFAULT_GRAIN_SIZE, PERCENT),
        ),
        lens=Lens(
            enabled=lens.get("enabled") is True,
            distortion=_number(lens.get("distortion"), 100.0, LENS),
            vignetting=_number(lens.get("vignetting"), 100.0, LENS),
            aberration=_number(lens.get("aberration"), 100.0, LENS),
            profile=LensProfile.from_dict(lens.get("profile")),
        ),
        profile=ColourProfile(
            id=_profile_id(profile.get("id")),
            name=profile.get("name") if isinstance(profile.get("name"), str) else "",
        ),
        rotate=rotate % 4 if isinstance(rotate, int) and not isinstance(rotate, bool) else 0,
        flip_horizontal=flip.get("horizontal") is True,
        flip_vertical=flip.get("vertical") is True,
        perspective=Perspective(
            vertical=_number(perspective.get("vertical"), 0.0, PERSPECTIVE),
            horizontal=_number(perspective.get("horizontal"), 0.0, PERSPECTIVE),
        ),
        straighten=_number(data.get("straighten"), 0.0, STRAIGHTEN),
        crop=Crop(left, top, right, bottom),
    )


# Version n -> the function turning a version-n document into version n + 1. Append
# one with every schema change, and never change one that was released.
def _to_2(data):
    # Version 2 added mirroring and straightening: none, so it looks as it did. Only
    # added where missing: a migration fills in, it never overwrites
    return {"flip": {"horizontal": False, "vertical": False}, "straighten": 0.0, **data}


def _to_3(data):
    # Version 3 added the Basic tone controls: all at zero, which changes nothing
    return {"basic": {name: 0.0 for name in BASIC_FIELDS}, **data}


def _to_4(data):
    # Version 4 added vibrance and saturation: both at zero, which changes nothing
    return {"colour": {name: 0.0 for name in COLOUR_FIELDS}, **data}


def _to_5(data):
    # Version 5 added the HSL bands and colour grading: all at zero, which changes nothing
    return {
        "hsl": {band: {name: 0.0 for name in BAND_FIELDS} for band in BANDS},
        "grading": {
            **{name: {field_: 0.0 for field_ in WHEEL_FIELDS} for name in RANGES},
            "blending": DEFAULT_BLENDING,
            "balance": 0.0,
        },
        **data,
    }


def _to_6(data):
    # Version 6 added the vignette and grain: both off, which changes nothing
    return {
        "vignette": {"amount": 0.0, "midpoint": DEFAULT_MIDPOINT, "feather": DEFAULT_FEATHER},
        "grain": {"amount": 0.0, "size": DEFAULT_GRAIN_SIZE},
        **data,
    }


def _to_7(data):
    # Version 7 added texture, clarity and dehaze: all at zero, which changes nothing
    return {"presence": {name: 0.0 for name in PRESENCE_FIELDS}, **data}


def _to_8(data):
    # Version 8 added sharpening and noise reduction: both off, which changes nothing
    return {
        "sharpening": {
            "amount": 0.0,
            "radius": DEFAULT_SHARPEN_RADIUS,
            "detail": DEFAULT_SHARPEN_DETAIL,
            "masking": 0.0,
        },
        "noise": {"luminance": 0.0, "detail": DEFAULT_NOISE_DETAIL, "colour": 0.0},
        **data,
    }


def _to_9(data):
    # Version 9 added lens corrections: off, as every edit before it was made without
    return {
        "lens": {
            "enabled": False,
            "distortion": 100.0,
            "vignetting": 100.0,
            "aberration": 100.0,
            "profile": None,
        },
        **data,
    }


def _to_10(data):
    # Version 10 added camera profiles: LibRaw's colours, which every edit before had
    return {"profile": {"id": "libraw", "name": ""}, **data}


def _to_11(data):
    # Version 11 added perspective correction: none, so it looks as it did
    return {"perspective": {"vertical": 0.0, "horizontal": 0.0}, **data}


_MIGRATIONS = {
    1: _to_2,
    2: _to_3,
    3: _to_4,
    4: _to_5,
    5: _to_6,
    6: _to_7,
    7: _to_8,
    8: _to_9,
    9: _to_10,
    10: _to_11,
}


def migrate(data):
    """`data` brought to the current version. Raises NewerParams for a newer one."""
    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        version = 1
    if version > VERSION:
        raise NewerParams(f"edit schema {version}, this Lumina knows {VERSION}")
    data = dict(data)
    while version < VERSION:
        data = _MIGRATIONS[version](data)
        version += 1
        data["version"] = version
    return data


def _profile_id(value):
    # A built-in name or a store id; anything else is LibRaw's, which always renders
    if value in ("libraw", "standard"):
        return value
    if isinstance(value, str) and len(value) == 16 and all(c in "0123456789abcdef" for c in value):
        return value
    return "libraw"


def _section(data, name):
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _number(value, default, bounds):
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    if value != value:  # NaN
        return default
    low, high = bounds
    return float(min(max(value, low), high))


# Presets: the sections of an edit, saved apart and applied over another

SECTIONS = (
    "exposure",
    "white_balance",
    "basic",
    "presence",
    "sharpening",
    "noise",
    "colour",
    "hsl",
    "grading",
    "curves",
    "matte",
    "vignette",
    "grain",
    "rotate",
    "flip",
    "perspective",
    "straighten",
    "crop",
)


def preset(params, sections):
    """The `sections` of `params` (names from SECTIONS) as a preset document."""
    whole = params.to_dict()
    unknown = set(sections) - set(SECTIONS)
    if unknown:
        raise ValueError(f"no such sections: {sorted(unknown)}")
    return {"version": VERSION, **{name: whole[name] for name in sections}}


def pasted(edit_json, document):
    """A stored edit (`edit_json`, or None for none) with the sections a settings
    `document` (made by `preset`) has put over it, as JSON, or None when what is left
    changes nothing. Raises NewerParams for an edit or a document from a newer Lumina."""
    current = from_json(edit_json) if edit_json else Params()
    result = apply_preset(current, document)
    return None if result.is_identity() else result.to_json()


def apply_preset(params, document):
    """`params` with what a preset document has replacing those sections. Raises
    NewerParams for a preset from a newer Lumina."""
    if not isinstance(document, dict):
        return params
    document = migrate(document)
    whole = params.to_dict()
    for name in SECTIONS:
        if name in document:
            whole[name] = document[name]
    return from_dict(whole)
