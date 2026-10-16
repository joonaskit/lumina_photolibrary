"""Panoramas: overlapping photos taken turning the camera, stitched into one photo of
linear light.

The job (`run`) goes in steps, each a call the job's thread makes in a worker process
(`call`), so a cancel waits for one step at most and a crash resumes from the last
step done:

1. Decode (`decode_frame`): the full linear decode of each frame, RAW files with the
   first one's white balance, kept on disk in the job's work folder, with a copy of
   about a megapixel.
2. Plan (`make_plan`), on those copies: features (SIFT) matched between the frames; a
   rotation and focal length for each frame (OpenCV's cv2.detail: from the matches'
   homographies, refined by bundle adjustment of rays, then turned so the horizon is
   level); gains that make the frames' exposures alike, measured in linear light where
   they overlap; seams by graph cut on copies of about 0.1 MP, and from them a
   feathered weight for each frame; and the part of the result kept: the largest
   rectangle all covered, or everything. Kept in the work folder: a resumed job
   stitches again from the first row with the geometry found.
3. Stitch (`stitch_rows`), a band of rows at a time, into the result file: for each
   pixel of the band, where it is in each frame (the projection run backwards; maps
   made here for the band alone, where OpenCV's warpers warp a whole frame at once),
   that frame's light there times its gain, the frames weighted by their feathered
   weights. Feathering needs only the band, where multi-band blending would need the
   whole result in memory.

The result is a linear TIFF (core/linear_tiff.py) as an HDR merge's, at most
MAX_PIXELS (made smaller to fit), beside the first photo, named after it with "-pano"
(then -pano-2 and so on: a file that is there is never written over). The originals
are only read.

Memory: one frame's decode at a time while decoding; then the small copies while
planning; then a band of rows of the result and the part of each frame it comes from.
The work folder holds every frame decoded (as much on disk) until the job ends.
"""

import json
import math
import os

import cv2
import numpy as np

from lumina.core import db as dbm
from lumina.core import derived, raw
from lumina.core.derived import finish_result, start_result
from lumina.core.edit import source
from lumina.core.errors import PhotoError

PANORAMA = "panorama"
DECODING, PLANNING, STITCHING = derived.DECODING, "planning", "stitching"
SETTINGS_VERSION = 1
MIN_FRAMES, MAX_FRAMES = 2, 60
CYLINDRICAL, SPHERICAL, PERSPECTIVE = "cylindrical", "spherical", "perspective"
PROJECTIONS = (CYLINDRICAL, SPHERICAL, PERSPECTIVE)
# The result is made smaller to be at most this
MAX_PIXELS = 300_000_000
# The copies the geometry is found on, and the seams
WORK_PIXELS = 1_000_000
SEAM_PIXELS = 100_000
# Matches between two frames: how alike two features must be (SIFT's ratio test), and
# how sure a pair must be to count (OpenCV's confidence, from the inliers)
_MATCH_CONFIDENCE = 0.65
_PAIR_CONFIDENCE = 1.0
# Two frames whose corners move less than this share of the diagonal from one to the
# other show the same view
_SAME_VIEW = 0.03
# Where a frame's median light is put before its features are found
_MIDDLE = 0.18
# Exposures are compared over the pixels both frames have this well exposed
_MEASURE_LOW, _MEASURE_HIGH = 0.02, 0.80
_MEASURE_LEAST = 200
# Frames cross-fade over this share of a frame's width, either side of a seam
_FEATHER = 0.015
# Every pixel a frame covers has at least this weight: where the seams' coarser masks
# leave a pixel to no frame, the frames there share it
_WEIGHT_FLOOR = 1e-3
# A frame's weight goes from all to none as its brightest channel goes from here to
# there of white, in a frame that clips (a RAW file's or a JPEG's; not a merge's)
_CLIP_FROM, _CLIP_AT = 0.80, 0.95
# Rows stitched in one call, and the widest strip remapped at once (cv2.remap takes
# fewer than 32767 columns)
BAND = 128
_STRIP = 8192


