"""The Develop view: the photo on the left, the edit's controls in a card on the right.

Every control previews while it moves and commits when it is let go (see
qt_models/develop.py). The crop is set on the photo itself: with Crop on, the
whole photo is shown and the crop is a frame over it, with handles at its corners
and edges; dragging inside it moves it.
"""

from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style
from lumina.qt_models import develop
from lumina.qt_models.develop import Crop, Curves, Matte
from lumina.ui.controls import ValueSlider
from lumina.ui.crop_frame import dragged, dragged_to_ratio, reshaped
from lumina.ui.curve_editor import CurveEditor
from lumina.ui.image_view import ImageView
from lumina.ui.preset_dialog import PresetDialog
from lumina.ui.ratio_dialog import RatioDialog

PANEL_WIDTH = 320
# The handles' size and how near the pointer must be to take one, unscaled
_HANDLE = 7
_GRAB = 12
# Milliseconds the zoom and the place stay as they are before the part of the photo in
# view is rendered
_REGION_DELAY = 80
_CHANNELS = (("rgb", "RGB"), ("red", "R"), ("green", "G"), ("blue", "B"))
_BANDS = tuple(zip(develop.BANDS, "ROYGABPM", strict=True))
# The colour of a band's button border: its hue, as the band acts on it, in the middle
# of the range of strengths. A hue is the band's meaning, not a theme colour
_BAND_COLOUR = (0.65, 0.9)
# How much of its colour fills the band button that is chosen
_TINT = 0.45
_RANGES = tuple((name, name.capitalize()) for name in develop.RANGES)
# The crop's aspect ratios: what is shown, and the long side over the short (a number),
# "original" (the photo's own), "custom" (asked for) or None (free)
_ASPECTS = (
    ("Free", None),
    ("Original", "original"),
    ("1:1", 1.0),
    ("4:5", 5 / 4),
    ("3:2", 3 / 2),
    ("16:9", 16 / 9),
    ("Custom…", "custom"),
)
# How many squares the grid shown while straightening makes along the photo's short side
_GRID = 8


class _Segment(QToolButton):
    """A segment of a row of choices. With a `colour` its border is drawn in it, and
    the button chosen is filled with it."""

    def __init__(self, colour=None, parent=None):
        super().__init__(parent)
        self.colour = colour
        self.setProperty("tinted", colour is not None)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.colour is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.colour)
        pen.setWidthF(style.px(2))
        painter.setPen(pen)
        if self.isChecked():
            fill = QColor(self.colour)
            fill.setAlphaF(_TINT)
            painter.setBrush(fill)
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        # Inside the button, the line's width within its edge
        inset = pen.widthF() / 2
        radius = style.radius("small")
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), radius, radius
        )


