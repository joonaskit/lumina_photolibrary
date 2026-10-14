"""The launcher entry and the icon install.sh puts in place, and the app's id they
must agree with."""

import configparser
from pathlib import Path

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from lumina import APP_ID

ROOT = Path(__file__).parent.parent.parent


def entry():
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(ROOT / "data" / f"{APP_ID}.desktop", encoding="utf-8")
    return parser["Desktop Entry"]


def test_the_launcher_starts_the_app_with_its_icon():
    desktop = entry()

    assert desktop["Type"] == "Application"
    assert desktop["Exec"].split()[0] == APP_ID
    assert desktop["Icon"] == APP_ID
    # The window is matched to the entry by this, which the app sets as its desktop
    # file name (application.py): the taskbar shows the entry's icon for it
    assert desktop["StartupWMClass"] == APP_ID


def test_the_install_script_installs_what_the_launcher_names():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert f"data/{APP_ID}.desktop" in script
    assert f"icons/{APP_ID}.svg" in script
    assert f".venv/bin/{APP_ID}" in script


def test_the_icon_draws_at_the_smallest_size(qapp):
    renderer = QSvgRenderer(str(ROOT / "icons" / f"{APP_ID}.svg"))
    assert renderer.isValid()
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, 16, 16))
    painter.end()

    # The tile fills the middle: not an empty or broken drawing
    assert image.pixelColor(8, 8).alpha() == 255
