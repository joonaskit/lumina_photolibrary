"""Year, camera, lens, flag, rating and edit state, above the grid, and the search field at the
right."""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QSizePolicy, QToolButton, QWidget

from lumina.integration import style
from lumina.qt_models.library_model import (
    EDITED,
    NOT_RATED,
    PICKED,
    REJECTED,
    UNEDITED,
    UNFLAGGED,
)

FLAG_CHOICES = (
    ("All flags", None),
    ("Thumbs up", PICKED),
    ("Not flagged", UNFLAGGED),
    ("Thumbs down", REJECTED),
)

RATING_CHOICES = (
    ("Any rating", None),
    ("★ 1 or more", 1),
    ("★★ 2 or more", 2),
    ("★★★ 3 or more", 3),
    ("★★★★ 4 or more", 4),
    ("★★★★★ 5", 5),
    ("Not rated", NOT_RATED),
)

EDIT_CHOICES = (
    ("Edited and unedited", None),
    ("Edited", EDITED),
    ("Unedited", UNEDITED),
)


class FilterBar(QWidget):
    # Something was chosen: read `values()`
    changed = Signal()
    # A flag to show only, or None for all
    flag_changed = Signal(object)
    # NOT_RATED, or a number of stars to show those with that many or more, or None for all
    rating_changed = Signal(object)
    # EDITED or UNEDITED to show only those, or None for all
    edit_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, style.px(8))
        layout.setSpacing(style.px(8))
        self.year = self._combo("All years")
        self.camera = self._combo("All cameras")
        self.lens = self._combo("All lenses")
        # Not the library's choices: the same whatever the folder
        self.flag = self._fixed_combo(FLAG_CHOICES, self.flag_changed)
        self.rating = self._fixed_combo(RATING_CHOICES, self.rating_changed)
        self.edit = self._fixed_combo(EDIT_CHOICES, self.edit_changed)
        for combo in (self.year, self.camera, self.lens, self.flag, self.rating, self.edit):
            layout.addWidget(combo)
        layout.addStretch(1)

    def _fixed_combo(self, choices, changed):
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        for text, value in choices:
            combo.addItem(text, value)
        combo.currentIndexChanged.connect(lambda _index: changed.emit(combo.currentData()))
        return combo

    def edit_value(self):
        """EDITED or UNEDITED to show only those, or None for all."""
        return self.edit.currentData()

    def rating_value(self):
        """NOT_RATED, a number of stars, or None for all."""
        return self.rating.currentData()

    def flag_value(self):
        """The flag to show only, or None for all."""
        return self.flag.currentData()

    def add_trailing(self, widget):
        """A widget at the right end of the row: the buttons, the search field. Square
        buttons in it are as tall as the combos beside them."""
        self.layout().addWidget(widget)
        self._fit_buttons()

    def _fit_buttons(self):
        # The combos' height comes from the font, which the desktop chooses
        side = self.year.sizeHint().height()
        for button in self.findChildren(QToolButton):
            if button.objectName() == "segment":
                button.setFixedSize(side, side)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (event.Type.FontChange, event.Type.StyleChange):
            # Once the combos have their new size
            QTimer.singleShot(0, self, self._fit_buttons)

    def _combo(self, everything):
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.setMinimumWidth(style.px(120))
        # A long lens name is cut short rather than making the row wider than the window
        combo.setMaximumWidth(style.px(280))
        # Never narrower than its text: the search field beside them gives way first
        combo.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        combo.addItem(everything, None)
        combo.currentIndexChanged.connect(lambda _index: self.changed.emit())
        return combo

    def values(self):
        """(year, camera, lens): each None for any."""
        return tuple(combo.currentData() for combo in (self.year, self.camera, self.lens))

    def set_choices(self, years, cameras, lenses):
        """What there is to choose from. A choice still there stays chosen; one that is
        not goes back to "All". Signals `changed` once, if a choice went."""
        before = self.values()
        for combo, values in ((self.year, years), (self.camera, cameras), (self.lens, lenses)):
            current = combo.currentData()
            combo.blockSignals(True)
            while combo.count() > 1:
                combo.removeItem(1)
            for value in values:
                combo.addItem(value, value)
            index = combo.findData(current) if current is not None else 0
            combo.setCurrentIndex(max(index, 0))
            combo.blockSignals(False)
        if self.values() != before:
            self.changed.emit()
