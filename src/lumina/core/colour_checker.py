"""A camera profile from a photo of a colour target (X-Rite ColorChecker Classic, 24 patches).

For digitizing and studio work: under the light the photos will be taken in, a shot of the
target gives the camera's colours of 24 known colours. The user points at the middle of
the four corner patches (dark skin top left, bluish green top right, black bottom right,
white bottom left, with the target as it reads); the others are found by the perspective
that fits those four. Each patch is the median of a square in its middle.

The fit: the grey patches set the white balance and the exposure (the profile changes
neither), then the forward matrix (white-balanced camera -> XYZ, D50) is the one with
white kept white that comes nearest the reference colours, weighted towards the dark
patches as the eye is. The profile is written as a DCP for that camera and that light
(`dcp.Profile`), its accuracy given as CIEDE2000.

No Qt.
"""

from dataclasses import dataclass

import cv2
import numpy as np

from lumina.core import dcp
from lumina.core.edit import colour_profile as cp

COLUMNS, ROWS = 6, 4
# X-Rite's values for the ColorChecker Classic made after November 2014, CIELAB under D50,
# in reading order from dark skin to black
REFERENCE_LAB = np.array(
    [
        (37.54, 14.37, 14.92),
        (64.66, 19.27, 17.50),
        (49.32, -3.82, -22.54),
        (43.46, -12.74, 22.72),
        (54.94, 9.61, -24.79),
        (70.48, -32.26, -0.37),
        (62.73, 35.83, 56.50),
        (39.43, 10.75, -45.17),
        (50.57, 48.64, 16.67),
        (30.10, 22.54, -20.87),
        (71.77, -24.13, 58.19),
        (71.51, 18.24, 67.37),
        (28.37, 15.42, -49.80),
        (54.38, -39.72, 32.27),
        (42.43, 51.05, 28.62),
        (81.80, 2.67, 80.41),
        (50.63, 51.28, -14.12),
        (49.57, -29.71, -28.32),
        (95.19, -1.03, 2.93),
        (81.29, -0.57, 0.44),
        (66.89, -0.75, -0.06),
        (50.76, -0.13, 0.14),
        (35.63, -0.46, -0.48),
        (20.64, 0.07, -0.46),
    ]
)
NAMES = (
    "dark skin",
    "light skin",
    "blue sky",
    "foliage",
    "blue flower",
    "bluish green",
    "orange",
    "purplish blue",
    "moderate red",
    "purple",
    "yellow green",
    "orange yellow",
    "blue",
    "green",
    "red",
    "yellow",
    "magenta",
    "cyan",
    "white",
    "neutral 8",
    "neutral 6.5",
    "neutral 5",
    "neutral 3.5",
    "black",
)
# The greys the white balance and exposure are taken from (white may be clipped)
GREYS = slice(19, 23)
# How much of the distance between patches the square sampled in each is
SAMPLE_SHARE = 0.3
D50_WHITE = cp.xy_to_xyz(cp.D50_XY)


def lab_to_xyz(lab, white=D50_WHITE):
    lab = np.asarray(lab, dtype=np.float64)
    fy = (lab[..., 0] + 16.0) / 116.0
    fx = fy + lab[..., 1] / 500.0
    fz = fy - lab[..., 2] / 200.0
    delta = 6.0 / 29.0

    def inverse(f):
        return np.where(f > delta, f**3, 3 * delta**2 * (f - 4.0 / 29.0))

    return np.stack([inverse(fx), inverse(fy), inverse(fz)], axis=-1) * white


def xyz_to_lab(xyz, white=D50_WHITE):
    t = np.asarray(xyz, dtype=np.float64) / white
    delta = 6.0 / 29.0
    f = np.where(t > delta**3, np.cbrt(t), t / (3 * delta**2) + 4.0 / 29.0)
    return np.stack(
        [
            116.0 * f[..., 1] - 16.0,
            500.0 * (f[..., 0] - f[..., 1]),
            200.0 * (f[..., 1] - f[..., 2]),
        ],
        axis=-1,
    )