# Choosing the frames


def check_frames(rows):
    """Raise ValueError saying why the photos `rows` (dicts, with their path) cannot be
    stitched: too few or many, of more than one camera, or RAW files with others."""
    if len(rows) < MIN_FRAMES:
        raise ValueError(f"A panorama needs at least {MIN_FRAMES} photos")
    if len(rows) > MAX_FRAMES:
        raise ValueError(f"A panorama takes at most {MAX_FRAMES} photos")
    derived.check_camera(rows)
    first = raw.is_raw(rows[0]["path"])
    for row in rows[1:]:
        if raw.is_raw(row["path"]) != first:
            kind = "a RAW file" if not first else "not a RAW file"
            raise ValueError(f"{os.path.basename(row['path'])} is {kind}, the others are not")


def settings_for(rows, projection=CYLINDRICAL, crop=True):
    """What a panorama of the photos `rows` (dicts with their details) keeps in the
    library: the frames in the order taken, the first one's white balance and name.
    Returns (rows in that order, settings JSON)."""
    if projection not in PROJECTIONS:
        raise ValueError(f"unknown projection {projection!r}")
    rows = sorted(rows, key=lambda row: (row.get("taken") or "", row["path"]))
    settings = {
        "version": SETTINGS_VERSION,
        "projection": projection,
        "crop": bool(crop),
        "beside": 0,
        "name": os.path.splitext(os.path.basename(rows[0]["path"]))[0] + "-pano",
        "tags": derived.tags_of(rows[0]),
    }
    return rows, json.dumps(settings, sort_keys=True)


def queue(library, photo_ids, projection=CYLINDRICAL, crop=True):
    """Queue a panorama of the photos `photo_ids` and return the job's id. Raises
    ValueError saying why they cannot be stitched."""
    rows = derived.rows_to_queue(library, photo_ids)
    check_frames(rows)
    rows, settings = settings_for(rows, projection, crop)
    sources = [dbm.Source(row["volume_key"], row["path"], row["content_id"]) for row in rows]
    return library.add_derived_job(PANORAMA, settings, sources, rows[0]["folder_id"])


# Projections. A point of the result (u, v), in pixels of the result, is a direction
# seen from where the camera turned; a frame's pixel (x, y) is the direction
# R K^-1 (x, y, 1), K its camera matrix and R its rotation


def _directions(kind, scale, u, v):
    """The directions (X, Y, Z) of the result's points `u` (columns, 1-D) and `v` (rows,
    1-D) of a result of `scale` pixels a radian, as arrays that broadcast to rows x
    columns."""
    a = (np.asarray(u, np.float64) / scale)[None, :]
    b = (np.asarray(v, np.float64) / scale)[:, None]
    if kind == SPHERICAL:
        ring = np.sin(math.pi - b)
        return ring * np.sin(a), np.cos(math.pi - b), ring * np.cos(a)
    if kind == CYLINDRICAL:
        return np.sin(a), b, np.cos(a)
    return a, b, np.ones_like(a)


def _to_frame(kind, scale, camera, u, v):
    """Where the result's points (columns `u`, rows `v`) are in a frame of `camera` (K,
    R): (x, y, ahead), rows x columns; ahead is false where the point is behind it."""
    k, r = camera
    m = k @ r.T
    x, y, z = _directions(kind, scale, u, v)
    px = m[0, 0] * x + m[0, 1] * y + m[0, 2] * z
    py = m[1, 0] * x + m[1, 1] * y + m[1, 2] * z
    pz = m[2, 0] * x + m[2, 1] * y + m[2, 2] * z
    ahead = pz > 1e-9
    pz = np.where(ahead, pz, 1.0)
    return px / pz, py / pz, ahead


