"""Backups for the UI: the daily one at the start, a second folder, and the Backups tab
of the settings window, from which one is restored."""

import os
from datetime import datetime

from conftest import TIMEOUT
from PySide6.QtWidgets import QMessageBox

from lumina import app
from lumina.core import backup
from lumina.qt_models import backups as backing
from lumina.qt_models.backups import BackupService
from lumina.ui import settings
from lumina.ui.main_window import MainWindow
from lumina.ui.settings_dialog import SettingsDialog


def make_backups(service, extra=None):
    backups = BackupService(service.path)
    backups.extra_folder = extra
    return backups


def wait_done(qtbot, backups):
    qtbot.waitUntil(lambda: not backups.is_busy(), timeout=TIMEOUT)


def test_the_start_backs_up_when_one_is_due(qtbot, service):
    backups = make_backups(service)

    with qtbot.waitSignal(backups.changed, timeout=TIMEOUT):
        backups.start()
    [made] = backups.backups()
    assert made.kind == backing.DAILY
    assert os.path.dirname(made.path) == backing.default_folder(service.path)

    # Not again the same day
    with qtbot.waitSignal(backups.changed, timeout=TIMEOUT):
        backups.start()
    assert len(backups.backups()) == 1


def test_a_running_app_backs_up_when_the_next_one_is_due(qtbot, service):
    backups = BackupService(service.path, due_ms=50)
    with qtbot.waitSignal(backups.changed, timeout=TIMEOUT):
        backups.start()
    [made] = backups.backups()

    # A day later, as far as the folder tells
    os.remove(made.path)
    qtbot.waitUntil(lambda: len(backups.backups()) == 1, timeout=TIMEOUT)
    backups.close()


def test_a_second_folder_is_backed_up_to_too(qtbot, service, tmp_path):
    extra = tmp_path / "other drive"
    extra.mkdir()
    backups = make_backups(service, str(extra))

    with qtbot.waitSignal(backups.changed, timeout=TIMEOUT):
        backups.start()

    assert {os.path.dirname(made.path) for made in backups.backups()} == {
        backing.default_folder(service.path),
        str(extra),
    }


def test_a_second_folder_not_there_is_not_made_and_says_so(qtbot, service, tmp_path):
    extra = tmp_path / "unplugged" / "Backups"
    backups = make_backups(service, str(extra))
    told = []
    backups.failed.connect(told.append)

    with qtbot.waitSignal(backups.changed, timeout=TIMEOUT):
        backups.start()

    assert not extra.parent.exists()
    assert told == [f"Could not back up the library into {extra}: it is not there"]
    # Beside the database, it was
    assert len(backups.backups()) == 1


def test_the_tab_lists_them_and_backs_up_now(qtbot, service):
    backups = make_backups(service)
    backup.back_up(
        service.path,
        backing.default_folder(service.path),
        backup.BEFORE_MIGRATION,
        schema=5,
        now=datetime(2026, 9, 1, 8, 0, 0),
    )
    dialog = SettingsDialog(backups=backups)
    qtbot.addWidget(dialog)

    assert dialog.backup_list.count() == 1
    assert dialog.backup_list.item(0).text().startswith("Tuesday 1 September 2026, 08:00")
    assert dialog.backup_list.item(0).text().endswith("before an update")
    qtbot.waitUntil(lambda: "photo" in dialog.backup_details.text(), timeout=TIMEOUT)
    assert dialog.backup_details.text().startswith("0 photos, 0 edited. In ")

    dialog.back_up_button.click()
    assert not dialog.back_up_button.isEnabled()
    wait_done(qtbot, backups)
    qtbot.waitUntil(lambda: dialog.backup_list.count() == 2, timeout=TIMEOUT)
    assert dialog.back_up_button.isEnabled()
    # The newest first
    assert dialog.backup_list.item(0).data(256).kind == backing.DAILY


def test_how_many_are_kept_and_where_else_are_remembered(qtbot, service, tmp_path):
    backups = make_backups(service)
    dialog = SettingsDialog(backups=backups)
    qtbot.addWidget(dialog)
    assert dialog.backup_details.text() == "No backup yet."
    assert not dialog.restore_button.isEnabled()
    assert (dialog.keep_daily.value(), dialog.keep_weekly.value()) == (
        backing.KEEP_DAILY,
        backing.KEEP_WEEKLY,
    )

    dialog.keep_daily.setValue(3)
    dialog.keep_weekly.setValue(10)
    dialog.set_backup_folder(str(tmp_path))

    assert settings.backups_kept(7, 4) == (3, 10)
    assert (backups.keep_daily, backups.keep_weekly) == (3, 10)
    assert settings.backup_folder() == backups.extra_folder == str(tmp_path)
    assert dialog.extra_folder.text() == str(tmp_path)

    dialog.clear_extra.click()
    assert settings.backup_folder() is backups.extra_folder is None
    assert dialog.extra_folder.text() == "Nowhere else"


