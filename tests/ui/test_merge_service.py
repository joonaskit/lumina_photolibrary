"""HDR merges and panoramas as jobs for the UI, and asking for them from the window."""

from datetime import datetime, timedelta

import pytest
from conftest import TIMEOUT
from PySide6.QtCore import QItemSelectionModel
from rawfiles import bracket_mosaic, make_dng, scene, surroundings, view

from lumina.core import db as dbm
from lumina.core import hdr, linear_tiff
from lumina.core.dbtypes import CANCELLED, DONE, QUEUED, RUNNING
from lumina.core.metadata import Metadata
from lumina.qt_models.merge import MergeService
from lumina.ui import main_window
from lumina.ui.controllers import merging
from lumina.ui.job_status import describe_merge
from lumina.ui.merge_dialog import MergeDialog, PanoramaDialog

START = datetime(2026, 10, 16, 14, 3, 5)


@pytest.fixture
def bracket(drive):
    light = scene()
    return [
        make_dng(drive / "Photos" / f"IMG_{i + 1:04d}.dng", mosaic=bracket_mosaic(light, stop))
        for i, stop in enumerate((-2, 0, 2))
    ]


@pytest.fixture
def make_merge(tmp_path):
    made = []

    def make(service, start=True):
        merge = MergeService(service, work_root=str(tmp_path / "work"))
        made.append(merge)
        if start:
            merge.start()
        return merge

    yield make
    for merge in made:
        merge.close()
        if merge.jobs.future is not None:
            merge.jobs.future.result(timeout=TIMEOUT / 1000)


def scanned(service, drive, qtbot):
    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT):
        service.add_folder(drive / "Photos")
    return {photo.path.rsplit("/", 1)[-1]: photo.id for photo in service.db.photos()}


def with_exposures(service, ids):
    """The bracket's photos as a camera writes their details, 0.3 s apart."""
    rows = []
    for i, (name, stop) in enumerate(
        zip(("IMG_0001", "IMG_0002", "IMG_0003"), (-2, 0, 2), strict=True)
    ):
        photo = ids[f"{name}.dng"]
        data = Metadata(
            captured=START + timedelta(seconds=0.3 * i),
            model="Canon EOS 90D",
            lens="EF-S10-18mm",
            focal_length=10.0,
            aperture=8.0,
            exposure_time=2.0**stop / 60,
            iso=100,
            file_type="DNG",
        )
        rows.append((photo, service.db.photo(photo).content_id, data))
    service.db.write_metadata(read=rows)


def ended(merge, qtbot):
    """The JobStatus a merge ends with."""
    statuses = []
    merge.job_changed.connect(statuses.append)
    qtbot.waitUntil(
        lambda: statuses and statuses[-1].state not in (QUEUED, RUNNING),
        timeout=TIMEOUT,
    )
    return statuses


def test_a_merge_runs_and_its_photo_comes_into_the_library(
    service, make_merge, drive, bracket, qtbot, wait_idle
):
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service)
    frames = [ids[path.name] for path in bracket]
    statuses = []
    merge.job_changed.connect(statuses.append)

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT) as rescanned:
        merge.merge_hdr(frames)
        qtbot.waitUntil(lambda: statuses and statuses[-1].state == DONE, timeout=TIMEOUT)

    assert statuses[-1].message == "Merged 3 photos into IMG_0001-HDR.tif"
    assert statuses[-1].name == "IMG_0001-HDR"
    assert {s.phase for s in statuses} >= {dbm.DECODING, dbm.MERGING}
    assert rescanned.args[0].state == DONE
    result = drive / "Photos" / "IMG_0001-HDR.tif"
    assert linear_tiff.is_linear(result)
    paths = [photo.path for photo in service.db.photos()]
    assert "Photos/IMG_0001-HDR.tif" in paths
    wait_idle(service)


def test_one_photo_chosen_brings_its_bracket(service, make_merge, drive, bracket, qtbot):
    ids = scanned(service, drive, qtbot)
    with_exposures(service, ids)
    merge = make_merge(service, start=False)

    found = merge.bracket([ids["IMG_0002.dng"]])

    assert found == [ids["IMG_0001.dng"], ids["IMG_0002.dng"], ids["IMG_0003.dng"]]
    assert [frame.exposure for frame in merge.frames(found)] == [
        "1/240 s · f/8 · ISO 100",
        "1/60 s · f/8 · ISO 100",
        "1/15 s · f/8 · ISO 100",
    ]
    # A photo in no bracket is itself, which cannot be merged alone
    assert merge.bracket([ids["a.jpg"]]) == [ids["a.jpg"]]
    assert merge.check([ids["a.jpg"]]) == "An HDR merge needs at least 2 photos"
    assert merge.check([ids["a.jpg"], ids["c.dng"]]) == "a.jpg is not a RAW file"
    assert merge.check(found) is None