def _to_result(kind, scale, camera, x, y):
    """Where a frame's points (`x`, `y`, 1-D) land in the result: (u, v). Raises
    ValueError when one cannot (behind a perspective result's plane)."""
    k, r = camera
    rays = r @ np.linalg.inv(k) @ np.stack([x, y, np.ones_like(x)])
    rx, ry, rz = rays
    if kind == PERSPECTIVE:
        if np.any(rz <= 1e-6):
            raise ValueError("behind the plane")
        return scale * rx / rz, scale * ry / rz
    u = scale * np.arctan2(rx, rz)
    if kind == CYLINDRICAL:
        return u, scale * ry / np.hypot(rx, rz)
    w = np.clip(ry / np.linalg.norm(rays, axis=0), -1.0, 1.0)
    return u, scale * (math.pi - np.arccos(w))


def _roi(kind, scale, camera, width, height, margin=2):
    """(left, top, right, bottom) of the result's pixels a frame of `width` x `height`
    may cover: its edges and a grid across it sent forward, and `margin` pixels more
    either way for what falls between them."""
    edge_x = np.linspace(-0.5, width - 0.5, 257)
    edge_y = np.linspace(-0.5, height - 0.5, 257)
    inner_x, inner_y = np.meshgrid(edge_x[::16], edge_y[::16])
    x = np.concatenate([edge_x, edge_x, np.full(257, -0.5), np.full(257, width - 0.5)])
    y = np.concatenate([np.full(257, -0.5), np.full(257, height - 0.5), edge_y, edge_y])
    x = np.concatenate([x, inner_x.ravel()])
    y = np.concatenate([y, inner_y.ravel()])
    u, v = _to_result(kind, scale, camera, x, y)
    return (
        math.floor(u.min()) - margin,
        math.floor(v.min()) - margin,
        math.ceil(u.max()) + 1 + margin,
        math.ceil(v.max()) + 1 + margin,
    )


