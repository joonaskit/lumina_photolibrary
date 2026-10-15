"""The settings window: each option is kept in QSettings as it is changed, and the
search model can be deleted and fetched again from it."""

import os

from conftest import TIMEOUT
from fakes import FakeEncoder, install_model
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMessageBox

from lumina.core import embed, models
from lumina.qt_models.search import SearchService
from lumina.ui import settings
from lumina.ui import settings_dialog as dialogs
from lumina.ui.main_window import MainWindow
from lumina.ui.settings_dialog import SettingsDialog


def make_search(service, root):
    encoder = FakeEncoder()
    return SearchService(service, root, encode=lambda _folder, paths: encoder(paths))


def test_next_photo_after_flagging_is_kept_as_it_is_ticked(qtbot):
    settings.set_advance_after_flag(True)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.advance.isChecked()

    dialog.advance.setChecked(False)
    assert not settings.advance_after_flag()
    dialog.advance.setChecked(True)
    assert settings.advance_after_flag()

    # Read again by a new window
    again = SettingsDialog()
    qtbot.addWidget(again)
    assert again.advance.isChecked()


def test_the_window_opens_it_with_ctrl_comma(qtbot, service, monkeypatch):
    opened = []
    monkeypatch.setattr(dialogs.SettingsDialog, "exec", lambda dialog: opened.append(dialog))
    window = MainWindow(service)
    qtbot.addWidget(window)

    # Offscreen the window is never active, so its own shortcuts are not pressed
    assert window.preferences_action.shortcut() == QKeySequence("Ctrl+,")
    window.preferences_action.trigger()

    assert len(opened) == 1
    # No search service, no Search section
    assert not hasattr(opened[0], "download")


def test_without_the_model_it_offers_the_download(qtbot, service, tmp_path):
    search = make_search(service, tmp_path / "models")
    dialog = SettingsDialog(search)
    qtbot.addWidget(dialog)

    assert "not downloaded" in dialog.model_state.text()
    assert dialog.model_folder.text() == search.folder
    assert dialog.download.isEnabled() and not dialog.remove.isEnabled()
    search.close()


def test_the_model_is_deleted_after_asking(qtbot, service, tmp_path, monkeypatch):
    root = tmp_path / "models"
    install_model(root)
    kept = root / "another" / "file"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x")
    search = make_search(service, root)
    dialog = SettingsDialog(search)
    qtbot.addWidget(dialog)
    assert "is on this computer" in dialog.model_state.text()
    assert dialog.remove.isEnabled() and not dialog.download.isEnabled()

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel)
    dialog.remove.click()
    assert search.is_ready() and search.remove_future is None

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    with qtbot.waitSignals([search.model_changed, search.model_changed], timeout=TIMEOUT):
        dialog.remove.click()
    qtbot.waitUntil(lambda: not search.is_removing(), timeout=TIMEOUT)

    assert not search.is_ready()
    assert not os.path.exists(models.directory(embed.MODEL, root))
    assert kept.exists()
    assert "not downloaded" in dialog.model_state.text()
    assert dialog.download.isEnabled() and not dialog.remove.isEnabled()
    # Nothing is indexed without it
    search.index()
    assert search.index_future is None
    search.close()


def test_without_a_library_there_is_no_thumbnails_section(qtbot):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)

    assert not hasattr(dialog, "cache_limit")


def test_the_thumbnail_limit_is_kept_and_applied_to_the_cache(qtbot, service):
    assert settings.thumbnail_limit_gb() == 4 and settings.thumbnail_limit() == 4 * 1024**3
    dialog = SettingsDialog(library=service)
    qtbot.addWidget(dialog)
    assert dialog.cache_limit.value() == 4

    dialog.cache_limit.setValue(2)
    assert settings.thumbnail_limit_gb() == 2
    # Not at once: spinning it down must not delete on the way
    assert service.thumbnail_limit is None
    with qtbot.waitSignal(service.thumbnails_pruned, timeout=TIMEOUT):
        dialog.apply_cache_limit()
    assert service.thumbnail_limit == 2 * 1024**3

    # Zero is no limit
    dialog.cache_limit.setValue(0)
    assert settings.thumbnail_limit() is None
    dialog.apply_cache_limit()
    assert service.thumbnail_limit is None
    assert dialog.cache_limit.text() == "No limit"


def test_closing_applies_a_limit_changed_a_moment_ago(qtbot, service):
    dialog = SettingsDialog(library=service)
    qtbot.addWidget(dialog)

    dialog.cache_limit.setValue(3)
    dialog.reject()

    assert service.thumbnail_limit == 3 * 1024**3


def test_what_the_thumbnails_take_is_shown(qtbot, service):
    from lumina.core import thumbs

    folder = thumbs.ThumbnailCache(thumbs.default_cache_dir()).root / "aa"
    folder.mkdir(parents=True)
    (folder / "aa-320.webp").write_bytes(b"x" * 3_000_000)
    dialog = SettingsDialog(library=service)
    qtbot.addWidget(dialog)

    qtbot.waitUntil(lambda: dialog.cache_usage.text().startswith("They take"), timeout=TIMEOUT)

    assert dialog.cache_usage.text() == "They take 3 MB now."


def test_a_stored_limit_is_shown_and_a_bad_one_is_the_default(qtbot, service):
    settings._settings().setValue("cache/thumbnail_limit_gb", "nonsense")
    assert settings.thumbnail_limit_gb() == settings.THUMBNAIL_LIMIT_DEFAULT
    settings.set_thumbnail_limit_gb(7)
    dialog = SettingsDialog(library=service)
    qtbot.addWidget(dialog)

    assert dialog.cache_limit.value() == 7


def test_size_text():
    assert dialog_size(3 * 1024**3) == "3.0 GB"
    assert dialog_size(1536 * 1024**2) == "1.5 GB"
    assert dialog_size(300 * 1024**2) == "300 MB"


def dialog_size(size):
    return dialogs.size_text(size)
