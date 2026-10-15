"""The render pipeline: `render(linear image, params)` to display-ready sRGB.

The image comes in as linear light in sRGB primaries (16-bit integers, or floats
in [0, 1]) and every step works on linear floats until the output encoding:

1. Geometry: quarter turns, mirroring, straightening and crop. Done first, as every
   later step treats each pixel on its own: cropping first gives the same result for
   less work. Straightening turns the photo about its centre and scales it just
   enough to fill the frame, so no empty corners show; the crop is then taken of
   that, and only the pixels inside it are sampled.
2. White balance and exposure: one gain per channel.
3. Tone: the Basic controls, the curves and the matte. They act on sRGB-encoded
   values, where curves feel as they do in other editors; the whole step, encode,
   Basic curve, RGB curve, channel curve, matte, decode, is a function of one value
   per channel, so it is a LUT.
4. Colour: the matte's shadow desaturation, then vibrance and saturation, the HSL
   bands and colour grading, on sRGB-encoded values (see colour.py, hsl.py and
   grading.py).
5. Detail: dehaze, noise reduction, clarity and texture, then sharpening. These look at
   the pixels around each one, so they need rows beyond a tile: see `render` (dehaze.py,
   denoise.py, detail.py and sharpen.py).
6. Effects: the vignette, then the grain, on the cropped photo (see effects.py).
7. Output: sRGB encoding to 8 or 16 bits.

Highlights pushed past white by exposure or white balance are clipped at step 3:
at white, or with the Basic controls in use, at basic.HEADROOM times white, where
the Basic curve can bring them back.
"""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from lumina.core.edit import (
    basic,
    colour,
    curves,
    dehaze,
    denoise,
    effects,
    grading,
    hsl,
    sharpen,
)
from lumina.core.edit import detail as detail_steps

# Entries of the tone LUT over linear [0, 1]
TONE_SIZE = 65536
_CHANNELS = ("rgb", "red", "green", "blue")
# Rec. 709 / sRGB luminance weights, for linear values
LUMA = colour.LUMA
# Mireds a full step of the temperature control moves the white point
_MIREDS_PER_STEP = 1.0
_D65_KELVIN = 6504.0
# How much a full step of tint scales green, as a power of two
_TINT_STOPS = 0.5
# Below this encoded luminance the matte's desaturation starts, strongest at black
_SHADOWS = 0.5
_XYZ_TO_SRGB = np.array(
    [
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ]
)


def srgb_encode(linear):
    linear = np.clip(linear, 0.0, 1.0)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * np.power(linear, 1 / 2.4) - 0.055)


def srgb_decode(encoded):
    encoded = np.clip(encoded, 0.0, 1.0)
    return np.where(encoded <= 0.04045, encoded / 12.92, np.power((encoded + 0.055) / 1.055, 2.4))


# White balance


def _planckian_xy(kelvin):
    """The CIE xy of a black body at `kelvin` (Kim et al., 1667 K to 25000 K)."""
    t = min(max(kelvin, 1667.0), 25000.0)
    if t <= 4000:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


def _white_rgb(kelvin):
    x, y = _planckian_xy(kelvin)
    xyz = np.array([x / y, 1.0, (1 - x - y) / y])
    return _XYZ_TO_SRGB @ xyz


def white_balance_gains(temperature, tint):
    """Linear RGB gains for the temperature and tint controls (-100 to 100 each).

    A positive temperature warms the photo: the white point moves along the black
    body curve to a lower colour temperature, `temperature` mireds from D65. A
    positive tint is towards magenta. The gains keep luminance, so the controls
    change colour, not brightness."""
    if temperature == 0 and tint == 0:
        return np.ones(3, dtype=np.float32)
    mired = 1e6 / _D65_KELVIN + temperature * _MIREDS_PER_STEP
    gains = _white_rgb(1e6 / max(mired, 1.0)) / _white_rgb(_D65_KELVIN)
    gains[1] *= 2.0 ** (-tint / 100.0 * _TINT_STOPS)
    gains /= float(LUMA @ gains.astype(np.float32))
    return gains.astype(np.float32)


def channel_gains(params):
    """White balance and exposure, as one gain per channel."""
    return white_balance_gains(params.temperature, params.tint) * np.float32(2.0**params.exposure)