def _warp(image, factor, kind, scale, camera, roi, size):
    """`image` (a frame `factor` of its full `size`, width x height, as floats) as the
    result's pixels `roi` see it: (pixels, covered)."""
    left, top, right, bottom = roi
    x, y, ahead = _to_frame(kind, scale, camera, np.arange(left, right), np.arange(top, bottom))
    width, height = size
    covered = ahead & (x >= -0.5) & (x <= width - 0.5) & (y >= -0.5) & (y <= height - 0.5)
    # Into the copy's pixels
    sx = ((x + 0.5) * factor - 0.5).astype(np.float32)
    sy = ((y + 0.5) * factor - 0.5).astype(np.float32)
    pixels = cv2.remap(image, sx, sy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    pixels[~covered] = 0
    return pixels, covered


# The steps, in a worker process


def _frame(work, index, what):
    return os.path.join(work, f"{what}-{index}")


def frame_done(work, index):
    return os.path.exists(_frame(work, index, "frame") + ".json")


def plan_done(work):
    return os.path.exists(os.path.join(work, "plan.json"))


def _save(path, array):
    # Under another name first: a step cut short leaves nothing that looks done
    with open(path + ".tmp", "wb") as file:
        np.save(file, array)
    os.replace(path + ".tmp", path)


def _save_json(path, data):
    with open(path + ".tmp", "w") as file:
        json.dump(data, file)
    os.replace(path + ".tmp", path)


def decode_frame(path, index, work, white_balance):
    """Decode frame `index` (the photo at `path`; a RAW file with `white_balance`, or the
    camera's when None) into the work folder `work`, with a copy of about WORK_PIXELS.
    Frames after the first must be of its size. Raises PhotoError naming the file."""
    if raw.is_raw(path):
        linear = source.raw_linear(path, white_balance=white_balance)
    else:
        linear = source.load_linear(path)
    height, width = linear.shape[:2]
    if index > 0:
        with open(_frame(work, 0, "frame") + ".json") as file:
            wanted = json.load(file)["shape"]
        if [height, width] != wanted:
            raise PhotoError(
                path,
                f"is {width} x {height}, the other photos {wanted[1]} x {wanted[0]}: "
                "they are not of one size",
            )
    small = source.resize(linear, *source.fit(width, height, WORK_PIXELS), quality=False)
    _save(_frame(work, index, "small") + ".npy", small)
    _save(_frame(work, index, "frame") + ".npy", linear)
    # What white is in the decode, and whether it clips there (a merge's does not)
    clips = linear.dtype == np.uint16
    info = {"shape": [height, width], "white": 65535.0 if clips else 1.0, "clips": clips}
    _save_json(_frame(work, index, "frame") + ".json", info)


def make_plan(work, count, projection, crop, paths):
    """Find how the `count` frames decoded in `work` go together, and keep it there.
    Returns the result's (width, height). Raises PhotoError naming a frame that does not
    fit with the others."""
    infos, smalls = [], []
    for index in range(count):
        with open(_frame(work, index, "frame") + ".json") as file:
            infos.append(json.load(file))
        smalls.append(np.load(_frame(work, index, "small") + ".npy"))
    height, width = infos[0]["shape"]
    factor = smalls[0].shape[1] / width
    cameras = _cameras(smalls, factor, paths)
    scale = float(np.median([k[0, 0] for k, _r in cameras]))
    try:
        # Made smaller until all the frames cover fits in MAX_PIXELS
        while True:
            whole = _union([_roi(projection, scale, c, width, height, 0) for c in cameras])
            pixels = (whole[2] - whole[0]) * (whole[3] - whole[1])
            if pixels <= MAX_PIXELS:
                break
            scale *= math.sqrt(MAX_PIXELS / pixels) * 0.99
        rois = [_roi(projection, scale, camera, width, height) for camera in cameras]
    except ValueError:
        raise PhotoError(
            paths[0], "the photos take in too wide a view for a perspective panorama"
        ) from None
    # The seams, gains and weights on smaller copies still, `ratio` of the result's size
    ratio = min(1.0, math.sqrt(SEAM_PIXELS / (width * height)))
    seam_scale = scale * ratio
    seam_rois = [_roi(projection, seam_scale, camera, width, height) for camera in cameras]
    warped = [
        _warp(small, factor, projection, seam_scale, camera, roi, (width, height))
        for small, camera, roi in zip(smalls, cameras, seam_rois, strict=True)
    ]
    del smalls
    gains = _gains(warped, seam_rois, [info["clips"] for info in infos])
    seams = _seams(warped, seam_rois, gains)
    feather = max(1, round(_FEATHER * width * ratio))
    for index, (seam, (_pixels, covered)) in enumerate(zip(seams, warped, strict=True)):
        _save(_frame(work, index, "weight") + ".npy", _weight(seam, covered, feather))
    box = _covered_box(warped, seam_rois, ratio) if crop else whole
    if box is None:
        raise PhotoError(paths[0], "the photos do not overlap enough to make a panorama")
    plan = {
        "projection": projection,
        "scale": scale,
        "ratio": ratio,
        "cameras": [[k.tolist(), r.tolist()] for k, r in cameras],
        "rois": rois,
        "seam_rois": seam_rois,
        "gains": gains,
        "whites": [info["white"] for info in infos],
        "clips": [info["clips"] for info in infos],
        "shape": [height, width],
        "box": list(box),
    }
    _save_json(os.path.join(work, "plan.json"), plan)
    return box[2] - box[0], box[3] - box[1]


def load_plan(work):
    with open(os.path.join(work, "plan.json")) as file:
        plan = json.load(file)
    plan["cameras"] = [(np.array(k), np.array(r)) for k, r in plan["cameras"]]
    return plan


def plan_size(work):
    """(width, height) of the result planned in `work`."""
    left, top, right, bottom = load_plan(work)["box"]
    return right - left, bottom - top


def _encoded(small):
    """A small copy (linear floats) as 8 bits, gamma-encoded, brought to one brightness
    (its median at _MIDDLE): what features are found on. Frames of different exposures
    then show the same features, which SIFT's contrast threshold would not find in a
    dark one."""
    middle = float(np.median(small[..., 1]))
    level = _MIDDLE / middle if middle > 1e-6 else 1.0
    grey = np.sqrt(np.clip(small * np.float32(level), 0.0, 1.0)) * 255 + 0.5
    return grey.astype(np.uint8)


def _cameras(smalls, factor, paths):
    """[(K, R)] of each frame, K for its full size. Raises PhotoError naming a frame that
    has nothing in common with the others that is found."""
    finder = cv2.SIFT_create()
    features = []
    for index, small in enumerate(smalls):
        found = cv2.detail.computeImageFeatures2(finder, _encoded(small))
        found.img_idx = index
        features.append(found)
    matcher = cv2.detail_BestOf2NearestMatcher(False, _MATCH_CONFIDENCE)
    pairs = matcher.apply2(features)
    matcher.collectGarbage()
    height, width = smalls[0].shape[:2]
    same = {}
    for pair in pairs:
        if pair.src_img_idx < 0 or pair.confidence > 0 or pair.num_inliers == 0:
            continue
        # OpenCV's matcher drops a pair whose matches are over about 90 % inliers, taking
        # them for the same view. Clean frames that overlap well can be that too: only
        # a pair whose corners hardly move from one to the other is the same view
        if _same_view(pair.H, width, height):
            same[pair.src_img_idx] = pair.dst_img_idx
        else:
            pair.confidence = pair.num_inliers / (8 + 0.3 * len(pair.matches))
    kept = set(cv2.detail.leaveBiggestComponent(features, pairs, _PAIR_CONFIDENCE).ravel())
    alone = [index for index in range(len(smalls)) if index not in kept]
    if alone and alone[0] in same:
        raise PhotoError(
            paths[alone[0]],
            f"shows nearly the same view as {os.path.basename(paths[same[alone[0]]])}: "
            "choose one of them, the photos of a panorama each show more of the view",
        )
    if alone:
        raise PhotoError(
            paths[alone[0]],
            "does not overlap the other photos, or too little of it does: nothing in it "
            "was found in them",
        )
    cannot = PhotoError(paths[0], "the photos could not be put together")
    ok, cameras = cv2.detail_HomographyBasedEstimator().apply(features, pairs, None)
    if not ok:
        raise cannot
    for camera in cameras:
        camera.R = camera.R.astype(np.float32)
    adjuster = cv2.detail_BundleAdjusterRay()
    adjuster.setConfThresh(_PAIR_CONFIDENCE)
    adjuster.setRefinementMask(np.array([[1, 1, 1], [0, 1, 1], [0, 0, 0]], np.uint8))
    ok, cameras = adjuster.apply(features, pairs, cameras)
    if not ok:
        raise cannot
    rotations = cv2.detail.waveCorrect(
        [np.array(camera.R) for camera in cameras], cv2.detail.WAVE_CORRECT_HORIZ
    )
    # From the small copy's pixels to the full frame's: x = s x' + (s - 1) / 2
    s = 1 / factor
    full = np.array([[s, 0, (s - 1) / 2], [0, s, (s - 1) / 2], [0, 0, 1]])
    found = []
    for camera, rotation in zip(cameras, rotations, strict=True):
        k = full @ np.asarray(camera.K(), np.float64)
        r = np.asarray(rotation, np.float64)
        if not (np.all(np.isfinite(k)) and np.all(np.isfinite(r))) or k[0, 0] <= 0:
            raise cannot
        found.append((k, r))
    return found


def _same_view(homography, width, height):
    """Whether `homography` (between two frames of `width` x `height`) moves no corner
    more than _SAME_VIEW of the frame's diagonal: the frames show the same view."""
    if homography is None or np.asarray(homography).size != 9:
        return False
    corners = np.array([[0, 0], [width, 0], [0, height], [width, height]], np.float64)
    moved = cv2.perspectiveTransform(corners[None], np.asarray(homography, np.float64))[0]
    return bool(np.abs(moved - corners).max() < _SAME_VIEW * math.hypot(width, height))


def _union(rois):
    return (
        min(roi[0] for roi in rois),
        min(roi[1] for roi in rois),
        max(roi[2] for roi in rois),
        max(roi[3] for roi in rois),
    )


def _overlap(a, b):
    box = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return box if box[0] < box[2] and box[1] < box[3] else None


def _cut(array, roi, box):
    """The part `box` of `array`, which is the result's part `roi`."""
    return array[box[1] - roi[1] : box[3] - roi[1], box[0] - roi[0] : box[2] - roi[0]]


def _gains(warped, rois, clips):
    """How much each frame is to be brightened to look as its neighbours, from the light
    of the pixels each pair has well exposed where they overlap: [gain], their
    geometric mean 1."""
    count = len(warped)
    luma = np.array([0.2126, 0.7152, 0.0722], np.float32)
    lumas = [pixels @ luma for pixels, _covered in warped]
    equations, sides = [], []
    for i in range(count):
        for j in range(i + 1, count):
            box = _overlap(rois[i], rois[j])
            if box is None:
                continue
            a, b = _cut(lumas[i], rois[i], box), _cut(lumas[j], rois[j], box)
            good = _cut(warped[i][1], rois[i], box) & _cut(warped[j][1], rois[j], box)
            good &= (a > _MEASURE_LOW) & (b > _MEASURE_LOW)
            if clips[i]:
                good &= a < _MEASURE_HIGH
            if clips[j]:
                good &= b < _MEASURE_HIGH
            if np.count_nonzero(good) < _MEASURE_LEAST:
                continue
            # gain_i a = gain_j b: log gain_i - log gain_j = log(b / a)
            row = np.zeros(count)
            row[i], row[j] = 1.0, -1.0
            weight = math.sqrt(np.count_nonzero(good))
            equations.append(row * weight)
            sides.append(math.log(float(np.median(b[good] / a[good]))) * weight)
    # Their geometric mean 1, weighed as much as all the pairs
    equations.append(np.ones(count) * max(1.0, sum(abs(row).max() for row in equations)))
    sides.append(0.0)
    logs = np.linalg.lstsq(np.array(equations), np.array(sides), rcond=None)[0]
    return [float(math.exp(value)) for value in logs]


def _seams(warped, rois, gains):
    """Each frame's part of the result, by graph cut where frames overlap (OpenCV's
    GraphCutSeamFinder, on the colours gamma-encoded): [mask as uint8]."""
    images = [
        np.ascontiguousarray(np.sqrt(np.clip(pixels * np.float32(gain), 0.0, 1.0)) * 255)
        for (pixels, _covered), gain in zip(warped, gains, strict=True)
    ]
    masks = [covered.astype(np.uint8) * 255 for _pixels, covered in warped]
    corners = [(int(roi[0]), int(roi[1])) for roi in rois]
    finder = cv2.detail_GraphCutSeamFinder("COST_COLOR")
    found = finder.find(images, corners, masks)
    return [np.asarray(mask.get() if isinstance(mask, cv2.UMat) else mask) for mask in found]


def _weight(seam, covered, feather):
    """A frame's feathered weight: 1 inside its part, falling to 0 over 2 x `feather`
    pixels across its seams (its part grown by `feather`) and to its edges."""
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * feather + 1, 2 * feather + 1))
    grown = cv2.dilate((seam > 0).astype(np.uint8), kernel)
    grown &= covered.astype(np.uint8)
    distance = cv2.distanceTransform(grown, cv2.DIST_L2, 3)
    return np.minimum(distance / np.float32(2 * feather), 1.0).astype(np.float32)


