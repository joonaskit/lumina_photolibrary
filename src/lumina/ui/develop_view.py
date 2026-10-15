"""The Develop view: the photo on the left, the edit's controls in a card on the right.

Every control previews while it moves and commits when it is let go (see
qt_models/develop.py). The crop is set on the photo itself: with Crop on, the
whole photo is shown and the crop is a frame over it, with handles at its corners
and edges; dragging inside it moves it.
"""

from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainterPath, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
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
from lumina.ui.curve_editor import CurveEditor
from lumina.ui.image_view import ImageView

PANEL_WIDTH = 320
# The handles' size and how near the pointer must be to take one, unscaled
_HANDLE = 7
_GRAB = 12
# The smallest crop, as a share of the photo
_MIN_CROP = 0.02
_CHANNELS = (("rgb", "RGB"), ("red", "R"), ("green", "G"), ("blue", "B"))


class CropImageView(ImageView):
    """The photo, and with crop mode on, the crop frame over it."""

    # The crop while it is dragged, and when let go
    crop_moved = Signal(object)
    crop_committed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cropping = False
        self.crop = Crop()
        self._drag = None
        self.setMouseTracking(True)

    def set_cropping(self, on, crop):
        self.cropping, self.crop = on, crop
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

    def paint_over(self, painter, target):
        if not self.cropping:
            return
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
        self.crop = dragged(before, part, dx, dy)
        self.crop_moved.emit(self.crop)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.crop_committed.emit(self.crop)
            return
        super().mouseReleaseEvent(event)


def dragged(crop, part, dx, dy):
    """`crop` with `part` (an edge, a corner, or "move") dragged by dx, dy shares."""
    left, top, right, bottom = crop.left, crop.top, crop.right, crop.bottom
    if part == "move":
        dx = min(max(dx, -left), 1 - right)
        dy = min(max(dy, -top), 1 - bottom)
        return Crop(left + dx, top + dy, right + dx, bottom + dy)
    if "l" in part:
        left = min(max(left + dx, 0.0), right - _MIN_CROP)
    if "r" in part:
        right = max(min(right + dx, 1.0), left + _MIN_CROP)
    if "t" in part:
        top = min(max(top + dy, 0.0), bottom - _MIN_CROP)
    if "b" in part:
        bottom = max(min(bottom + dy, 1.0), top + _MIN_CROP)
    return Crop(left, top, right, bottom)


class DevelopView(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
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
        session.image_ready.connect(self._on_image)
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
        self._heading(layout, "WHITE BALANCE")
        self.temperature = self._slider(
            layout, "Temperature", *develop.WARMTH, step=1, decimals=0, field="temperature"
        )
        self.tint = self._slider(layout, "Tint", *develop.WARMTH, step=1, decimals=0, field="tint")

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
        self.straighten = self._slider(
            layout,
            "Straighten",
            *develop.STRAIGHTEN,
            step=0.1,
            decimals=1,
            unit="°",
            field="straighten",
        )
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
        layout.addStretch(1)
        scroll.setWidget(panel)
        return scroll

    def _heading(self, layout, text):
        heading = QLabel(text)
        heading.setObjectName("heading")
        layout.addSpacing(style.px(4))
        layout.addWidget(heading)

    def _slider(self, layout, title, low, high, step=0.01, decimals=2, unit="", field=None):
        slider = ValueSlider(title, low, high, 0.0, step=step, decimals=decimals, unit=unit)
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
        return params.with_changes(**{field: value})

    # Showing the edit

    def _show_params(self, params):
        self.exposure.set_value(params.exposure)
        for name, slider in self.basic.items():
            slider.set_value(getattr(params.basic, name))
        self.temperature.set_value(params.temperature)
        self.tint.set_value(params.tint)
        self.matte_black.set_value(params.matte.black)
        self.matte_white.set_value(params.matte.white)
        self.matte_desaturation.set_value(params.matte.shadow_desaturation)
        self.straighten.set_value(params.straighten)
        self._show_curve()
        self.image.set_crop(params.crop)

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

    def _on_image(self, image, histogram, _kind):
        self.image.set_image(image)
        self.curve.set_histogram(histogram)

    def _on_failed(self, message):
        self.image.set_message(message)

    def show_loading(self, name):
        self.image.set_message(f"Opening {name}…")
        self.crop_button.setChecked(False)

    # Geometry

    def _turn(self, quarters):
        # The crop, the mirroring and the straightening go with the photo
        self.session.commit(self.session.params.turned(quarters))

    def _flip(self, horizontal):
        self.session.commit(self.session.params.flipped(horizontal))

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
        name, ok = QInputDialog.getText(
            self,
            "Save Preset",
            "Name of the preset. It keeps the white balance, contrast, highlights, shadows,\n"
            "whites, blacks, tone curve and matte of this edit:",
        )
        name = name.strip()
        if ok and name:
            self.session.save_preset(name, ["white_balance", "basic", "curves", "matte"])

    def delete_preset(self):
        names = self.session.presets()
        if not names:
            return
        name, ok = QInputDialog.getItem(self, "Delete Preset", "Preset to delete:", names, 0, False)
        if ok and name:
            answer = QMessageBox.question(self, "Delete Preset", f"Delete the preset “{name}”?")
            if answer == QMessageBox.StandardButton.Yes:
                self.session.delete_preset(name)
