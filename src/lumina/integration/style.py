"""Maps the shell's theme to Qt: palette, font, sizes, icons and a stylesheet.

A port of `../filehouse`'s `integration/style.py`, cut to what Lumina uses, so the
two apps share one look: the same window layers, cards, corner radii, hover and
selection colours. Colours and fonts go through `QPalette` and `QFont`; the
stylesheet is for shapes and takes every colour, radius and size from here.
"""

import os

from PySide6.QtCore import QEvent, QObject, QPointF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)

from lumina import APP_ID
from lumina.integration import theme as theme_reader

BASE_FONT_PX = 13
# Height of a button in the toolbar's groups, and the space between a group's edge and
# its buttons: filehouse's "comfortable" density
CONTROL_HEIGHT = 36
GROUP_PADDING = 4

# The kinds of widget a platform theme may give a font of its own
_CLASS_FONTS = (
    "QAbstractItemView",
    "QListView",
    "QToolButton",
    "QPushButton",
    "QComboBox",
    "QStatusBar",
    "QLabel",
    "QLineEdit",
    "QMenu",
    "QMenuBar",
    "QMessageBox",
    "QTipLabel",
)

# The theme in use. Complete defaults until apply() runs, so sizes work in tests.
_theme = theme_reader.merge(theme_reader.DEFAULTS, {})
# Goes up each time a theme is applied
generation = 0


class _Themed(QObject):
    # A theme was applied while the app runs: whatever holds an icon or a colour
    # made from the old one has to make it again
    changed = Signal()


themed = _Themed()


# The popup windows the stylesheet rounds: a menu, and the list a combo box opens
_ROUNDED_POPUPS = ("QMenu", "QComboBoxPrivateContainer")


class _RoundedPopups(QObject):
    """Makes every menu and combo box list a window without a background of its own,
    so only its rounded shape is painted. Otherwise the popup's square window shows
    behind the corners. As filehouse's menus are made, but for all of them, those of
    Qt's own dialogs and text fields too: done as each is polished, before its window
    is made, which is when it has to be."""

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Polish and _is_popup(watched):
            round_popup(watched)
        return False


def _is_popup(widget):
    # inherits() knows the C++ classes, the combo box's private container among them
    return isinstance(widget, QObject) and any(widget.inherits(n) for n in _ROUNDED_POPUPS)


def round_popup(widget):
    """Let `widget`'s rounded stylesheet corners show: before its window is made."""
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    widget.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


_rounder = None


def px(size):
    """An unscaled pixel size at the user's UI scale."""
    return theme_reader.scaled(_theme, size)


def font_px(size):
    """An unscaled font size in pixels at the user's font and UI scale."""
    return theme_reader.font_scaled(_theme, size)


def color(name):
    return QColor(_theme["colors"][name])


# The window's layers, as the shell's own windows are built: the window is the theme's
# `surface`, not its `background`, which is pitch black in the dark scheme. What lies on
# the window is that colour with a little of the text colour mixed in, so the light
# scheme follows by itself.
_LAYERS = {"window": 0.0, "card": 0.04, "raised": 0.10}
# The hairline around a card: it is what sets a card apart from the window
_CARD_BORDER = 0.06
_DIVIDER = 0.10


def layer(name):
    """The colour of one of the window's layers: "window", the "card"s on it (toolbar
    groups, menus) or the "raised" controls on a card."""
    base, text, share = color("surface"), color("textPrimary"), _LAYERS[name]
    return QColor(
        round(base.red() + (text.red() - base.red()) * share),
        round(base.green() + (text.green() - base.green()) * share),
        round(base.blue() + (text.blue() - base.blue()) * share),
    )


def palette_color(name):
    """One of the theme's eight decoration colours: "green", "blue", "orange", …"""
    return QColor(_theme["colors"]["palette"][name])


def radius(name):
    """`small`, `medium` or `large`, in pixels. Follows the shell's corner roundness."""
    return px(_theme["radius"][name])


