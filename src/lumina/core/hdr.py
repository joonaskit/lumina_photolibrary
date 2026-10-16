"""HDR merge: a bracket of RAW exposures made one photo of linear light, past white.

The job (`run`) goes in steps, each a call the job's thread makes in a worker
process (`call`), so a cancel waits for one step at most and a crash resumes from
the last step done:

1. Decode (`decode_frame`), the reference frame (the middle exposure) first: the full
   linear decode of each frame, all with the reference's white balance, kept on disk
   as 16 bits in the job's work folder; and how far the frame is shifted from the
   reference, found with median threshold bitmaps (OpenCV's AlignMTB), which do not
   care about exposure.
2. Plan (`make_plan`): how much brighter each frame is than the reference, measured
   from the pixels neighbouring frames both have well exposed (a camera's 1/60 s is
   not quite 1/60 s; the metadata is the fallback), and the part every frame covers
   once shifted, which the result is cut to.
3. Merge (`merge_rows`), a band of rows at a time, into the result file: each pixel
   is the frames' light scaled to the reference's exposure, weighted by how much light
   the frame caught (a brighter frame has less noise) and by how far it is from
   clipping. Where every frame is clipped, the darkest one's value. Deghosting leaves
   out of a pixel the frames that disagree with the reference there (or, where it is
   clipped, with the next darker frame), so something that moved comes from one frame.

The result is a linear TIFF (core/linear_tiff.py), 1.0 at the reference's white,
beside the first photo of the bracket, named after it with "-HDR" (then -HDR-2 and so
on: a file that is there is never written over). It is written under a hidden name and
put in place by an exclusive create; a cancel or failure leaves nothing. The originals
are only read.

Memory: one frame's decode at a time (190 MB at 32 MP), then bands of rows. The work
folder holds every frame decoded (as much on disk) until the job ends.
"""

import json
import math
import os
from dataclasses import dataclass

import cv2
import numpy as np

from lumina.core import db as dbm
from lumina.core import derived, raw
from lumina.core.derived import finish_result, start_result
from lumina.core.edit import source
from lumina.core.errors import PhotoError

HDR = "hdr"
DECODING, PLANNING, MERGING = derived.DECODING, "planning", "merging"
SETTINGS_VERSION = 1
MIN_FRAMES, MAX_FRAMES = 2, 9
# Deghosting: how far (in stops) a frame may be from the guide before it is left out
DEGHOST = {"off": None, "low": 1.0, "medium": 0.6, "high": 0.35}
# A frame's weight goes from all to none as its brightest channel goes from here to there
_CLIP_FROM, _CLIP_AT = 0.80, 0.95
# Pixels both frames of a pair have this well exposed tell their exposures apart
_MEASURE_LOW, _MEASURE_HIGH = 0.02, 0.80
_MEASURE_PIXELS = 1_000_000
_MEASURE_LEAST = 200
# What deghosting adds to light before comparing it, so noise in the dark is not a ghost
_GHOST_FLOOR = 0.004
_GHOST_BLUR = 9
# Alignment: levels of the pyramid (a shift of up to 2^levels - 1 pixels), what counts as
# clipped in the 8-bit copies compared, and how near the median a pixel is noise
_ALIGN_LEVELS = 6
_ALIGN_CLIPPED = 250
_ALIGN_NOISE = 4
# Rows merged in one call
BAND = 128
# Brackets: frames at most this many seconds apart, of one camera, lens and focal length
BRACKET_GAP = 2.0
# Exposures closer than this (stops) are the same: a burst, not a bracket
_SAME_EXPOSURE = 1 / 3
_LEAST_SPREAD = 0.9


# Choosing the frames


def exposure_value(row):
    """How much light a photo let in, in stops (more is brighter), from its details: a
    dict with exposure_time, aperture and iso. None when one is not known."""
    t, n, iso = row.get("exposure_time"), row.get("aperture"), row.get("iso")
    if not t or not n or not iso or t <= 0 or n <= 0 or iso <= 0:
        return None
    return math.log2(t * iso / (n * n))