class CropImageView(ImageView):
    """The photo, and with crop mode on, the crop frame over it."""

    # The crop while it is dragged, and when let go
    crop_moved = Signal(object)
    crop_committed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent, zoomable=True)
        self.cropping = False
        # The zoom there was when the crop was opened, which shows the whole photo
        self._saved_zoom = None
        self.crop = Crop()
        self._drag = None
        # The frame's width over its height in pixels, and the photo's, while the crop is
        # held to a ratio; the fine grid while straightening; the label of the original
        self.ratio = self.aspect = None
        self.grid = False
        self.before = False
        self.setMouseTracking(True)

    def set_constraint(self, ratio, aspect):
        """Hold the crop to `ratio` (None: free) on a photo of `aspect`."""
        self.ratio, self.aspect = ratio, aspect

    def set_grid(self, on):
        if on != self.grid:
            self.grid = on
            self.update()

    def set_before(self, on):
        if on != self.before:
            self.before = on
            self.update()

    def set_cropping(self, on, crop):
        opened = on and not self.cropping
        closed = self.cropping and not on
        self.cropping, self.crop = on, crop
        if opened:
            self._saved_zoom = self.zoom_state()
            self.reset_zoom()
            self.set_zoomable(False)
        elif closed:
            self.set_zoomable(True)
            self.restore_zoom(self._saved_zoom)
        self.update()

    def set_crop(self, crop):
        self.crop = crop
        self.update()

    def _frame(self, target):
        c = self.crop
        return QRectF(
            target.left() + c.left * target.width(),
            target.top() + c.top * target.height(),
            (c.right - c.left) * target.width(),
            (c.bottom - c.top) * target.height(),
        )

    def _paint_grid(self, painter, target):
        # A fine grid over the photo, to level a horizon by
        line = QColor(style.color("textPrimary"))
        line.setAlphaF(0.35)
        painter.setPen(QPen(line, 1))
        step = min(target.width(), target.height()) / _GRID
        x = target.left() + step
        while x < target.right():
            painter.drawLine(QPointF(x, target.top()), QPointF(x, target.bottom()))
            x += step
        y = target.top() + step
        while y < target.bottom():
            painter.drawLine(QPointF(target.left(), y), QPointF(target.right(), y))
            y += step

    def _paint_label(self, painter, target, text):
        # A pill at the photo's corner: what is being shown
        margin = style.px(10)
        pad = style.px(8)
        box = QRectF(painter.fontMetrics().boundingRect(text))
        box = QRectF(
            target.left() + margin,
            target.top() + margin,
            box.width() + 2 * pad,
            box.height() + pad,
        )
        fill = QColor(style.color("background"))
        fill.setAlphaF(0.75)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(box, box.height() / 2, box.height() / 2)
        painter.setPen(style.color("textPrimary"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def paint_over(self, painter, target):
        if self.grid:
            self._paint_grid(painter, target)
        if self.cropping:
            self._paint_crop(painter, target)
        if self.before:
            self._paint_label(painter, target, "Original")

    def _paint_crop(self, painter, target):
        frame = self._frame(target)
        # The part cut away is dimmed: the background colour, half through
        shade = QColor(style.color("background"))
        shade.setAlphaF(0.55)
        outside = QPainterPath()
        outside.addRect(target)
        inside = QPainterPath()
        inside.addRect(frame)
        painter.fillPath(outside.subtracted(inside), shade)
        line = style.color("textPrimary")
        painter.setPen(QPen(line, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(frame)
        thirds = QColor(line)
        thirds.setAlphaF(0.35)
        painter.setPen(QPen(thirds, 1))
        for share in (1 / 3, 2 / 3):
            x = frame.left() + share * frame.width()
            y = frame.top() + share * frame.height()
            painter.drawLine(QPointF(x, frame.top()), QPointF(x, frame.bottom()))
            painter.drawLine(QPointF(frame.left(), y), QPointF(frame.right(), y))
        painter.setPen(QPen(style.color("background"), 1))
        painter.setBrush(line)
        size = style.px(_HANDLE)
        for point in self._handles(frame).values():
            painter.drawRect(QRectF(point.x() - size / 2, point.y() - size / 2, size, size))

    @staticmethod
    def _handles(frame):
        c = frame.center()
        return {
            "tl": frame.topLeft(),
            "tr": frame.topRight(),
            "bl": frame.bottomLeft(),
            "br": frame.bottomRight(),
            "t": QPointF(c.x(), frame.top()),
            "b": QPointF(c.x(), frame.bottom()),
            "l": QPointF(frame.left(), c.y()),
            "r": QPointF(frame.right(), c.y()),
        }

    def _hit(self, position):
        target = self.image_rect()
        if target is None:
            return None
        frame = self._frame(target)
        grab = style.px(_GRAB)
        for name, point in self._handles(frame).items():
            if abs(point.x() - position.x()) <= grab and abs(point.y() - position.y()) <= grab:
                return name
        if frame.contains(position):
            return "move"
        return None

    def mousePressEvent(self, event):
        if not self.cropping or event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        part = self._hit(event.position())
        if part is not None:
            self._drag = (part, event.position(), self.crop)

    def mouseMoveEvent(self, event):
        if not self.cropping:
            return super().mouseMoveEvent(event)
        if self._drag is None:
            part = self._hit(event.position())
            cursors = {
                "move": Qt.CursorShape.SizeAllCursor,
                "tl": Qt.CursorShape.SizeFDiagCursor,
                "br": Qt.CursorShape.SizeFDiagCursor,
                "tr": Qt.CursorShape.SizeBDiagCursor,
                "bl": Qt.CursorShape.SizeBDiagCursor,
                "t": Qt.CursorShape.SizeVerCursor,
                "b": Qt.CursorShape.SizeVerCursor,
                "l": Qt.CursorShape.SizeHorCursor,
                "r": Qt.CursorShape.SizeHorCursor,
            }
            self.setCursor(cursors.get(part, Qt.CursorShape.ArrowCursor))
            return
        target = self.image_rect()
        part, start, before = self._drag
        dx = (event.position().x() - start.x()) / target.width()
        dy = (event.position().y() - start.y()) / target.height()
        if self.ratio is not None and self.aspect:
            self.crop = dragged_to_ratio(before, part, dx, dy, self.ratio, self.aspect)
        else:
            self.crop = dragged(before, part, dx, dy)
        self.crop_moved.emit(self.crop)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.crop_committed.emit(self.crop)
            return
        super().mouseReleaseEvent(event)


class DevelopView(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        # The crop's aspect ratio: the long side over the short (a number, "original"
        # for the photo's own, None for free), and whether it is turned from the photo's
        # own orientation
        self._ratio_base = None
        self._flipped = False
        self._aspect_index = 0
        # The original is shown while the button is down or the key is held
        self._held = False
        self.image = CropImageView()
        self.image.crop_moved.connect(
            lambda crop: session.preview(session.params.with_changes(crop=crop))
        )
        self.image.crop_committed.connect(
            lambda crop: session.commit(session.params.with_changes(crop=crop))
        )
        frame = QFrame()
        frame.setObjectName("pane")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.addWidget(self.image)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(style.px(8))
        layout.addWidget(frame, 1)
        layout.addWidget(self._make_panel())

        # C opens the crop or accepts it; Enter accepts it. Escape (cancel) is the
        # window's back action, which asks cancel_crop first
        self.crop_action = QAction("Crop", self)
        self.crop_action.setShortcut(QKeySequence(Qt.Key.Key_C))
        self.crop_action.triggered.connect(self.toggle_cropping)
        self.accept_action = QAction("Accept Crop", self)
        self.accept_action.setShortcuts(
            [QKeySequence(Qt.Key.Key_Return), QKeySequence(Qt.Key.Key_Enter)]
        )
        self.accept_action.triggered.connect(self.accept_crop)
        self.accept_action.setEnabled(False)
        for action in (self.crop_action, self.accept_action):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)
        self._crop_before = (Crop(), 0.0)

        session.params_changed.connect(self._show_params)
        session.before_changed.connect(self.image.set_before)
        session.opened.connect(lambda *_size: self._sync_constraint())
        session.image_ready.connect(self._on_image)
        session.region_ready.connect(self.image.set_detail)
        # The part of the photo at the zoom is asked for when the zoom or the place has
        # stopped changing
        self._region_timer = QTimer(self)
        self._region_timer.setSingleShot(True)
        self._region_timer.setInterval(_REGION_DELAY)
        self._region_timer.timeout.connect(self._request_region)
        self.image.zoom_changed.connect(self._schedule_region)
        session.failed.connect(self._on_failed)
        session.service.presets_changed.connect(self._refresh_presets)
        self._refresh_presets()
        self._show_params(session.params)

    # Building

    def _make_panel(self):
        scroll = QScrollArea()
        scroll.setObjectName("developScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(style.px(PANEL_WIDTH))
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        panel = QFrame()
        panel.setObjectName("card")
        layout = QVBoxLayout(panel)
        margin = style.px(14)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(style.px(10))

        self.before_button = QPushButton("Show Original")
        self.before_button.setCheckable(True)
        self.before_button.setToolTip(
            "The photo as it is, without the edit: stays on while pressed in, "
            "or only while the \\ key is held"
        )
        self.before_button.toggled.connect(lambda _on: self._update_before())
        layout.addWidget(self.before_button)

        self._heading(layout, "LIGHT")
        self.exposure = self._slider(
            layout, "Exposure", *develop.EXPOSURE, unit=" EV", field="exposure"
        )
        # Contrast, Highlights, Shadows, Whites, Blacks
        self.basic = {
            name: self._slider(
                layout, name.capitalize(), *develop.BASIC, step=1, decimals=0, field=name
            )
            for name in develop.BASIC_FIELDS
        }
        self._heading(layout, "PRESENCE")
        # Texture, Clarity, Dehaze
        self.presence = {
            name: self._slider(
                layout, name.capitalize(), *develop.PRESENCE, step=1, decimals=0, field=name
            )
            for name in develop.PRESENCE_FIELDS
        }
        self._heading(layout, "SHARPENING")
        self.sharpening = {
            "amount": self._slider(
                layout,
                "Amount",
                *develop.SHARPEN_AMOUNT,
                step=1,
                decimals=0,
                field="sharpening.amount",
            ),
            "radius": self._slider(
                layout,
                "Radius",
                *develop.SHARPEN_RADIUS,
                step=0.1,
                decimals=1,
                unit=" px",
                field="sharpening.radius",
                default=develop.DEFAULT_SHARPEN_RADIUS,
            ),
            "detail": self._slider(
                layout,
                "Detail",
                *develop.PERCENT,
                step=1,
                decimals=0,
                field="sharpening.detail",
                default=develop.DEFAULT_SHARPEN_DETAIL,
            ),
            "masking": self._slider(
                layout, "Masking", *develop.PERCENT, step=1, decimals=0, field="sharpening.masking"
            ),
        }
        self._heading(layout, "NOISE REDUCTION")
        self.noise = {
            "luminance": self._slider(
                layout,
                "Luminance",
                *develop.PERCENT,
                step=1,
                decimals=0,
                field="noise.luminance",
            ),
            "detail": self._slider(
                layout,
                "Detail",
                *develop.PERCENT,
                step=1,
                decimals=0,
                field="noise.detail",
                default=develop.DEFAULT_NOISE_DETAIL,
            ),
            "colour": self._slider(
                layout, "Colour", *develop.PERCENT, step=1, decimals=0, field="noise.colour"
            ),
        }
        self._heading(layout, "WHITE BALANCE")
        self.temperature = self._slider(
            layout, "Temperature", *develop.WARMTH, step=1, decimals=0, field="temperature"
        )
        self.tint = self._slider(layout, "Tint", *develop.WARMTH, step=1, decimals=0, field="tint")

        self._heading(layout, "COLOUR")
        # Vibrance, Saturation
        self.colour = {
            name: self._slider(
                layout, name.capitalize(), *develop.COLOUR, step=1, decimals=0, field=name
            )
            for name in develop.COLOUR_FIELDS
        }

        self._heading(layout, "HSL")
        colours = {
            name: QColor.fromHsvF(hue / 360.0, *_BAND_COLOUR)
            for name, hue in develop.BAND_HUES.items()
        }
        self.band_group = self._segments(layout, _BANDS, self._show_mixers, colours)
        # Hue, Saturation, Luminance of the band chosen above
        self.hsl = {
            name: self._slider(
                layout, name.capitalize(), *develop.HSL, step=1, decimals=0, field=f"hsl.{name}"
            )
            for name in develop.BAND_FIELDS
        }

        self._heading(layout, "COLOUR GRADING")
        self.range_group = self._segments(layout, _RANGES, self._show_mixers)
        # Hue (a tint's direction), Saturation (its strength), Luminance of the range
        self.wheel = {
            name: self._slider(
                layout,
                name.capitalize(),
                *{"hue": develop.HUE, "saturation": develop.WHEEL}.get(name, develop.GRADING),
                step=1,
                decimals=0,
                unit="°" if name == "hue" else "",
                field=f"grading.{name}",
            )
            for name in develop.WHEEL_FIELDS
        }
        self.blending = self._slider(
            layout,
            "Blending",
            *develop.BLENDING,
            step=1,
            decimals=0,
            field="grading.blending",
            default=develop.DEFAULT_BLENDING,
        )
        self.balance = self._slider(
            layout, "Balance", *develop.GRADING, step=1, decimals=0, field="grading.balance"
        )

        self._heading(layout, "TONE CURVE")
        channels = QHBoxLayout()
        channels.setSpacing(style.px(4))
        self.channel_group = QButtonGroup(self)
        for name, label in _CHANNELS:
            button = QToolButton()
            button.setObjectName("segment")
            button.setText(label)
            button.setCheckable(True)
            button.setProperty("channel", name)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.channel_group.addButton(button)
            channels.addWidget(button)
        self.channel_group.buttons()[0].setChecked(True)
        self.channel_group.buttonClicked.connect(lambda _b: self._show_curve())
        layout.addLayout(channels)
        self.curve = CurveEditor()
        self.curve.moved.connect(lambda points: self._curve_changed(points, commit=False))
        self.curve.committed.connect(lambda points: self._curve_changed(points, commit=True))
        layout.addWidget(self.curve)
        reset_curve = QPushButton("Reset Curve")
        reset_curve.setObjectName("link")
        reset_curve.clicked.connect(self.curve.reset)
        layout.addWidget(reset_curve, 0, Qt.AlignmentFlag.AlignRight)

        self._heading(layout, "MATTE")
        self.matte_black = self._slider(layout, "Lift blacks", *develop.MATTE_BLACK, field="black")
        self.matte_white = self._slider(layout, "Lower whites", *develop.MATTE_WHITE, field="white")
        self.matte_desaturation = self._slider(
            layout, "Desaturate shadows", *develop.DESATURATION, field="shadow_desaturation"
        )

        self._heading(layout, "EFFECTS")
        self.vignette = {
            "amount": self._slider(
                layout, "Vignette", *develop.VIGNETTE, step=1, decimals=0, field="vignette.amount"
            ),
            "midpoint": self._slider(
                layout,
                "Midpoint",
                *develop.PERCENT,
                step=1,
                decimals=0,
                field="vignette.midpoint",
                default=develop.DEFAULT_MIDPOINT,
            ),
            "feather": self._slider(
                layout,
                "Feather",
                *develop.PERCENT,
                step=1,
                decimals=0,
                field="vignette.feather",
                default=develop.DEFAULT_FEATHER,
            ),
        }
        self.grain = {
            "amount": self._slider(
                layout, "Grain", *develop.PERCENT, step=1, decimals=0, field="grain.amount"
            ),
            "size": self._slider(
                layout,
                "Grain size",
                *develop.PERCENT,
                step=1,
                decimals=0,
                field="grain.size",
                default=develop.DEFAULT_GRAIN_SIZE,
            ),
        }

        self._heading(layout, "CROP & ROTATE")
        row = QHBoxLayout()
        self.crop_button = QPushButton("Crop")
        self.crop_button.setCheckable(True)
        self.crop_button.setToolTip("Set the crop on the photo (C); Enter accepts, Escape cancels")
        self.crop_button.toggled.connect(self.set_cropping)
        self.reset_crop = QPushButton("Reset Crop")
        self.reset_crop.setToolTip("No crop and no straightening")
        self.reset_crop.clicked.connect(
            lambda: self.session.commit(
                self.session.params.with_changes(crop=Crop(), straighten=0.0)
            )
        )
        row.addWidget(self.crop_button)
        row.addWidget(self.reset_crop)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.aspect = QComboBox()
        for text, key in _ASPECTS:
            self.aspect.addItem(text, key)
        self.aspect.setToolTip("Hold the crop to an aspect ratio")
        self.aspect.activated.connect(self._on_aspect_chosen)
        self.swap_ratio = QPushButton("Swap")
        self.swap_ratio.setToolTip("Turn the frame between landscape and portrait")
        self.swap_ratio.clicked.connect(self._swap_ratio)
        row.addWidget(self.aspect, 1)
        row.addWidget(self.swap_ratio)
        layout.addLayout(row)
        self.lock_ratio = QCheckBox("Lock aspect ratio")
        self.lock_ratio.setToolTip("Keep the crop's shape as you drag it")
        self.lock_ratio.clicked.connect(self._on_lock)
        layout.addWidget(self.lock_ratio)
        self.straighten = self._slider(
            layout,
            "Straighten",
            *develop.STRAIGHTEN,
            step=0.1,
            decimals=1,
            unit="°",
            field="straighten",
        )
        # The grid is there while the slider moves, and goes when it is let go
        self.straighten.moved.connect(lambda _v: self.image.set_grid(True))
        self.straighten.committed.connect(lambda _v: self.image.set_grid(False))
        row = QHBoxLayout()
        self.rotate_left = QPushButton("Rotate Left")
        self.rotate_left.clicked.connect(lambda: self._turn(-1))
        self.rotate_right = QPushButton("Rotate Right")
        self.rotate_right.clicked.connect(lambda: self._turn(1))
        row.addWidget(self.rotate_left)
        row.addWidget(self.rotate_right)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.flip_horizontal = QPushButton("Flip Horizontal")
        self.flip_horizontal.clicked.connect(lambda: self._flip(True))
        self.flip_vertical = QPushButton("Flip Vertical")
        self.flip_vertical.clicked.connect(lambda: self._flip(False))
        row.addWidget(self.flip_horizontal)
        row.addWidget(self.flip_vertical)
        layout.addLayout(row)

        self._heading(layout, "PRESETS")
        self.presets = QComboBox()
        self.presets.activated.connect(self._on_preset_chosen)
        layout.addWidget(self.presets)
        row = QHBoxLayout()
        save = QPushButton("Save Preset…")
        save.clicked.connect(self.save_preset)
        self.delete_preset_button = QPushButton("Delete")
        self.delete_preset_button.clicked.connect(self.delete_preset)
        row.addWidget(save)
        row.addWidget(self.delete_preset_button)
        layout.addLayout(row)
        self._heading(layout, "SETTINGS")
        row = QHBoxLayout()
        self.copy_settings_button = QPushButton("Copy…")
        self.copy_settings_button.setToolTip("Copy parts of this edit, to paste on other photos")
        self.paste_settings_button = QPushButton("Paste")
        self.paste_settings_button.setToolTip("Put the copied parts of an edit over this one")
        row.addWidget(self.copy_settings_button)
        row.addWidget(self.paste_settings_button)
        layout.addLayout(row)
        layout.addStretch(1)
        scroll.setWidget(panel)
        return scroll

    def _heading(self, layout, text):
        heading = QLabel(text)
        heading.setObjectName("heading")
        layout.addSpacing(style.px(4))
        layout.addWidget(heading)

    def _segments(self, layout, choices, slot, colours=None):
        """A row of buttons, one chosen at a time: (name, label) each, marked with
        `colours[name]` if given. The chosen name is `_chosen(group)`; `slot` is called
        when the choice is clicked."""
        row = QHBoxLayout()
        row.setSpacing(style.px(4))
        group = QButtonGroup(self)
        for name, label in choices:
            button = _Segment((colours or {}).get(name))
            button.setObjectName("segment")
            button.setText(label)
            button.setToolTip(name.capitalize())
            button.setCheckable(True)
            button.setProperty("choice", name)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            group.addButton(button)
            row.addWidget(button)
        group.buttons()[0].setChecked(True)
        group.buttonClicked.connect(lambda _button: slot())
        layout.addLayout(row)
        return group

    @staticmethod
    def _chosen(group):
        return group.checkedButton().property("choice")

    def _slider(
        self, layout, title, low, high, step=0.01, decimals=2, unit="", field=None, default=0.0
    ):
        slider = ValueSlider(title, low, high, default, step=step, decimals=decimals, unit=unit)
        slider.moved.connect(lambda value: self.session.preview(self._with(field, value)))
        slider.committed.connect(lambda value: self.session.commit(self._with(field, value)))
        layout.addWidget(slider)
        return slider

    def _with(self, field, value):
        params = self.session.params
        if field in ("black", "white", "shadow_desaturation"):
            matte = params.matte
            values = {
                "black": matte.black,
                "white": matte.white,
                "shadow_desaturation": matte.shadow_desaturation,
            }
            values[field] = value
            return params.with_changes(matte=Matte(**values))
        if field in develop.BASIC_FIELDS:
            return params.with_changes(basic=replace(params.basic, **{field: value}))
        if field.startswith("sharpening."):
            return params.with_changes(sharpening=replace(params.sharpening, **{field[11:]: value}))
        if field.startswith("noise."):
            return params.with_changes(noise=replace(params.noise, **{field[6:]: value}))
        if field in develop.PRESENCE_FIELDS:
            return params.with_changes(presence=replace(params.presence, **{field: value}))
        if field in develop.COLOUR_FIELDS:
            return params.with_changes(colour=replace(params.colour, **{field: value}))
        if field.startswith("vignette."):
            return params.with_changes(vignette=replace(params.vignette, **{field[9:]: value}))
        if field.startswith("grain."):
            return params.with_changes(grain=replace(params.grain, **{field[6:]: value}))
        if field.startswith("hsl."):
            band = self._chosen(self.band_group)
            changed = replace(getattr(params.hsl, band), **{field[4:]: value})
            return params.with_changes(hsl=replace(params.hsl, **{band: changed}))
        if field in ("grading.blending", "grading.balance"):
            return params.with_changes(grading=replace(params.grading, **{field[8:]: value}))
        if field.startswith("grading."):
            name = self._chosen(self.range_group)
            changed = replace(getattr(params.grading, name), **{field[8:]: value})
            return params.with_changes(grading=replace(params.grading, **{name: changed}))
        return params.with_changes(**{field: value})

    # Showing the edit

    def _show_params(self, params):
        self.exposure.set_value(params.exposure)
        for name, slider in self.basic.items():
            slider.set_value(getattr(params.basic, name))
        for name, slider in self.presence.items():
            slider.set_value(getattr(params.presence, name))
        for name, slider in self.sharpening.items():
            slider.set_value(getattr(params.sharpening, name))
        for name, slider in self.noise.items():
            slider.set_value(getattr(params.noise, name))
        self.temperature.set_value(params.temperature)
        self.tint.set_value(params.tint)
        for name, slider in self.colour.items():
            slider.set_value(getattr(params.colour, name))
        self._show_mixers(params)
        self.matte_black.set_value(params.matte.black)
        self.matte_white.set_value(params.matte.white)
        self.matte_desaturation.set_value(params.matte.shadow_desaturation)
        for name, slider in self.vignette.items():
            slider.set_value(getattr(params.vignette, name))
        for name, slider in self.grain.items():
            slider.set_value(getattr(params.grain, name))
        self.straighten.set_value(params.straighten)
        self._show_curve()
        self.image.set_crop(params.crop)
        self._sync_constraint()

    def _show_mixers(self, params=None):
        """The sliders of the band and the range chosen, and blending and balance."""
        params = params if isinstance(params, develop.Params) else self.session.params
        band = getattr(params.hsl, self._chosen(self.band_group))
        for name, slider in self.hsl.items():
            slider.set_value(getattr(band, name))
        wheel = getattr(params.grading, self._chosen(self.range_group))
        for name, slider in self.wheel.items():
            slider.set_value(getattr(wheel, name))
        self.blending.set_value(params.grading.blending)
        self.balance.set_value(params.grading.balance)

    def _channel(self):
        checked = self.channel_group.checkedButton()
        return checked.property("channel") if checked is not None else "rgb"

    def _show_curve(self):
        channel = self._channel()
        if self.curve._dragging is None:
            self.curve.set_curve(channel, getattr(self.session.params.curves, channel))
        else:
            self.curve.channel = channel

    def _curve_changed(self, points, commit):
        current = self.session.params.curves
        values = {name: getattr(current, name) for name, _ in _CHANNELS}
        values[self._channel()] = points
        params = self.session.params.with_changes(curves=Curves(**values))
        if commit:
            self.session.commit(params)
        else:
            self.session.preview(params)

    def _on_image(self, image, histogram, kind):
        self.image.set_image(image, self.session.full_size())
        self.curve.set_histogram(histogram)
        if kind == develop.SCREEN:
            # Zoomed, the screen-sized image is scaled up: the photo's own pixels follow
            self._request_region()

    def _schedule_region(self):
        if self.image.region_wanted() is None or self.image.cropping:
            self._region_timer.stop()
            self.image.set_detail(None, None, None)
        else:
            self._region_timer.start()

    def _request_region(self):
        wanted = self.image.region_wanted()
        if wanted is not None and not self.image.cropping:
            self.session.request_region(*wanted)

    def _on_failed(self, message):
        self.image.set_message(message)

    def show_loading(self, name):
        self.image.set_message(f"Opening {name}…")
        self.crop_button.setChecked(False)
        self.before_button.setChecked(False)
        self._held = False

    # Geometry

    def _turn(self, quarters):
        # The crop, the mirroring and the straightening go with the photo
        self.session.commit(self.session.params.turned(quarters))

    def _flip(self, horizontal):
        self.session.commit(self.session.params.flipped(horizontal))

    # The crop's aspect ratio

    def _current_ratio(self):
        """The frame's width over its height in pixels that the crop is held to, or
        None while it is free."""
        base, aspect = self._ratio_base, self.session.photo_aspect()
        if base is None or aspect is None:
            return None
        if base == "original":
            base = max(aspect, 1 / aspect)
        portrait = (aspect < 1) != self._flipped
        return 1 / base if portrait else base

    def _sync_constraint(self):
        ratio = self._current_ratio()
        self.image.set_constraint(ratio, self.session.photo_aspect())
        self.swap_ratio.setEnabled(ratio is not None)
        self.lock_ratio.setChecked(self._ratio_base is not None)

    def _reshape(self):
        """The crop in the shape asked for, about the same size and place."""
        ratio, aspect = self._current_ratio(), self.session.photo_aspect()
        self._sync_constraint()
        if ratio is None:
            return
        params = self.session.params
        crop = reshaped(params.crop, ratio, aspect)
        if crop != params.crop:
            self.session.commit(params.with_changes(crop=crop))

    def _on_aspect_chosen(self, index):
        key = self.aspect.itemData(index)
        if key == "custom":
            dialog = RatioDialog(parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.aspect.setCurrentIndex(self._aspect_index)
                return
            width, height = dialog.ratio()
            self._ratio_base = max(width, height) / min(width, height)
            self._flipped = (height > width) != ((self.session.photo_aspect() or 1.0) < 1)
        else:
            self._ratio_base, self._flipped = key, False
        self._aspect_index = index
        if key is not None:
            # The crop is for setting over the whole photo
            self.set_cropping(True)
        self._reshape()

    def _swap_ratio(self):
        if self._ratio_base is not None:
            self._flipped = not self._flipped
            self._reshape()

    def _on_lock(self, locked):
        aspect = self.session.photo_aspect()
        if locked and aspect is not None:
            if self._ratio_base is None:
                # Keep the shape the frame has now
                crop = self.session.params.crop
                shape = (crop.right - crop.left) * aspect / (crop.bottom - crop.top)
                self._ratio_base = max(shape, 1 / shape)
                self._flipped = (shape < 1) != (aspect < 1)
        else:
            self._ratio_base = None
            self._aspect_index = 0
            self.aspect.setCurrentIndex(0)
        self._sync_constraint()

    # Showing the original

    def _update_before(self):
        self.session.set_before(self.before_button.isChecked() or self._held)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Backslash and not event.isAutoRepeat():
            self._held = True
            self._update_before()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Backslash and not event.isAutoRepeat():
            self._held = False
            self._update_before()
            return
        super().keyReleaseEvent(event)

    def hideEvent(self, event):
        # A key let go while another page showed would never be heard of
        if self._held:
            self._held = False
            self._update_before()
        super().hideEvent(event)

    def is_cropping(self):
        return self.crop_button.isChecked()

    def toggle_cropping(self):
        """C: open the crop, or accept it."""
        self.set_cropping(not self.is_cropping())

    def accept_crop(self):
        """Enter: keep the crop as it is and close it."""
        self.set_cropping(False)

    def cancel_crop(self):
        """Escape: back to the crop and straightening it had when it was opened."""
        if not self.is_cropping():
            return
        before = self._crop_before
        current = self.session.params
        self.set_cropping(False)
        if (current.crop, current.straighten) != before:
            self.session.commit(current.with_changes(crop=before[0], straighten=before[1]))

    def set_cropping(self, on):
        if self.crop_button.isChecked() != on:
            self.crop_button.setChecked(on)
            return
        if on:
            self._crop_before = (self.session.params.crop, self.session.params.straighten)
        self.image.set_cropping(on, self.session.params.crop)
        self.session.set_crop_view(on)
        self.crop_button.setText("Done" if on else "Crop")
        self.accept_action.setEnabled(on)

    # Presets

    def _refresh_presets(self):
        names = self.session.presets()
        self.presets.blockSignals(True)
        self.presets.clear()
        self.presets.addItem("Apply a preset…" if names else "No presets yet", None)
        for name in names:
            self.presets.addItem(name, name)
        self.presets.setCurrentIndex(0)
        self.presets.blockSignals(False)
        self.delete_preset_button.setEnabled(bool(names))

    def _on_preset_chosen(self, index):
        name = self.presets.itemData(index)
        if name:
            self.session.apply_preset(name)
        self.presets.setCurrentIndex(0)

    def save_preset(self):
        dialog = PresetDialog(self.session.presets(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.session.save_preset(dialog.name(), dialog.sections())

    def delete_preset(self):
        names = self.session.presets()
        if not names:
            return
        name, ok = QInputDialog.getItem(self, "Delete Preset", "Preset to delete:", names, 0, False)
        if ok and name:
            answer = QMessageBox.question(self, "Delete Preset", f"Delete the preset “{name}”?")
            if answer == QMessageBox.StandardButton.Yes:
                self.session.delete_preset(name)