def test_a_merge_queued_when_the_app_quit_runs_at_the_next_start(
    service, make_merge, drive, bracket, qtbot
):
    ids = scanned(service, drive, qtbot)
    job = hdr.queue(service.db, [ids[path.name] for path in bracket])

    merge = make_merge(service, start=False)
    statuses = []
    merge.job_changed.connect(statuses.append)
    merge.start()
    qtbot.waitUntil(lambda: statuses and statuses[-1].state == DONE, timeout=TIMEOUT)

    assert service.db.job(job).state == dbm.DONE
    assert (drive / "Photos" / "IMG_0001-HDR.tif").exists()


def test_a_cancel_stops_the_merge_and_leaves_no_file(
    service, make_merge, drive, bracket, qtbot, monkeypatch
):
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service)
    statuses = []
    merge.job_changed.connect(statuses.append)
    # Cancelled as soon as it starts, before its first step is done
    merge.job_changed.connect(lambda status: status.state == RUNNING and merge.cancel())

    merge.merge_hdr([ids[path.name] for path in bracket])
    qtbot.waitUntil(lambda: statuses and statuses[-1].state == CANCELLED, timeout=TIMEOUT)

    assert statuses[-1].state == CANCELLED
    assert sorted(p.name for p in (drive / "Photos").iterdir()) == [
        "IMG_0001.dng",
        "IMG_0002.dng",
        "IMG_0003.dng",
        "a.jpg",
        "b.jpg",
        "c.dng",
    ]


def test_merge_to_hdr_in_the_grid_asks_how_then_merges(
    service, make_merge, drive, bracket, qtbot, monkeypatch
):
    ids = scanned(service, drive, qtbot)
    with_exposures(service, ids)
    merge = make_merge(service, start=False)
    window = main_window.MainWindow(service, merge=merge)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.model.rowCount() == 6, timeout=TIMEOUT)
    asked = []

    def answer(dialog):
        asked.append(dialog)
        dialog.deghost_box.setCurrentIndex(dialog.deghost_box.findData("medium"))
        return MergeDialog.DialogCode.Accepted

    monkeypatch.setattr(MergeDialog, "exec", answer)
    queued = []
    monkeypatch.setattr(merge, "merge_hdr", lambda *args: queued.append(args))
    middle = next(
        row
        for row in range(window.model.rowCount())
        if window.model.photo_id(row) == ids["IMG_0002.dng"]
    )
    window.grid.setCurrentIndex(window.model.index(middle))

    window.merge_hdr_action.trigger()

    assert len(asked) == 1
    assert queued == [
        ([ids["IMG_0001.dng"], ids["IMG_0002.dng"], ids["IMG_0003.dng"]], "medium", True)
    ]


def test_a_photo_in_no_bracket_says_so(service, make_merge, drive, qtbot, monkeypatch):
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service, start=False)
    window = main_window.MainWindow(service, merge=merge)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    told = []
    monkeypatch.setattr(
        merging.QMessageBox, "information", lambda _parent, _title, text: told.append(text)
    )
    row = next(r for r in range(3) if window.model.photo_id(r) == ids["c.dng"])
    window.grid.setCurrentIndex(window.model.index(row))

    window.merge_hdr_action.trigger()

    assert told == [
        "c.dng is not in a bracket that Lumina can find. Choose the photos to merge in the grid."
    ]


@pytest.fixture
def turned(drive):
    """Three frames of a camera turning, 35 degrees apart."""
    around = surroundings()
    return [
        make_dng(
            drive / "Photos" / f"PAN_{i + 1:04d}.dng", mosaic=bracket_mosaic(view(around, yaw))
        )
        for i, yaw in enumerate((-35, 0, 35))
    ]


def test_a_panorama_runs_and_its_photo_comes_into_the_library(
    service, make_merge, drive, turned, qtbot, wait_idle
):
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service)
    statuses = []
    merge.job_changed.connect(statuses.append)

    with qtbot.waitSignal(service.job_ended, timeout=TIMEOUT) as rescanned:
        merge.stitch_panorama([ids[path.name] for path in turned], "spherical", crop=False)
        qtbot.waitUntil(lambda: statuses and statuses[-1].state == DONE, timeout=TIMEOUT)

    assert statuses[-1].message == "Stitched 3 photos into PAN_0001-pano.tif"
    assert statuses[-1].kind == dbm.PANORAMA
    assert {s.phase for s in statuses} >= {dbm.DECODING, dbm.STITCHING}
    assert describe_merge(statuses[-2]).startswith("Stitching PAN_0001-pano: ")
    assert rescanned.args[0].state == DONE
    assert linear_tiff.is_linear(drive / "Photos" / "PAN_0001-pano.tif")
    assert "Photos/PAN_0001-pano.tif" in [photo.path for photo in service.db.photos()]
    wait_idle(service)


