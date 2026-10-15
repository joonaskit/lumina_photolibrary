"""Year, camera, lens, flag, rating and edit state, above the grid: what is shown, and
nothing else. Clear at the right end shows everything again."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QPushButton, QSizePolicy, QWidget

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
        for combo in (self.year, self.camera, self.lens):
            layout.addWidget(combo)
        # What the photos are, then what was done with them
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedWidth(1)
        layout.addWidget(divider)
        for combo in (self.flag, self.rating, self.edit):
            layout.addWidget(combo)
            combo.currentIndexChanged.connect(self._update_clear)
        for combo in (self.year, self.camera, self.lens):
            combo.currentIndexChanged.connect(self._update_clear)
        layout.addStretch(1)
        self.clear_button = QPushButton("Clear")
        # Rounded for its height in the row, not a dialog's button's
        self.clear_button.setObjectName("row")
        self.clear_button.setToolTip("Show every photo again")
        self.clear_button.clicked.connect(self.clear)
        layout.addWidget(self.clear_button)
        # As tall and as round as the toolbar's cards and search field, whatever the font
        for widget in (*self._combos(), self.clear_button):
            widget.setFixedHeight(style.px(style.ROW_HEIGHT))
        for combo in self._combos():
            combo.setObjectName("row")
        self._update_clear()

    def _combos(self):
        return (self.year, self.camera, self.lens, self.flag, self.rating, self.edit)

    def _update_clear(self):
        self.clear_button.setEnabled(any(combo.currentIndex() for combo in self._combos()))

    def clear(self):
        """Every filter back to all, each signal once for what changed."""
        before = self.values()
        changed = [combo for combo in (self.flag, self.rating, self.edit) if combo.currentIndex()]
        for combo in self._combos():
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self._update_clear()
        if self.values() != before:
            self.changed.emit()
        signals = {
            self.flag: self.flag_changed,
            self.rating: self.rating_changed,
            self.edit: self.edit_changed,
        }
        for combo in changed:
            signals[combo].emit(None)

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

    def _combo(self, everything):
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.setMinimumWidth(style.px(120))
        # A long lens name is cut short rather than making the row wider than the window
        combo.setMaximumWidth(style.px(280))
        # Never narrower than its text
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
        self._update_clear()
        if self.values() != before:
            self.changed.emit()