def brackets(rows):
    """The brackets among `rows` (photo dicts with id, taken, model, lens, focal_length
    and the exposure's details), as lists of ids in time order: runs of photos at most
    BRACKET_GAP seconds apart, of one camera, lens and focal length, at exposures that
    differ, spread at least about a stop."""
    found = []
    group = []
    for row in sorted(rows, key=lambda r: (r["taken"], r.get("path", ""))):
        if group and _continues(group, row):
            group.append(row)
            continue
        _keep_bracket(group, found)
        group = [row]
    _keep_bracket(group, found)
    return found


def _continues(group, row):
    last = group[-1]
    if len(group) >= MAX_FRAMES:
        return False
    if any(last.get(key) != row.get(key) for key in ("model", "lens", "focal_length")):
        return False
    gap = _seconds_between(last["taken"], row["taken"])
    if gap is None or gap > BRACKET_GAP:
        return False
    ev = exposure_value(row)
    return ev is not None and all(
        abs(ev - (exposure_value(other) or 1e9)) >= _SAME_EXPOSURE for other in group
    )


def _keep_bracket(group, found):
    evs = [exposure_value(row) for row in group]
    if len(group) >= MIN_FRAMES and None not in evs and max(evs) - min(evs) >= _LEAST_SPREAD:
        found.append([row["id"] for row in group])


def _seconds_between(a, b):
    from datetime import datetime

    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except (TypeError, ValueError):
        return None


def bracket_around(rows, photo_id):
    """The bracket among `rows` that has the photo `photo_id`, as ids, or None."""
    return next((group for group in brackets(rows) if photo_id in group), None)


def check_frames(rows):
    """Raise ValueError saying why the photos `rows` (dicts, with their path) cannot be
    merged: too few or many, not RAW, or not all of one camera."""
    if len(rows) < MIN_FRAMES:
        raise ValueError(f"An HDR merge needs at least {MIN_FRAMES} photos")
    if len(rows) > MAX_FRAMES:
        raise ValueError(f"An HDR merge takes at most {MAX_FRAMES} photos")
    for row in rows:
        if not raw.is_raw(row["path"]):
            raise ValueError(f"{os.path.basename(row['path'])} is not a RAW file")
    derived.check_camera(rows)


