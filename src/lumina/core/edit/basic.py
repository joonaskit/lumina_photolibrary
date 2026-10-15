"""The Basic tone controls: contrast, highlights, shadows, whites and blacks.

Together they are one curve, built here from the five values: a monotone PCHIP (see
curves.py) through fixed points on the sRGB-encoded scale, each control moving some
of the points up or down. With every control at zero the points lie on the line
y = x, and so does the PCHIP through them: the curve changes nothing. It is folded
into the pipeline's tone LUT, so it costs nothing per pixel.

The curve reaches past white: its input goes up to HEADROOM times white in linear
light (two stops), so light that exposure or white balance pushed past white is
still there for it. Whites and highlights below zero bring that light back into
range, which is what recovers a sky pushed past white by exposure; at zero it is
clipped at white as before.
"""

import numpy as np

from lumina.core.edit import curves

# How far past white the curve's input reaches, in linear light
HEADROOM = 4.0


def encode(linear):
    # sRGB encoding, continued past 1 by the same power law
    linear = np.maximum(linear, 0.0)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * np.power(linear, 1 / 2.4) - 0.055)


# The encoded value of HEADROOM: the curve's input runs from 0 to this
TOP = float(encode(np.float64(HEADROOM)))

# The points, on the encoded scale: deep black, shadows, quarter, middle grey,
# three-quarter, bright, white, two points in the headroom, and its end
_X = np.array([0.0, 0.03, 0.10, 0.25, 0.46, 0.70, 0.90, 1.0, 1.3, 1.6, TOP])
_WHITE = 7
_HEADROOM_POINTS = slice(8, 10)

# How far each control at 100 moves each point. Middle grey moves least, so the
# controls change the ends of the range and leave the photo's overall brightness
# to exposure
_CONTRAST = np.array([0, 0, -0.04, -0.07, 0, 0.07, 0.04, 0, 0, 0, 0])
_SHADOWS = np.array([0, 0, 0.05, 0.10, 0.03, 0, 0, 0, 0, 0, 0])
_HIGHLIGHTS = np.array([0, 0, 0, 0, 0, 0.10, 0.14, 0.16, 0, 0, 0])
_WHITES = np.array([0, 0, 0, 0, 0, 0.02, 0.06, 0.10, 0.10, 0.10, 0.10])
_BLACKS = np.array([0, 0.03, 0.06, 0.02, 0, 0, 0, 0, 0, 0, 0])
# Highlights above zero move the points less than below: they would clip soon
_HIGHLIGHTS_UP = 0.6
# Below zero, how much of the headroom whites and highlights each bring back at -100;
# together never more than all of it, which puts its end at white: two stops past
# white seen again. Highlights at -100 alone do that, whites at -100 about one stop
_WHITES_RECOVERY = 0.8
_HIGHLIGHTS_RECOVERY = 1.0


def points(basic):
    """The curve's points (x, y) on the encoded scale, x from 0 to TOP."""
    c, h, s, w, b = (
        basic.contrast / 100,
        basic.highlights / 100,
        basic.shadows / 100,
        basic.whites / 100,
        basic.blacks / 100,
    )
    if h > 0:
        h *= _HIGHLIGHTS_UP
    y = _X + c * _CONTRAST + s * _SHADOWS + h * _HIGHLIGHTS + w * _WHITES + b * _BLACKS
    # The end of the headroom: down to white below zero, and the points in it towards
    # the straight line from white to there, so what comes back keeps its detail
    # evenly instead of the curve flattening out at the end
    recovered = min(1.0, -min(w, 0.0) * _WHITES_RECOVERY - min(h, 0.0) * _HIGHLIGHTS_RECOVERY)
    y[-1] = TOP - recovered * (TOP - 1.0) + max(w, 0.0) * _WHITES[-1]
    inside = _X[_HEADROOM_POINTS]
    line = y[_WHITE] + (y[-1] - y[_WHITE]) * (inside - 1.0) / (TOP - 1.0)
    y[_HEADROOM_POINTS] += recovered * (line - y[_HEADROOM_POINTS])
    # Black stays black: lifting it is the matte's job
    y[0] = 0.0
    # Rising everywhere, whatever the controls do together: the PCHIP then never falls
    y = np.maximum.accumulate(np.maximum(y, 0.0))
    return _X, y


def evaluate(basic, encoded):
    """The curve at `encoded` (sRGB-encoded values from 0 to TOP), clipped to [0, 1]."""
    x, y = points(basic)
    return np.clip(curves.monotone(x, y, encoded), 0.0, 1.0)
