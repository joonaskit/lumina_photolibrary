"""The application: Qt, the theme, the library, the window."""

import gc
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from lumina import APP_ID
from lumina.integration import style, theme
from lumina.integration.theme_watcher import ThemeWatcher
from lumina.qt_models import workers
from lumina.qt_models.library import LibraryService, NewerDatabase
from lumina.ui.main_window import MainWindow

# How often the garbage is collected, in milliseconds
_COLLECT_MS = 5000


def run(folder=None):
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

    # The same look as filehouse: the shell's theme.json over the same defaults, followed
    # while the app runs
    style.apply(app, theme.load_theme())
    ThemeWatcher(app, parent=app)

    try:
        service = LibraryService(parent=app)
    except NewerDatabase as error:
        QMessageBox.critical(None, "Lumina", f"{error}\n\nIt was left as it is.")
        return 1
    # A scan in progress stops and is queued for the next start
    app.aboutToQuit.connect(service.close)
    app.aboutToQuit.connect(workers.shutdown)

    window = MainWindow(service)
    window.show()
    service.start()
    if folder:
        window.add_folder(folder)
    return app.exec()
