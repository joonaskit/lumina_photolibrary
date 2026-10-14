"""The render pipeline: `render(linear image, params)` to display-ready sRGB.

The image comes in as linear light in sRGB primaries (16-bit integers, or floats
in [0, 1]) and every step works on linear floats until the output encoding:

1. Geometry: quarter turns and crop. Done first, as every later step treats each
   pixel on its own: cropping first gives the same result for less work.
2. White balance and exposure: one gain per channel.
3. Tone: the curves and the matte. Curves act on sRGB-encoded values, where they
   feel as curves do in other editors; the whole step, encode, RGB curve, channel
   curve, matte, decode, is a function of one value per channel, so it is a LUT.
4. Colour: the matte's shadow desaturation.
5. Output: sRGB encoding to 8 or 16 bits.

Highlights pushed past white by exposure or white balance are clipped at step 3.
"""

from functools import lru_cache

import numpy as np

from lumina.core.edit import curves

# Entries of the tone LUT over linear [0, 1]
TONE_SIZE = 65536
# Rec. 709 / sRGB luminance weights, for linear values
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
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


@lru_cache(maxsize=16)
def _tone_luts(rgb, red, green, blue, black, white):
    """Per channel: linear value -> linear value through encode, the RGB curve, the
    channel's curve, the matte's range and decode. 3 x TONE_SIZE float32."""
    encoded = srgb_encode(np.linspace(0.0, 1.0, TONE_SIZE))
    shared = encoded if curves.is_identity(rgb) else curves.evaluate(rgb, encoded)
    tables = []
    for points in (red, green, blue):
        value = shared if curves.is_identity(points) else curves.evaluate(points, shared)
        value = black + (1.0 - black - white) * value
        tables.append(srgb_decode(value))
    return np.stack(tables).astype(np.float32)


def tone_luts(params):
    c = params.curves
    return _tone_luts(c.rgb, c.red, c.green, c.blue, params.matte.black, params.matte.white)


def _is_tone_identity(params):
    return (
        all(curves.is_identity(getattr(params.curves, n)) for n in ("rgb", "red", "green", "blue"))
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


def geometry(image, params):
    """`image` turned and cropped, as a view where it can be."""
    if params.rotate:
        image = np.rot90(image, k=-params.rotate, axes=(0, 1))
    crop = params.crop
    height, width = image.shape[:2]
    top, bottom = round(crop.top * height), round(crop.bottom * height)
    left, right = round(crop.left * width), round(crop.right * width)
    return image[top : max(bottom, top + 1), left : max(right, left + 1)]


def output_size(width, height, params):
    """The size `render` gives for a source of `width` x `height`."""
    probe = geometry(np.empty((height, width, 0), dtype=np.uint8), params)
    return probe.shape[1], probe.shape[0]


# The whole


def render(image, params, bits=8, tile_rows=512):
    """`image` (H x W x 3 linear: uint16, or float32 in [0, 1]) rendered with `params`
    to H' x W' x 3 sRGB, uint8 for `bits` 8 or uint16 for 16.

    Works `tile_rows` rows at a time, so a full-resolution render never holds more
    than one tile as floats. 8 bits go through LUTs (fast, for the screen); 16 bits
    are computed exactly (for export), where a LUT's steps could show."""
    if bits not in (8, 16):
        raise ValueError(f"bits must be 8 or 16, not {bits}")
    view = geometry(image, params)
    height, width = view.shape[:2]
    out = np.empty((height, width, 3), dtype=np.uint8 if bits == 8 else np.uint16)
    gains = channel_gains(params)
    if view.dtype == np.uint16:
        gains = gains / np.float32(65535.0)
    tone = None if _is_tone_identity(params) else tone_luts(params)
    desaturation = params.matte.shadow_desaturation
    step = max(1, tile_rows or height)
    for start in range(0, height, step):
        tile = view[start : start + step].astype(np.float32) * gains
        out[start : start + step] = _render_tile(tile, tone, desaturation, bits)
    return out


def _render_tile(linear, tone, desaturation, bits):
    np.clip(linear, 0.0, 1.0, out=linear)
    if bits == 8 and desaturation == 0:
        # One lookup per channel: tone and output together
        index = (linear * (TONE_SIZE - 1) + 0.5).astype(np.uint16)
        out = _output_lut(8)
        if tone is None:
            return out[index]
        toned = np.empty_like(linear)
        for c in range(3):
            toned[..., c] = tone[c][index[..., c]]
        return out[(toned * (TONE_SIZE - 1) + 0.5).astype(np.uint16)]
    if tone is not None:
        for c in range(3):
            if bits == 8:
                index = (linear[..., c] * (TONE_SIZE - 1) + 0.5).astype(np.uint16)
                linear[..., c] = tone[c][index]
            else:
                linear[..., c] = _lookup(linear[..., c], tone[c])
    if desaturation:
        _desaturate_shadows(linear, desaturation)
    if bits == 8:
        return _output_lut(8)[(linear * (TONE_SIZE - 1) + 0.5).astype(np.uint16)]
    np.clip(linear, 0.0, 1.0, out=linear)
    encoded = _lookup(linear, _encode_table())
    encoded *= np.float32(65535.0)
    encoded += np.float32(0.5)
    return encoded.astype(np.uint16)


def _desaturate_shadows(linear, amount):
    luma = linear @ LUMA
    weight = np.clip(1.0 - _lookup(np.clip(luma, 0.0, 1.0), _encode_table()) / _SHADOWS, 0.0, 1.0)
    keep = 1.0 - amount * weight * weight
    linear[:] = luma[..., None] + (linear - luma[..., None]) * keep[..., None].astype(np.float32)
