"""The photo open in the Develop view, kept decoded in a worker process.

The UI sends small requests (a path and the parameters as JSON) and gets back
8-bit pixels and a histogram: the 2 MP proxy while a control moves, the
screen-sized render when it stops. The decode stays in the worker between
requests, and only one photo is held at a time (see CLAUDE.md, memory).

Runs in a process of its own (qt_models/workers.py, the "develop" pool), so this
module's state is that process's.
"""

from dataclasses import dataclass

import numpy as np

from lumina.core.edit import params as edit_params
from lumina.core.edit import pipeline
from lumina.core.edit.source import EditSource, fitted

PROXY, SCREEN = "proxy", "screen"

_open = None
_screen = {}


@dataclass(frozen=True)
class Rendered:
    width: int
    height: int
    # RGB, 8 bits, rows without padding
    data: bytes
    # 4 x 256 counts: red, green, blue and luminance of the rendered image
    histogram: np.ndarray
    kind: str


def _source(path):
    global _open
    if _open is None or _open.path != path:
        # The one before goes first: never two decodes at once
        close()
        _open = EditSource(path)
    return _open


def open_photo(path):
    """Decode the photo and make its proxy. Returns its size (width, height)."""
    source = _source(path)
    # Made now, while the view opens, not at the first move of a control
    source.make_ready()
    return source.size()


def render(path, params_json, kind=PROXY, screen_side=2560):
    """The photo rendered with the edit, small (PROXY) or at `screen_side` (SCREEN)."""
    source = _source(path)
    edit = edit_params.from_json(params_json)
    if kind == PROXY:
        image = source.proxy
    else:
        image = _screen_image(source, screen_side)
    pixels = pipeline.render(image, edit, full=source.size())
    return Rendered(pixels.shape[1], pixels.shape[0], pixels.tobytes(), histogram(pixels), kind)


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
