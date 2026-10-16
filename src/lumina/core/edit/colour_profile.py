"""From the camera's colours to the photo's: LibRaw's matrix, or a camera profile (DCP).

A RAW file is decoded in the camera's own colours, white-balanced as shot (source.py), and
carries `CameraColour`: LibRaw's matrix to linear sRGB, the camera's matrix from XYZ
(Adobe's, as LibRaw has it) and the white the camera chose. The edit names a profile:

- "libraw": LibRaw's matrix, as Lumina rendered before profiles (an edit made before them,
  and `Params()`); nothing else.
- "standard": Lumina's own look (`STANDARD_CURVE` and a little saturation) on LibRaw's
  matrix: what a new edit starts with when the camera has no profile installed.
- a profile's id (dcp.store): its matrices, hue/saturation map, look table and tone curve.

The DNG specification's way of applying a profile (chapter 6): the colour matrices are
mixed by the inverse of the colour temperature of the photo's white, found by iterating
from D50; the forward matrices (mixed the same) take the white-balanced camera's colours
to XYZ (D50), or, without them, the colour matrix's inverse and a Bradford adaptation do;
then in linear ProPhoto RGB the hue/saturation map, the look table and the tone curve (by
Adobe's "RGB tone" method, which keeps hue). The result is linear sRGB.

What comes after the matrix depends on the colour alone, not on where the pixel is, so it
is made once per profile into a 3D table over ProPhoto RGB (sRGB-encoded axes, `GRID`
points a side) and looked up with two OpenCV remaps (`Transform.apply`). The edit's white
balance and exposure act before it, as one 3 x 3 matrix with the profile's. (Adobe applies
a hue/saturation map before the exposure; for the maps with one value division that most
profiles have, that is the same.)
"""

from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

from lumina.core import dcp
from lumina.core.edit import curves

LIBRAW, STANDARD = "libraw", "standard"
GRID = 49
# Lumina's own tone curve, on linear values (as a DCP's): a toe, mid-tones brought up as a
# camera's JPEG has them, and a soft shoulder. Three quarters of the way from a plain curve
# to the one fitted (profile_fit.py) from a Canon EOS 90D's own JPEGs: as bright, with a
# little more room in the highlights. Points (x, y), joined by a monotone spline
STANDARD_CURVE = (
    (0.0, 0.0),
    (0.002, 0.002),
    (0.01, 0.0087),
    (0.03, 0.0458),
    (0.06, 0.129),
    (0.1, 0.256),
    (0.18, 0.478),
    (0.3, 0.709),
    (0.5, 0.872),
    (0.7, 0.950),
    (0.85, 0.985),
    (1.0, 1.0),
)
# How much more saturated Lumina's look is than LibRaw's colours
STANDARD_SATURATION = 1.08

# Matrices: linear, column vectors
SRGB_TO_XYZ_D65 = np.array(
    [[0.412453, 0.357580, 0.180423], [0.212671, 0.715160, 0.072169], [0.019334, 0.119193, 0.950227]]
)
XYZ_D65_TO_SRGB = np.linalg.inv(SRGB_TO_XYZ_D65)
PROPHOTO_TO_XYZ_D50 = np.array(
    [[0.7976749, 0.1351917, 0.0313534], [0.2880402, 0.7118741, 0.0000857], [0.0, 0.0, 0.8252100]]
)
XYZ_D50_TO_PROPHOTO = np.linalg.inv(PROPHOTO_TO_XYZ_D50)
_BRADFORD = np.array(
    [[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]]
)
D50_XY = (0.34567, 0.35850)
D65_XY = (0.31271, 0.32902)


def xy_to_xyz(xy):
    x, y = xy
    return np.array([x / y, 1.0, (1.0 - x - y) / y])


def adaptation(source_xy, target_xy):
    """The Bradford matrix taking colours seen under `source_xy` white to `target_xy`."""
    source = _BRADFORD @ xy_to_xyz(source_xy)
    target = _BRADFORD @ xy_to_xyz(target_xy)
    return np.linalg.inv(_BRADFORD) @ np.diag(target / source) @ _BRADFORD


D65_TO_D50 = adaptation(D65_XY, D50_XY)
SRGB_TO_PROPHOTO = XYZ_D50_TO_PROPHOTO @ D65_TO_D50 @ SRGB_TO_XYZ_D65
PROPHOTO_TO_SRGB = np.linalg.inv(SRGB_TO_PROPHOTO)


