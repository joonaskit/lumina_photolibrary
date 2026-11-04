"""The Develop panel's local adjustments: the edit's masks as a list, and the controls of
the one chosen. Where a mask is on the photo is set on the photo itself (see
mask_frame.py and the Develop view); everything here goes through the session like any
other control: previewed while a slider moves, one undo step when it is let go."""

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lumina.core.edit.params import (
    BASIC,
    COLOUR,
    DEFAULT_FEATHER,
    EXPOSURE,
    LOCAL_FIELDS,
    MAX_MASKS,
    PERCENT,
    WARMTH,
    Local,
    Mask,
)
from lumina.integration import style
from lumina.ui.controls import ValueSlider

# Each local control's range, step, decimals and unit, in the order they are shown
_CONTROLS = {
    "exposure": (*EXPOSURE, 0.01, 2, " EV"),
    "temperature": (*WARMTH, 1, 0, ""),
    "tint": (*WARMTH, 1, 0, ""),
    "contrast": (*BASIC, 1, 0, ""),
    "highlights": (*BASIC, 1, 0, ""),
    "shadows": (*BASIC, 1, 0, ""),
    "whites": (*BASIC, 1, 0, ""),
    "blacks": (*BASIC, 1, 0, ""),
    "vibrance": (*COLOUR, 1, 0, ""),
    "saturation": (*COLOUR, 1, 0, ""),
}
_KINDS = (("linear", "Linear"), ("radial", "Radial"))
# Rows of the list shown before it scrolls
_LIST_ROWS = 3


def label(masks, index):
    """What the list calls the mask at `index`: its name, or its kind and number among
    those of its kind."""
    mask = masks[index]
    if mask.name:
        return mask.name
    number = 1 + sum(1 for other in masks[:index] if other.kind == mask.kind)
    return f"{mask.kind.capitalize()} {number}"


