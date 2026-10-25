"""How sharp a photo is, and which photo of a stack is the sharpest (#89).

The score is the variance of the Laplacian of the photo's preview at a fixed size, taken
in the most detailed tile of a grid, so a subject sharp against a soft background scores
as sharp as it is. It says nothing across different scenes (a brick wall scores higher
than a clear sky): only the photos of one stack, shots of the same scene, are compared.

Pure: nothing here reads the library or changes it, apart from `score_files`, which the
worker process runs on files.
"""

import os
from dataclasses import dataclass

import cv2
import numpy as np

from lumina.core import images
from lumina.core.dbtypes import PICKED, REJECTED
from lumina.core.errors import PhotoError

# The preview's longer side, in pixels, that the score is taken at
SIDE = 1024
# The grid the most detailed tile is looked for in
TILES = 4
# A photo scoring less than this share of the best of its stack is blurry
BLURRY_SHARE = 0.5
# Photos scoring within this share of the best one are as sharp: the score is noise below it
TIE = 0.05


@dataclass(frozen=True)
class Member:
    """A photo of a stack, as `pick` weighs it: `score` is None while it is not scored."""

    id: int
    taken: str
    flag: int
    rating: int
    score: float | None


def score_gray(gray):
    """The score of a grey image (uint8, 2D) scaled to SIDE on its longer side."""
    height, width = gray.shape
    scale = SIDE / max(height, width)
    if scale != 1:
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        gray = cv2.resize(
            gray, size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        )
    # A touch of blur so that the grain of a high ISO is not taken for detail
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    laplacian = cv2.Laplacian(gray, cv2.CV_32F)
    height, width = laplacian.shape
    best = 0.0
    for row in range(TILES):
        for column in range(TILES):
            tile = laplacian[
                row * height // TILES : (row + 1) * height // TILES,
                column * width // TILES : (column + 1) * width // TILES,
            ]
            if tile.size:
                best = max(best, float(tile.var()))
    return best


def score_pixels(pixels):
    """The score of `pixels` (images.Pixels, RGB)."""
    rgb = np.frombuffer(pixels.data, np.uint8).reshape(pixels.height, pixels.width, 3)
    return score_gray(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))


def score_file(path):
    """The score of the photo at `path`, from its preview. Raises PhotoError naming it."""
    return score_pixels(images.load_pixels(path, SIDE))


_lowered = False


def score_files(paths):
    """For a worker process: [score or PhotoError] for each path, in order. The process
    lowers its own priority the first time, so the UI and the Develop view go first."""
    global _lowered
    if not _lowered:
        _lowered = True
        try:
            os.nice(10)
        except OSError:
            pass
    results = []
    for path in paths:
        try:
            results.append(score_file(path))
        except PhotoError as error:
            results.append(error)
        except (cv2.error, ValueError, MemoryError) as error:
            results.append(PhotoError(path, f"Could not be scored for sharpness: {error}"))
    return results


def is_blurry(score, best):
    """Whether a photo scoring `score` is blurry beside the best of its stack, `best`.
    A photo not scored is not told."""
    return score is not None and best is not None and score < best * BLURRY_SHARE


def pick(members):
    """The id of the photo to put on top of a stack: the sharpest, but among those within
    TIE of it the one already given a thumbs up, then the most stars, then the first
    taken. None when one of the photos is not scored yet, or there are none."""
    if not members or any(m.score is None for m in members):
        return None
    top = max(m.score for m in members)
    close = [m for m in members if m.score >= top * (1 - TIE)]
    order = {PICKED: 0, REJECTED: 2}
    return min(close, key=lambda m: (order.get(m.flag, 1), -m.rating, m.taken, m.id)).id
