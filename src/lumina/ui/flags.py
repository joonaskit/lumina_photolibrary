"""Thumbs up and thumbs down: their icons, the badge on a thumbnail, and the button
above the grid with what to do with thumbs-down photos.

The icon themes have no thumbs, so they are drawn here from one outline of our own,
turned over for thumbs down, in the theme's colours and drawn again when it changes.
"""

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QMenu, QToolButton

from lumina.integration import style
from lumina.qt_models.library_model import DIM, HIDE, PICKED, REJECTED, SHOW

# A thumb on a 24 pixel square: the cuff, and the hand with the thumb up
_THUMB = (
    '<rect x="2.5" y="10" width="4" height="11" rx="1"/>'
    '<path d="M9 10 L12.6 3.2 C13.9 3.1 15.2 4.3 14.8 5.9 L14 9.5 H19.2 '
    'C20.5 9.5 21.4 10.8 21 12 L18.8 19.6 C18.5 20.5 17.7 21 16.8 21 H9 Z"/>'
)
_SIZES = (16, 20, 24, 32)

# The theme colour of each flag, on its badge and on its button while it is set
COLOURS = {PICKED: "success", REJECTED: "danger"}

REJECTED_CHOICES = (
    (SHOW, "Show Thumbs-Down Photos"),
    (DIM, "Dim Thumbs-Down Photos"),
    (HIDE, "Hide Thumbs-Down Photos"),
)


def _svg(flag, colour, filled):
    turn = ' transform="rotate(180 12 12)"' if flag == REJECTED else ""
    fill = colour if filled else "none"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        f'<g fill="{fill}" stroke="{colour}" stroke-width="1.8" stroke-linejoin="round"{turn}>'
        f"{_THUMB}</g></svg>"
    )


def paint_thumb(painter, rect, flag, colour, filled=False):
    """A thumb up (PICKED) or down (REJECTED) in `rect`, in a QColor."""
    renderer = QSvgRenderer(QByteArray(_svg(flag, colour.name(), filled).encode()))
    renderer.render(painter, QRectF(rect))


def _pixmap(flag, colour, side, ratio, filled):
    pixmap = QPixmap(round(side * ratio), round(side * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    paint_thumb(painter, QRectF(0, 0, side, side), flag, colour, filled)
    painter.end()
    return pixmap


def icon(flag, ratio=1.0):
    """The icon of a flag's button: an outline, and filled in its colour while set."""
    made = QIcon()
    for side in _SIZES:
        side = style.px(side)
        made.addPixmap(
            _pixmap(flag, style.color("textPrimary"), side, ratio, False),
            QIcon.Mode.Normal,
            QIcon.State.Off,
        )
        made.addPixmap(
            _pixmap(flag, style.color(COLOURS[flag]), side, ratio, True),
            QIcon.Mode.Normal,
            QIcon.State.On,
        )
        made.addPixmap(
            _pixmap(flag, style.color("textTertiary"), side, ratio, False),
            QIcon.Mode.Disabled,
            QIcon.State.Off,
        )
    return made


def paint_badge(painter, photo_rect, flag):
    """A flag's badge on the bottom right corner of a thumbnail: a filled thumb in its
    colour on a disc of the window's card colour, so it shows on any photo."""
    side = style.px(22)
    inset = style.px(4)
    if photo_rect.width() < side + 2 * inset or photo_rect.height() < side + 2 * inset:
        return
    disc = QRectF(photo_rect.right() - inset - side, photo_rect.bottom() - inset - side, side, side)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    background = style.layer("card")
    background.setAlphaF(0.9)
    painter.setBrush(background)
    painter.drawEllipse(disc)
    pad = side * 0.2
    paint_thumb(
        painter, disc.adjusted(pad, pad, -pad, -pad), flag, style.color(COLOURS[flag]), True
    )
    painter.restore()


class FlagOptions(QToolButton):
    """A button with a menu: show, dim or hide thumbs-down photos."""

    rejected_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("segment")
        self.setToolTip("Thumbs-down photos")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setFixedSize(style.px(style.CONTROL_HEIGHT - 4), style.px(style.CONTROL_HEIGHT - 4))
        self.setIconSize(QSize(style.px(16), style.px(16)))
        menu = QMenu(self)
        group = QActionGroup(self)
        self.rejected_actions = {}
        for mode, text in REJECTED_CHOICES:
            action = QAction(text, self)
            action.setCheckable(True)
            action.triggered.connect(lambda _on, mode=mode: self.rejected_changed.emit(mode))
            group.addAction(action)
            menu.addAction(action)
            self.rejected_actions[mode] = action
        self.setMenu(menu)
        style.round_popup(menu)
        self.draw_icons()
        style.themed.changed.connect(self.draw_icons)

    def set_rejected(self, rejected):
        """Show what is chosen, without signalling."""
        self.rejected_actions.get(rejected, self.rejected_actions[DIM]).setChecked(True)

    def rejected(self):
        return next(mode for mode, action in self.rejected_actions.items() if action.isChecked())

    def draw_icons(self):
        pixmap = _pixmap(
            REJECTED, style.color("textSecondary"), style.px(16), self.devicePixelRatioF(), False
        )
        self.setIcon(QIcon(pixmap))
