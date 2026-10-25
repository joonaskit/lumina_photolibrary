"""Maps the shell's theme to Qt: palette, font, sizes, icons and a stylesheet.

A port of `../filehouse`'s `integration/style.py`, cut to what Lumina uses, so the
two apps share one look: the same window layers, cards, corner radii, hover and
selection colours. Colours and fonts go through `QPalette` and `QFont`; the
stylesheet is for shapes and takes every colour, radius and size from here.
"""

import os

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPointF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
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

# How much room the interface takes: the padding and spacing of the menus, buttons, lists
# and the Develop panel, and the height of the toolbar's and the filters' row. Not the
# fonts (the theme's font scale), the icons or the thumbnails. "comfortable" is
# filehouse's density. `factor` scales what is made with `gap()`; the heights are their
# own, as a control needs room for its icon. Unscaled pixels, like every size here
DENSITIES = ("comfortable", "compact")
_DENSITY_SIZES = {
    "comfortable": {"factor": 1.0, "row_height": 36, "group_padding": 4},
    "compact": {"factor": 0.7, "row_height": 30, "group_padding": 3},
}
_density = "comfortable"


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
    # The density changed while the app runs: whatever fixed a size from it has to fix
    # it again (see `follow_density`)
    density_changed = Signal()


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


def density():
    """The name of the density in use."""
    return _density


def set_density(name, app=None):
    """Use a density from now on. Before apply() at the start; with `app` while it runs:
    the stylesheet is made again and `themed.density_changed` is emitted, for what fixed
    a size when it was made. Returns whether it changed."""
    global _density
    name = name if name in _DENSITY_SIZES else "comfortable"
    if name == _density:
        return False
    _density = name
    if app is not None:
        app.setStyleSheet(_stylesheet())
    themed.density_changed.emit()
    return True


def gap(size):
    """An unscaled padding, margin or spacing at the user's UI scale and the density.
    What sits inside a control or between controls, not the control's own size."""
    scaled = px(size) * _DENSITY_SIZES[_density]["factor"]
    return max(1, round(scaled)) if size else 0


def row_height():
    """The height of everything in the toolbar and the filter row, from edge to edge: a
    group's card, the search field, the combos and the buttons beside them. Scaled."""
    return px(_DENSITY_SIZES[_density]["row_height"])


def segment_padding():
    """The space between a segmented choice's well and its buttons (the settings'
    Dark or Light). Scaled."""
    return px(3)


def group_padding():
    """The space between a toolbar group's edge and its buttons. Scaled."""
    return px(_DENSITY_SIZES[_density]["group_padding"])


def row_inner_height():
    """The height of a button inside a group's card: less its padding and its 1 pixel
    border on each side. Scaled."""
    return row_height() - 2 * group_padding() - 2


class _Follower(QObject):
    def __init__(self, owner, apply):
        super().__init__(owner)
        self._apply = apply
        themed.density_changed.connect(self.run)

    def run(self):
        if not shiboken6.isValid(self):
            # The owner is gone and took this with it, but not the connection: what
            # `apply` holds may be gone too
            themed.density_changed.disconnect(self.run)
            return
        self._apply()


def follow_density(owner, apply, now=True):
    """Call `apply()` now (unless `now` is False) and again whenever the density
    changes, for as long as the QObject `owner` lives. `apply` sets the sizes that come
    from `gap()`, `row_height()` and the like, which are fixed in a widget when it is
    made."""
    if now:
        apply()
    _Follower(owner, apply)


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


