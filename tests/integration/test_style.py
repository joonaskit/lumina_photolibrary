import json

from PySide6.QtCore import QPoint
from PySide6.QtGui import QPalette

from lumina.integration import style, theme
from lumina.integration.theme_watcher import ThemeWatcher


def test_defaults_give_filehouse_window_colours(qapp):
    style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))
    palette = qapp.palette()

    # The window is the theme's surface, never its pitch-black background
    assert palette.color(QPalette.ColorRole.Window).name() == "#1c1c1e"
    assert palette.color(QPalette.ColorRole.Highlight).name() == "#0a84ff"
    assert "#group" in qapp.styleSheet()


def test_a_light_scheme_makes_cards_darker_than_the_window(qapp, tmp_path):
    path = tmp_path / "theme.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "scheme": "light",
                "colors": {"surface": "#f2f2f7", "textPrimary": "#000000"},
            }
        )
    )
    style.apply(qapp, theme.load_theme(path))

    assert style.layer("card").lightness() < style.layer("window").lightness()
    style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))


def test_a_changed_theme_file_is_applied_live(qapp, qtbot, tmp_path):
    path = tmp_path / "quickshell-island" / "theme.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 1}))
    style.apply(qapp, theme.load_theme(path))
    watcher = ThemeWatcher(qapp, path=path, settle_ms=10)  # noqa: F841 (kept alive)

    with qtbot.waitSignal(style.themed.changed, timeout=5000):
        path.write_text(json.dumps({"version": 1, "colors": {"accent": "#30d158"}}))

    assert qapp.palette().color(QPalette.ColorRole.Highlight).name() == "#30d158"
    style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))


def test_the_combo_box_arrow_is_drawn_in_the_theme_into_the_cache(qapp, tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))

    drawn = list((tmp_path / "lumina" / "style").glob("chevron-*.png"))
    assert len(drawn) == 1
    assert style.color("textSecondary").name()[1:] in drawn[0].name
    assert f"url({drawn[0]})" in qapp.styleSheet()
    assert not [p for p in os.listdir(drawn[0].parent) if p.endswith(".part")]


def test_without_a_writable_cache_the_combo_box_keeps_the_styles_arrow(qapp, tmp_path, monkeypatch):
    import os

    import pytest

    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("XDG_CACHE_HOME", str(locked / "cache"))
    try:
        style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))
    finally:
        locked.chmod(0o700)

    assert "chevron" not in qapp.styleSheet()


def test_menus_and_combo_box_lists_have_round_corners_that_show(qapp, qtbot):
    # Any of them, also those Qt's own dialogs make: the square window behind the
    # rounded card is transparent, so nothing shows past the corners
    from PySide6.QtWidgets import QComboBox, QMenu

    style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))
    combo = QComboBox()
    combo.addItems(["Directories", "Photos"])
    qtbot.addWidget(combo)
    combo.show()
    menu = QMenu()
    menu.addAction("Rescan")

    combo.showPopup()
    menu.popup(combo.mapToGlobal(combo.rect().bottomLeft()))
    popups = (combo.view().window(), menu)
    qtbot.waitUntil(lambda: all(p.isVisible() for p in popups))

    for popup in popups:
        corner = popup.grab().toImage().pixelColor(0, 0)
        assert corner.alpha() == 0, type(popup).__name__
    combo.hidePopup()
    menu.close()


def test_slider_handles_and_tracks_follow_the_roundness_of_the_theme(qapp, tmp_path):
    def rules(radii):
        """The slider's handle and groove rules under a theme with these corner radii."""
        path = tmp_path / "theme.json"
        path.write_text(json.dumps({"version": 1, "radius": radii}))
        style.apply(qapp, theme.load_theme(path))
        sheet = qapp.styleSheet()

        def block(selector):
            start = sheet.index(selector + " {")
            return sheet[start : sheet.index("}", start)]

        return block("QSlider::handle:horizontal"), block("QSlider::groove:horizontal")

    try:
        # The shell's default corners: a circle (the handle is 14 px), and a pill (4 px)
        handle, groove = rules({"small": 6, "medium": 10, "large": 14})
        assert "border-radius: 7px" in handle and "border-radius: 2px" in groove
        # Squarer corners: a squarer handle, and a squarer track
        handle, groove = rules({"small": 1, "medium": 3, "large": 4})
        assert "border-radius: 3px" in handle and "border-radius: 1px" in groove
        # Rounder corners never make it more than a circle
        handle, groove = rules({"small": 20, "medium": 40, "large": 60})
        assert "border-radius: 7px" in handle and "border-radius: 2px" in groove
    finally:
        style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))