def test_a_preview_shows_the_panorama_and_follows_the_choices(
    service, make_merge, drive, turned, qtbot, tmp_path
):
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service, start=False)
    preview = merge.preview_panorama([ids[path.name] for path in turned])
    pictures, said = [], []
    preview.picture.connect(pictures.append)
    preview.progress.connect(said.append)

    preview.start("cylindrical", True)
    qtbot.waitUntil(lambda: len(pictures) == 1, timeout=TIMEOUT)
    # Another projection: the photos are not read again
    preview.start("perspective", True)
    qtbot.waitUntil(lambda: len(pictures) == 2, timeout=TIMEOUT)

    assert said[:3] == [f"Reading photo {i} of 3…" for i in (1, 2, 3)]
    assert said.count("Reading photo 1 of 3…") == 1
    assert pictures[0].width() > pictures[0].height() > 0
    # A plane stretches the sides
    assert pictures[1].width() > pictures[0].width()
    folders = list((tmp_path / "work" / "preview").iterdir())
    assert len(folders) == 1
    preview.close()
    qtbot.waitUntil(lambda: not folders[0].exists(), timeout=TIMEOUT)
    # The photos are as they were
    assert sorted(path.name for path in (drive / "Photos").iterdir()) == sorted(
        [*(path.name for path in turned), "a.jpg", "b.jpg", "c.dng"]
    )


def test_a_preview_says_which_photo_does_not_fit(
    service, make_merge, drive, turned, qtbot, tmp_path
):
    make_dng(
        drive / "Photos" / "PAN_0004.dng",
        mosaic=bracket_mosaic(view(surroundings(), 180)),
    )
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service, start=False)
    preview = merge.preview_panorama([ids[f"PAN_000{i}.dng"] for i in (1, 2, 3, 4)])
    told = []
    preview.failed.connect(told.append)

    preview.start("cylindrical", True)
    qtbot.waitUntil(lambda: bool(told), timeout=TIMEOUT)

    assert told[0].startswith("PAN_0004.dng does not overlap the other photos")
    preview.close()


def test_a_preview_closed_at_once_leaves_nothing_behind(
    service, make_merge, drive, turned, qtbot, tmp_path
):
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service, start=False)
    preview = merge.preview_panorama([ids[path.name] for path in turned])
    pictures = []
    preview.picture.connect(pictures.append)

    preview.start("cylindrical", True)
    preview.close()
    qtbot.wait(300)

    assert pictures == []
    qtbot.waitUntil(lambda: not list((tmp_path / "work" / "preview").iterdir()), timeout=TIMEOUT)


def test_there_is_no_preview_of_photos_on_a_drive_that_is_not_connected(
    service, make_merge, drive, turned, qtbot, monkeypatch
):
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service, start=False)
    monkeypatch.setattr(service, "mounts", [])

    assert merge.preview_panorama([ids[path.name] for path in turned]) is None


def test_stitch_panorama_in_the_grid_asks_how_then_stitches(
    service, make_merge, drive, turned, qtbot, monkeypatch
):
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service, start=False)
    window = main_window.MainWindow(service, merge=merge)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.model.rowCount() == 6, timeout=TIMEOUT)
    asked = []

    def answer(dialog):
        asked.append([frame.name for frame in merge.panorama_frames(chosen)])
        dialog.projection_box.setCurrentIndex(dialog.projection_box.findData("perspective"))
        dialog.crop_box.setChecked(False)
        return PanoramaDialog.DialogCode.Accepted

    monkeypatch.setattr(PanoramaDialog, "exec", answer)
    queued = []
    monkeypatch.setattr(merge, "stitch_panorama", lambda *args: queued.append(args))
    chosen = [ids[path.name] for path in turned]
    selection = window.grid.selectionModel()
    for row in range(window.model.rowCount()):
        if window.model.photo_id(row) in chosen:
            selection.select(window.model.index(row), QItemSelectionModel.SelectionFlag.Select)

    window.stitch_panorama_action.trigger()

    assert asked == [["PAN_0001.dng", "PAN_0002.dng", "PAN_0003.dng"]]
    assert len(queued) == 1
    assert sorted(queued[0][0]) == sorted(chosen) and queued[0][1:] == ("perspective", False)


def test_one_photo_is_not_a_panorama(service, make_merge, drive, qtbot, monkeypatch):
    ids = scanned(service, drive, qtbot)
    merge = make_merge(service, start=False)
    window = main_window.MainWindow(service, merge=merge)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.model.rowCount() == 3, timeout=TIMEOUT)
    told = []
    monkeypatch.setattr(
        merging.QMessageBox, "information", lambda _parent, _title, text: told.append(text)
    )
    row = next(r for r in range(3) if window.model.photo_id(r) == ids["c.dng"])
    window.grid.setCurrentIndex(window.model.index(row))

    window.stitch_panorama_action.trigger()

    assert told == ["Choose the photos to stitch in the grid: two or more that overlap."]
    assert merge.check_panorama([ids["a.jpg"], ids["c.dng"]]) == (
        "c.dng is a RAW file, the others are not"
    )
