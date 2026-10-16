"""The photo open in the Develop view, kept decoded in a worker process.

The UI sends small requests (a path and the parameters as JSON) and gets back
8-bit pixels and a histogram: the 2 MP proxy while a control moves, the
screen-sized render when it stops. The decode stays in the worker between
requests, and only one photo is held at a time (see CLAUDE.md, memory).

Runs in a process of its own (qt_models/workers.py, the "develop" pool), so this
module's state is that process's.
"""

import math
from dataclasses import dataclass

import numpy as np

from lumina.core import lensdb, raw
from lumina.core.edit import params as edit_params
from lumina.core.edit import pipeline
from lumina.core.edit.source import EditSource, fitted

PROXY, SCREEN, REGION = "proxy", "screen", "region"

_open = None
_screen = {}
# The photo made smaller for a zoom under 100 %, and what dehaze measured of it
_zoomed = {}
_haze = {}


@dataclass(frozen=True)
class Rendered:
    width: int
    height: int
    # RGB, 8 bits, rows without padding
    data: bytes
    # 4 x 256 counts: red, green, blue and luminance of the rendered image
    histogram: np.ndarray | None
    kind: str
    # The size of the whole photo as this edit renders it at full size
    full: tuple = (0, 0)
    # For a REGION: its (left, top) in the whole as rendered, and the whole's (width, height)
    # as rendered, both in pixels of the image this part is of
    corner: tuple = (0, 0)
    whole: tuple = (0, 0)


def _source(path):
    global _open
    if _open is None or _open.path != path:
        # The one before goes first: never two decodes at once
        close()
        _open = EditSource(path)
    return _open


@dataclass(frozen=True)
class Opened:
    # The photo's (width, height) as decoded
    size: tuple
    # The profile of the lens it was taken with, if lensfun's data has one; whether it is a
    # RAW file (which a camera has not corrected); whether there is lens data at all
    lens: object
    raw: bool
    lens_data: bool


def open_photo(path, shot=None):
    """Decode the photo and make its proxy; find its lens in the lens database (`shot`: what
    the photo says of how it was taken, see lensdb.Shot). Returns `Opened`."""
    source = _source(path)
    # Made now, while the view opens, not at the first move of a control
    source.make_ready()
    database = lensdb.load()
    profile = lensdb.match(shot, database=database) if shot is not None else None
    return Opened(source.size(), profile, raw.is_raw(path), bool(len(database)))


def lens_names(shot):
    """The names of the lenses that fit the camera of `shot`, to choose from."""
    return lensdb.lens_names(shot)


def lens_profile(shot, name):
    """The profile of the lens called `name` at the focal length and aperture of `shot`."""
    return lensdb.match(shot, name=name)


def render(path, params_json, kind=PROXY, screen_side=2560):
    """The photo rendered with the edit, small (PROXY) or at `screen_side` (SCREEN)."""
    source = _source(path)
    edit = edit_params.from_json(params_json)
    if kind == PROXY:
        image = source.proxy
    else:
        image = _screen_image(source, screen_side)
    pixels = pipeline.render(image, edit, full=source.size())
    return Rendered(
        pixels.shape[1],
        pixels.shape[0],
        pixels.tobytes(),
        histogram(pixels),
        kind,
        full=pipeline.output_size(*source.size(), edit),
    )


def render_region(path, params_json, zoom, centre, view):
    """The part of the photo in a view: `view` (width, height) device pixels, at `zoom`
    (device pixels a pixel of the photo at full size; 1 is 100 %), with `centre` (fractions
    of the photo as the edit renders it) in the middle.

    Rendered from the full decode (REGION) at the zoom, or at 100 % and left to be
    scaled up when zoomed in past it, with the halo that the steps that look at
    neighbours need. The part is a little larger than the view."""
    source = _source(path)
    edit = edit_params.from_json(params_json)
    scale = min(zoom, 1.0)
    image = source.full if scale >= 0.999 else _zoomed_image(source, scale)
    width, height = pipeline.output_size(image.shape[1], image.shape[0], edit)
    # A pixel of the image is this many device pixels in the view
    step = zoom / scale
    wide = min(width, math.ceil(view[0] / step) + 2)
    high = min(height, math.ceil(view[1] / step) + 2)
    left = min(max(round(centre[0] * width - wide / 2), 0), width - wide)
    top = min(max(round(centre[1] * height - high / 2), 0), height - high)
    pixels = pipeline.render(
        image,
        edit,
        full=source.size(),
        region=(left, top, left + wide, top + high),
        haze=_haze_of(source, params_json, edit),
    )
    return Rendered(
        wide,
        high,
        pixels.tobytes(),
        None,
        REGION,
        full=pipeline.output_size(*source.size(), edit),
        corner=(left, top),
        whole=(width, height),
    )


def _zoomed_image(source, scale):
    # The decode made smaller, once per photo and size: the one before goes first
    side = max(1, round(max(source.size()) * scale))
    key = (source.path, side)
    if key not in _zoomed:
        _zoomed.clear()
        _zoomed[key] = fitted(source.full, edit_params.Params(), side)
    return _zoomed[key]


def _haze_of(source, params_json, edit):
    # What dehaze measures of the whole photo, from the proxy, kept while the edit is the
    # same (panning asks again and again)
    if not edit.presence.dehaze:
        return None
    key = (source.path, params_json)
    if key not in _haze:
        _haze.clear()
        _haze[key] = pipeline.haze_analysis(source.proxy, edit)
    return _haze[key]


def _screen_image(source, side):
    # A screen-sized copy of the decode, made once per photo and size. Fitted to the
    # uncropped photo, so a crop is cut from it, as from the full image
    key = (source.path, side)
    if key not in _screen:
        _screen.clear()
        _screen[key] = fitted(source.full, edit_params.Params(), side)
    return _screen[key]


def histogram(pixels):
    """Counts of each 8-bit value in red, green, blue and luminance."""
    counts = [np.bincount(pixels[..., c].ravel(), minlength=256) for c in range(3)]
    luma = (pixels.astype(np.float32) @ pipeline.LUMA).round().astype(np.uint8)
    counts.append(np.bincount(luma.ravel(), minlength=256))
    return np.stack(counts)


def close():
    """Let go of the open photo."""
    global _open
    _open = None
    _screen.clear()
    _zoomed.clear()
    _haze.clear()