def _covered_box(warped, rois, ratio):
    """The largest rectangle of the result every pixel of which some frame covers, in the
    result's pixels (found on the seams' copies, `ratio` of its size, and kept a pixel of
    them inside), or None."""
    canvas = _union(rois)
    covered = np.zeros((canvas[3] - canvas[1], canvas[2] - canvas[0]), bool)
    for (_pixels, mask), roi in zip(warped, rois, strict=True):
        _cut(covered, canvas, roi)[...] |= mask
    # Fewer pixels to search: a block counts when all of it is covered
    step = max(1, math.ceil(math.sqrt(covered.size / 200_000)))
    if step > 1:
        height, width = covered.shape[0] // step, covered.shape[1] // step
        blocks = covered[: height * step, : width * step].reshape(height, step, width, step)
        covered = blocks.all(axis=(1, 3))
    found = _largest_rectangle(covered)
    if found is None:
        return None
    left, top, right, bottom = (value * step for value in found)
    # Seam pixel p is the result's p / ratio; a pixel in from each side
    box = (
        math.ceil((canvas[0] + left + 1) / ratio),
        math.ceil((canvas[1] + top + 1) / ratio),
        math.floor((canvas[0] + right - 2) / ratio) + 1,
        math.floor((canvas[1] + bottom - 2) / ratio) + 1,
    )
    return box if box[0] < box[2] and box[1] < box[3] else None