class MasksPanel(QWidget):
    """Add, choose, switch on and off, invert and delete masks; set the chosen one's
    adjustments."""

    # The mask chosen (an index into the edit's masks) or None, and whether the tint shows
    selection_changed = Signal(object)
    overlay_changed = Signal(bool)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.selected = None
        self.overlay = True
        self._showing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        style.follow_density(self, lambda: layout.setSpacing(style.gap(10)))

        row = QHBoxLayout()
        self.add_buttons = {}
        for kind, title in _KINDS:
            button = QPushButton(f"+ {title}")
            button.setToolTip(f"Add a {kind} mask: an adjustment for part of the photo")
            button.clicked.connect(lambda _checked=False, kind=kind: self.add(kind))
            self.add_buttons[kind] = button
            row.addWidget(button)
        layout.addLayout(row)

        self.list = QListWidget()
        self.list.setObjectName("maskList")
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.currentRowChanged.connect(self._on_row)
        self.list.itemChanged.connect(self._on_item_changed)
        style.follow_density(self, self._size_list)
        layout.addWidget(self.list)

        row = QHBoxLayout()
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete)
        self.invert_box = QCheckBox("Invert")
        self.invert_box.setToolTip("Apply the adjustment everywhere but where the mask is")
        self.invert_box.toggled.connect(self._on_invert)
        self.overlay_box = QCheckBox("Show Mask")
        self.overlay_box.setChecked(True)
        self.overlay_box.setToolTip("Tint the photo where the chosen mask applies (O)")
        self.overlay_box.toggled.connect(self.set_overlay)
        row.addWidget(self.delete_button)
        row.addWidget(self.invert_box)
        row.addWidget(self.overlay_box)
        layout.addLayout(row)

        self.feather = ValueSlider("Feather", *PERCENT, DEFAULT_FEATHER, step=1, decimals=0)
        self.feather.moved.connect(lambda value: session.preview(self._with("feather", value)))
        self.feather.committed.connect(lambda value: session.commit(self._with("feather", value)))
        layout.addWidget(self.feather)

        self.sliders = {}
        for name in LOCAL_FIELDS:
            low, high, step, decimals, unit = _CONTROLS[name]
            slider = ValueSlider(
                name.capitalize(), low, high, 0.0, step=step, decimals=decimals, unit=unit
            )
            slider.moved.connect(
                lambda value, name=name: session.preview(self._with_local(name, value))
            )
            slider.committed.connect(
                lambda value, name=name: session.commit(self._with_local(name, value))
            )
            self.sliders[name] = slider
            layout.addWidget(slider)
        self.show_params(session.params)

    def _size_list(self):
        rows = self.list.fontMetrics().height() + 2 * style.gap(4)
        self.list.setFixedHeight(_LIST_ROWS * rows + 2 * self.list.frameWidth())

    # The edit

    @property
    def masks(self):
        return self.session.params.masks

    def mask(self):
        """The mask chosen, or None."""
        masks = self.masks
        if self.selected is None or self.selected >= len(masks):
            return None
        return masks[self.selected]

    def _replace(self, index, mask):
        masks = list(self.masks)
        masks[index] = mask
        return self.session.params.with_changes(masks=tuple(masks))

    def _with(self, field, value):
        """The edit with the chosen mask's `field` set."""
        mask = self.mask()
        if mask is None:
            return self.session.params
        return self._replace(self.selected, replace(mask, **{field: value}))

    def _with_local(self, name, value):
        mask = self.mask()
        if mask is None:
            return self.session.params
        return self._replace(
            self.selected, replace(mask, local=replace(mask.local, **{name: value}))
        )

    def with_shape(self, shape):
        """The edit with the chosen mask's shape set (dragged on the photo)."""
        return self._with("shape", shape)

    def add(self, kind):
        masks = self.masks
        if len(masks) >= MAX_MASKS:
            return
        self.session.commit(
            self.session.params.with_changes(masks=(*masks, Mask(kind, local=Local())))
        )
        self.select(len(masks))

    def delete(self):
        if self.selected is None or self.mask() is None:
            return
        index = self.selected
        masks = list(self.masks)
        del masks[index]
        # Committing shows the edit, which drops a choice past the end: the next mask (or
        # the last) is chosen once it has
        self.session.commit(self.session.params.with_changes(masks=tuple(masks)))
        self.select(min(index, len(masks) - 1) if masks else None)

    def select(self, index):
        """Choose the mask at `index` (None: none)."""
        if index is not None and not 0 <= index < len(self.masks):
            index = None
        if index != self.selected:
            self.selected = index
            self.selection_changed.emit(index)
        self.show_params(self.session.params)

    def set_overlay(self, on):
        if on != self.overlay:
            self.overlay = on
            self.overlay_changed.emit(on)
        if self.overlay_box.isChecked() != on:
            self.overlay_box.setChecked(on)

    # Showing the edit

    def show_params(self, params):
        """List the masks of `params` and their chosen one's controls."""
        masks = params.masks
        self._showing = True
        try:
            if self.selected is not None and self.selected >= len(masks):
                self.selected = None
                self.selection_changed.emit(None)
            self.list.clear()
            for index, mask in enumerate(masks):
                item = QListWidgetItem(label(masks, index))
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(
                    Qt.CheckState.Checked if mask.enabled else Qt.CheckState.Unchecked
                )
                item.setToolTip("Switch the mask on or off")
                self.list.addItem(item)
            self.list.setCurrentRow(-1 if self.selected is None else self.selected)
            mask = self.mask()
            chosen = mask is not None
            self.delete_button.setEnabled(chosen)
            self.invert_box.setEnabled(chosen)
            self.invert_box.setChecked(chosen and mask.invert)
            self.feather.setVisible(chosen and mask.kind == "radial")
            if chosen:
                self.feather.set_value(mask.feather)
            for name, slider in self.sliders.items():
                slider.setEnabled(chosen)
                slider.set_value(getattr(mask.local, name) if chosen else 0.0)
            for button in self.add_buttons.values():
                button.setEnabled(len(masks) < MAX_MASKS)
        finally:
            self._showing = False

    # The widgets

    def _on_row(self, row):
        if self._showing:
            return
        self.select(row if row >= 0 else None)

    def _on_item_changed(self, item):
        if self._showing:
            return
        index = self.list.row(item)
        masks = self.masks
        if not 0 <= index < len(masks):
            return
        on = item.checkState() == Qt.CheckState.Checked
        if masks[index].enabled != on:
            self.session.commit(self._replace(index, replace(masks[index], enabled=on)))

    def _on_invert(self, on):
        mask = self.mask()
        if self._showing or mask is None or mask.invert == on:
            return
        self.session.commit(self._with("invert", on))