def delta_e_2000(lab1, lab2):
    """CIEDE2000 between two arrays of CIELAB colours."""
    l1, a1, b1 = np.moveaxis(np.asarray(lab1, dtype=np.float64), -1, 0)
    l2, a2, b2 = np.moveaxis(np.asarray(lab2, dtype=np.float64), -1, 0)
    c1, c2 = np.hypot(a1, b1), np.hypot(a2, b2)
    c_mean = (c1 + c2) / 2
    g = 0.5 * (1 - np.sqrt(c_mean**7 / (c_mean**7 + 25.0**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dl, dc = l2 - l1, c2p - c1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(c1p * c2p == 0, 0.0, dh)
    dhh = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dh / 2))
    l_mean = (l1 + l2) / 2
    cp_mean = (c1p + c2p) / 2
    h_sum = h1p + h2p
    h_mean = np.where(
        c1p * c2p == 0,
        h_sum,
        np.where(
            np.abs(h1p - h2p) <= 180,
            h_sum / 2,
            np.where(h_sum < 360, (h_sum + 360) / 2, (h_sum - 360) / 2),
        ),
    )
    t = (
        1
        - 0.17 * np.cos(np.radians(h_mean - 30))
        + 0.24 * np.cos(np.radians(2 * h_mean))
        + 0.32 * np.cos(np.radians(3 * h_mean + 6))
        - 0.20 * np.cos(np.radians(4 * h_mean - 63))
    )
    sl = 1 + 0.015 * (l_mean - 50) ** 2 / np.sqrt(20 + (l_mean - 50) ** 2)
    sc = 1 + 0.045 * cp_mean
    sh = 1 + 0.015 * cp_mean * t
    rt = (
        -2
        * np.sqrt(cp_mean**7 / (cp_mean**7 + 25.0**7))
        * np.sin(np.radians(60 * np.exp(-(((h_mean - 275) / 25) ** 2))))
    )
    return np.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dhh / sh) ** 2 + rt * (dc / sc) * (dhh / sh))


REFERENCE_XYZ = lab_to_xyz(REFERENCE_LAB)


# Finding the patches


def patch_centres(corners):
    """The middle of each of the 24 patches, in reading order, from the middles of the four
    corner patches (top left, top right, bottom right, bottom left: dark skin, bluish
    green, black, white) as (x, y) in any units."""
    grid = np.float32([[0, 0], [COLUMNS - 1, 0], [COLUMNS - 1, ROWS - 1], [0, ROWS - 1]])
    transform = cv2.getPerspectiveTransform(grid, np.float32(corners))
    columns, rows = np.meshgrid(np.arange(COLUMNS), np.arange(ROWS))
    points = np.stack([columns.ravel(), rows.ravel()], axis=-1).astype(np.float32)[None]
    return cv2.perspectiveTransform(points, transform)[0].astype(np.float64)


def sample(image, corners):
    """The median colour of each patch of `image` (H x W x 3, linear) as 24 x 3, with
    `corners` in pixels of `image`. Raises ValueError when a patch is off the photo."""
    centres = patch_centres(corners)
    spacing = min(
        np.linalg.norm(centres[1] - centres[0]), np.linalg.norm(centres[COLUMNS] - centres[0])
    )
    half = max(1, int(spacing * SAMPLE_SHARE / 2))
    height, width = image.shape[:2]
    found = []
    for index, (x, y) in enumerate(centres):
        x, y = int(round(x)), int(round(y))
        if not (half <= x < width - half and half <= y < height - half):
            raise ValueError(f"the {NAMES[index]} patch is off the photo")
        square = image[y - half : y + half + 1, x - half : x + half + 1].reshape(-1, 3)
        found.append(np.median(square.astype(np.float64), axis=0))
    return np.array(found)


# Fitting


@dataclass
class Fitted:
    profile: dcp.Profile
    # CIEDE2000 of each patch, as the profile renders it
    errors: np.ndarray
    # The light's colour temperature, from the greys, in kelvin
    kelvin: float

    @property
    def mean_error(self):
        return float(self.errors.mean())

    @property
    def max_error(self):
        return float(self.errors.max())


