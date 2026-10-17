"""The Develop view's session: one photo's edit, its renders, undo, presets and export.

Renders run in the Develop view's own worker process (core/edit/session.py), which
keeps the photo decoded. At most one render is in flight; while it runs, a newer
request replaces the waiting one, so the image follows a dragged control as fast
as renders come back and never falls behind. While a control moves the proxy is
rendered; when it stops, the screen-sized image. Zoomed in, the part of the photo in view
is then rendered from the full decode (`request_region`), and kept while a newer one is
made.

An edit is saved to the library when a change is committed (a control let go, a
point dropped, undo), not on every move. Undo and redo are a stack of committed
parameter states. An edit back at no change at all is removed from the library:
the photo is its original again.
"""

import json
import os
from dataclasses import replace

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from lumina.core import colour_checker as _colour_checker
from lumina.core import dcp as _dcp
from lumina.core.edit import curves as _curves
from lumina.core.edit import export as _export
from lumina.core.edit import hsl as _hsl
from lumina.core.edit import params as _params
from lumina.core.edit import pipeline as _pipeline
from lumina.core.edit import session as _session
from lumina.qt_models import workers

# For the UI, which does not import core/
Params = _params.Params
Basic = _params.Basic
Colour = _params.Colour
Presence = _params.Presence
Sharpening = _params.Sharpening
NoiseReduction = _params.NoiseReduction
Band = _params.Band
Hsl = _params.Hsl
Wheel = _params.Wheel
Vignette = _params.Vignette
Grain = _params.Grain
Grading = _params.Grading
Curves = _params.Curves
Matte = _params.Matte
Perspective = _params.Perspective
Crop = _params.Crop
Lens = _params.Lens
LENS = _params.LENS
ColourProfile = _params.ColourProfile
patch_centres = _colour_checker.patch_centres
CHECKER_SAMPLE_SHARE = _colour_checker.SAMPLE_SHARE
PROFILE_FOLDERS = _dcp.default_folders
ExportOptions = _export.ExportOptions
FORMATS = _export.FORMATS
EXPOSURE = _params.EXPOSURE
WARMTH = _params.WARMTH
BASIC = _params.BASIC
BASIC_FIELDS = _params.BASIC_FIELDS
COLOUR = _params.COLOUR
PRESENCE = _params.PRESENCE
PRESENCE_FIELDS = _params.PRESENCE_FIELDS
SHARPEN_AMOUNT = _params.SHARPEN_AMOUNT
SHARPEN_RADIUS = _params.SHARPEN_RADIUS
DEFAULT_SHARPEN_RADIUS = _params.DEFAULT_SHARPEN_RADIUS
DEFAULT_SHARPEN_DETAIL = _params.DEFAULT_SHARPEN_DETAIL
DEFAULT_NOISE_DETAIL = _params.DEFAULT_NOISE_DETAIL
COLOUR_FIELDS = _params.COLOUR_FIELDS
HSL = _params.HSL
HUE = _params.HUE
WHEEL = _params.WHEEL
GRADING = _params.GRADING
BLENDING = _params.BLENDING
DEFAULT_BLENDING = _params.DEFAULT_BLENDING
VIGNETTE = _params.VIGNETTE
PERCENT = _params.PERCENT
DEFAULT_MIDPOINT = _params.DEFAULT_MIDPOINT
DEFAULT_FEATHER = _params.DEFAULT_FEATHER
DEFAULT_GRAIN_SIZE = _params.DEFAULT_GRAIN_SIZE
BANDS = _params.BANDS
# Where each band sits on the hue circle, in degrees: for showing its colour
BAND_HUES = dict(zip(_params.BANDS, _hsl.CENTRES, strict=True))
BAND_FIELDS = _params.BAND_FIELDS
RANGES = _params.RANGES
WHEEL_FIELDS = _params.WHEEL_FIELDS
MATTE_BLACK = _params.MATTE_BLACK
MATTE_WHITE = _params.MATTE_WHITE
DESATURATION = _params.DESATURATION
STRAIGHTEN = _params.STRAIGHTEN
PERSPECTIVE = _params.PERSPECTIVE
PRESET_SECTIONS = _params.SECTIONS
IDENTITY_CURVE = _curves.IDENTITY
PROXY, SCREEN, REGION = _session.PROXY, _session.SCREEN, _session.REGION


