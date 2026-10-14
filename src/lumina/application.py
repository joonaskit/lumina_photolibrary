"""The application: Qt, the theme, the window."""

import gc
import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from lumina import APP_ID
from lumina.integration import style, theme
from lumina.integration.theme_watcher import ThemeWatcher
from lumina.qt_models import workers
from lumina.ui import settings
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
    app.aboutToQuit.connect(workers.shutdown)

    window = MainWindow()
    folder = folder or settings.last_folder()
    if folder and os.path.isdir(folder):
        window.open_folder(folder)
    window.show()
    return app.exec()