# Tone


def tone_range(params):
    """The linear light the tone LUT spans, from 0: white, or with the Basic controls
    in use the headroom past it they can bring back."""
    return 1.0 if params.basic.is_identity() else basic.HEADROOM


@lru_cache(maxsize=16)
def _tone_luts(rgb, red, green, blue, black, white, basic_controls):
    """Per channel: linear value -> linear value through encode, the Basic curve, the
    RGB curve, the channel's curve, the matte's range and decode. 3 x TONE_SIZE
    float32, over linear [0, 1], or [0, HEADROOM] with the Basic controls in use."""
    if basic_controls.is_identity():
        encoded = srgb_encode(np.linspace(0.0, 1.0, TONE_SIZE))
    else:
        linear = np.linspace(0.0, basic.HEADROOM, TONE_SIZE)
        encoded = basic.evaluate(basic_controls, basic.encode(linear))
    shared = encoded if curves.is_identity(rgb) else curves.evaluate(rgb, encoded)
    tables = []
    for points in (red, green, blue):
        value = shared if curves.is_identity(points) else curves.evaluate(points, shared)
        value = black + (1.0 - black - white) * value
        tables.append(srgb_decode(value))
    return np.stack(tables).astype(np.float32)


def tone_luts(params):
    c = params.curves
    return _tone_luts(
        c.rgb, c.red, c.green, c.blue, params.matte.black, params.matte.white, params.basic
    )


def _is_tone_identity(params):
    return (
        params.basic.is_identity()
        and all(curves.is_identity(getattr(params.curves, n)) for n in _CHANNELS)
        and params.matte.black == 0
        and params.matte.white == 0
    )


# Output


@lru_cache(maxsize=2)
def _output_lut(bits):
    # Linear [0, 1] at TONE_SIZE steps -> encoded integers, for 8-bit output
    top = (1 << bits) - 1
    encoded = srgb_encode(np.linspace(0.0, 1.0, TONE_SIZE))
    dtype = np.uint8 if bits == 8 else np.uint16
    return np.round(encoded * top).astype(dtype)


@lru_cache(maxsize=1)
def _encode_table():
    # Linear [0, 1] at TONE_SIZE steps -> encoded floats, read with interpolation
    return srgb_encode(np.linspace(0.0, 1.0, TONE_SIZE)).astype(np.float32)


def _lookup(values, table):
    """`table` (sampled evenly over [0, 1]) at `values` in [0, 1], interpolated between
    neighbouring entries: exact enough for 16 bits, and far faster than np.interp or a
    power function on tens of millions of values."""
    position = values * np.float32(len(table) - 1)
    index = np.minimum(position.astype(np.int32), len(table) - 2)
    position -= index
    low = table[index]
    return low + (table[index + 1] - low) * position


# Geometry


# Rows sampled at a time when straightening: bounds the float temporaries
_STRAIGHTEN_ROWS = 256


def _oriented(image, params):
    """Turned and mirrored: views, nothing copied."""
    if params.rotate:
        image = np.rot90(image, k=-params.rotate, axes=(0, 1))
    if params.flip_horizontal:
        image = image[:, ::-1]
    if params.flip_vertical:
        image = image[::-1]
    return image


def _crop_box(width, height, crop):
    top, bottom = round(crop.top * height), round(crop.bottom * height)
    left, right = round(crop.left * width), round(crop.right * width)
    return top, max(bottom, top + 1), left, max(right, left + 1)


def geometry(image, params):
    """`image` turned, mirrored, straightened and cropped: a view when not straightened,
    a new array of the same type when it is."""
    image = _oriented(image, params)
    height, width = image.shape[:2]
    top, bottom, left, right = _crop_box(width, height, params.crop)
    if params.straighten == 0:
        return image[top:bottom, left:right]
    return _straightened(image, params.straighten, (top, bottom, left, right))


def straighten_scale(width, height, degrees):
    """How much a `width` x `height` photo turned by `degrees` is scaled up to fill
    its own frame."""
    angle = np.radians(abs(degrees))
    cos, sin = np.cos(angle), np.sin(angle)
    return max((width * cos + height * sin) / width, (width * sin + height * cos) / height)