@dataclass(frozen=True)
class CameraColour:
    """What a RAW file's decode needs to become colours: `rgb_cam` (white-balanced camera
    -> linear sRGB, LibRaw's, rows summing to 1), `cam_xyz` (XYZ -> camera, Adobe's matrix
    for D65 as LibRaw has it, or None), `neutral` (the camera's colours of white as shot),
    and the camera's `make` and `model`. Tuples, so it can be a cache key."""

    rgb_cam: tuple
    cam_xyz: tuple | None
    neutral: tuple
    make: str = ""
    model: str = ""


def libraw_rgb_cam(cam_xyz):
    """LibRaw's camera -> sRGB matrix from Adobe's XYZ -> camera matrix: the camera's
    colours of the sRGB primaries, each row made to sum to 1 (white-balanced white stays
    white), inverted."""
    cam_rgb = np.asarray(cam_xyz, dtype=np.float64) @ SRGB_TO_XYZ_D65
    cam_rgb = cam_rgb / cam_rgb.sum(axis=1, keepdims=True)
    return np.linalg.inv(cam_rgb)


# White balance and colour temperature


def xyz_to_xy(xyz):
    total = float(np.sum(xyz))
    if total <= 0:
        return D50_XY
    return float(xyz[0] / total), float(xyz[1] / total)


def temperature(xy):
    """The correlated colour temperature of `xy` in kelvin (McCamy), kept in 2000 to 50000."""
    x, y = xy
    n = (x - 0.3320) / (0.1858 - y) if y != 0.1858 else 0.0
    kelvin = 449.0 * n**3 + 3525.0 * n**2 + 6823.3 * n + 5520.33
    return float(min(max(kelvin, 2000.0), 50000.0))


def _weight(kelvin, profile):
    """How much of the first of two calibrations to take at `kelvin` (by inverse
    temperature, as the DNG specification has it)."""
    if len(profile.color_matrices) < 2:
        return 1.0
    t1 = dcp.ILLUMINANTS.get(profile.illuminants[0], 5000.0)
    t2 = dcp.ILLUMINANTS.get(profile.illuminants[1], 5000.0)
    if t1 == t2:
        return 1.0
    if t1 > t2:
        # The lower temperature first
        return 1.0 - _weight_between(kelvin, t2, t1)
    return _weight_between(kelvin, t1, t2)


def _weight_between(kelvin, low, high):
    if kelvin <= low:
        return 1.0
    if kelvin >= high:
        return 0.0
    return (1 / kelvin - 1 / high) / (1 / low - 1 / high)


def _mixed(matrices, weight):
    if len(matrices) == 1:
        return matrices[0]
    return weight * matrices[0] + (1 - weight) * matrices[1]


def white_of(profile, neutral):
    """(xy of the photo's white, the first calibration's weight), from the camera's
    `neutral`, found by iterating: the temperature gives the matrix, the matrix the white."""
    neutral = np.asarray(neutral, dtype=np.float64)
    xy = D50_XY
    weight = 1.0
    for _ in range(20):
        weight = _weight(temperature(xy), profile)
        matrix = _mixed(profile.color_matrices, weight)
        try:
            found = xyz_to_xy(np.linalg.solve(matrix, neutral))
        except np.linalg.LinAlgError:
            break
        if abs(found[0] - xy[0]) + abs(found[1] - xy[1]) < 1e-7:
            xy = found
            break
        xy = found
    return xy, weight


def camera_to_xyz_d50(profile, neutral):
    """The matrix from the white-balanced camera's colours (white as (1, 1, 1)) to XYZ
    under D50 (white with Y 1), for a photo whose white the camera saw as `neutral`."""
    xy, weight = white_of(profile, neutral)
    if profile.forward_matrices:
        return _mixed(profile.forward_matrices, weight)
    matrix = _mixed(profile.color_matrices, weight)
    neutral = np.asarray(neutral, dtype=np.float64)
    to_xyz = np.linalg.inv(matrix) @ np.diag(neutral)
    white = to_xyz @ np.ones(3)
    return adaptation(xyz_to_xy(white), D50_XY) @ (to_xyz / white[1])


# HSV, as the DNG SDK has it: hue in [0, 6), saturation, value the largest channel


def rgb_to_hsv(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    v = np.max(rgb, axis=-1)
    gap = v - np.min(rgb, axis=-1)
    safe = np.where(gap > 0, gap, 1.0)
    h = np.where(
        r == v, (g - b) / safe, np.where(g == v, 2.0 + (b - r) / safe, 4.0 + (r - g) / safe)
    )
    h = np.where(gap > 0, np.mod(h, 6.0), 0.0)
    s = np.where(v > 0, gap / np.where(v > 0, v, 1.0), 0.0)
    return h, s, v


def hsv_to_rgb(h, s, v):
    h = np.mod(h, 6.0)
    i = np.floor(h)
    f = h - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    i = i.astype(np.int64) % 6
    choices = (
        np.stack([v, t, p], -1),
        np.stack([q, v, p], -1),
        np.stack([p, v, t], -1),
        np.stack([p, q, v], -1),
        np.stack([t, p, v], -1),
        np.stack([v, p, q], -1),
    )
    out = np.empty(h.shape + (3,), dtype=np.float64)
    for index, choice in enumerate(choices):
        mask = i == index
        out[mask] = choice[mask]
    return out


def _encode(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1 / 2.4) - 0.055)


