"""The application: Qt, the theme, the library, the window."""

import gc
import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from lumina import APP_ID
from lumina.integration import style, theme
from lumina.integration.single_instance import SingleInstance
from lumina.integration.theme_watcher import ThemeWatcher
from lumina.qt_models import workers
from lumina.qt_models.library import LibraryService, NewerDatabase
from lumina.qt_models.merge import MergeService
from lumina.qt_models.search import SearchService
from lumina.ui import settings
from lumina.ui.main_window import MainWindow

# How often the garbage is collected, in milliseconds
_COLLECT_MS = 5000


def use_desktop_dialogs(environ=os.environ, platform=sys.platform):
    """Open and save dialogs from the desktop (the file chooser portal: Plasma's own, or
    whichever app the user set to serve it), not Qt's own drawn one. PyPI's Qt cannot
    load Plasma's dialogs; the portal theme wraps the desktop's theme, so icons and
    fonts are as before. A theme the user chose is left alone. Before the app starts."""
    if platform.startswith("linux") and not environ.get("QT_QPA_PLATFORMTHEME"):
        environ["QT_QPA_PLATFORMTHEME"] = "xdgdesktopportal"


def run(paths=()):
    """`paths`: folders to add and photos to open, from the command line or the file manager."""
    use_desktop_dialogs()
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName("Lumina")
    QGuiApplication.setDesktopFileName(APP_ID)
    # Python's garbage collector runs in whichever thread allocates enough, and a Qt
    # object whose wrapper it collects in a worker thread aborts the process. It is for
    # the main thread: off, and run from here
    gc.disable()
    collector = QTimer(app)
    collector.setInterval(_COLLECT_MS)
    collector.timeout.connect(gc.collect)
    collector.start()
    # Let Ctrl+C in a terminal end the app
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # One Lumina per user: a running one is handed the paths, and this start ends before
    # it opens the database a second time
    instance = SingleInstance(parent=app)
    if instance.forward(paths):
        return 0
    instance.listen()

    # The same look as filehouse: the shell's theme.json over the same defaults, followed
    # while the app runs
    style.set_density(settings.density())
    style.apply(app, theme.load_theme())
    ThemeWatcher(app, parent=app)

    try:
        service = LibraryService(parent=app)
    except NewerDatabase as error:
        QMessageBox.critical(None, "Lumina", f"{error}\n\nIt was left as it is.")
        return 1
    service.thumbnail_limit = settings.thumbnail_limit()
    service.gentle_scans = settings.gentle_scans()
    service.watcher.set_enabled(settings.watch_folders())
    # Nothing is fetched until the user asks: without the model, no indexing
    search = SearchService(service, parent=app)
    merge = MergeService(service, parent=app)
    # A scan, indexing or a merge in progress stops and is queued for the next start
    app.aboutToQuit.connect(search.close)
    app.aboutToQuit.connect(merge.close)
    app.aboutToQuit.connect(service.close)
    app.aboutToQuit.connect(workers.shutdown)

    window = MainWindow(service, search, merge)
    instance.opened.connect(lambda wanted: _open(window, wanted))
    service.start()
    search.start()
    merge.start()
    _open(window, paths)
    return app.exec()


def _open(window, paths):
    """Show the paths asked for, or the window itself when nothing else shows."""
    window.open_paths(paths)
    if not paths or not (window.isVisible() or window.quick_views):
        window.present()