def radius_within(name, height):
    """A radius for something `height` unscaled pixels tall: never more than half of it,
    since a stylesheet draws square corners when the radius doesn't fit."""
    return min(radius(name), px(height) // 2)


def overlay(alpha):
    """The text colour at `alpha`: for hover, dividers and the like.
    From the text colour, not white, so it works on a light background too."""
    return _rgba(color("textPrimary"), alpha)


def tint(name, alpha):
    """A theme colour at `alpha`, for a stylesheet."""
    return _rgba(color(name), alpha)


def _rgba(colour, alpha):
    return f"rgba({colour.red()}, {colour.green()}, {colour.blue()}, {alpha})"


def apply(app, theme, live=False):
    """Style the whole application. Call before creating any window.

    With `live`, the app is already running with an earlier theme: colours and corner
    radii change at once, but the scales and the fonts stay as they are, because sizes
    and fonts are fixed in the widgets when they are made. Returns whether anything
    changed; `themed.changed` is emitted when it did."""
    global _theme, generation
    if live:
        theme = {**theme, "scale": _theme["scale"], "fonts": _theme["fonts"]}
        if theme == _theme:
            return False
    _theme = theme
    generation += 1
    app.setPalette(_palette())
    font = _font()
    app.setFont(font)
    app.setStyleSheet(_stylesheet())
    global _rounder
    if _rounder is None:
        _rounder = _RoundedPopups(app)
        app.installEventFilter(_rounder)
    # The platform theme gives some kinds of widget a font of its own (Plasma's system
    # font, in points), which would win over the app's font: they are told to use the
    # shell's too. After the style sheet, which makes Qt read the platform's again
    for name in _CLASS_FONTS:
        app.setFont(font, name)
    if live:
        themed.changed.emit()
    return True


# Icons: drawn once as ordinary pixmaps in one colour, as filehouse does

_ICON_SIZES = (16, 20, 24, 32)


def _theme_icon(names):
    for name in names:
        if QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
    return QIcon()


def _tinted(source, side, colour):
    """`source` at `side` pixels with every pixel in `colour`, keeping its shape."""
    pixmap = source.pixmap(QSize(side, side))
    if pixmap.isNull():
        return pixmap
    painter = QPainter(pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pixmap.rect(), colour)
    painter.end()
    return pixmap


def icon(*names, colour="textPrimary"):
    """A one-colour icon: the first of `names` the icon theme has, in a theme colour."""
    source = _theme_icon(names)
    made = QIcon()
    for mode, shade in (
        (QIcon.Mode.Normal, color(colour)),
        (QIcon.Mode.Disabled, color("textTertiary")),
    ):
        for side in _ICON_SIZES:
            pixmap = _tinted(source, px(side), shade)
            if not pixmap.isNull():
                made.addPixmap(pixmap, mode)
    return made


def app_icon():
    """The app's own icon where one is installed under its name, or the icon theme's."""
    for name in (APP_ID, "camera-photo", "image-x-generic"):
        if QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
    return QIcon()


# Palette, font, stylesheet


def _palette():
    palette = QPalette()
    roles = {
        QPalette.ColorRole.WindowText: "textPrimary",
        QPalette.ColorRole.Text: "textPrimary",
        QPalette.ColorRole.ButtonText: "textPrimary",
        QPalette.ColorRole.ToolTipText: "textPrimary",
        QPalette.ColorRole.PlaceholderText: "textSecondary",
        QPalette.ColorRole.Highlight: "accent",
        QPalette.ColorRole.HighlightedText: "onAccent",
        QPalette.ColorRole.Link: "accent",
    }
    for role, name in roles.items():
        palette.setColor(role, color(name))
    layers = {
        QPalette.ColorRole.Window: "window",
        QPalette.ColorRole.Base: "window",
        QPalette.ColorRole.AlternateBase: "card",
        QPalette.ColorRole.Button: "raised",
        QPalette.ColorRole.ToolTipBase: "raised",
    }
    for role, name in layers.items():
        palette.setColor(role, layer(name))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, color("textTertiary"))
    return palette


def _font():
    installed = set(QFontDatabase.families())
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    for family in _theme["fonts"]["families"]:
        if family in installed:
            font = QFont(family)
            break
    font.setPixelSize(theme_reader.font_scaled(_theme, BASE_FONT_PX))
    return font


# The combo box's chevron and the room it has, unscaled
_ARROW = 10
_ARROW_BOX = 16


