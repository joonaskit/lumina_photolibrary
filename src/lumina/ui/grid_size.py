"""Small, medium or large thumbnails: three buttons above the grid.

Each button's icon is a grid of tiles, three by three for small down to one for
large, drawn in the theme's text colour and drawn again when the theme changes.
"""

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QToolButton, QWidget

from lumina.integration import style

# Name, tooltip, tiles along a side of the icon
SIZES = (
    ("small", "Small thumbnails", 3),
    ("medium", "Medium thumbnails", 2),
    ("large", "Large thumbnails", 1),
)
NAMES = tuple(name for name, _tip, _tiles in SIZES)
_ICON = 16


class GridSizeSwitch(QWidget):
    # The name of the size chosen
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.group = QButtonGroup(self)
        self.buttons = {}
        for name, tip, _tiles in SIZES:
            button = QToolButton()
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setIconSize(QSize(style.px(_ICON), style.px(_ICON)))
            self.group.addButton(button)
            self.buttons[name] = button
            layout.addWidget(button)
        self.group.buttonClicked.connect(lambda button: self.changed.emit(self.value()))
        style.follow_density(self, lambda: self._fit(layout))
        self.draw_icons()
        style.themed.changed.connect(self.draw_icons)

    def _fit(self, layout):
        layout.setSpacing(style.gap(2))
        side = style.row_height() - style.px(4)
        for button in self.buttons.values():
            button.setFixedSize(side, side)

    def value(self):
        return next((name for name, b in self.buttons.items() if b.isChecked()), "medium")

    def set_value(self, name):
        """Show `name` as chosen, without signalling."""
        self.buttons.get(name, self.buttons["medium"]).setChecked(True)

    def draw_icons(self):
        for name, _tip, tiles in SIZES:
            self.buttons[name].setIcon(_tiles_icon(tiles, self.devicePixelRatioF()))


def _tiles_icon(tiles, ratio):
    side = style.px(_ICON)
    pixmap = QPixmap(round(side * ratio), round(side * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(style.color("textSecondary"))
    gap = side * 0.14
    tile = (side - gap * (tiles - 1)) / tiles
    radius = max(1.0, tile * 0.2)
    for row in range(tiles):
        for column in range(tiles):
            rect = QRectF(column * (tile + gap), row * (tile + gap), tile, tile)
            painter.drawRoundedRect(rect, radius, radius)
    painter.end()
    return QIcon(pixmap)