def _straightened(image, degrees, box):
    """The `box` (top, bottom, left, right) of `image` turned `degrees` clockwise about
    its centre and scaled to fill, sampled bilinearly."""
    height, width, channels = image.shape
    top, bottom, left, right = box
    angle = np.radians(degrees)
    scale = straighten_scale(width, height, degrees)
    cos, sin = np.float32(np.cos(angle) / scale), np.float32(np.sin(angle) / scale)
    cx, cy = np.float32((width - 1) / 2), np.float32((height - 1) / 2)
    # Rows of pixels, read by flat index: far faster than indexing in two dimensions
    flat = np.ascontiguousarray(image).reshape(-1, channels)
    out = np.empty((bottom - top, right - left, channels), dtype=image.dtype)
    xs = np.arange(left, right, dtype=np.float32) - cx
    integer = np.issubdtype(image.dtype, np.integer)
    for start in range(top, bottom, _STRAIGHTEN_ROWS):
        ys = np.arange(start, min(start + _STRAIGHTEN_ROWS, bottom), dtype=np.float32) - cy
        # Where each output pixel comes from: the turn undone (y points down, so this
        # turns the photo clockwise for a positive angle)
        sx = cos * xs[None, :] + (sin * ys + cx)[:, None]
        sy = (-sin) * xs[None, :] + (cos * ys + cy)[:, None]
        np.clip(sx, 0, max(width - 1.001, 0), out=sx)
        np.clip(sy, 0, max(height - 1.001, 0), out=sy)
        x0 = sx.astype(np.int32)
        y0 = sy.astype(np.int32)
        fx = (sx - x0).ravel()[:, None]
        fy = (sy - y0).ravel()[:, None]
        index = (y0 * width + x0).ravel()
        step_x = 1 if width > 1 else 0
        step_y = width if height > 1 else 0
        a = flat.take(index, axis=0).astype(np.float32)
        b = flat.take(index + step_x, axis=0)
        a += (b - a) * fx
        c = flat.take(index + step_y, axis=0).astype(np.float32)
        d = flat.take(index + step_y + step_x, axis=0)
        c += (d - c) * fx
        a += (c - a) * fy
        if integer:
            a += np.float32(0.5)
        out[start - top : start - top + len(ys)] = a.reshape(len(ys), right - left, channels)
    return out


def output_size(width, height, params):
    """The size `render` gives for a source of `width` x `height`."""
    if params.rotate % 2:
        width, height = height, width
    top, bottom, left, right = _crop_box(width, height, params.crop)
    return right - left, bottom - top


# The whole


def render(image, params, bits=8, tile_rows=512, full=None):
    """`image` (H x W x 3 linear: uint16, or float32 in [0, 1]) rendered with `params`
    to H' x W' x 3 sRGB, uint8 for `bits` 8 or uint16 for 16.

    Works `tile_rows` rows at a time, so a full-resolution render never holds more
    than one tile as floats. 8 bits go through LUTs (fast, for the screen); 16 bits
    are computed exactly (for export), where a LUT's steps could show.

    `full` is the (width, height) of the photo at full size, when `image` is a smaller
    copy of it (the proxy, the screen's, a thumbnail's): sharpening and noise reduction
    are in pixels of the full-size photo and are made smaller to match. Without it
    `image` is taken to be the full size.

    A photo with a step that looks at the pixels around each one (texture, clarity,
    dehaze, noise reduction, sharpening) is rendered in two passes: the first does
    everything that treats a pixel on its own and keeps the photo as 16-bit sRGB (a
    third of the memory of floats); the second takes it in tiles with rows to spare
    beyond each, applies those steps, the effects, and the output encoding."""
    if bits not in (8, 16):
        raise ValueError(f"bits must be 8 or 16, not {bits}")
    view = geometry(image, params)
    height, width = view.shape[:2]
    out = np.empty((height, width, 3), dtype=np.uint8 if bits == 8 else np.uint16)
    gains = channel_gains(params)
    if view.dtype == np.uint16:
        gains = gains / np.float32(65535.0)
    # The tone LUT spans [0, tone_range] in linear light: scaled to [0, 1] with the gains
    gains = gains / np.float32(tone_range(params))
    tone = None if _is_tone_identity(params) else tone_luts(params)
    desaturation = params.matte.shadow_desaturation
    plan = _plan(params, (height, width), _scale(image, params, full))
    step = max(1, tile_rows or height)
    if plan is not None and plan.detail is not None:
        _render_in_two_passes(view, gains, tone, desaturation, plan, params, out, step)
        return out
    for start in range(0, height, step):
        tile = view[start : start + step].astype(np.float32) * gains
        out[start : start + step] = _render_tile(tile, tone, desaturation, plan, bits, start)
    return out