def _radius_for_text(name, padding, border=0):
    """`small`, `medium` or `large` for a control as tall as a line of text, its
    `padding` (unscaled, above and below) and its border, as long as it fits: in the
    compact density, with a small font or with the shell's rounder corners the radius
    is more than half the control's height, and a radius that does not fit is drawn
    square."""
    height = QFontMetrics(_font()).height() + 2 * (gap(padding) + border)
    return min(radius(name), height // 2)


def overlay(alpha):
    """The text colour at `alpha`: for hover, dividers and the like.
    From the text colour, not white, so it works on a light background too."""
    return _rgba(color("textPrimary"), alpha)


def deeper(name, share):
    """A theme colour taken `share` of the way to black: a deep fill under light text."""
    colour = color(name)
    return QColor(*(round(part * (1 - share)) for part in colour.getRgb()[:3])).name()


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
    """The first of `names` the icon theme has, each tried first as its `-symbolic`
    version, as filehouse asks for them: a full-colour icon made one colour can lose its
    shape (a gear drawn as a flower)."""
    for name in names:
        for candidate in (f"{name}-symbolic", name):
            if QIcon.hasThemeIcon(candidate):
                return QIcon.fromTheme(candidate)
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
    # A slider's handle and track follow the roundness too: a circle and a pill at the
    # shell's default, squarer as its corners are
    handle_radius, track_radius = radius_within("medium", 14), radius_within("small", 4)
    # A combo box, a field or a spin box: a line of text, its padding and a border
    boxed = _radius_for_text("medium", 4, border=1)
    hover, pressed, divider = overlay(0.08), overlay(0.14), overlay(_DIVIDER)
    selected = tint("accent", 0.22)
    shutter_border = px(2)
    segment = _radius_for_text("medium", 5)
    shutter_radius = _radius_for_text("medium", 10, border=shutter_border)
    window, card, raised = (layer(name).name() for name in ("window", "card", "raised"))
    card_border = overlay(_CARD_BORDER)
    small_font = theme_reader.font_scaled(_theme, 11)
    row = _DENSITY_SIZES[_density]["row_height"]
    return f"""
        QToolBar {{
            border: none; spacing: {gap(8)}px; padding: {gap(8)}px;
            background: {window};
        }}

        /* The rounded groups in the toolbar and the cards */
        #group, #card {{
            background: {card}; border: 1px solid {card_border}; border-radius: {large}px;
        }}
        #group {{ border-radius: {radius_within("large", row)}px; }}
        /* As tall as row_inner_height(), less the group's border too: a radius made for
           a button 2 pixels taller was more than half of it in the compact density, and
           the hover was drawn square */
        #group QToolButton {{
            border-radius: {min(radius("medium"), row_inner_height() // 2)}px;
            padding: {gap(3)}px;
        }}

        QToolButton {{
            border: none; border-radius: {small}px; padding: {gap(5)}px;
            background: transparent;
        }}
        QToolButton:hover {{ background: {hover}; }}
        QToolButton:pressed {{ background: {pressed}; }}
        QToolButton:checked {{ background: {raised}; }}

        QPushButton {{
            border: none; border-radius: {_radius_for_text("medium", 8)}px;
            padding: {gap(8)}px {gap(16)}px; background: {raised};
        }}
        QPushButton:hover {{ background: {hover}; }}
        QPushButton:pressed {{ background: {pressed}; }}
        /* A button in the toolbar or the filter row: row_height() tall, so a radius made
           for a taller button would not fit and the corners would be drawn square */
        QPushButton#row {{
            border-radius: {radius_within("large", row)}px; padding: 0 {gap(16)}px;
        }}

        QPushButton:checked {{ background: {selected}; }}
        QPushButton:disabled {{ color: {colors["textTertiary"]}; }}
        /* The camera bar's Capture, a deep red, and Focus beside it, ringed in the
           accent: larger than a button, the two the eye finds first */
        QPushButton#shutter, QPushButton#focus {{
            border: {shutter_border}px solid {colors["danger"]};
            border-radius: {shutter_radius}px; padding: {gap(10)}px {gap(28)}px;
        }}
        QPushButton#shutter {{
            background: {deeper("danger", 0.45)}; color: {colors["onAccent"]};
            font-weight: 600;
        }}
        QPushButton#shutter:hover {{ background: {deeper("danger", 0.35)}; }}
        QPushButton#shutter:pressed {{ background: {deeper("danger", 0.55)}; }}
        QPushButton#focus {{ border-color: {colors["accent"]}; }}
        /* An ordinary disabled button, as large; a solid border, as a translucent one
           shows seams where it overlaps itself at the corners */
        QPushButton#shutter:disabled, QPushButton#focus:disabled {{
            background: {raised}; border-color: {raised}; color: {colors["textTertiary"]};
        }}

        /* One of a few choices, as the shell has them: buttons in a rounded well, the
           one chosen raised. The well's corners are its buttons' and the padding, so
           the two are round about one centre */
        #segments {{
            background: {overlay(0.05)}; border-radius: {segment + segment_padding()}px;
        }}
        #segments QPushButton {{
            background: transparent; border-radius: {segment}px;
            padding: {gap(5)}px {gap(12)}px;
        }}
        #segments QPushButton:hover {{ background: {hover}; }}
        #segments QPushButton:checked {{ background: {overlay(0.12)}; font-weight: 600; }}

        /* A button that reads as a link: "Reset Curve" */
        QPushButton#link {{
            background: transparent; padding: {gap(2)}px {gap(4)}px; color: {colors["accent"]};
        }}
        QPushButton#link:hover {{ background: {hover}; }}

        /* RGB / R / G / B: one row of buttons, one of them on */
        QToolButton#segment {{
            border-radius: {small}px; padding: {gap(4)}px; background: {raised};
        }}
        QToolButton#segment:hover {{ background: {hover}; }}
        QToolButton#segment:checked {{ background: {selected}; }}
        /* A segment with a colour of its own shows it chosen by filling with it (drawn
           by the button), not with the selection colour */
        QToolButton#segment[tinted="true"]:checked {{ background: {raised}; }}
        /* A segment with a menu: its icon says so, no arrow on top of it */
        QToolButton#segment::menu-indicator {{ image: none; width: 0; }}

        /* "Like IMG_1234.CR3" in front of the search field: click to end it */
        QToolButton#chip {{
            border-radius: {medium}px; padding: {gap(4)}px {gap(10)}px; background: {selected};
        }}
        QToolButton#chip:hover {{ background: {hover}; }}

        /* Not shorter than the handle, which the density does not change: in the compact
           density gap(20) was, and the handle's top was cut off */
        QSlider {{ min-height: {max(gap(20), px(14) + px(3))}px; }}
        QSlider::groove:horizontal {{
            height: {px(4)}px; border-radius: {track_radius}px; background: {raised};
        }}
        QSlider::sub-page:horizontal {{
            height: {px(4)}px; border-radius: {track_radius}px; background: {colors["accent"]};
        }}
        QSlider[bipolar="true"]::sub-page:horizontal {{ background: {raised}; }}
        QSlider::handle:horizontal {{
            width: {px(14)}px; height: {px(14)}px; margin: -{px(5)}px 0;
            border-radius: {handle_radius}px; background: {colors["textPrimary"]};
        }}
        QSlider::handle:horizontal:hover {{ background: {colors["accent"]}; }}

        QSpinBox {{
            border: 1px solid {divider}; border-radius: {boxed}px;
            padding: {gap(4)}px {gap(8)}px; background: {raised};
        }}
        QSpinBox:focus {{ border-color: {colors["accent"]}; }}
        QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}

        QScrollArea#developScroll, QScrollArea#developScroll > QWidget > QWidget {{
            background: transparent;
        }}

        QLineEdit {{
            border: 1px solid {divider}; border-radius: {boxed}px;
            padding: {gap(4)}px {gap(8)}px; background: {raised};
        }}
        QLineEdit:focus {{ border-color: {colors["accent"]}; }}
        /* The search field in the toolbar: rounded as the cards beside it */
        QLineEdit#row {{ border-radius: {radius_within("large", row)}px; }}

        QListView {{ border: none; outline: none; background: transparent; }}
        QListView::item {{ border-radius: {medium}px; padding: {gap(4)}px; }}
        QListView::item:hover {{ background: {hover}; }}
        QListView::item:selected {{ background: {selected}; color: {colors["textPrimary"]}; }}

        /* The grid and the viewer have a border around them, as the cards beside them */
        #pane {{
            border: 1px solid {card_border}; border-radius: {large}px; padding: {gap(2)}px;
        }}

        /* The library's folders, as filehouse's sidebar */
        #sidebar {{
            padding: {gap(8)}px; background: {card}; border: 1px solid {card_border};
            border-radius: {large}px;
        }}
        #sidebar QListWidget, #sidebar QTreeWidget {{
            border: none; outline: none; background: transparent;
        }}
        #sidebar QListWidget::item, #sidebar QTreeWidget::item {{
            padding: {gap(6)}px {gap(8)}px; border-radius: {medium}px;
        }}
        #sidebar QListWidget::item:hover, #sidebar QTreeWidget::item:hover {{
            background: {hover};
        }}
        #sidebar QListWidget::item:selected, #sidebar QTreeWidget::item:selected {{
            background: {selected}; color: {colors["textPrimary"]};
        }}
        /* Add Folder, Import, Tether Camera: as round as the folders above them, and
           row_height() tall, so no rounder than fits */
        #sidebar QToolButton {{ border-radius: {radius_within("medium", row)}px; }}
        /* The settings window's tabs: pills over a rounded card, as the menus' items */
        QTabWidget::pane {{
            border: 1px solid {card_border}; border-radius: {large}px; background: {card};
            top: -1px;
        }}
        QTabBar {{ background: transparent; }}
        QTabBar::tab {{
            padding: {gap(6)}px {gap(14)}px; margin: 0 {gap(2)}px {gap(6)}px 0;
            border-radius: {_radius_for_text("medium", 6)}px; background: transparent;
        }}
        QTabBar::tab:hover {{ background: {hover}; }}
        QTabBar::tab:selected {{ background: {selected}; color: {colors["textPrimary"]}; }}
        QListWidget#settingsList {{
            border: 1px solid {divider}; border-radius: {boxed}px; background: {raised};
            padding: {gap(4)}px; outline: none;
        }}
        QListWidget#settingsList::item {{
            padding: {gap(4)}px {gap(6)}px; border-radius: {medium}px;
        }}
        QListWidget#settingsList::item:hover {{ background: {hover}; }}
        QListWidget#settingsList::item:selected {{
            background: {selected}; color: {colors["textPrimary"]};
        }}
        #heading, #sectionHeader {{
            color: {colors["textSecondary"]}; font-size: {small_font}px; font-weight: 600;
            padding: {gap(4)}px {gap(8)}px;
        }}
        #sectionHeader {{ text-align: left; }}
        /* A key in the shortcuts' overlay */
        QLabel#keycap {{
            background: {raised}; border: 1px solid {divider}; border-radius: {small}px;
            padding: {gap(1)}px {gap(6)}px; font-size: {small_font}px;
        }}
        QScrollArea#shortcutsScroll, QScrollArea#shortcutsScroll > QWidget > QWidget {{
            background: transparent;
        }}
        /* A group's heading over its keys, in line with them */
        QScrollArea#shortcutsScroll #heading {{ padding: 0 0 {gap(2)}px 0; }}

        /* The arrow sits inside the rounded box, in the padding, not in a square
           button of the platform style's own */
        QComboBox {{
            border: 1px solid {divider}; border-radius: {boxed}px;
            padding: {gap(4)}px {gap(10)}px; padding-right: {gap(10) + px(_ARROW_BOX)}px;
            background: {raised};
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding; subcontrol-position: center right;
            width: {px(_ARROW_BOX)}px; border: none; background: transparent;
            margin-right: {gap(6)}px;
        }}
        QComboBox::down-arrow {{ {_arrow_image()} width: {px(_ARROW)}px; height: {px(_ARROW)}px; }}
        /* The filters above the grid: rounded as the toolbar's cards */
        QComboBox#row {{ border-radius: {radius_within("large", row)}px; }}
        QComboBox:hover {{ border-color: {overlay(0.2)}; }}
        QComboBox:focus {{ border-color: {colors["accent"]}; }}
        /* The list's window: transparent, the list inside it is the rounded card */
        QComboBoxPrivateContainer {{ background: transparent; border: none; }}
        /* A list under the box, not the platform style's menu over it, which the
           stylesheet can't round: the same card as a menu */
        QComboBox {{ combobox-popup: 0; }}
        QComboBox QAbstractItemView {{
            background: {card}; border: 1px solid {divider}; border-radius: {large}px;
            padding: {gap(6)}px; outline: none;
            selection-background-color: {hover}; selection-color: {colors["textPrimary"]};
        }}
        QComboBox QAbstractItemView::item {{
            padding: {gap(6)}px {gap(10)}px; border-radius: {medium}px; min-height: {gap(20)}px;
        }}
        QComboBox QAbstractItemView::item:selected, QComboBox QAbstractItemView::item:hover {{
            background: {hover}; color: {colors["textPrimary"]};
        }}

        #title {{ font-size: {theme_reader.font_scaled(_theme, 15)}px; font-weight: 600; }}
        #secondary {{ color: {colors["textSecondary"]}; font-size: {small_font}px; }}
        #divider {{ background: {divider}; border: none; }}
        #placeholder {{ color: {colors["textSecondary"]}; }}

        QMenu {{
            background: {card}; border: 1px solid {divider};
            border-radius: {large}px; padding: {gap(6)}px;
        }}
        QMenu::item {{
            padding: {gap(8)}px {gap(14)}px; border-radius: {medium}px; margin: 0 {gap(2)}px;
        }}
        QMenu::item:selected {{ background: {hover}; color: {colors["textPrimary"]}; }}
        QMenu::item:disabled {{ color: {colors["textTertiary"]}; }}
        QMenu::separator {{ height: 1px; background: {divider}; margin: {gap(6)}px {gap(8)}px; }}

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