def _decode(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, np.power((v + 0.055) / 1.055, 2.4))


def apply_table(rgb, table):
    """A hue/saturation map or look table on linear ProPhoto `rgb` (N x 3), interpolated
    across hue (round), saturation and value."""
    hues, sats, vals = table.dims
    h, s, v = rgb_to_hsv(rgb)
    data = table.data.astype(np.float64)
    hh = h * (hues / 6.0)
    h0 = np.floor(hh).astype(np.int64)
    fh = hh - h0
    h0 %= hues
    h1 = (h0 + 1) % hues
    ss = np.clip(s, 0.0, 1.0) * (sats - 1)
    s0 = np.minimum(np.floor(ss).astype(np.int64), sats - 2)
    fs = ss - s0
    if vals > 1:
        vv = (_encode(v) if table.encoding == 1 else np.clip(v, 0.0, 1.0)) * (vals - 1)
        v0 = np.minimum(np.floor(vv).astype(np.int64), vals - 2)
        fv = vv - v0
        layers = ((v0, 1 - fv), (v0 + 1, fv))
    else:
        layers = ((np.zeros_like(h0), np.ones_like(fh)),)
    shift = np.zeros_like(h)
    sat_scale = np.zeros_like(h)
    val_scale = np.zeros_like(h)
    for layer, w_v in layers:
        for hi, w_h in ((h0, 1 - fh), (h1, fh)):
            for si, w_s in ((s0, 1 - fs), (s0 + 1, fs)):
                entry = data[layer, hi, si]
                w = (w_v * w_h * w_s)[..., None]
                shift += w[..., 0] * entry[..., 0]
                sat_scale += w[..., 0] * entry[..., 1]
                val_scale += w[..., 0] * entry[..., 2]
    h = h + shift * (6.0 / 360.0)
    s = np.minimum(s * sat_scale, 1.0)
    if table.encoding == 1 and vals > 1:
        v = _decode(_encode(v) * val_scale)
    else:
        v = v * val_scale
    return hsv_to_rgb(h, s, np.clip(v, 0.0, None))


def tone_function(curve):
    """The tone curve (N x 2, x and y in [0, 1]) as a function on arrays: a few points
    joined by a monotone spline, a dense table (a DCP's, thousands of points) by lines."""
    curve = np.asarray(curve, dtype=np.float64)
    xs, ys = curve[:, 0], curve[:, 1]
    if len(xs) <= 64:
        return lambda values: curves.monotone(xs, ys, np.clip(values, xs[0], xs[-1]))
    return lambda values: np.interp(np.clip(values, 0.0, 1.0), xs, ys)


def rgb_tone(rgb, curve):
    """Adobe's RGB tone method: the curve on the largest and smallest channel, the middle
    one placed between them as it was, which keeps the hue."""
    order = np.argsort(rgb, axis=-1)
    low = np.take_along_axis(rgb, order[..., :1], -1)[..., 0]
    mid = np.take_along_axis(rgb, order[..., 1:2], -1)[..., 0]
    high = np.take_along_axis(rgb, order[..., 2:], -1)[..., 0]
    new_high, new_low = curve(high), curve(low)
    span = high - low
    share = np.divide(mid - low, span, out=np.zeros_like(mid), where=span > 0)
    new_mid = new_low + (new_high - new_low) * share
    out = np.empty_like(rgb)
    np.put_along_axis(out, order[..., :1], new_low[..., None], -1)
    np.put_along_axis(out, order[..., 1:2], new_mid[..., None], -1)
    np.put_along_axis(out, order[..., 2:], new_high[..., None], -1)
    return out


# Putting it together


@dataclass(frozen=True, eq=False)
class Transform:
    """What an edit's profile does to a RAW file's colours: `matrix` from the white-
    balanced camera to linear sRGB (`lut` None) or to linear ProPhoto RGB, then `lut`, the
    rest of the profile as a table, to linear sRGB. `name` is for showing."""

    matrix: np.ndarray
    lut: np.ndarray | None
    name: str

    def apply(self, prophoto):
        """`lut` at `prophoto` (H x W x 3 float32 in [0, 1], linear): linear sRGB."""
        return lookup(self.lut, prophoto)