@dataclass(frozen=True)
class Plan:
    """What the steps after the tone step will do to a photo of `size` (height, width) as
    rendered: `colour` (vividness, bands, grading), `detail` (the steps that look at
    neighbours, see detail.py) and `effects` (vignette, grain), each None for nothing."""

    colour: tuple | None
    detail: tuple | None
    effects: tuple | None
    size: tuple


def _scale(image, params, full):
    """How large the render of `image` is, as a share of the render of the photo at
    `full` (width, height) size: 1 for the full size, or without `full`."""
    if full is None:
        return 1.0
    here = max(output_size(image.shape[1], image.shape[0], params))
    return min(1.0, here / max(output_size(full[0], full[1], params)))


def _plan(params, size, scale=1.0):
    """The `Plan` for `params` on a photo `size` (height, width) as rendered, at `scale`
    of its full size, or None when there is nothing after the tone step."""
    vividness = (params.colour.vibrance, params.colour.saturation)
    bands = None if params.hsl.is_identity() else hsl.tables(params.hsl)
    graded = None if params.grading.is_identity() else grading.prepare(params.grading)
    coloured = (vividness, bands, graded) if any(vividness) or bands or graded else None
    detail = _detail_plan(params, size, scale)
    vignette = None if params.vignette.is_identity() else effects.prepare_vignette(params.vignette)
    grain = None if params.grain.is_identity() else effects.prepare_grain(params.grain, size)
    finish = (vignette, grain) if vignette is not None or grain is not None else None
    if coloured is None and detail is None and finish is None:
        return None
    return Plan(coloured, detail, finish, size)


