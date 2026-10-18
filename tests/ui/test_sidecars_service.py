"""XMP sidecars in the app: the setting, writing after changes settle, reading when
turned on."""

import os

from conftest import TIMEOUT

from lumina.core import xmp
from lumina.ui import settings
from lumina.ui.settings_dialog import SettingsDialog


def sidecar_files(drive):
    return sorted(name for name in os.listdir(drive / "Photos") if name.endswith(".xmp"))


def first_photo(service):
    return service.db.photos()[0]


def test_sidecars_are_off_until_the_user_turns_them_on(qtbot, service, drive):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    assert settings.sidecars() is False and service.sidecars is False

    service.set_flags({first_photo(service).id: 1})
    qtbot.wait(150)

    assert sidecar_files(drive) == []


def test_a_change_is_written_after_it_settles_and_once(qtbot, service, drive):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    service._sidecar_timer.setInterval(50)
    service.sidecars = True
    photo = first_photo(service)

    with qtbot.waitSignal(service.sidecars_written, timeout=TIMEOUT) as written:
        service.set_ratings({photo.id: 3})
        service.set_flags({photo.id: 1})
        service.save_edit(photo.id, '{"version": 1}')

    assert written.args[0].written == 1 and written.args[0].failures == []
    name = os.path.basename(photo.path) + ".xmp"
    assert sidecar_files(drive) == [name]
    held = xmp.read(str(drive / "Photos" / os.path.basename(photo.path)))
    assert (held.flag, held.rating, held.edits) == (1, 3, ((0, '{"version": 1}'),))


def test_turning_them_on_writes_for_the_whole_library_and_reads_those_there(qtbot, service, drive):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    marked, other = service.db.photos()[:2]
    service.set_ratings({marked.id: 4})
    service.settle()
    # A sidecar made elsewhere, newer than anything here
    xmp.write(
        str(drive / "Photos" / os.path.basename(other.path)),
        xmp.SidecarState(updated=service.db.sidecar_state(other.id).updated + 1e6, rating=2),
    )

    with qtbot.waitSignal(service.sidecars_written, timeout=TIMEOUT):
        service.set_sidecars(True)
    qtbot.waitUntil(lambda: service.db.photo(other.id).rating == 2, timeout=TIMEOUT)

    assert os.path.basename(marked.path) + ".xmp" in sidecar_files(drive)
    assert service.db.photo(marked.id).rating == 4


def test_turning_them_off_stops_writing(qtbot, service, drive):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    service.set_sidecars(True)
    service.set_sidecars(False)

    service.set_flags({first_photo(service).id: 1})
    qtbot.wait(150)

    assert sidecar_files(drive) == []


def test_the_settings_window_turns_them_on_and_remembers(qtbot, service):
    dialog = SettingsDialog(library=service)
    qtbot.addWidget(dialog)
    assert not dialog.sidecars.isChecked()

    dialog.sidecars.setChecked(True)

    assert settings.sidecars() is True and service.sidecars is True
    again = SettingsDialog(library=service)
    qtbot.addWidget(again)
    assert again.sidecars.isChecked()


def test_a_sidecar_that_could_not_be_written_is_said_in_the_status_bar(qtbot, service, drive):
    from lumina.ui.main_window import MainWindow

    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        window.browser.add_folder(drive / "Photos")
    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    service._sidecar_timer.setInterval(50)
    service.sidecars = True
    photo = first_photo(service)
    theirs = drive / "Photos" / (os.path.basename(photo.path) + ".xmp")
    theirs.write_bytes(b"another program's")

    with qtbot.waitSignal(service.sidecars_written, timeout=TIMEOUT):
        service.set_ratings({photo.id: 3})

    assert "1 sidecar not written" in window.statusBar().currentMessage()
    assert theirs.read_bytes() == b"another program's"
