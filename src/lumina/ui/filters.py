"""Year, camera, lens, flag, rating, edit state and keyword, above the grid: what is shown, and
nothing else. Clear at the right end shows everything again."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QPushButton, QSizePolicy, QWidget

from lumina.core import keywords as keywordm
from lumina.core.dbtypes import PICKED, REJECTED, UNFLAGGED
from lumina.integration import style
from lumina.qt_models.library_model import BLURRY, EDITED, NOT_RATED, UNEDITED

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

# Blurry is measured against the best photo of the photo's stack, so only stacks have any
SHARPNESS_CHOICES = (
    ("Sharp and blurry", None),
    ("Blurry in stacks", BLURRY),
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
    # BLURRY to show only the blurry photos of stacks, or None for all
    sharpness_changed = Signal(object)
    # A keyword path to show only the photos given it or one below it, or None for all
    keyword_changed = Signal(object)
    # Save the filters as a smart collection
    save_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        self._searching = False
        self.year = self._combo("All years")
        self.camera = self._combo("All cameras")
        self.lens = self._combo("All lenses")
        # Not the library's choices: the same whatever the folder
        self.flag = self._fixed_combo(FLAG_CHOICES, self.flag_changed)
        self.rating = self._fixed_combo(RATING_CHOICES, self.rating_changed)
        self.edit = self._fixed_combo(EDIT_CHOICES, self.edit_changed)
        self.sharpness = self._fixed_combo(SHARPNESS_CHOICES, self.sharpness_changed)
        self.sharpness.setToolTip(
            "Photos much less sharp than the sharpest of their stack, to reject or review"
        )
        self.keyword = self._fixed_combo((("Any keyword", None),), self.keyword_changed)
        self.keyword.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.keyword.setMaximumWidth(style.px(240))
        for combo in (self.year, self.camera, self.lens):
            layout.addWidget(combo)
        # What the photos are, then what was done with them
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedWidth(1)
        layout.addWidget(divider)
        for combo in (self.flag, self.rating, self.edit, self.sharpness, self.keyword):
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
        self.save_button = QPushButton("Save…")
        self.save_button.setObjectName("row")
        self.save_button.setToolTip("Save these filters as a smart collection")
        self.save_button.clicked.connect(self.save_requested)
        layout.addWidget(self.save_button)
        # As tall and as round as the toolbar's cards and search field, whatever the font
        for combo in self._combos():
            combo.setObjectName("row")
        style.follow_density(self, lambda: self._fit(layout))
        self._update_clear()

    def _fit(self, layout):
        layout.setContentsMargins(0, 0, 0, style.gap(8))
        layout.setSpacing(style.gap(8))
        for widget in (*self._combos(), self.clear_button, self.save_button):
            widget.setFixedHeight(style.row_height())

    def _combos(self):
        return (
            self.year,
            self.camera,
            self.lens,
            self.flag,
            self.rating,
            self.edit,
            self.sharpness,
            self.keyword,
        )

    def _update_clear(self):
        active = any(combo.currentIndex() for combo in self._combos())
        self.clear_button.setEnabled(active)
        self.save_button.setEnabled(active or self._searching)

    def set_searching(self, on):
        """Whether a search is on, which a smart collection can save too."""
        self._searching = on
        self._update_clear()

    def clear(self):
        """Every filter back to all, each signal once for what changed."""
        before = self.values()
        changed = [
            combo
            for combo in (self.flag, self.rating, self.edit, self.sharpness, self.keyword)
            if combo.currentIndex()
        ]
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
            self.sharpness: self.sharpness_changed,
            self.keyword: self.keyword_changed,
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

    def keyword_value(self):
        """A keyword path, or None for any."""
        return self.keyword.currentData()

    def set_keywords(self, paths):
        """The keywords to choose from, as paths by name; a choice still there stays
        chosen. Signals `keyword_changed` when the chosen one went."""
        current = self.keyword.currentData()
        self.keyword.blockSignals(True)
        while self.keyword.count() > 1:
            self.keyword.removeItem(1)
        for path in paths:
            depth = len(keywordm.levels(path)) - 1
            self.keyword.addItem("\u2003" * depth + keywordm.leaf(path), path)
            self.keyword.setItemData(self.keyword.count() - 1, path, Qt.ItemDataRole.ToolTipRole)
        index = self.keyword.findData(current) if current is not None else 0
        self.keyword.setCurrentIndex(max(index, 0))
        self.keyword.blockSignals(False)
        self._update_clear()
        if self.keyword.currentData() != current:
            self.keyword_changed.emit(self.keyword.currentData())

    def set_keyword(self, path):
        """Choose keyword `path` (None for any); signals `keyword_changed` if that is a
        change. A keyword that is not in the list is not chosen."""
        index = self.keyword.findData(path) if path is not None else 0
        self.keyword.setCurrentIndex(max(index, 0))

    def apply(self, values):
        """Choose year, camera, lens, flag, rating, edit state and keyword from the dict
        `values` (keys "year", "camera", "lens", "flag", "rating", "edit", "keyword";
        a missing one is any); a choice that is not there is any. Signals once for the
        year, camera and lens together."""
        before = self.values()
        for name in ("year", "camera", "lens"):
            combo = getattr(self, name)
            combo.blockSignals(True)
            combo.setCurrentIndex(max(combo.findData(values.get(name)), 0))
            combo.blockSignals(False)
        for name in ("flag", "rating", "edit", "sharpness", "keyword"):
            combo = getattr(self, name)
            combo.setCurrentIndex(max(combo.findData(values.get(name)), 0))
        self._update_clear()
        if self.values() != before:
            self.changed.emit()

    def edit_value(self):
        """EDITED or UNEDITED to show only those, or None for all."""
        return self.edit.currentData()

    def sharpness_value(self):
        """BLURRY to show only the blurry photos of stacks, or None for all."""
        return self.sharpness.currentData()

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