def test_restore_asks_then_has_the_app_start_again_on_it(qtbot, service, monkeypatch):
    backups = make_backups(service)
    made = backup.back_up(service.path, backing.default_folder(service.path))
    dialog = SettingsDialog(backups=backups)
    qtbot.addWidget(dialog)
    asked = []

    def answer(button):
        def ask(_parent, title, text, *_args):
            asked.append(text)
            return button

        return ask

    monkeypatch.setattr(QMessageBox, "question", answer(QMessageBox.StandardButton.Cancel))
    dialog.restore_backup()
    assert backups.restore_from is None

    monkeypatch.setattr(QMessageBox, "question", answer(QMessageBox.StandardButton.Yes))
    with qtbot.waitSignal(backups.restore_requested):
        dialog.restore_backup()
    assert backups.restore_from == made
    assert "backed up first" in asked[-1]


def test_a_restore_is_made_with_nothing_open(qtbot, make_service):
    service = make_service(start=False)
    path = service.path
    made = backup.back_up(path, backing.default_folder(path))
    service.close()

    assert backing.restore(made, path) is None
    assert backing.check(path) is None


def test_the_window_gives_the_settings_its_backups(qtbot, service, monkeypatch):
    backups = make_backups(service)
    opened = []
    monkeypatch.setattr(SettingsDialog, "exec", lambda dialog: opened.append(dialog))
    window = MainWindow(service, backups=backups)
    qtbot.addWidget(window)

    window.preferences_action.trigger()

    assert opened[0].backups is backups


def test_restore_is_handed_to_the_app(monkeypatch):
    asked = []
    monkeypatch.setattr(
        "lumina.application.run", lambda paths, restore: asked.append((paths, restore)) or 0
    )

    app.main(["--restore", "/backups/library-2026-10-16-093012.sqlite3"])

    assert asked == [([], "/backups/library-2026-10-16-093012.sqlite3")]


def damage(path):
    with open(path, "r+b") as file:
        file.seek(4096)
        file.write(b"\xff" * 4096)


def press(monkeypatch, text, shown):
    """QMessageBox.exec presses the button `text`; `shown` gets the box's texts."""

    def exec_(box):
        shown.append(box.informativeText())
        [button] = [b for b in box.buttons() if b.text() == text]
        button.click()

    monkeypatch.setattr(QMessageBox, "exec", exec_)


def fill_default_library(tmp_path, monkeypatch):
    from lumina.core.db import Library

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = backing.default_path()
    with Library(path) as library:
        folder = library.add_folder("uuid:A", "Pictures")
        library.write_scan(
            folder, library.volume_id("uuid:A"), new=[("Pictures/a.jpg", 1, 1, "2026")]
        )
    return path


def test_a_damaged_library_at_the_start_offers_the_newest_backup(qtbot, tmp_path, monkeypatch):
    from lumina import application

    path = fill_default_library(tmp_path, monkeypatch)
    backup.back_up(path, backing.default_folder(path))
    damage(path)
    shown = []

    press(monkeypatch, "Quit", shown)
    assert not application._check_database()
    assert backing.check(path)
    assert ", with 1 photo. Restore it?" in shown[-1]

    press(monkeypatch, "Restore Backup", shown)
    assert application._check_database()
    assert backing.check(path) is None
    kinds = {made.kind for made in backing.backups(path, [backing.default_folder(path)])}
    assert kinds == {backing.DAILY, backing.DAMAGED}


def test_a_damaged_library_without_a_backup_can_be_opened_anyway(qtbot, tmp_path, monkeypatch):
    from lumina import application

    path = fill_default_library(tmp_path, monkeypatch)
    damage(path)
    shown = []

    press(monkeypatch, "Open Anyway", shown)
    assert application._check_database()
    assert "There is no backup" in shown[-1]
    press(monkeypatch, "Quit", shown)
    assert not application._check_database()
