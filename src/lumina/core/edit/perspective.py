"""Perspective (keystone) correction: vertical and horizontal, as a homography.

The correction is what turning the camera would have done: the photo is a view through a
pinhole at `focal` pixels from its centre, and the correction turns that view about the
horizontal axis (vertical keystone) and the vertical axis (horizontal keystone) by up to
`MAX_ANGLE` degrees. Both are one rotation, by the vector (vertical, horizontal, 0) times
the angle per unit, so turning and mirroring an edit give the edit of the turned or
mirrored photo exactly (see `Perspective.turned` and `flipped`).

A positive vertical amount widens the top of the photo (it corrects a shot tilted up at a
building, whose top narrows); a positive horizontal amount widens its right side.

It runs in the turned and mirrored frame, before straightening: the corrected photo is
turned about its centre and scaled up just enough that no empty corner shows. The scale is
found here (`fill_scale`), as the corrected photo is no longer a rectangle.
"""

import math

import cv2
import numpy as np

from lumina.core.edit.params import PERSPECTIVE as AMOUNT

# The turn at an amount of 100, in degrees
MAX_ANGLE = 20.0
# The focal length in pixels, as a multiple of the photo's diagonal: a 43 mm lens on full
# frame, a normal lens, which is also what the amount's scale is meant for
FOCAL = 1.0


def focal(width, height):
    return FOCAL * math.hypot(width, height)


def rotation(perspective):
    """The 3 x 3 rotation (Rodrigues) the correction turns the view by."""
    scale = math.radians(MAX_ANGLE) / AMOUNT[1]
    vector = np.array([perspective.vertical * scale, perspective.horizontal * scale, 0.0])
    angle = float(np.linalg.norm(vector))
    if angle == 0.0:
        return np.eye(3)
    x, y, z = vector / angle
    cross = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) + math.sin(angle) * cross + (1.0 - math.cos(angle)) * (cross @ cross)


def forward(width, height, perspective):
    """The matrix taking a place in the photo as shot to the corrected photo, both in
    pixels from the photo's centre, which stays where it is."""
    f = focal(width, height)
    k = np.diag([f, f, 1.0])
    matrix = k @ rotation(perspective) @ np.linalg.inv(k)
    # Turning the view moves the middle of the photo: put it back
    x, y, w = matrix @ np.array([0.0, 0.0, 1.0])
    return np.array([[1.0, 0.0, -x / w], [0.0, 1.0, -y / w], [0.0, 0.0, 1.0]]) @ matrix


def _turn_undone(degrees, scale):
    """Pixels from the centre of the straightened photo to those of the one before it:
    the turn clockwise by `degrees` undone, and the scale (zoom) too."""
    angle = math.radians(degrees)
    cos, sin = math.cos(angle) / scale, math.sin(angle) / scale
    return np.array([[cos, sin, 0.0], [-sin, cos, 0.0], [0.0, 0.0, 1.0]])


def source_matrix(width, height, perspective, degrees=0.0, scale=1.0):
    """The matrix taking the pixel (x, y, 1) of the result (before the crop) to where in
    the turned and mirrored photo it comes from, as the geometry step samples it."""
    cx, cy = (width - 1) / 2, (height - 1) / 2
    centre = np.array([[1.0, 0.0, -cx], [0.0, 1.0, -cy], [0.0, 0.0, 1.0]])
    back = np.array([[1.0, 0.0, cx], [0.0, 1.0, cy], [0.0, 0.0, 1.0]])
    undone = np.linalg.inv(forward(width, height, perspective))
    return back @ undone @ _turn_undone(degrees, scale) @ centre


def _fits(matrix, width, height):
    """Whether the four corners of the result come from inside the photo. A projective
    map keeps lines straight, so the whole frame does too."""
    corners = np.array(
        [[0, 0, 1], [width - 1, 0, 1], [0, height - 1, 1], [width - 1, height - 1, 1]],
        dtype=np.float64,
    ).T
    x, y, w = matrix @ corners
    if np.any(w <= 1e-9):
        return False
    x, y = x / w, y / w
    return bool(x.min() >= 0 and x.max() <= width - 1 and y.min() >= 0 and y.max() <= height - 1)


_MAX_ZOOM = 8.0


def fill_scale(width, height, perspective, degrees=0.0):
    """How much the corrected, turned photo is scaled up (1 or more) to fill its own
    frame, found by bisection (a larger scale shows less of the photo, so one that fits
    can be shrunk towards the one that just does)."""
    if perspective.is_identity() and degrees == 0:
        return 1.0

    def fits(scale):
        return _fits(source_matrix(width, height, perspective, degrees, scale), width, height)

    if fits(1.0):
        return 1.0
    low, high = 1.0, _MAX_ZOOM
    if not fits(high):
        return high
    for _ in range(40):
        middle = (low + high) / 2
        if fits(middle):
            high = middle
        else:
            low = middle
    return high


def warp(image, matrix, box):
    """The `box` (top, bottom, left, right) of the result of sampling `image` (H x W x 3:
    uint16 or float32) by `matrix` (see `source_matrix`), bilinearly, as the same type."""
    top, bottom, left, right = box
    if image.dtype not in (np.uint16, np.float32):
        image = image.astype(np.float32)
    source = np.ascontiguousarray(image)
    shift = np.array([[1.0, 0.0, left], [0.0, 1.0, top], [0.0, 0.0, 1.0]])
    return cv2.warpPerspective(
        source,
        matrix @ shift,
        (right - left, bottom - top),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )
