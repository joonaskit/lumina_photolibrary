"""A camera's own look, fitted from its RAW files and the JPEGs inside them.

Every RAW file carries the JPEG the camera made of it, in the camera's picture style. Over
a few photos of a camera, the pairs show what the camera does: a tone curve, a change of
colours (a 3 x 3 matrix that keeps white white) and what is left of hue and saturation (a
coarse look table). They are fitted on the decode in the camera's colours, through
LibRaw's matrix to linear ProPhoto RGB, against the JPEG decoded to the same space, and
written as a DCP (`dcp.Profile`) that is chosen like any other: "<model> Camera Look".

The two images do not line up to the pixel (the decode has a margin the JPEG has not, the
camera may correct its lens in the JPEG), so both are made small (`SIDE`) and only the
middle of the frame, flat areas and colours neither clipped nor black are used.

No Qt. Runs in a worker process.
"""

import io
from dataclasses import dataclass

import numpy as np
import rawpy
from PIL import Image

from lumina.core import dcp
from lumina.core.edit import colour_profile as cp
from lumina.core.edit import curves, source
from lumina.core.errors import PhotoError

SIDE = 400
# The share of the frame left out at each edge
BORDER = 0.15
# Samples and passes
MAX_PIXELS = 60_000
# How badly conditioned the camera colours may be (greys alone are not enough)
MAX_CONDITION = 1e6
# Fewest usable pixels to learn from
MIN_PIXELS = 2_000
PASSES = 3
# The look table: hues and saturations
HUES, SATURATIONS = 36, 8
# Fewest pixels a cell of the look table, or a bin of the tone curve, is fitted from
MIN_COUNT = 40
MAX_HUE_SHIFT = 25.0
SATURATION_RANGE = (0.6, 1.6)
CURVE_POINTS = 257


@dataclass
class Pairs:
    """Pixels of RAW files and of their JPEGs: `camera` (white-balanced camera colours,
    linear, N x 3) and `target` (the JPEG's linear sRGB), with the camera's colours."""

    camera: np.ndarray
    target: np.ndarray
    colour: cp.CameraColour
    photos: int


def _embedded_jpeg(file):
    try:
        thumb = file.extract_thumb()
    except (rawpy.LibRawError, ValueError):
        return None
    if thumb.format != rawpy.ThumbFormat.JPEG:
        return None
    return Image.open(io.BytesIO(thumb.data)).convert("RGB")


