"""Auto tone and auto white balance: edit values found from the photo itself.

Both look at the photo as the edit renders it up to the controls they set: geometry,
lens corrections and the colour profile, and the white balance for tone; nothing else.
`analysis` makes that view (linear sRGB, 1 is white, the light past white kept for two
stops), and the functions below measure it. They give the controls' values and change
nothing else, so what Auto finds does not depend on what was set before it.

Auto tone aims the exposure at a middle grey for the median, then lets highlights and
whites bring in what is past white, shadows and blacks lift and set the ends, and
contrast open up a flat photo. Auto white balance is grey world over the pixels that are
neither dark nor clipped, narrowed to the ones nearest neutral, which a photo mostly of
one colour does not drag as far as a plain mean. The picker takes the median of a few
pixels around a click.
"""

from dataclasses import replace

import numpy as np

from lumina.core.edit import basic, params, pipeline

# How far down the analysis is rendered, so that two stops past white are not clipped
_HEADROOM_STOPS = 2
_GAIN = 2.0**_HEADROOM_STOPS
# At most this many pixels are measured
_SAMPLE = 200_000

# The median of the photo goes to this much linear light (middle grey is 0.18), and only
# this share of the way: a night scene stays dark and snow stays bright
_MEDIAN_TARGET = 0.22
_EXPOSURE_SHARE = 0.55
EXPOSURE_LIMIT = 3.0
# The brightest pixels (the 99.5th percentile) go no further past white than this
_BRIGHT_LIMIT = 2.0


def analysis(image, edit, full=None, camera=None, white_balance=True):
    """`image` (as `pipeline.render` takes it) as linear sRGB floats, H x W x 3, 1 being the
    photo's white, with the geometry, lens corrections and colour profile of `edit` and its
    white balance, unless `white_balance` is False. Light up to two stops past white is
    kept."""
    plain = replace(
        params.Params(),
        exposure=-float(_HEADROOM_STOPS),
        temperature=edit.temperature if white_balance else 0.0,
        tint=edit.tint if white_balance else 0.0,
        profile=edit.profile,
        lens=edit.lens,
        rotate=edit.rotate,
        flip_horizontal=edit.flip_horizontal,
        flip_vertical=edit.flip_vertical,
        perspective=edit.perspective,
        straighten=edit.straighten,
        crop=edit.crop,
    )
    pixels = pipeline.render(image, plain, bits=16, full=full, camera=camera)
    return pipeline.srgb_decode(pixels.astype(np.float32) / 65535.0).astype(np.float32) * _GAIN


