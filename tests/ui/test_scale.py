"""A library of 100,000 photos: what the window does on its own thread stays short."""

import json
import time

import pytest

from lumina.core import db as dbm
from lumina.core.edit.params import Params
from lumina.qt_models.library_model import LibraryModel

ROWS = 100_000


@pytest.fixture
def big(make_service):
    """A service whose library holds ROWS photos of one folder, a third of them edited."""
    service = make_service(start=False)
    library = service.db
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    edit = Params(exposure=0.5).to_json()
    with library.db:
        library.db.executemany(
            "INSERT INTO photos (volume_id, folder_id, path, size, mtime_ns, file_time, "
            "captured, content_id, available, added_at, metadata_state, model) "
            "VALUES (?, ?, ?, 1, 1, '2020-01-01 00:00:00.000000', ?, ?, 1, 0, 1, '90D')",
            [
                (
                    volume,
                    folder,
                    f"p/{n:07}.jpg",
                    f"2020-01-01 00:{n // 60 % 60:02}:{n % 60:02}.{n:06}",
                    f"c{n}",
                )
                for n in range(ROWS)
            ],
        )
        ids = [row[0] for row in library.db.execute("SELECT id FROM photos")]
        library.db.executemany(
            "INSERT INTO edits (photo_id, copy, params, updated_at) VALUES (?, 0, ?, 0)",
            [(photo, edit) for photo in ids[::3]],
        )
    return service, ids


def timed(call):
    began = time.perf_counter()
    result = call()
    return time.perf_counter() - began, result


def test_the_query_and_the_filters_of_a_big_library_are_quick(big):
    service, _ids = big

    took, rows = timed(lambda: service.db.photos())
    assert len(rows) == ROWS and took < 3.0
    took, found = timed(lambda: service.db.photos(dbm.Filter(year="2020")))
    assert len(found) == ROWS and took < 3.0
    took, _ = timed(service.db.choices)
    assert took < 0.5


def test_taking_in_the_rows_costs_the_window_little_and_an_edit_nothing_much(big):
    service, ids = big
    model = LibraryModel(service)
    rows = service.db.photos()

    took, _ = timed(lambda: model._loaded(model._generation, rows, True))
    assert model.rowCount() == ROWS
    # On the UI thread: a second at most, here on a slow machine's allowance
    assert took < 1.5, f"taking in {ROWS} rows took {took:.2f} s"

    photo = ids[ROWS // 2]
    took, _ = timed(lambda: service.save_edit(photo, Params(exposure=1.0).to_json()))
    service.flush()
    assert json.loads(model.edit(model.row_of(photo)))["exposure"] == 1.0
    assert took < 0.25, f"an edit saved took {took:.2f} s on the window's thread"
