"""Scoring photos for sharpness, picking the sharpest of a stack, and the blurry filter."""

import os

import pytest
from conftest import TIMEOUT
from PySide6.QtWidgets import QMessageBox
from test_flags import names, open_window
from test_stacks import stack_all, wait_for_rows

from lumina.core.dbtypes import REJECTED, UNFLAGGED
from lumina.qt_models.library_model import BLURRY
from lumina.qt_models.sharpness import SharpnessService

# a.jpg is the softest, b.jpg the sharpest
SCORES = {"a.jpg": 10.0, "b.jpg": 90.0, "c.dng": 60.0}


def score(paths):
    return [SCORES[os.path.basename(path)] for path in paths]


@pytest.fixture
def scoring(qtbot, service):
    made = SharpnessService(service, score=score)
    yield made
    made.close()
    if made.jobs.future is not None:
        made.jobs.future.result(timeout=TIMEOUT / 1000)


def scored_window(qtbot, service, scoring, drive):
    window = open_window(qtbot, service, drive / "Photos")
    scoring.score()
    qtbot.waitUntil(lambda: scoring.jobs.future is not None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not scoring.is_scoring(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    qtbot.waitUntil(
        lambda: all(p.sharpness is not None for p in window.model.photo_rows()), timeout=TIMEOUT
    )
    return window


def test_the_photos_of_a_folder_are_scored_and_the_grid_reads_the_scores(
    qtbot, service, scoring, drive
):
    window = scored_window(qtbot, service, scoring, drive)

    by_name = {os.path.basename(p.path): p.sharpness for p in window.model.photo_rows()}
    assert by_name == SCORES


def test_picking_the_sharpest_puts_it_on_top_of_the_stack(qtbot, service, scoring, drive):
    window = scored_window(qtbot, service, scoring, drive)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    assert window.pick_best_action.isEnabled()

    window.stacking.pick_best()

    qtbot.waitUntil(lambda: names(window) == ["b.jpg"], timeout=TIMEOUT)


def test_nothing_is_picked_while_a_photo_is_not_scored(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    top = names(window)

    window.stacking.pick_best()
    service.flush()
    qtbot.wait(100)

    assert names(window) == top
    assert "left as they were" in window.statusBar().currentMessage()


def test_the_rest_of_a_stack_is_rejected_after_asking_and_undo_puts_the_flags_back(
    qtbot, service, scoring, drive, monkeypatch
):
    window = scored_window(qtbot, service, scoring, drive)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    window.stacking.pick_best()
    qtbot.waitUntil(lambda: names(window) == ["b.jpg"], timeout=TIMEOUT)
    asked = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: asked.append(args[2]) or QMessageBox.StandardButton.Yes,
    )

    window.stacking.reject_rest()

    service.settle()
    flags = {os.path.basename(p.path): p.flag for p in service.db.photos()}
    assert flags == {"a.jpg": REJECTED, "b.jpg": UNFLAGGED, "c.dng": REJECTED}
    assert "2 photos" in asked[0]
    assert all(os.path.exists(drive / "Photos" / name) for name in SCORES)
    window.culling.undo_flags()
    service.settle()
    assert set(p.flag for p in service.db.photos()) == {UNFLAGGED}


def test_nothing_is_rejected_when_the_user_says_no(qtbot, service, scoring, drive, monkeypatch):
    window = scored_window(qtbot, service, scoring, drive)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)

    window.stacking.reject_rest()

    service.settle()
    assert set(p.flag for p in service.db.photos()) == {UNFLAGGED}


def test_the_blurry_filter_shows_the_photos_much_softer_than_the_best_of_their_stack(
    qtbot, service, scoring, drive
):
    window = scored_window(qtbot, service, scoring, drive)
    # Alone, a photo is not blurry beside anything
    window.model.set_sharpness_filter(BLURRY)
    assert window.model.rowCount() == 0
    window.model.set_sharpness_filter(None)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)

    window.model.set_sharpness_filter(BLURRY)

    # 10 is under half of 90; 60 is not
    assert names(window) == ["a.jpg"]
    window.model.set_sharpness_filter(None)
    wait_for_rows(qtbot, window, 1)


def test_the_blurry_choice_is_in_the_filters_and_cleared_with_them(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    filters = window.filters

    filters.sharpness.setCurrentIndex(filters.sharpness.findData(BLURRY))

    assert filters.sharpness_value() == BLURRY
    assert window.model.sharpness_filter == BLURRY
    filters.clear()
    assert filters.sharpness_value() is None
    assert window.model.sharpness_filter is None