def curve_values(points, count):
    """The curve through `points` at `count` even steps from 0 to 1: for drawing it."""
    return _curves.evaluate(points, np.linspace(0.0, 1.0, count))


def normalize_curve(points):
    return _curves.normalize(points)


def suggested_name(path, options, copy=0):
    return _export.suggested_name(path, options, copy)


def _replaces(new, waiting):
    """Whether a request of kind `new` takes the place of the one waiting. A part of the
    photo never replaces what changes the look (the part is asked for again when the new
    look is shown); a proxy never replaces a screen render of the same look."""
    if new == REGION:
        return waiting == REGION
    return not (waiting == SCREEN and new == PROXY)


class DevelopSession(QObject):
    # The parameters shown changed: a move, a commit, undo, a preset
    params_changed = Signal(object)
    # A render came back: QImage, its histogram (4 x 256) and PROXY or SCREEN
    image_ready = Signal(object, object, str)
    # The photo was decoded: its width and height
    opened = Signal(int, int)
    # What is known of the photo's lens changed: see `lens_name`, `lens_state`
    lens_changed = Signal()
    # The names of the lenses that fit the camera, asked for by `request_lenses`
    lenses_ready = Signal(object)
    # The profiles the photo can be rendered with, asked for by `request_profiles`: a list
    # of (name, key), key "libraw", "standard" or a profile file's path
    profiles_ready = Signal(object)
    # Lumina made a profile and the edit uses it: what to tell the user
    profile_made = Signal(str)
    # Making a profile started or ended
    profile_busy_changed = Signal(bool)
    # A part of the photo rendered at the zoom came back: QImage, its (left, top) and the
    # size of the whole, in pixels of the image it is a part of
    region_ready = Signal(object, object, object)
    # A part of the photo started to be rendered, or no more is wanted or waiting
    region_loading_changed = Signal()
    # The original is being shown instead of the edit, or is not any more
    before_changed = Signal(bool)
    # The photo could not be opened or rendered: why
    failed = Signal(str)
    # Undo or redo became possible or not
    history_changed = Signal()
    # An export ended: the destination, and None or why it failed
    exported = Signal(str, object)
    # An export found its destination taken at the last moment: the destination
    export_exists = Signal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.photo_id = None
        self.path = None
        # 0: the photo's own edit; more: a virtual copy
        self.copy = 0
        self.params = Params()
        # What an edit of this photo starts as, and what a Reset goes back to: the lens
        # corrections on for a RAW file whose lens is known, else nothing (see `_on_opened`)
        self._baseline = Params()
        # How the photo was taken, for finding its lens (lensdb.Shot), and the lens
        # profile the database has for it, if any; False before it is known
        self._shot = None
        self._lens = None
        self._lens_known = False
        self._lens_data = True
        # Whether the open photo is a RAW file, which has a camera profile; and the folders
        # the user keeps profiles in, asked for when they are needed (the UI's settings)
        self._raw = False
        self.profile_folders = list
        self._making_profile = False
        self._undo = []
        self._redo = []
        self._busy = False
        self._waiting = None
        self._token = 0
        self.screen_side = 2560
        # While the crop is being set, the whole photo is shown with the crop over it
        self._crop_view = False
        # While true, the photo as it is, without the edit, is shown: to compare
        self._before = False
        # The open photo's (width, height) as decoded, before any turning of the edit
        self.size = None
        # A control is moving: what is shown is the proxy, and a part is not wanted
        self._moving = False
        # What the render in flight is of
        self._running = None

    # Opening and closing

    def open(self, photo_id, path, copy=0):
        """Edit the photo `photo_id` at `path` (it must be connected): its own edit, or
        virtual copy `copy`."""
        self.close()
        self.photo_id, self.path, self.copy = photo_id, path, copy
        self.size = None
        self._moving = False
        self._token += 1
        self._baseline = Params()
        self._shot = self.service.lens_shot(photo_id)
        self._lens, self._lens_known, self._lens_data = None, False, True
        self._raw = False
        stored = self.service.edit_json(photo_id, copy)
        try:
            self.params = _params.from_json(stored) if stored else self._baseline
        except _params.NewerParams as error:
            # Read only, in a sense: nothing is saved over it
            self.photo_id = None
            self.failed.emit(f"This edit was made by a newer Lumina ({error})")
            return
        self._undo, self._redo = [], []
        self.history_changed.emit()
        self.params_changed.emit(self.params)
        token = self._token
        workers.submit(
            _session.open_photo,
            path,
            self._shot,
            list(self.profile_folders()),
            on_done=lambda opened: self._on_opened(token, opened),
            on_error=lambda error: self._on_failed(token, error),
            owner=self,
            process=workers.DEVELOP,
        )

    def close(self):
        """Stop editing: the decode in the worker is let go."""
        if self.path is None:
            return
        self._token += 1
        self._busy, self._waiting, self._running = False, None, None
        self._moving = False
        self._crop_view = False
        if self._before:
            self._before = False
            self.before_changed.emit(False)
        self.photo_id = self.path = None
        self.copy = 0
        workers.submit(
            _session.close,
            on_done=lambda _r: None,
            on_error=lambda _e: None,
            owner=self,
            process=workers.DEVELOP,
        )

    def is_open(self):
        return self.photo_id is not None

    def _on_opened(self, token, opened):
        if token != self._token:
            return
        self.size = (int(opened.size[0]), int(opened.size[1]))
        self._lens, self._lens_known, self._lens_data = opened.lens, True, opened.lens_data
        self._raw = opened.profile is not None
        if self._raw:
            # A RAW file has none of its lens's faults corrected and none of the camera's
            # look: new edits start with the corrections on and the camera's profile (or
            # Lumina's look). A photo with an edit keeps what the edit says
            lens = _params.Lens()
            if opened.lens is not None and opened.raw:
                lens = _params.Lens(enabled=True, profile=opened.lens)
            self._baseline = Params(lens=lens, profile=ColourProfile(*opened.profile))
            # A copy made of a photo that had no edit holds the identity, which is not
            # where a RAW file's edit starts
            unedited = (
                self.params == Params()
                if self.copy
                else self.params == Params() and self.service.edit_json(self.photo_id) is None
            )
            if unedited:
                self.params = self._baseline
                self.params_changed.emit(self.params)
        self.lens_changed.emit()
        self.opened.emit(*self.size)
        self.request(SCREEN)

    def _on_failed(self, token, error):
        if token != self._token:
            return
        self._busy, self._running = False, None
        self.failed.emit(getattr(error, "message", None) or str(error))

    # Changing the edit

    def preview(self, params):
        """A control is moving: show `params`, rendered small, without saving or undo."""
        if not self.is_open():
            return
        same_look = self._shown(params) == self._shown(self.params)
        self.params = params
        self.params_changed.emit(params)
        if not same_look:
            self._moving = True
            self.request(PROXY)

    def set_crop_view(self, on):
        """Show the whole photo, for setting the crop over it, or the cropped photo."""
        if on != self._crop_view:
            self._crop_view = on
            self.request(SCREEN)

    def _shown(self, params):
        # What is rendered: with the original wanted, no edit at all; in the crop view,
        # the photo without its crop
        if self._before:
            return Params()
        return params.with_changes(crop=Crop()) if self._crop_view else params

    def set_before(self, on):
        """Show the photo as it is without the edit (`on`), or the edit again. Only what
        is shown: the edit and its controls are as they were."""
        if on != self._before:
            self._before = on
            self.before_changed.emit(on)
            self.request(SCREEN)

    def is_before(self):
        return self._before

    def full_size(self):
        """The photo's (width, height) as the edit shows it at full size, or None before
        it is known."""
        if self.size is None:
            return None
        return _pipeline.output_size(*self.size, self._shown(self.params))

    def photo_aspect(self):
        """The open photo's width over its height as the edit turns it (the crop and the
        straightening do not change it), or None before it is known."""
        if self.size is None:
            return None
        width, height = self.size
        if self.params.rotate % 2:
            width, height = height, width
        return width / height

    def commit(self, params=None):
        """A change is done: keep it for undo, save it, and render it at screen size."""
        if not self.is_open():
            return
        params = params if params is not None else self.params
        before = self._committed()
        if params != before:
            self._undo.append(before)
            self._redo.clear()
            self.history_changed.emit()
        self._set(params)

    def _committed(self):
        stored = self.service.edit_json(self.photo_id, self.copy)
        try:
            return _params.from_json(stored) if stored else self._baseline
        except _params.NewerParams:
            return Params()

    def _set(self, params):
        same_look = self._shown(params) == self._shown(self.params)
        self._moving = False
        self.params = params
        self.params_changed.emit(params)
        self._save()
        if not same_look or not self._crop_view:
            self.request(SCREEN)

    def _save(self):
        # What a new edit starts as is no edit: the photo is its original (see `reset`). With
        # the lens corrections on to start with, an edit with them off is one to keep
        # A virtual copy stays whatever it holds: only the photo's own edit goes away
        if self.params == self._baseline and not self.copy:
            self.service.save_edit(self.photo_id, None)
        else:
            self.service.save_edit(self.photo_id, self.params.to_json(), self.copy)

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def undo(self):
        if self._undo and self.is_open():
            self._redo.append(self.params)
            self._set(self._undo.pop())
            self.history_changed.emit()

    def redo(self):
        if self._redo and self.is_open():
            self._undo.append(self.params)
            self._set(self._redo.pop())
            self.history_changed.emit()

    def reset(self):
        """Back to where a new edit starts, as one step that can be undone."""
        self.commit(self._baseline)

    # The camera profile

    def is_raw(self):
        """Whether the open photo is a RAW file (known once it is decoded): only those have
        a camera profile."""
        return self._raw

    def profile_name(self):
        """The name of the profile the edit renders with."""
        profile = self.params.profile
        return profile.name or {"libraw": "Neutral (LibRaw)", "standard": "Lumina Standard"}.get(
            profile.id, "Camera profile"
        )

    def request_profiles(self):
        """Ask for the profiles the photo can be rendered with: `profiles_ready` gives them."""
        if not self.is_open() or not self._raw:
            self.profiles_ready.emit([])
            return
        token = self._token
        workers.submit(
            _session.profile_choices,
            self._shot,
            list(self.profile_folders()),
            on_done=lambda choices: self._on_profiles(token, choices),
            on_error=lambda _error: self._on_profiles(token, []),
            owner=self,
            process=workers.DEVELOP,
        )

    def _on_profiles(self, token, choices):
        if token == self._token:
            self.profiles_ready.emit(choices)

    def choose_profile(self, key, name=None):
        """Render with the profile `key`: "libraw", "standard", or a profile file's path
        (copied into the store first)."""
        if not self.is_open() or not self._raw:
            return
        if key in ("libraw", "standard"):
            self.commit(self.params.with_changes(profile=ColourProfile(key, name or "")))
            return
        token = self._token
        workers.submit(
            _session.use_profile,
            os.fspath(key),
            on_done=lambda found: self._on_profile_stored(token, found),
            on_error=lambda error: self._on_failed(token, error),
            owner=self,
            process=workers.DEVELOP,
        )

    def _on_profile_stored(self, token, found):
        if token == self._token:
            self.commit(self.params.with_changes(profile=ColourProfile(*found)))

    def is_making_profile(self):
        return self._making_profile

    def _set_making(self, on):
        self._making_profile = on
        self.profile_busy_changed.emit(on)

    def fit_camera_look(self, limit=12):
        """Make a profile of the camera's own look from up to `limit` of its RAW files in
        the library and the JPEGs inside them, and render with it. Ends with
        `profile_made` or `failed`."""
        if not self.is_open() or not self._raw or self._making_profile or self._shot is None:
            return
        model = self._shot.model
        paths = self.service.raw_files_of(model, limit)
        if not paths:
            self.failed.emit(f"No RAW files of the {model} are connected to learn from")
            return
        token = self._token
        self._set_making(True)
        workers.submit(
            _session.fit_camera_look,
            paths,
            model,
            on_done=lambda made: self._on_profile_made(token, made, "look", len(paths)),
            on_error=lambda error: self._on_making_failed(token, error),
            owner=self,
        )

    def make_checker_profile(self, corners, linear=False):
        """Make a profile from the open photo of a colour target, `corners` the middles of
        its four corner patches as fractions of the photo as shown (dark skin, bluish
        green, black, white), and render with it. Ends with `profile_made` or `failed`."""
        if not self.is_open() or not self._raw or self._making_profile or self._shot is None:
            return
        token = self._token
        self._set_making(True)
        workers.submit(
            _session.checker_profile,
            self.path,
            self.params.to_json(),
            [tuple(map(float, c)) for c in corners],
            self._shot.model,
            linear,
            on_done=lambda made: self._on_profile_made(token, made, "target", 1),
            on_error=lambda error: self._on_making_failed(token, error),
            owner=self,
            process=workers.DEVELOP,
        )

    def _on_profile_made(self, token, made, kind, count):
        self._set_making(False)
        if kind == "look":
            told = (
                f"Made “{made.name}” from {count} photos: {made.after:.1f} levels from their "
                f"JPEGs on average, against {made.before:.1f} with neutral colours."
            )
        else:
            told = (
                f"Made “{made.name}”: its colours are {made.before:.1f} ΔE00 from the target "
                f"on average, {made.after:.1f} at most."
            )
        if token == self._token:
            self.commit(self.params.with_changes(profile=ColourProfile(made.ident, made.name)))
        self.profile_made.emit(f"{told} Saved as {made.path}")

    def _on_making_failed(self, token, error):
        self._set_making(False)
        self.failed.emit(getattr(error, "message", None) or str(error))

    # The lens

    def lens_name(self):
        """The name of the lens profile the edit has, else the one the database has for
        the photo, else None."""
        profile = self.params.lens.profile or self._lens
        return profile.name if profile is not None else None

    def lens_state(self):
        """What is known of the photo's lens: "waiting" (not yet), "known" (the database has
        it), "unknown" (it does not), or "no data" (there is no lens database at all)."""
        if not self._lens_known:
            return "waiting"
        if self._lens is not None:
            return "known"
        return "unknown" if self._lens_data else "no data"

    def detected_lens(self):
        """The profile the database has for the photo's own lens, or None."""
        return self._lens

    def request_lenses(self):
        """Ask for the lenses that fit the photo's camera: `lenses_ready` gives their names."""
        if not self.is_open() or self._shot is None:
            self.lenses_ready.emit([])
            return
        token = self._token
        workers.submit(
            _session.lens_names,
            self._shot,
            on_done=lambda names: self._on_lenses(token, names),
            on_error=lambda _error: self._on_lenses(token, []),
            owner=self,
            process=workers.DEVELOP,
        )

    def _on_lenses(self, token, names):
        if token == self._token:
            self.lenses_ready.emit(names)

    def choose_lens(self, name):
        """Correct the photo as the lens called `name` (from `lenses_ready`) would need."""
        if not self.is_open() or self._shot is None:
            return
        token = self._token
        workers.submit(
            _session.lens_profile,
            self._shot,
            name,
            on_done=lambda profile: self._on_lens_chosen(token, profile),
            on_error=lambda error: self._on_failed(token, error),
            owner=self,
            process=workers.DEVELOP,
        )

    def _on_lens_chosen(self, token, profile):
        if token != self._token or profile is None:
            return
        lens = replace(self.params.lens, enabled=True, profile=profile)
        self.commit(self.params.with_changes(lens=lens))
        self.lens_changed.emit()

    # Presets

    def presets(self):
        return [name for name, _ in self.service.presets()]

    def apply_preset(self, name):
        document = dict(self.service.presets()).get(name)
        if document is None or not self.is_open():
            return
        try:
            self.commit(_params.apply_preset(self.params, json.loads(document)))
        except (ValueError, _params.NewerParams) as error:
            self.failed.emit(f"Preset “{name}” can't be used: {error}")

    def settings_document(self, sections):
        """The `sections` of the edit as it is now, to put over other photos."""
        return _params.preset(self.params, sections)

    def paste_settings(self, document):
        """Put the sections of `document` over the edit, as one change that can be undone."""
        if not self.is_open():
            return
        try:
            self.commit(_params.apply_preset(self.params, document))
        except _params.NewerParams as error:
            self.failed.emit(f"These settings can't be used: {error}")

    def save_preset(self, name, sections):
        """Keep `sections` of the current edit as the preset `name`."""
        document = _params.preset(self.params, sections)
        self.service.save_preset(name, json.dumps(document, sort_keys=True))

    def delete_preset(self, name):
        self.service.delete_preset(name)

    # Rendering

    def request(self, kind):
        """Render the current parameters; the newest request waiting wins."""
        if self.is_open():
            self._enqueue((self._look(), kind, None))

    def request_region(self, zoom, centre, view):
        """Render the part of the photo in a view of `view` (width, height) device pixels
        at `zoom` (1.0: 100 %) with `centre` (fractions of the photo) in the middle. Not
        while a control moves: what is shown is then the proxy."""
        if self.is_open() and not self._moving:
            self._enqueue((self._look(), REGION, (zoom, tuple(centre), tuple(view))))

    def region_loading(self):
        """Whether a part of the photo is being rendered or waits to be."""
        return REGION in (self._running, self._waiting[1] if self._waiting else None)

    def _look(self):
        return self._shown(self.params).to_json()

    def _enqueue(self, job):
        before = self.region_loading()
        if not self._busy:
            self._start(job)
        elif self._waiting is None or _replaces(job[1], self._waiting[1]):
            self._waiting = job
        if self.region_loading() != before:
            self.region_loading_changed.emit()

    def _start(self, job):
        params_json, kind, spec = job
        self._busy, self._running = True, kind
        token = self._token
        if kind == REGION:
            call = (_session.render_region, self.path, params_json, *spec)
        else:
            call = (_session.render, self.path, params_json, kind, self.screen_side)
        workers.submit(
            *call,
            on_done=lambda rendered: self._on_rendered(token, job, rendered),
            on_error=lambda error: self._on_failed(token, error),
            owner=self,
            process=workers.DEVELOP,
        )

    def _on_rendered(self, token, job, rendered):
        if token != self._token:
            return
        before = self.region_loading()
        self._busy, self._running = False, None
        image = QImage(
            rendered.data,
            rendered.width,
            rendered.height,
            3 * rendered.width,
            QImage.Format.Format_RGB888,
        ).copy()
        waiting, self._waiting = self._waiting, None
        if rendered.kind == REGION:
            # Only for the look that is shown: a part of an older one is dropped
            if job[0] == self._look() and not self._moving:
                self.region_ready.emit(image, rendered.corner, rendered.whole)
        else:
            self.image_ready.emit(image, rendered.histogram, rendered.kind)
        if waiting is not None:
            self._start(waiting)
        if self.region_loading() != before:
            self.region_loading_changed.emit()

    # Export

    def export(self, destination, options, replace=False):
        """Export the photo with the current edit. Ends with `exported` or
        `export_exists` (the file appeared at the last moment and Replace was not
        chosen)."""
        if not self.is_open():
            return
        destination = os.fspath(destination)
        workers.submit(
            _export.export,
            self.path,
            self.params,
            destination,
            options,
            replace,
            on_done=lambda path: self.exported.emit(path, None),
            on_error=lambda error: self._on_export_failed(destination, error),
            owner=self,
        )

    def _on_export_failed(self, destination, error):
        if isinstance(error, _export.ExportExists):
            self.export_exists.emit(destination)
            return
        self.exported.emit(destination, getattr(error, "message", None) or str(error))
