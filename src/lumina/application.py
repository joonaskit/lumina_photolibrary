"""The application: Qt, the theme, the library, the window."""

import gc
import os
import signal
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from lumina import APP_ID
from lumina.integration import appearance, style
from lumina.integration.island import Activities, IslandClient
from lumina.integration.notifications import Notifier
from lumina.integration.single_instance import SingleInstance
from lumina.integration.theme_watcher import ThemeWatcher
from lumina.qt_models import backups as backing
from lumina.qt_models import workers
from lumina.qt_models.backups import BackupService
from lumina.qt_models.exporting import ExportService
from lumina.qt_models.hot_folder import HotFolder
from lumina.qt_models.importing import ImportService
from lumina.qt_models.library import LibraryService, NewerDatabase, NoBackup
from lumina.qt_models.merge import MergeService
from lumina.qt_models.search import SearchService
from lumina.qt_models.tether import CameraService
from lumina.ui import settings
from lumina.ui.job_reports import JobReports, watch_services
from lumina.ui.main_window import MainWindow
from lumina.ui.settings_dialog import when_text

# How often the garbage is collected, in milliseconds
_COLLECT_MS = 5000


def use_desktop_dialogs(environ=os.environ, platform=sys.platform):
    """Open and save dialogs from the desktop (the file chooser portal: Plasma's own, or
    whichever app the user set to serve it), not Qt's own drawn one. PyPI's Qt cannot
    load Plasma's dialogs; the portal theme wraps the desktop's theme, so icons and
    fonts are as before. A theme the user chose is left alone. Before the app starts."""
    if platform.startswith("linux") and not environ.get("QT_QPA_PLATFORMTHEME"):
        environ["QT_QPA_PLATFORMTHEME"] = "xdgdesktopportal"


def run(paths=(), restore=None):
    """`paths`: folders to add and photos to open, from the command line or the file
    manager. `restore`: a backup to put in place of the library first, which the user
    chose before the app started itself again."""
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
    # while the app runs; or with the scheme, accent and corners chosen in the settings
    style.set_density(settings.density())
    appearance.apply(app, settings.chosen_look(), live=False)
    ThemeWatcher(
        app,
        parent=app,
        adjust=lambda read: appearance.theme_with(read, settings.chosen_look()),
    )

    # Before anything opens the database
    told = None
    if restore is not None:
        told = _restore(restore)
    elif not _check_database():
        return 1

    try:
        service = LibraryService(parent=app)
    except NewerDatabase as error:
        QMessageBox.critical(None, "Lumina", f"{error}\n\nIt was left as it is.")
        return 1
    except NoBackup as error:
        QMessageBox.critical(None, "Lumina", str(error))
        return 1
    backups = BackupService(service.path, parent=app)
    backups.extra_folder = settings.backup_folder()
    backups.keep_daily, backups.keep_weekly = settings.backups_kept(
        backing.KEEP_DAILY, backing.KEEP_WEEKLY
    )
    # Restoring a backup: quit, and start again on it
    backups.restore_requested.connect(app.quit)
    service.thumbnail_limit = settings.thumbnail_limit()
    service.gentle_scans = settings.gentle_scans()
    service.watcher.set_enabled(settings.watch_folders())
    service.sidecars = settings.sidecars()
    # Nothing is fetched until the user asks: without the model, no indexing
    search = SearchService(service, parent=app)
    merge = MergeService(service, parent=app)
    importer = ImportService(service, parent=app)
    exporter = ExportService(service, parent=app)
    tether = HotFolder(service, search, parent=app)
    camera = CameraService(service, tether, parent=app)
    # The jobs in the Quickshell Island while they run, and a notification when one ends
    # unseen. Without the island or a notification service this does nothing
    island = IslandClient(parent=app)
    notifier = Notifier(parent=app)
    reports = JobReports(
        Activities(island, parent=app),
        notifier,
        lambda: app.applicationState() == Qt.ApplicationState.ApplicationActive,
        parent=app,
    )
    watch_services(reports, service, search, merge, importer, exporter)
    # Before the jobs stop: the island is told they will carry on at the next start
    app.aboutToQuit.connect(reports.close)
    # A scan, indexing or a merge in progress stops and is queued for the next start
    app.aboutToQuit.connect(search.close)
    app.aboutToQuit.connect(merge.close)
    app.aboutToQuit.connect(importer.close)
    app.aboutToQuit.connect(exporter.close)
    app.aboutToQuit.connect(camera.close)
    app.aboutToQuit.connect(tether.close)
    app.aboutToQuit.connect(backups.close)
    app.aboutToQuit.connect(service.close)
    app.aboutToQuit.connect(workers.shutdown)

    window = MainWindow(
        service,
        search,
        merge,
        importer,
        backups=backups,
        tether=tether,
        camera=camera,
        exporter=exporter,
    )
    backups.failed.connect(lambda text: window.statusBar().showMessage(text, 15000))
    instance.opened.connect(lambda wanted: _open(window, wanted))
    notifier.activated.connect(window.present)
    island.start()
    notifier.start()
    service.start()
    search.start()
    merge.start()
    importer.start()
    exporter.start()
    backups.start()
    _open(window, paths)
    if told:
        window.statusBar().showMessage(told, 15000)
    code = app.exec()
    if backups.restore_from is not None:
        instance.close()
        _start_again(backups.restore_from)
    return code


def _start_again(restore):
    """Lumina again, in this process, with the backup `restore` to put in place first.
    Every thread and its connection to the database ends here."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "lumina.app", "--restore", restore])


def _restore(path):
    """Put the backup `path` in place of the library. What to tell the user when it is
    done; when it could not be, they are told now, and the library is as it was."""
    problem = backing.restore(path)
    if problem is not None:
        QMessageBox.critical(
            None,
            "Lumina",
            f"The backup could not be restored.\n\n{problem}\n\nThe library is as it was.",
        )
        return None
    return f"Restored the library from {os.path.basename(path)}"


def _check_database():
    """Whether to go on: the library database is sound, or was damaged and the user had
    a backup restored, or chose to open it as it is."""
    problem = backing.check()
    if problem is None:
        return True
    path = backing.default_path()
    folders = [backing.default_folder(path), settings.backup_folder()]
    found = [
        made
        for made in backing.backups(path, [folder for folder in folders if folder])
        if made.kind != backing.DAMAGED
    ]
    box = QMessageBox(QMessageBox.Icon.Critical, "Lumina", "The library database is damaged.")
    quit_button = box.addButton("Quit", QMessageBox.ButtonRole.RejectRole)
    if not found:
        box.setInformativeText(
            f"{problem}\n\nThere is no backup of it. Lumina can open it as it is, but "
            "what is damaged may be missing or wrong, and may get worse."
        )
        go_on = box.addButton("Open Anyway", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(quit_button)
        box.exec()
        return box.clickedButton() is go_on
    newest = found[0]
    held = backing.contents(newest.path)
    photos = ""
    if held is not None:
        photos = f", with {held.photos:,} photo{'s' * (held.photos != 1)}"
    box.setInformativeText(
        f"{problem}\n\nThe newest backup is from {when_text(newest.taken, ' at ')}"
        f"{photos}. Restore it? The damaged database is kept beside the backups."
    )
    restore = box.addButton("Restore Backup", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(restore)
    box.exec()
    if box.clickedButton() is not restore:
        return False
    return _restore(newest.path) is not None


def _open(window, paths):
    """Show the paths asked for, or the window itself when nothing else shows."""
    window.opening.open_paths(paths)
    if not paths or not (window.isVisible() or window.opening.quick_views):
        window.present()