def _sample(linear):
    # About _SAMPLE pixels of the photo, N x 3
    pixels = linear.reshape(-1, 3)
    return pixels[:: max(1, len(pixels) // _SAMPLE)]


def _encoded(linear):
    return basic.encode(linear)


def auto_tone(linear):
    """(exposure in EV, `params.Basic`) for the photo `linear` (see `analysis`)."""
    pixels = _sample(linear)
    luma = pixels @ pipeline.LUMA
    if not np.any(luma > 0):
        return 0.0, params.Basic()
    _, middle, bright = np.quantile(luma, [0.005, 0.5, 0.995])
    exposure = 0.0
    if middle > 1e-4:
        exposure = _EXPOSURE_SHARE * float(np.log2(_MEDIAN_TARGET / middle))
    # Not so far up that the brightest light cannot be brought back
    if bright > 1e-4:
        exposure = min(exposure, float(np.log2(_BRIGHT_LIMIT / bright)))
    exposure = round(float(np.clip(exposure, -EXPOSURE_LIMIT, EXPOSURE_LIMIT)), 2)
    luma = luma * 2.0**exposure
    quantiles = np.quantile(luma, [0.005, 0.10, 0.05, 0.95, 0.995])
    e_black, e_shadow, e_dark, e_light, _ = (float(_encoded(v)) for v in quantiles)
    bright = quantiles[-1]
    e_bright = float(_encoded(bright))

    # Light past white comes back with highlights; below white the top is stretched to it
    highlights = whites = 0.0
    if bright > 1.0:
        highlights -= 100 * min(1.0, float(np.log2(bright)) / _HEADROOM_STOPS)
    else:
        whites = 80 * float(np.clip((0.98 - e_bright) / 0.10, 0.0, 1.0))
    if e_light > 0.85:
        highlights -= min(e_light - 0.85, 0.15) / 0.15 * 30
    # Dark shadows are lifted; blacks above the black point are pulled down to it
    shadows = 80 * float(np.clip((0.12 - e_shadow) / 0.08, 0.0, 1.0))
    blacks = -60 * float(np.clip((e_black - 0.03) / 0.08, 0.0, 1.0))
    # A flat photo gets contrast, one already using all of the range gets some taken
    spread = min(e_light, 1.0) - e_dark
    contrast = 0.0
    if spread < 0.6:
        contrast = 50 * (0.6 - spread) / 0.3
    elif spread > 0.9:
        contrast = -30 * min(spread - 0.9, 0.1) / 0.1

    def value(x):
        return float(np.clip(round(x), -100, 100))

    controls = params.Basic(
        contrast=value(contrast),
        highlights=value(highlights),
        shadows=value(shadows),
        whites=value(whites),
        blacks=value(blacks),
    )
    return exposure, controls


# White balance


def _error(mean, temperature, tint):
    # How far the mean colour is from grey with these controls, in stops between channels
    seen = mean * pipeline.white_balance_gains(temperature, tint)
    logs = np.log2(seen / seen[1])
    return float(logs[0] ** 2 + logs[2] ** 2)


def neutralise(mean):
    """(temperature, tint), whole numbers within the controls' range, that make the linear
    colour `mean` the nearest to grey."""
    mean = np.maximum(np.asarray(mean, dtype=np.float64), 1e-6)
    low, high = params.WARMTH
    around = [(low, high), (low, high)]
    best = (0.0, 0.0)
    for _ in range(4):
        ts = np.linspace(*around[0], 11)
        tints = np.linspace(*around[1], 11)
        best = min(((t, i) for t in ts for i in tints), key=lambda c: _error(mean, *c))
        step_t, step_i = ts[1] - ts[0], tints[1] - tints[0]
        around = [
            (max(low, best[0] - step_t), min(high, best[0] + step_t)),
            (max(low, best[1] - step_i), min(high, best[1] + step_i)),
        ]
    return float(round(best[0])), float(round(best[1]))


def auto_white_balance(linear):
    """(temperature, tint) that make the photo `linear` (see `analysis`, rendered without
    its white balance) grey on average, or None for a photo with no light."""
    pixels = _sample(linear)
    luma = pixels @ pipeline.LUMA
    usable = (luma > 0.03) & (pixels.max(axis=1) < 0.97)
    if np.count_nonzero(usable) < max(10, len(pixels) // 100):
        usable = pixels.max(axis=1) > 1e-4
    pixels = pixels[usable]
    if len(pixels) == 0:
        return None
    mean = pixels.mean(axis=0)
    for _ in range(4):
        corrected = np.maximum(pixels * (mean.mean() / np.maximum(mean, 1e-6)), 1e-6)
        chroma = np.abs(np.log2(corrected[:, 0] / corrected[:, 1])) + np.abs(
            np.log2(corrected[:, 2] / corrected[:, 1])
        )
        near = chroma <= np.median(chroma)
        mean = pixels[near].mean(axis=0)
    return neutralise(mean)


def white_balance_at(linear, x, y, radius=3):
    """(temperature, tint) that make the photo grey at (`x`, `y`), fractions of `linear`
    (see `analysis`, rendered without its white balance), by the median of the pixels
    within `radius` of it. None where there is no light."""
    height, width = linear.shape[:2]
    column = int(np.clip(round(x * (width - 1)), 0, width - 1))
    row = int(np.clip(round(y * (height - 1)), 0, height - 1))
    near = linear[
        max(0, row - radius) : row + radius + 1, max(0, column - radius) : column + radius + 1
    ]
    colour = np.median(near.reshape(-1, 3), axis=0)
    if colour.max() <= 1e-4:
        return None
    return neutralise(colour)
