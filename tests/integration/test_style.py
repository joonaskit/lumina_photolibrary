import json

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
