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


def test_the_launcher_takes_files_and_names_the_types_the_library_reads():
    from lumina.core import images

    desktop = entry()

    # %F: the file manager's selection, as paths
    assert "%F" in desktop["Exec"].split() or "%U" in desktop["Exec"].split()
    types = [t for t in desktop["MimeType"].split(";") if t]
    assert len(types) == len(set(types))
    mimes = mimetypes_by_extension()
    # Every type is one of an extension the library reads
    for mime in types:
        assert {ext for ext, name in mimes.items() if name == mime} & images.PHOTO_EXTENSIONS, mime
    # ... and every extension the library reads has its type listed
    for extension in images.PHOTO_EXTENSIONS:
        assert mimes[extension] in types, extension


def mimetypes_by_extension():
    """Extension -> shared-mime-info type, for what the library reads. Written out: the
    system's database differs between machines (and has none in the CI container)."""
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".dng": "image/x-adobe-dng",
        ".cr2": "image/x-canon-cr2",
        ".cr3": "image/x-canon-cr3",
        ".crw": "image/x-canon-crw",
        ".raf": "image/x-fuji-raf",
        ".nef": "image/x-nikon-nef",
        ".nrw": "image/x-nikon-nrw",
        ".orf": "image/x-olympus-orf",
        ".rw2": "image/x-panasonic-rw2",
        ".pef": "image/x-pentax-pef",
        ".arw": "image/x-sony-arw",
    }
