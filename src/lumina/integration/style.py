"""Maps the shell's theme to Qt: palette, font, sizes, icons and a stylesheet.

A port of `../filehouse`'s `integration/style.py`, cut to what Lumina uses, so the
two apps share one look: the same window layers, cards, corner radii, hover and
selection colours. Colours and fonts go through `QPalette` and `QFont`; the
stylesheet is for shapes and takes every colour, radius and size from here.
"""

from PySide6.QtCore import QObject, QSize, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette

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

        QComboBox {{
            border: 1px solid {divider}; border-radius: {medium}px;
            padding: {px(4)}px {px(10)}px; background: {raised};
        }}
        QComboBox:hover {{ border-color: {overlay(0.2)}; }}
        QComboBox:focus {{ border-color: {colors["accent"]}; }}
        QComboBox QAbstractItemView {{
            background: {card}; border: 1px solid {divider}; border-radius: {medium}px;
            selection-background-color: {selected}; selection-color: {colors["textPrimary"]};
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