def _look(profile):
    """The part of `profile` after its matrix, as a function on linear ProPhoto colours
    (N x 3) to linear sRGB: hue/saturation map, look table, tone curve. A profile without
    a tone curve gets Lumina's, as Adobe's software gives such a profile its own."""
    tone = tone_function(profile.tone_curve if profile.tone_curve is not None else STANDARD_CURVE)

    def look(rgb, weight):
        if profile.hue_sat_maps:
            maps = profile.hue_sat_maps
            table = maps[0]
            if len(maps) == 2 and weight < 1:
                table = dcp.Table(
                    maps[0].dims,
                    weight * maps[0].data + (1 - weight) * maps[1].data,
                    maps[0].encoding,
                )
            rgb = apply_table(rgb, table)
        if profile.look_table is not None:
            rgb = apply_table(rgb, profile.look_table)
        rgb = rgb_tone(np.clip(rgb, 0.0, 1.0), tone)
        return rgb @ PROPHOTO_TO_SRGB.T

    return look


def _standard_look(rgb, _weight):
    h, s, v = rgb_to_hsv(np.clip(rgb, 0.0, 1.0))
    rgb = hsv_to_rgb(h, np.minimum(s * STANDARD_SATURATION, 1.0), v)
    rgb = rgb_tone(rgb, tone_function(STANDARD_CURVE))
    return rgb @ PROPHOTO_TO_SRGB.T


def build_lut(look, weight=1.0, grid=GRID):
    """`look` (linear ProPhoto N x 3 -> linear sRGB) sampled on a grid x grid x grid table
    over sRGB-encoded ProPhoto, laid out for `lookup`: grid rows (green) of grid slices
    (blue) of grid columns (red), float32."""
    axis = _decode(np.linspace(0.0, 1.0, grid))
    blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")
    rgb = np.stack([red, green, blue], axis=-1).reshape(-1, 3)
    out = look(rgb, weight).reshape(grid, grid, grid, 3)  # blue, green, red
    # Rows green; across: blue slice, then red within it
    return np.ascontiguousarray(
        out.transpose(1, 0, 2, 3).reshape(grid, grid * grid, 3), dtype=np.float32
    )


@lru_cache(maxsize=1)
def _encode_table():
    return _encode(np.linspace(0.0, 1.0, 4096)).astype(np.float32)


def lookup(table, rgb):
    """The table `build_lut` made at `rgb` (H x W x 3 float32 linear in [0, 1]), trilinear:
    the two blue slices either side as two bilinear remaps, mixed."""
    grid = table.shape[0]
    top = np.float32(grid - 1)
    encoded = _encode_table()
    index = np.minimum((np.clip(rgb, 0.0, 1.0) * 4095).astype(np.int32), 4095)
    position = encoded[index] * top
    red, green, blue = position[..., 0], position[..., 1], position[..., 2]
    low = np.minimum(np.floor(blue), top - 1)
    share = (blue - low)[..., None]
    x = np.ascontiguousarray(low * np.float32(grid) + red)
    green = np.ascontiguousarray(green)
    below = cv2.remap(table, x, green, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    x += np.float32(grid)
    above = cv2.remap(table, x, green, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    below += (above - below) * share
    return below


@lru_cache(maxsize=8)
def _profile_lut(ident, weight_key):
    profile = dcp.stored(ident)
    return build_lut(_look(profile), weight_key / 1000.0)


@lru_cache(maxsize=2)
def _standard_lut():
    return build_lut(_standard_look)


@lru_cache(maxsize=32)
def prepare(ident, camera):
    """The `Transform` for the profile `ident` (LIBRAW, STANDARD or a stored profile's id)
    on a RAW file's `camera` colours. A stored profile that is missing falls back to the
    standard look, named so."""
    rgb_cam = np.array(camera.rgb_cam, dtype=np.float64)
    if ident == LIBRAW:
        return Transform(rgb_cam, None, "Neutral (LibRaw)")
    to_prophoto = SRGB_TO_PROPHOTO @ rgb_cam
    if ident != STANDARD:
        profile = dcp.stored(ident)
        if profile is not None:
            _xy, weight = white_of(profile, camera.neutral)
            matrix = XYZ_D50_TO_PROPHOTO @ camera_to_xyz_d50(profile, camera.neutral)
            matrix = matrix * 2.0**profile.exposure_offset
            # The tables mixed by weight are made again only for a white a little different
            return Transform(matrix, _profile_lut(ident, round(weight * 1000)), profile.name)
    return Transform(to_prophoto, _standard_lut(), "Lumina Standard")