def balanced(samples):
    """`samples` (24 x 3 camera colours) scaled per channel so that the greys are grey and
    as light as the reference says: the white balance and exposure of the target."""
    greys = samples[GREYS]
    luminance = REFERENCE_XYZ[GREYS, 1]
    gains = (greys * luminance[:, None]).sum(axis=0) / (greys**2).sum(axis=0)
    return samples * gains, gains


def fit_forward(camera, reference=REFERENCE_XYZ):
    """The forward matrix (white-balanced camera -> XYZ D50) with camera white (1, 1, 1)
    going to D50's white, nearest `reference` (24 x 3) by least squares weighted towards
    dark patches, refined once on the CIELAB error."""
    weights = 1.0 / (reference[:, 1] + 0.05)
    matrix = _constrained(camera, reference, weights)
    for _ in range(4):
        error = delta_e_2000(xyz_to_lab(reference), xyz_to_lab(camera @ matrix.T))
        matrix = _constrained(camera, reference, weights * (1.0 + error))
    return matrix


def _constrained(camera, reference, weights):
    w = weights[:, None]
    a = (camera * w).T @ camera
    ones = np.ones(3)
    rows = []
    for c in range(3):
        b = (camera * w).T @ reference[:, c]
        system = np.block([[2 * a, ones[:, None]], [ones[None, :], np.zeros((1, 1))]])
        rows.append(np.linalg.solve(system, np.concatenate([2 * b, [D50_WHITE[c]]]))[:3])
    return np.array(rows)


def _nearest_illuminant(kelvin):
    standard = (17, 24, 23, 20, 21, 22)
    return min(standard, key=lambda code: abs(1e6 / dcp.ILLUMINANTS[code] - 1e6 / kelvin))


def fit(samples, colour, model, name=None, linear=False):
    """A `Fitted` profile for the camera called `model` from its colours of the target's
    patches (`samples`, 24 x 3, white-balanced as shot, linear) and its `colour`
    (CameraColour). With `linear` the profile keeps the tone as the camera recorded it
    (for reproduction); without, the photo gets Lumina's tone curve, as other profiles.
    Raises ValueError when the patches are not a target's (clicked in another order, or
    not on one)."""
    luminance = np.asarray(samples, dtype=np.float64) @ np.array(colour.rgb_cam)[1]
    greys = luminance[18:24]
    if (greys <= 0).any() or not (np.diff(greys) < 0).all():
        raise ValueError(
            "these are not a ColorChecker's patches: the greys should get darker from white "
            "to black. Click the middle of dark skin, bluish green, black and white, in that "
            "order, with the target the way it reads"
        )
    camera, _gains = balanced(samples)
    try:
        forward = fit_forward(camera)
    except np.linalg.LinAlgError as error:
        raise ValueError("the patches have too few different colours to fit a profile") from error
    errors = delta_e_2000(REFERENCE_LAB, xyz_to_lab(camera @ forward.T))
    rgb_cam = np.array(colour.rgb_cam)
    cam_xyz = np.array(colour.cam_xyz) if colour.cam_xyz is not None else None
    if cam_xyz is None:
        cam_xyz = np.linalg.inv(cp.SRGB_TO_XYZ_D65 @ rgb_cam)
    # The light: the white the camera's greys show, through Adobe's matrix
    grey = samples[GREYS].mean(axis=0) * np.array(colour.neutral)
    kelvin = cp.temperature(cp.xyz_to_xy(np.linalg.solve(cam_xyz, grey)))
    label = name or f"{model} ColorChecker {round(kelvin, -2):.0f} K"
    profile = dcp.Profile(
        name=label,
        camera=model,
        color_matrices=(cam_xyz,),
        illuminants=(_nearest_illuminant(kelvin),),
        forward_matrices=(forward,),
        tone_curve=np.array([[0.0, 0.0], [1.0, 1.0]]) if linear else None,
        copyright="Made by Lumina from a photo of a ColorChecker",
    )
    return Fitted(profile, errors, kelvin)