def test_buttons_and_boxes_are_rounded_whatever_the_density_and_corners(qapp, qtbot, tmp_path):
    # A radius more than half a control's height is drawn square: the push buttons were
    # square in the compact density, and the boxes too with the shell's rounder corners
    from PySide6.QtWidgets import QComboBox, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget

    def corners_are_round(radii, density):
        path = tmp_path / "theme.json"
        path.write_text(json.dumps({"version": 1, "radius": radii}))
        style.set_density(density)
        style.apply(qapp, theme.load_theme(path))
        card = QWidget()
        qtbot.addWidget(card)
        layout = QVBoxLayout(card)
        combo = QComboBox()
        combo.addItem("Free")
        controls = [QPushButton("Crop"), combo, QLineEdit("A field"), QSpinBox()]
        for control in controls:
            layout.addWidget(control)
        card.resize(300, 200)
        card.show()
        qtbot.waitExposed(card)
        shot = card.grab().toImage()
        round_ = []
        for control in controls:
            corner = control.geometry().topLeft()
            # Its fill, at the middle of its left edge, inside the border
            middle = corner + QPoint(2, control.height() // 2)
            round_.append(shot.pixelColor(corner) != shot.pixelColor(middle))
        card.close()
        return round_

    try:
        for density in ("comfortable", "compact"):
            for radii in (
                {"small": 6, "medium": 10, "large": 14},
                {"small": 12, "medium": 20, "large": 28},
            ):
                assert corners_are_round(radii, density) == [True] * 4, (density, radii)
    finally:
        style.set_density("comfortable")
        style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))


def test_buttons_follow_the_shells_corners_as_its_rows_do(qapp, tmp_path):
    # The medium radius, as the sidebar's rows: rounder with the shell's Round corners
    # than with its Default ones, not a pill at both
    def button_radius(scale):
        path = tmp_path / "theme.json"
        radii = {"small": 6 * scale, "medium": 10 * scale, "large": 14 * scale}
        path.write_text(json.dumps({"version": 1, "radius": radii}))
        style.apply(qapp, theme.load_theme(path))
        sheet = qapp.styleSheet()
        start = sheet.index("QPushButton {")
        block = sheet[start : sheet.index("}", start)]
        return int(block.split("border-radius: ")[1].split("px")[0])

    try:
        assert button_radius(0.6) < button_radius(1.0) < button_radius(1.5)
        assert button_radius(1.0) == style.radius("medium")
    finally:
        style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))


def test_the_sidebars_buttons_are_as_round_as_its_rows(qapp, qtbot, tmp_path):
    # Add Folder, Import and Tether Camera had a tool button's small radius under the
    # folders' medium one; and they are a row tall, so not rounder than fits in it
    from PySide6.QtWidgets import QFrame, QToolButton, QVBoxLayout

    def sidebar_button(radii, density):
        path = tmp_path / "theme.json"
        path.write_text(json.dumps({"version": 1, "radius": radii}))
        style.set_density(density)
        style.apply(qapp, theme.load_theme(path))
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        qtbot.addWidget(sidebar)
        button = QToolButton()
        button.setText("Import…")
        button.setFixedHeight(style.row_height())
        button.setDown(True)
        QVBoxLayout(sidebar).addWidget(button)
        sidebar.resize(240, 80)
        sidebar.show()
        qtbot.waitExposed(sidebar)
        shot = sidebar.grab().toImage()
        corner = button.geometry().topLeft()
        middle = corner + QPoint(2, button.height() // 2)
        round_ = shot.pixelColor(corner) != shot.pixelColor(middle)
        sidebar.close()
        sheet = qapp.styleSheet()
        start = sheet.index("#sidebar QToolButton {")
        block = sheet[start : sheet.index("}", start)]
        return int(block.split("border-radius: ")[1].split("px")[0]), round_

    try:
        for density in ("comfortable", "compact"):
            for radii in (
                {"small": 6, "medium": 10, "large": 14},
                {"small": 12, "medium": 20, "large": 28},
            ):
                radius, round_ = sidebar_button(radii, density)
                assert round_, (density, radii)
                assert radius == min(style.radius("medium"), style.row_height() // 2)
    finally:
        style.set_density("comfortable")
        style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))


def test_a_toolbar_groups_buttons_are_round_in_both_densities(qapp, qtbot, tmp_path):
    # The radius was made for a button 2 pixels taller than it is (the group's border):
    # more than half of it in the compact density, so Settings' hover was drawn square
    from PySide6.QtWidgets import QFrame, QHBoxLayout, QToolButton

    def round_(density):
        style.set_density(density)
        # The shell's Round corners at its 1.1 scale, as the author's
        path = tmp_path / "theme.json"
        radii = {"small": 9, "medium": 15, "large": 21}
        path.write_text(json.dumps({"version": 1, "radius": radii, "scale": {"ui": 1.1}}))
        style.apply(qapp, theme.load_theme(path))
        group = QFrame()
        group.setObjectName("group")
        qtbot.addWidget(group)
        layout = QHBoxLayout(group)
        padding = style.group_padding()
        layout.setContentsMargins(padding, padding, padding, padding)
        group.setFixedHeight(style.row_height())
        button = QToolButton()
        button.setText("S")
        button.setFixedSize(style.row_inner_height(), style.row_inner_height())
        button.setDown(True)
        layout.addWidget(button)
        group.show()
        qtbot.waitExposed(group)
        shot = group.grab().toImage()
        corner = button.geometry().topLeft() + QPoint(1, 1)
        middle = button.geometry().topLeft() + QPoint(1, button.height() // 2)
        group.close()
        return shot.pixelColor(corner) != shot.pixelColor(middle)

    try:
        assert round_("comfortable") and round_("compact")
    finally:
        style.set_density("comfortable")
        style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))