def _largest_rectangle(mask):
    """(left, top, right, bottom) of the largest rectangle all true in `mask`, or None."""
    height, width = mask.shape
    heights = np.zeros(width, np.int64)
    best, found = 0, None
    for row in range(height):
        heights = np.where(mask[row], heights + 1, 0)
        stack = []
        for column, high in enumerate([*heights.tolist(), 0]):
            start = column
            while stack and stack[-1][1] >= high:
                start, tall = stack.pop()
                area = tall * (column - start)
                if area > best:
                    best, found = area, (start, row - tall + 1, column, row + 1)
            stack.append((start, high))
    return found


def stitch_rows(work, part, tags, first, last):
    """Rows `first` to `last` of the result, stitched from the frames in `work`, written
    to the result file `part` (laid out by `start_result`)."""
    plan = load_plan(work)
    left, top, right, bottom = plan["box"]
    width, height = right - left, bottom - top
    kind, scale, ratio = plan["projection"], plan["scale"], plan["ratio"]
    frame_height, frame_width = plan["shape"]
    rows = (top + first, top + last)
    total = np.zeros((last - first, width, 3), np.float32)
    weight = np.zeros((last - first, width), np.float32)
    for index, camera in enumerate(plan["cameras"]):
        roi = plan["rois"][index]
        box = _overlap(roi, (left, rows[0], right, rows[1]))
        if box is None:
            continue
        frame = np.load(_frame(work, index, "frame") + ".npy", mmap_mode="r")
        seam_weight = np.load(_frame(work, index, "weight") + ".npy")
        level = np.float32(plan["gains"][index] / plan["whites"][index])
        for start in range(box[0], box[2], _STRIP):
            strip = (start, box[1], min(box[2], start + _STRIP), box[3])
            u, v = np.arange(strip[0], strip[2]), np.arange(strip[1], strip[3])
            x, y, ahead = _to_frame(kind, scale, camera, u, v)
            covered = ahead & (x >= -0.5) & (x <= frame_width - 0.5)
            covered &= (y >= -0.5) & (y <= frame_height - 0.5)
            if not covered.any():
                continue
            # Only the part of the frame these come from, a pixel around
            x0 = max(0, math.floor(x[covered].min()) - 1)
            x1 = min(frame_width, math.ceil(x[covered].max()) + 2)
            y0 = max(0, math.floor(y[covered].min()) - 1)
            y1 = min(frame_height, math.ceil(y[covered].max()) + 2)
            window = np.asarray(frame[y0:y1, x0:x1], dtype=np.float32)
            window *= level
            if plan["clips"][index]:
                # 1 well under clipping, 0 at it: where another frame has the light a
                # clipped one lost, in their overlap, it is taken from there
                peak = window.max(axis=2) * np.float32(1 / plan["gains"][index])
                room = np.clip((_CLIP_AT - peak) / (_CLIP_AT - _CLIP_FROM), 0.0, 1.0)
            pixels = cv2.remap(
                window,
                (x - x0).astype(np.float32),
                (y - y0).astype(np.float32),
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            # The seam's weight, from its smaller copy
            seam_roi = plan["seam_rois"][index]
            su = np.broadcast_to((u * ratio - seam_roi[0]).astype(np.float32), x.shape)
            sv = np.broadcast_to((v * ratio - seam_roi[1]).astype(np.float32)[:, None], x.shape)
            w = cv2.remap(
                seam_weight,
                np.ascontiguousarray(su),
                np.ascontiguousarray(sv),
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            w += np.float32(_WEIGHT_FLOOR)
            if plan["clips"][index]:
                w *= cv2.remap(
                    room.astype(np.float32),
                    (x - x0).astype(np.float32),
                    (y - y0).astype(np.float32),
                    cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE,
                )
                # Where every frame is clipped they share it
                w += np.float32(_WEIGHT_FLOOR * _WEIGHT_FLOOR)
            w[~covered] = 0
            place = (
                slice(strip[1] - rows[0], strip[3] - rows[0]),
                slice(strip[0] - left, strip[2] - left),
            )
            total[place] += pixels * w[..., None]
            weight[place] += w
    nothing = weight <= 0
    weight[nothing] = 1.0
    total /= weight[..., None]
    derived.write_rows(part, width, height, tags, first, total)
    return last - first


# The job, on a thread


def run(
    library,
    job_id,
    mounts,
    call=lambda fn, *args: fn(*args),
    should_stop=lambda: False,
    report=lambda phase, done, total, changed: None,
    work_root=None,
    keep_work=lambda: False,
):
    """Run panorama job `job_id` to its end and return its state. `call(fn, *args)` runs
    a step (in a worker process, in the app). A job stopped while `keep_work()` is true
    (a quit) keeps its decoded frames and plan, for the next start; otherwise the work
    folder goes when the job ends. Raises NotConnected, leaving the job queued, when a
    source's drive is not mounted."""

    def make(job, paths, settings, work, part):
        return _stitch(job, paths, settings, work, part, call, should_stop)

    return derived.run(
        library,
        job_id,
        mounts,
        make,
        lambda count, name: f"Stitched {count} photos into {name}",
        report,
        work_root,
        keep_work,
    )


def _stitch(job, paths, settings, work, part, call, should_stop):
    """(state, message or the result's path, PhotoError or None)."""
    tags = {key: value for key, value in settings["tags"].items() if value}
    tags["description"] = f"Panorama of {len(paths)} photos"
    cancelled = (dbm.CANCELLED, "Cancelled while stitching", None)
    try:
        derived.check_sources(paths)
        os.makedirs(work, exist_ok=True)
        balance = None
        if raw.is_raw(paths[0]):
            balance = call(source.camera_white_balance, paths[0])
        for index, path in enumerate(paths):
            if should_stop():
                return cancelled
            if not frame_done(work, index):
                call(decode_frame, path, index, work, balance)
            job.progress(DECODING, index + 1, len(paths), force=True)
        if should_stop():
            return cancelled
        job.progress(PLANNING, 0, None, force=True)
        if plan_done(work):
            width, height = call(plan_size, work)
        else:
            width, height = call(
                make_plan, work, len(paths), settings["projection"], settings["crop"], paths
            )
        call(start_result, part, width, height, tags)
        for first in range(0, height, BAND):
            if should_stop():
                return cancelled
            last = min(height, first + BAND)
            call(stitch_rows, work, part, tags, first, last)
            job.progress(STITCHING, last, height)
        job.progress(STITCHING, height, height, force=True)
        call(finish_result, part)
        if should_stop():
            return cancelled
        return dbm.DONE, derived.place(part, os.path.dirname(part), settings["name"]), None
    except (PhotoError, OSError) as error:
        return derived.failure(error, part, "stitch")
