"""Year, camera and lens, above the grid, and the search field at the right."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QSizePolicy, QWidget

from lumina.integration import style


class FilterBar(QWidget):
    # Something was chosen: read `values()`
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, style.px(8))
        layout.setSpacing(style.px(8))
        self.year = self._combo("All years")
        self.camera = self._combo("All cameras")
        self.lens = self._combo("All lenses")
        for combo in (self.year, self.camera, self.lens):
            layout.addWidget(combo)
        layout.addStretch(1)

    def add_trailing(self, widget):
        """A widget at the right end of the row: the search field."""
        self.layout().addWidget(widget)

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