def pair(path):
    """(camera colours, JPEG linear sRGB, CameraColour) of the RAW file at `path`, both
    SIDE pixels on the long side, as the sensor has them (not turned); None when the file
    has no JPEG inside. Raises PhotoError naming it."""
    try:
        with rawpy.imread(str(path)) as file:
            jpeg = _embedded_jpeg(file)
            if jpeg is None:
                return None
            colour = source._camera_colour(file)
            camera = file.postprocess(
                gamma=(1, 1),
                no_auto_bright=True,
                output_bps=16,
                output_color=rawpy.ColorSpace.raw,
                half_size=True,
                use_camera_wb=True,
                user_flip=0,
            )
    except (rawpy.LibRawError, OSError, ValueError) as error:
        raise PhotoError(path, f"cannot decode RAW ({error})") from error
    height, width = camera.shape[:2]
    jw, jh = jpeg.size
    if (jw > jh) != (width > height):
        # The JPEG stored turned: as the sensor has it
        jpeg = jpeg.transpose(Image.Transpose.ROTATE_90)
        jw, jh = jpeg.size
    # The decode has a margin: cut it to the JPEG's shape, about the middle
    if width / height > jw / jh:
        keep = round(height * jw / jh)
        camera = camera[:, (width - keep) // 2 : (width - keep) // 2 + keep]
    else:
        keep = round(width * jh / jw)
        camera = camera[(height - keep) // 2 : (height - keep) // 2 + keep]
    size = source.fit(jw, jh, side=SIDE)
    camera = source.resize(camera, *size, quality=False)
    target = np.asarray(jpeg.resize(size, Image.Resampling.BOX), dtype=np.float64) / 255.0
    return camera.astype(np.float64), cp._decode(target), colour


def _usable(camera, target):
    """Which pixels to fit on: the middle of the frame, flat (the two images do not line
    up exactly at edges), neither clipped nor black."""
    height, width = target.shape[:2]
    mask = np.zeros((height, width), dtype=bool)
    mask[
        int(height * BORDER) : int(height * (1 - BORDER)),
        int(width * BORDER) : int(width * (1 - BORDER)),
    ] = True
    luma = target @ np.array([0.2126, 0.7152, 0.0722])
    gy, gx = np.gradient(cp._encode(luma))
    steep = np.hypot(gx, gy)
    mask &= steep < np.quantile(steep[mask], 0.6) if mask.any() else mask
    mask &= camera.max(axis=-1) < 0.95
    mask &= target.max(axis=-1) < 0.97
    mask &= target.min(axis=-1) > 0.002
    mask &= camera.min(axis=-1) > 0.0005
    return mask


def gather(paths, should_stop=lambda: False, progress=lambda done, total: None):
    """The `Pairs` of the RAW files at `paths` (one camera's). Files without a JPEG inside
    are left out; one that cannot be read raises PhotoError naming it."""
    cameras, targets, colour, photos = [], [], None, 0
    for done, path in enumerate(paths):
        if should_stop():
            break
        found = pair(path)
        progress(done + 1, len(paths))
        if found is None:
            continue
        camera, target, colour = found
        mask = _usable(camera, target)
        cameras.append(camera[mask])
        targets.append(target[mask])
        photos += 1
    if not photos:
        raise ValueError("none of the photos has a JPEG inside to learn from")
    camera, target = np.concatenate(cameras), np.concatenate(targets)
    if len(camera) < MIN_PIXELS:
        raise ValueError(
            "too little of the photos can be learnt from: they are too small, too dark, too "
            "bright or too busy"
        )
    if len(camera) > MAX_PIXELS:
        chosen = np.random.default_rng(0).choice(len(camera), MAX_PIXELS, replace=False)
        camera, target = camera[chosen], target[chosen]
    return Pairs(camera, target, colour, photos)


# Fitting


def fit_tone(before, after):
    """A rising tone curve (CURVE_POINTS x 2 on linear values) taking `before` to `after`
    (ProPhoto, N x 3) as Adobe's RGB tone method uses it: on each pixel's largest and
    smallest channel. Medians in bins even in log2, made to rise, ends at (0, 0), (1, 1)."""
    x = np.concatenate([before.max(axis=-1), before.min(axis=-1)])
    y = np.concatenate([after.max(axis=-1), after.min(axis=-1)])
    keep = (x > 1e-4) & (x < 1.0)
    x, y = x[keep], y[keep]
    edges = np.geomspace(1e-4, 1.0, 41)
    xs, ys = [0.0], [0.0]
    for low, high in zip(edges, edges[1:], strict=False):
        inside = (x >= low) & (x < high)
        if inside.sum() >= MIN_COUNT:
            xs.append(float(np.median(x[inside])))
            ys.append(float(np.median(y[inside])))
    xs.append(1.0)
    ys.append(max(1.0, ys[-1]))
    ys = np.maximum.accumulate(np.minimum(ys, 1.0))
    xs, keep = np.unique(xs, return_index=True)
    ys = ys[keep]
    grid = np.linspace(0.0, 1.0, CURVE_POINTS) ** 2
    return np.stack([grid, curves.monotone(xs, ys, grid)], axis=-1)


def inverse(curve):
    """The tone curve undone (it must rise)."""
    xs, ys = curve[:, 0], curve[:, 1]
    ys, keep = np.unique(ys, return_index=True)
    return np.stack([ys, xs[keep]], axis=-1)


def fit_matrix(before, after):
    """The 3 x 3 matrix C with rows summing to 1 (white stays white) taking `before` to
    `after` (N x 3) best, by least squares."""
    a = before.T @ before
    rows = []
    ones = np.ones(3)
    for c in range(3):
        b = before.T @ after[:, c]
        # Minimise |before r - after_c|^2 with sum(r) = 1 (Lagrange)
        system = np.block([[2 * a, ones[:, None]], [ones[None, :], np.zeros((1, 1))]])
        solution = np.linalg.solve(system, np.concatenate([2 * b, [1.0]]))
        rows.append(solution[:3])
    return np.array(rows)


def fit_look(before, after):
    """A look table (`dcp.Table`, HUES x SATURATIONS, one value division) taking the hue and
    saturation of `before` to those of `after` (ProPhoto, N x 3, before the tone curve):
    medians per cell, smoothed round the hue circle; cells with too few pixels, and grey,
    change nothing."""
    h0, s0, _v0 = cp.rgb_to_hsv(before)
    h1, s1, _v1 = cp.rgb_to_hsv(after)
    hue_index = np.minimum((h0 / 6.0 * HUES).astype(int), HUES - 1)
    sat_index = np.minimum(np.rint(s0 * (SATURATIONS - 1)).astype(int), SATURATIONS - 1)
    shift = np.zeros((HUES, SATURATIONS))
    scale = np.ones((HUES, SATURATIONS))
    weight = np.zeros((HUES, SATURATIONS))
    difference = (h1 - h0 + 3.0) % 6.0 - 3.0
    for hue in range(HUES):
        for sat in range(1, SATURATIONS):
            inside = (hue_index == hue) & (sat_index == sat) & (s0 > 0.02)
            if inside.sum() < MIN_COUNT:
                continue
            shift[hue, sat] = float(np.median(difference[inside])) * 60.0
            scale[hue, sat] = float(np.median(s1[inside] / s0[inside]))
            weight[hue, sat] = 1.0
    # Smooth round the hue circle, weighted by what was measured
    kernel = np.array([0.25, 0.5, 0.25])

    def smooth(values, neutral):
        total = sum(k * np.roll(values * weight, i - 1, axis=0) for i, k in enumerate(kernel))
        count = sum(k * np.roll(weight, i - 1, axis=0) for i, k in enumerate(kernel))
        return np.where(count > 0, total / np.where(count > 0, count, 1), neutral)

    shift = np.clip(smooth(shift, 0.0), -MAX_HUE_SHIFT, MAX_HUE_SHIFT)
    scale = np.clip(smooth(scale, 1.0), *SATURATION_RANGE)
    shift[:, 0], scale[:, 0] = 0.0, 1.0
    data = np.stack([shift, scale, np.ones_like(scale)], axis=-1)[None].astype(np.float32)
    return dcp.Table((HUES, SATURATIONS, 1), data, 0)


@dataclass
class Fitted:
    profile: dcp.Profile
    # Mean difference from the JPEGs, as 8-bit sRGB levels, with LibRaw's colours and with
    # the fitted look
    before: float
    after: float


def _render(pairs, matrix, look, tone):
    prophoto = np.clip(pairs.camera @ matrix.T, 0.0, 1.0)
    if look is not None:
        prophoto = cp.apply_table(prophoto, look)
    toned = cp.rgb_tone(np.clip(prophoto, 0.0, 1.0), cp.tone_function(tone))
    return cp._encode(np.clip(toned @ cp.PROPHOTO_TO_SRGB.T, 0.0, 1.0))


def fit(pairs, model, name=None):
    """The camera's look from `pairs` as a `Fitted` DCP for the camera called `model`.
    Raises ValueError when the photos have too few different colours to learn from."""
    # Colours all along one line (greys, one hue) leave the matrix undetermined
    spread = np.linalg.cond(pairs.camera.T @ pairs.camera)
    try:
        if not np.isfinite(spread) or spread > MAX_CONDITION:
            raise np.linalg.LinAlgError("colours along one line")
        return _fit(pairs, model, name)
    except np.linalg.LinAlgError as error:
        raise ValueError(
            "the photos have too few different colours to learn the camera's look from; "
            "try with more photos of more varied scenes"
        ) from error


def _fit(pairs, model, name):
    rgb_cam = np.array(pairs.colour.rgb_cam)
    base = cp.SRGB_TO_PROPHOTO @ rgb_cam
    x = pairs.camera @ base.T
    target = np.clip(pairs.target @ cp.SRGB_TO_PROPHOTO.T, 0.0, 1.0)
    correction = np.eye(3)
    tone = fit_tone(x, target)
    for _ in range(PASSES):
        undone = cp.rgb_tone(target, cp.tone_function(inverse(tone)))
        correction = fit_matrix(x, undone)
        tone = fit_tone(x @ correction.T, target)
    matrix = correction @ base
    undone = cp.rgb_tone(target, cp.tone_function(inverse(tone)))
    look = fit_look(np.clip(pairs.camera @ matrix.T, 0.0, 1.0), undone)
    expected = cp._encode(np.clip(pairs.target, 0.0, 1.0))
    plain = cp._encode(np.clip(pairs.camera @ rgb_cam.T, 0.0, 1.0))
    after = _render(pairs, matrix, look, tone)
    cam_xyz = pairs.colour.cam_xyz
    if cam_xyz is None:
        cam_xyz = np.linalg.inv(cp.SRGB_TO_XYZ_D65 @ rgb_cam)
    profile = dcp.Profile(
        name=name or f"{model} Camera Look",
        camera=model,
        color_matrices=(np.array(cam_xyz),),
        illuminants=(dcp.D65,),
        forward_matrices=(cp.PROPHOTO_TO_XYZ_D50 @ matrix,),
        look_table=look,
        tone_curve=tone,
        copyright=f"Fitted by Lumina from {pairs.photos} photos and their camera JPEGs",
    )
    return Fitted(
        profile,
        float(np.abs(plain - expected).mean() * 255),
        float(np.abs(after - expected).mean() * 255),
    )