def _arrow_image():
    """`image: url(...);` for a chevron in the secondary text colour, drawn for this
    theme into Lumina's cache folder: a stylesheet can only show an image from a file.
    Nothing when it can't be written: the style's own arrow is shown then."""
    colour = color("textSecondary")
    # Drawn at twice the size, for dense screens
    side = px(_ARROW) * 2
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    folder = os.path.join(base, "lumina", "style")
    path = os.path.join(folder, f"chevron-{colour.name()[1:]}-{side}.png")
    if not os.path.exists(path):
        pixmap = QPixmap(side, side)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(colour, side * 0.14)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        chevron = QPainterPath(QPointF(side * 0.2, side * 0.38))
        chevron.lineTo(side * 0.5, side * 0.66)
        chevron.lineTo(side * 0.8, side * 0.38)
        painter.drawPath(chevron)
        painter.end()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            return ""
        partial = f"{path}.{os.getpid()}.part"
        if not pixmap.save(partial, "PNG"):
            return ""
        os.replace(partial, path)
    return f"image: url({path});"


def _stylesheet():
    colors = _theme["colors"]
    small, medium, large = radius("small"), radius("medium"), radius("large")
    hover, pressed, divider = overlay(0.08), overlay(0.14), overlay(_DIVIDER)
    selected = tint("accent", 0.22)
    window, card, raised = (layer(name).name() for name in ("window", "card", "raised"))
    card_border = overlay(_CARD_BORDER)
    small_font = theme_reader.font_scaled(_theme, 11)
    control, group = CONTROL_HEIGHT, GROUP_PADDING
    return f"""
        QToolBar {{
            border: none; spacing: {px(8)}px; padding: {px(8)}px;
            background: {window};
        }}

        /* The rounded groups in the toolbar and the cards */
        #group, #card {{
            background: {card}; border: 1px solid {card_border}; border-radius: {large}px;
        }}
        #group {{ border-radius: {radius_within("large", control + 2 * group)}px; }}
        #group QToolButton {{
            border-radius: {radius_within("medium", control)}px; padding: {px(7)}px;
        }}

        QToolButton {{
            border: none; border-radius: {small}px; padding: {px(5)}px;
            background: transparent;
        }}
        QToolButton:hover {{ background: {hover}; }}
        QToolButton:pressed {{ background: {pressed}; }}
        QToolButton:checked {{ background: {raised}; }}

        QPushButton {{
            border: none; border-radius: {radius_within("large", 40)}px;
            padding: {px(8)}px {px(16)}px; background: {raised};
        }}
        QPushButton:hover {{ background: {hover}; }}
        QPushButton:pressed {{ background: {pressed}; }}

        QPushButton:checked {{ background: {selected}; }}
        QPushButton:disabled {{ color: {colors["textTertiary"]}; }}
        /* A button that reads as a link: "Reset Curve" */
        QPushButton#link {{
            background: transparent; padding: {px(2)}px {px(4)}px; color: {colors["accent"]};
        }}
        QPushButton#link:hover {{ background: {hover}; }}

        /* RGB / R / G / B: one row of buttons, one of them on */
        QToolButton#segment {{
            border-radius: {small}px; padding: {px(4)}px; background: {raised};
        }}
        QToolButton#segment:hover {{ background: {hover}; }}
        QToolButton#segment:checked {{ background: {selected}; }}

        QSlider {{ min-height: {px(20)}px; }}
        QSlider::groove:horizontal {{
            height: {px(4)}px; border-radius: {px(2)}px; background: {raised};
        }}
        QSlider::sub-page:horizontal {{
            height: {px(4)}px; border-radius: {px(2)}px; background: {colors["accent"]};
        }}
        QSlider[bipolar="true"]::sub-page:horizontal {{ background: {raised}; }}
        QSlider::handle:horizontal {{
            width: {px(14)}px; height: {px(14)}px; margin: -{px(5)}px 0;
            border-radius: {px(7)}px; background: {colors["textPrimary"]};
        }}
        QSlider::handle:horizontal:hover {{ background: {colors["accent"]}; }}

        QSpinBox {{
            border: 1px solid {divider}; border-radius: {medium}px;
            padding: {px(4)}px {px(8)}px; background: {raised};
        }}
        QSpinBox:focus {{ border-color: {colors["accent"]}; }}
        QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}

        QScrollArea#developScroll, QScrollArea#developScroll > QWidget > QWidget {{
            background: transparent;
        }}

        QLineEdit {{
            border: 1px solid {divider}; border-radius: {medium}px;
            padding: {px(4)}px {px(8)}px; background: {raised};
        }}
        QLineEdit:focus {{ border-color: {colors["accent"]}; }}

        QListView {{ border: none; outline: none; background: transparent; }}
        QListView::item {{ border-radius: {medium}px; padding: {px(4)}px; }}
        QListView::item:hover {{ background: {hover}; }}
        QListView::item:selected {{ background: {selected}; color: {colors["textPrimary"]}; }}

        /* The grid and the viewer have a border around them, as the cards beside them */
        #pane {{
            border: 1px solid {card_border}; border-radius: {large}px; padding: {px(2)}px;
        }}

        /* The library's folders, as filehouse's sidebar */
        #sidebar {{
            padding: {px(8)}px; background: {card}; border: 1px solid {card_border};
            border-radius: {large}px;
        }}
        #sidebar QListWidget {{ border: none; outline: none; background: transparent; }}
        #sidebar QListWidget::item {{
            padding: {px(6)}px {px(8)}px; border-radius: {medium}px;
        }}
        #sidebar QListWidget::item:hover {{ background: {hover}; }}
        #sidebar QListWidget::item:selected {{
            background: {selected}; color: {colors["textPrimary"]};
        }}
        #heading {{
            color: {colors["textSecondary"]}; font-size: {small_font}px; font-weight: 600;
            padding: {px(4)}px {px(8)}px;
        }}

        /* The arrow sits inside the rounded box, in the padding, not in a square
           button of the platform style's own */
        QComboBox {{
            border: 1px solid {divider}; border-radius: {medium}px;
            padding: {px(4)}px {px(10)}px; padding-right: {px(10 + _ARROW_BOX)}px;
            background: {raised};
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding; subcontrol-position: center right;
            width: {px(_ARROW_BOX)}px; border: none; background: transparent;
            margin-right: {px(6)}px;
        }}
        QComboBox::down-arrow {{ {_arrow_image()} width: {px(_ARROW)}px; height: {px(_ARROW)}px; }}
        QComboBox:hover {{ border-color: {overlay(0.2)}; }}
        QComboBox:focus {{ border-color: {colors["accent"]}; }}
        /* The list's window: transparent, the list inside it is the rounded card */
        QComboBoxPrivateContainer {{ background: transparent; border: none; }}
        /* A list under the box, not the platform style's menu over it, which the
           stylesheet can't round: the same card as a menu */
        QComboBox {{ combobox-popup: 0; }}
        QComboBox QAbstractItemView {{
            background: {card}; border: 1px solid {divider}; border-radius: {large}px;
            padding: {px(6)}px; outline: none;
            selection-background-color: {hover}; selection-color: {colors["textPrimary"]};
        }}
        QComboBox QAbstractItemView::item {{
            padding: {px(6)}px {px(10)}px; border-radius: {medium}px; min-height: {px(20)}px;
        }}
        QComboBox QAbstractItemView::item:selected, QComboBox QAbstractItemView::item:hover {{
            background: {hover}; color: {colors["textPrimary"]};
        }}

        #title {{ font-size: {theme_reader.font_scaled(_theme, 15)}px; font-weight: 600; }}
        #secondary {{ color: {colors["textSecondary"]}; font-size: {small_font}px; }}
        #placeholder {{ color: {colors["textSecondary"]}; }}

        QMenu {{
            background: {card}; border: 1px solid {divider};
            border-radius: {large}px; padding: {px(6)}px;
        }}
        QMenu::item {{
            padding: {px(8)}px {px(14)}px; border-radius: {medium}px; margin: 0 {px(2)}px;
        }}
        QMenu::item:selected {{ background: {hover}; color: {colors["textPrimary"]}; }}
        QMenu::item:disabled {{ color: {colors["textTertiary"]}; }}
        QMenu::separator {{ height: 1px; background: {divider}; margin: {px(6)}px {px(8)}px; }}

        QScrollBar:vertical {{ background: transparent; width: {px(10)}px; margin: {px(2)}px; }}
        QScrollBar:horizontal {{ background: transparent; height: {px(10)}px; margin: {px(2)}px; }}
        QScrollBar::handle {{ background: {pressed}; border-radius: {px(3)}px; }}
        QScrollBar::handle:vertical {{ min-height: {px(28)}px; }}
        QScrollBar::handle:horizontal {{ min-width: {px(28)}px; }}
        QScrollBar::handle:hover {{ background: {overlay(0.3)}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

        QStatusBar, QStatusBar QLabel {{ color: {colors["textSecondary"]}; }}
        QStatusBar QLabel {{ padding: 0 {px(8)}px; }}
        QStatusBar::item {{ border: none; }}
        QProgressBar {{
            border: none; border-radius: {px(3)}px; background: {raised};
        }}
        QProgressBar::chunk {{ border-radius: {px(3)}px; background: {colors["accent"]}; }}
    """