@dataclass(frozen=True)
class Detail:
    """The steps that look at the pixels around each one, in the order they are made."""

    dehaze: float
    noise: tuple | None
    local: tuple
    sharpen: tuple | None

    def reach(self):
        """(rows beyond a tile that the steps need, the multiple a tile must start at)."""
        rows, align = 0, 1
        if self.noise is not None:
            rows += denoise.reach(self.noise)
            align = denoise.align(self.noise)
        if any(self.local):
            local, local_align = detail_steps.layout(self.local)
            rows += local
            align = max(align, local_align)
        if self.sharpen is not None:
            rows += sharpen.reach(self.sharpen)
        return -(-rows // align) * align, align


def _detail_plan(params, size, scale):
    dehazed = params.presence.dehaze
    noise = denoise.prepare(params.noise, scale)
    local = detail_steps.prepare(params.presence, size)
    sharp = sharpen.prepare(params.sharpening, scale)
    if not dehazed and noise is None and not any(local) and sharp is None:
        return None
    return Detail(dehazed, noise, local, sharp)


def _tone_step(linear, tone, desaturation, bits):
    """The tone LUT and the matte's shadow desaturation, in place on `linear` (floats
    in [0, 1]): through LUT lookups for 8 bits, interpolated tables for 16."""
    if tone is not None:
        for c in range(3):
            if bits == 8:
                index = (linear[..., c] * (TONE_SIZE - 1) + 0.5).astype(np.uint16)
                linear[..., c] = tone[c][index]
            else:
                linear[..., c] = _lookup(linear[..., c], tone[c])
    if desaturation:
        _desaturate_shadows(linear, desaturation)


def _render_tile(linear, tone, desaturation, plan, bits, first_row):
    np.clip(linear, 0.0, 1.0, out=linear)
    if bits == 8 and desaturation == 0 and plan is None:
        # One lookup per channel: tone and output together
        index = (linear * (TONE_SIZE - 1) + 0.5).astype(np.uint16)
        out = _output_lut(8)
        if tone is None:
            return out[index]
        toned = np.empty_like(linear)
        for c in range(3):
            toned[..., c] = tone[c][index[..., c]]
        return out[(toned * (TONE_SIZE - 1) + 0.5).astype(np.uint16)]
    _tone_step(linear, tone, desaturation, bits)
    if bits == 8 and plan is None:
        return _output_lut(8)[(linear * (TONE_SIZE - 1) + 0.5).astype(np.uint16)]
    encoded = _encoded(linear)
    if plan is not None:
        _adjust(encoded, first_row, plan.size, plan.colour, plan.effects)
    return _quantized(encoded, bits)


def _encoded(linear):
    """`linear` (floats, in place) as sRGB-encoded floats in [0, 1]."""
    np.clip(linear, 0.0, 1.0, out=linear)
    return _lookup(linear, _encode_table())


def _quantized(encoded, bits):
    """`encoded` (floats in [0, 1], in place) as 8 or 16 bits."""
    encoded *= np.float32(255.0 if bits == 8 else 65535.0)
    encoded += np.float32(0.5)
    return encoded.astype(np.uint8 if bits == 8 else np.uint16)


def _adjust(encoded, first_row, size, coloured, finishing):
    """The colour steps, then the effects, on `encoded` (sRGB-encoded floats, the rows
    of the photo of `size` from `first_row`), a few rows at a time so the temporaries
    stay in the CPU cache. Either may be None."""
    for start in range(0, len(encoded), colour.ROWS):
        rows = encoded[start : start + colour.ROWS]
        row = first_row + start
        if coloured is not None:
            vividness, bands, graded = coloured
            if any(vividness):
                colour.adjust_rows(rows, *vividness)
            if bands is not None:
                hsl.adjust_rows(rows, bands)
            if graded is not None:
                grading.adjust_rows(rows, graded)
        if finishing is not None:
            vignette, grain = finishing
            if vignette is not None:
                effects.vignette_rows(rows, row, size, vignette)
            if grain is not None:
                effects.grain_rows(rows, row, size, grain)


def _render_in_two_passes(view, gains, tone, desaturation, plan, params, out, step):
    """`render` for a photo with steps that need the pixels around each one: see there."""
    height, width = view.shape[:2]
    bits = 8 if out.dtype == np.uint8 else 16
    detail = plan.detail
    # First pass: what treats each pixel on its own, kept as 16-bit sRGB. The exact
    # tables, for 8 bits too, so that a photo looks the same at either
    stage = np.empty((height, width, 3), dtype=np.uint16)
    for start in range(0, height, step):
        tile = view[start : start + step].astype(np.float32) * gains
        np.clip(tile, 0.0, 1.0, out=tile)
        _tone_step(tile, tone, desaturation, 16)
        encoded = _encoded(tile)
        _adjust(encoded, start, plan.size, plan.colour, None)
        stage[start : start + step] = _quantized(encoded, 16)
    analysis = dehaze.analyse(stage, detail.dehaze) if detail.dehaze else None
    # Second pass: tiles with the rows the neighbour-looking steps reach beyond them
    halo, align = detail.reach()
    # Tiles of at least four halos, to a multiple of the rows a blur's copy is cut at
    step = -(-max(step, 4 * halo) // align) * align
    for start in range(0, height, step):
        stop = min(start + step, height)
        first, last = max(0, start - halo), min(height, stop + halo)
        x = stage[first:last].astype(np.float32)
        x *= np.float32(1.0 / 65535.0)
        if detail.dehaze:
            dehaze.apply_rows(x, first, analysis, detail.dehaze)
        if detail.noise is not None:
            denoise.apply_rows(x, detail.noise)
        if any(detail.local):
            detail_steps.apply_rows(x, detail.local)
        if detail.sharpen is not None:
            sharpen.apply_rows(x, detail.sharpen)
        kept = x[start - first : stop - first]
        if plan.effects is not None:
            _adjust(kept, start, plan.size, None, plan.effects)
        out[start:stop] = _quantized(np.ascontiguousarray(kept), bits)


def _desaturate_shadows(linear, amount):
    luma = linear @ LUMA
    weight = np.clip(1.0 - _lookup(np.clip(luma, 0.0, 1.0), _encode_table()) / _SHADOWS, 0.0, 1.0)
    keep = 1.0 - amount * weight * weight
    linear[:] = luma[..., None] + (linear - luma[..., None]) * keep[..., None].astype(np.float32)