def settings_for(rows, deghost="off", align=True):
    """What a merge of the photos `rows` (dicts with their details, as chosen) keeps in
    the library: the frames in order of exposure, darkest first, when every exposure is
    known, and as given otherwise; the reference (the middle one); and what the result
    says of itself. Returns (rows in that order, settings JSON)."""
    if deghost not in DEGHOST:
        raise ValueError(f"unknown deghosting {deghost!r}")
    evs = [exposure_value(row) for row in rows]
    if None not in evs:
        order = sorted(range(len(rows)), key=lambda i: evs[i])
        rows, evs = [rows[i] for i in order], [evs[i] for i in order]
    reference = rows[len(rows) // 2]
    beside = min(range(len(rows)), key=lambda i: (rows[i].get("taken") or "", rows[i]["path"]))
    settings = {
        "version": SETTINGS_VERSION,
        "reference": len(rows) // 2,
        "deghost": deghost,
        "align": bool(align),
        "exposures": evs,
        # The result goes beside the first photo taken, named after it
        "beside": beside,
        "name": os.path.splitext(os.path.basename(rows[beside]["path"]))[0] + "-HDR",
        "tags": derived.tags_of(reference),
    }
    return rows, json.dumps(settings, sort_keys=True)


def queue(library, photo_ids, deghost="off", align=True):
    """Queue a merge of the photos `photo_ids` and return the job's id. Raises
    ValueError saying why they cannot be merged."""
    rows = derived.rows_to_queue(library, photo_ids)
    check_frames(rows)
    rows, settings = settings_for(rows, deghost, align)
    sources = [dbm.Source(row["volume_key"], row["path"], row["content_id"]) for row in rows]
    beside = json.loads(settings)["beside"]
    return library.add_derived_job(HDR, settings, sources, rows[beside]["folder_id"])


# The steps, in a worker process


def _frame(work, index, what):
    return os.path.join(work, f"{what}-{index}")


def frame_done(work, index):
    return os.path.exists(_frame(work, index, "frame") + ".json")


def _save(path, array):
    # Under another name first: a step cut short leaves nothing that looks done
    with open(path + ".tmp", "wb") as file:
        np.save(file, array)
    os.replace(path + ".tmp", path)


def decode_frame(path, index, reference, work, white_balance, align=True, stops=None):
    """Decode frame `index` (the RAW file at `path`) with `white_balance` into the work
    folder `work`, with its shift from the reference frame (decoded first). `stops` is
    how much brighter the frame is than the reference from the metadata, or None.
    Returns the shift (x, y). Raises PhotoError naming the file."""
    linear = source.raw_linear(path, white_balance=white_balance)
    height, width = linear.shape[:2]
    step = max(1, round(math.sqrt(height * width / _MEASURE_PIXELS)))
    small = np.ascontiguousarray(linear[::step, ::step, 1])
    shift = (0, 0)
    if index == reference:
        _save(os.path.join(work, "green-reference.npy"), np.ascontiguousarray(linear[..., 1]))
    else:
        with open(_frame(work, reference, "frame") + ".json") as file:
            wanted = json.load(file)["shape"]
        if [height, width] != wanted:
            raise PhotoError(
                path,
                f"is {width} x {height}, the other photos {wanted[1]} x {wanted[0]}: "
                "they are not of one size",
            )
        if align:
            ratio = _pair_ratio(
                np.load(_frame(work, reference, "small") + ".npy"), small, (0, 0), (0, 0), step
            )
            if ratio is None:
                ratio = 2.0**stops if stops is not None else 1.0
            green = np.load(os.path.join(work, "green-reference.npy"), mmap_mode="r")
            # Both at one exposure, clipped where the brighter one clips: the bitmaps are
            # then of the same light, whichever frame is mostly white
            clip = min(1.0, 1.0 / ratio) * _CLIP_AT
            shift = _shift(_grey(green, 1.0, clip), _grey(linear[..., 1], ratio, clip))
    _save(_frame(work, index, "small") + ".npy", small)
    _save(_frame(work, index, "frame") + ".npy", linear)
    info = {"shift": list(shift), "shape": [height, width], "step": step}
    with open(_frame(work, index, "frame") + ".json.tmp", "w") as file:
        json.dump(info, file)
    os.replace(_frame(work, index, "frame") + ".json.tmp", _frame(work, index, "frame") + ".json")
    return tuple(shift)


def _grey(green, ratio, clip):
    """A frame's green (uint16) as the reference's exposure sees it (divided by `ratio`),
    clipped at `clip` and gamma-encoded to 8 bits: what the alignment compares."""
    grey = green.astype(np.float32)
    grey *= np.float32(1 / (65535 * ratio * clip))
    np.minimum(grey, 1.0, out=grey)
    np.sqrt(grey, out=grey)
    grey *= np.float32(255)
    grey += np.float32(0.5)
    return grey.astype(np.uint8)


def _shift(reference, grey):
    """(x, y) whole pixels that `grey` is to be moved by to lie over `reference` (both
    8-bit, at one exposure and clipped alike, see `_grey`).

    Median threshold bitmaps, as OpenCV's AlignMTB, searched from coarse to fine; but the
    median is of the pixels clipped in neither, and clipped pixels are not compared: in a
    frame mostly white the median is white, and AlignMTB's bitmaps say nothing."""
    smaller = min(reference.shape)
    levels = max(1, min(_ALIGN_LEVELS, int(math.log2(max(smaller, 2))) - 4))
    pyramid = [(reference, grey)]
    for _ in range(levels - 1):
        a, b = pyramid[-1]
        pyramid.append((cv2.pyrDown(a), cv2.pyrDown(b)))
    x = y = 0
    for a, b in reversed(pyramid):
        x, y = 2 * x, 2 * y
        clipped = (a >= _ALIGN_CLIPPED) | (b >= _ALIGN_CLIPPED)
        bitmaps = (_bitmap(a, clipped), _bitmap(b, clipped))
        tests = [(x + i, y + j) for j in (-1, 0, 1) for i in (-1, 0, 1)]
        x, y = min(tests, key=lambda shift: _mismatch(*bitmaps, shift))
    return x, y


def _bitmap(grey, clipped):
    """(above the median, far enough from it to count) of `grey`, the median being of
    the pixels not `clipped`, which never count."""
    counts = np.bincount(grey[~clipped], minlength=256)
    middle = int(np.searchsorted(np.cumsum(counts), counts.sum() / 2))
    counting = ~clipped & (np.abs(grey.astype(np.int16) - middle) > _ALIGN_NOISE)
    return grey > middle, counting


def _mismatch(a, b, shift):
    """The share of the pixels compared where the bitmap `a` and the bitmap `b` moved by
    `shift` (x, y) differ: each (above the median, counting)."""
    x, y = shift
    height, width = a[0].shape
    if abs(x) >= width or abs(y) >= height:
        return 1.0
    # b moved by (x, y): b[r - y, c - x] lies over a[r, c]
    here = (slice(max(0, y), height + min(0, y)), slice(max(0, x), width + min(0, x)))
    there = (slice(max(0, -y), height + min(0, -y)), slice(max(0, -x), width + min(0, -x)))
    counting = a[1][here] & b[1][there]
    compared = np.count_nonzero(counting)
    if compared < _MEASURE_LEAST:
        return 1.0
    differ = (a[0][here] != b[0][there]) & counting
    return np.count_nonzero(differ) / compared


@dataclass(frozen=True)
class Plan:
    # Per frame: its brightness over the reference's, and its shift (x, y)
    ratios: tuple
    shifts: tuple
    # (left, top, right, bottom) of the result in the reference's pixels
    box: tuple

    @property
    def size(self):
        left, top, right, bottom = self.box
        return right - left, bottom - top


def make_plan(work, count, reference, exposures, paths):
    """The Plan of a merge whose `count` frames are decoded in `work`: the exposures
    measured, `exposures` (stops from the metadata, or None) where the pixels do not
    tell. Raises PhotoError naming a frame whose exposure cannot be told."""
    infos, smalls = [], []
    for index in range(count):
        with open(_frame(work, index, "frame") + ".json") as file:
            infos.append(json.load(file))
        smalls.append(np.load(_frame(work, index, "small") + ".npy"))
    step = infos[0]["step"]
    shifts = [tuple(info["shift"]) for info in infos]
    # Brightness of each frame over the first's, pair by pair: neighbours have light in common
    brightness = [1.0]
    for index in range(1, count):
        before = index - 1
        ratio = _pair_ratio(smalls[before], smalls[index], shifts[before], shifts[index], step)
        if ratio is None:
            if exposures is None or None in exposures:
                raise PhotoError(
                    paths[index],
                    "its exposure cannot be told from the photo before it: too little of "
                    "the picture is well exposed in both",
                )
            ratio = 2.0 ** (exposures[index] - exposures[index - 1])
        brightness.append(brightness[-1] * ratio)
    ratios = tuple(b / brightness[reference] for b in brightness)
    height, width = infos[0]["shape"]
    dxs, dys = [s[0] for s in shifts], [s[1] for s in shifts]
    box = (max(0, *dxs), max(0, *dys), width + min(0, *dxs), height + min(0, *dys))
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        raise PhotoError(paths[0], "the photos do not overlap once aligned")
    plan = Plan(ratios, tuple(shifts), box)
    with open(os.path.join(work, "plan.json"), "w") as file:
        json.dump({"ratios": plan.ratios, "shifts": plan.shifts, "box": plan.box}, file)
    return plan


def _pair_ratio(a, b, shift_a, shift_b, step):
    """How much brighter `b` is than `a` (small copies of two frames, green), over the
    pixels both have well exposed once aligned; None when too few are."""
    # Each moved by its shift, in steps of the small copies: what lies over what
    dx = round((shift_b[0] - shift_a[0]) / step)
    dy = round((shift_b[1] - shift_a[1]) / step)
    height, width = a.shape
    # b moved by (dx, dy) lies over a: a[y, x] ~ b[y - dy, x - dx]
    ay, by = (
        (slice(dy, height), slice(0, height - dy))
        if dy >= 0
        else (
            slice(0, height + dy),
            slice(-dy, height),
        )
    )
    ax, bx = (
        (slice(dx, width), slice(0, width - dx))
        if dx >= 0
        else (
            slice(0, width + dx),
            slice(-dx, width),
        )
    )
    first = a[ay, ax].astype(np.float32) / 65535
    second = b[by, bx].astype(np.float32) / 65535
    good = (first > _MEASURE_LOW) & (first < _MEASURE_HIGH)
    good &= (second > _MEASURE_LOW) & (second < _MEASURE_HIGH)
    if np.count_nonzero(good) < _MEASURE_LEAST:
        return None
    return float(np.median(second[good] / first[good]))


def load_plan(work):
    with open(os.path.join(work, "plan.json")) as file:
        data = json.load(file)
    return Plan(tuple(data["ratios"]), tuple(tuple(s) for s in data["shifts"]), tuple(data["box"]))


def merge_rows(work, part, tags, first, last, deghost=None):
    """Rows `first` to `last` of the result, merged from the frames in `work`, written to
    the result file `part` (laid out by `start_result`). `deghost` is how far a frame
    may stray from the guide, in stops, or None."""
    plan = load_plan(work)
    width, height = plan.size
    halo = _GHOST_BLUR if deghost else 0
    top, bottom = max(0, first - halo), min(height, last + halo)
    left, upper, right, _lower = plan.box
    light, room = [], []
    for index, (dx, dy) in enumerate(plan.shifts):
        frame = np.load(_frame(work, index, "frame") + ".npy", mmap_mode="r")
        rows = frame[upper + top - dy : upper + bottom - dy, left - dx : right - dx]
        rows = rows.astype(np.float32)
        rows *= np.float32(1 / 65535)
        peak = rows.max(axis=2)
        # 1 well under clipping, 0 at it
        room.append(np.clip((_CLIP_AT - peak) / (_CLIP_AT - _CLIP_FROM), 0.0, 1.0))
        rows /= np.float32(plan.ratios[index])
        light.append(rows)
    weights = [room[i] * np.float32(plan.ratios[i]) for i in range(len(light))]
    if deghost:
        _deghost(light, room, weights, plan.ratios, deghost)
    total = np.zeros(light[0].shape, np.float32)
    weight = np.zeros(light[0].shape[:2], np.float32)
    for rows, w in zip(light, weights, strict=True):
        total += rows * w[..., None]
        weight += w
    darkest = int(np.argmin(plan.ratios))
    nothing = weight < 1e-6
    weight[nothing] = 1.0
    total /= weight[..., None]
    total[nothing] = light[darkest][nothing]
    derived.write_rows(part, width, height, tags, first, total[first - top : last - top])
    return last - first


def _deghost(light, room, weights, ratios, stops):
    """Weights (in place) of the frames that disagree with the guide at a pixel cut to
    nothing there: the reference where it is well exposed, else the next darker frame
    that is."""
    reference = min(range(len(ratios)), key=lambda i: abs(math.log2(ratios[i])))
    guide = light[reference].copy()
    have = room[reference] >= 0.5
    darker = sorted(
        (i for i in range(len(ratios)) if ratios[i] < ratios[reference]), key=lambda i: -ratios[i]
    )
    for index in darker:
        take = ~have & (room[index] >= 0.5)
        guide[take] = light[index][take]
        have |= take
    darkest = int(np.argmin(ratios))
    guide[~have] = light[darkest][~have]
    floor = np.float32(_GHOST_FLOOR)
    guide_luma = guide @ source_luma() + floor
    for index, rows in enumerate(light):
        distance = np.abs(np.log2((rows @ source_luma() + floor) / guide_luma))
        agree = 1.0 - _smoothstep(stops, 2 * stops, distance)
        agree = cv2.blur(agree.astype(np.float32), (_GHOST_BLUR, _GHOST_BLUR))
        # A frame clipped here has no say anyway; the guide's own frame always agrees
        weights[index] *= agree


def source_luma():
    return np.array([0.2126, 0.7152, 0.0722], np.float32)


def _smoothstep(low, high, x):
    t = np.clip((x - low) / (high - low), 0.0, 1.0)
    return t * t * (3 - 2 * t)


# The job, on a thread


default_work_root = derived.default_work_root


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
    """Run merge job `job_id` to its end and return its state. `call(fn, *args)` runs a
    step (in a worker process, in the app). A job stopped while `keep_work()` is true (a
    quit) keeps its decoded frames, for the next start; otherwise the work folder goes
    when the job ends. Raises NotConnected, leaving the job queued, when a source's drive
    is not mounted."""

    def make(job, paths, settings, work, part):
        return _merge(job, paths, settings, work, part, call, should_stop)

    return derived.run(
        library,
        job_id,
        mounts,
        make,
        lambda count, name: f"Merged {count} photos into {name}",
        report,
        work_root,
        keep_work,
    )


def _merge(job, paths, settings, work, part, call, should_stop):
    """(state, message or the result's path, PhotoError or None)."""
    reference = settings["reference"]
    tags = {key: value for key, value in settings["tags"].items() if value}
    tags["description"] = f"HDR merge of {len(paths)} photos"
    cancelled = (dbm.CANCELLED, "Cancelled while merging", None)
    try:
        derived.check_sources(paths)
        os.makedirs(work, exist_ok=True)
        balance = call(source.camera_white_balance, paths[reference])
        order = [reference] + [i for i in range(len(paths)) if i != reference]
        for done, index in enumerate(order):
            if should_stop():
                return cancelled
            if not frame_done(work, index):
                call(
                    decode_frame,
                    paths[index],
                    index,
                    reference,
                    work,
                    balance,
                    settings.get("align", True),
                    _stops(settings.get("exposures"), index, reference),
                )
            job.progress(DECODING, done + 1, len(paths), force=True)
        if should_stop():
            return cancelled
        job.progress(PLANNING, 0, None, force=True)
        plan = call(make_plan, work, len(paths), reference, settings.get("exposures"), paths)
        width, height = plan.size
        call(start_result, part, width, height, tags)
        deghost = DEGHOST.get(settings.get("deghost", "off"))
        for first in range(0, height, BAND):
            if should_stop():
                return cancelled
            last = min(height, first + BAND)
            call(merge_rows, work, part, tags, first, last, deghost)
            job.progress(MERGING, last, height)
        job.progress(MERGING, height, height, force=True)
        call(finish_result, part)
        if should_stop():
            return cancelled
        return dbm.DONE, derived.place(part, os.path.dirname(part), settings["name"]), None
    except (PhotoError, OSError) as error:
        return derived.failure(error, part, "merge")


def _stops(exposures, index, reference):
    if not exposures or exposures[index] is None or exposures[reference] is None:
        return None
    return exposures[index] - exposures[reference]
