import time

import pytest
from conftest import TIMEOUT
from fakes import FakeEncoder, install_model

from lumina.core.dbtypes import QUEUED
from lumina.qt_models import library as lib
from lumina.qt_models.search import SearchService


@pytest.fixture
def model_root(tmp_path):
    """Where the model is not."""
    return tmp_path / "models"


@pytest.fixture
def make_search(qtbot, model_root):
    made = []

    def make(library, encoder=None):
        encoder = encoder or FakeEncoder()
        search = SearchService(library, model_root, encode=lambda _folder, paths: encoder(paths))
        made.append(search)
        return search

    yield make
    for search in made:
        search.close()
        for future in (search.index_future, search.download_future):
            if future is not None:
                future.result(timeout=TIMEOUT / 1000)


def wait_indexed(qtbot, search):
    qtbot.waitUntil(lambda: search.index_future is not None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not search.is_indexing(), timeout=TIMEOUT)


def test_without_the_model_nothing_is_indexed_or_fetched(qtbot, service, wait_idle, make_search):
    search = make_search(service)
    search.start()
    wait_idle(service)
    qtbot.wait(100)

    assert not search.is_ready() and not search.is_downloading()
    assert search.index_future is None and search.download_future is None
    assert search.download_size() > 700_000_000


def test_photos_scanned_are_indexed_and_similar_ones_found(
    qtbot, make_service, wait_idle, make_search, model_root, drive
):
    install_model(model_root)
    service = make_service(start=False)
    search = make_search(service)
    folder = service.add_folder(str(drive / "Photos"))
    service.start()
    search.start()
    wait_idle(service)
    wait_indexed(qtbot, search)

    photos = service.db.photos(lib.dbm.Filter(folder_id=folder))
    assert len(photos) == 3
    first = next(p for p in photos if p.path.endswith("a.jpg"))
    got = []
    search.similar(first.content_id, photos, got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)

    ranked = got[0]
    assert ranked[0][0].content_id == first.content_id
    assert len(ranked) == 3
    assert ranked[0][1] >= ranked[1][1] >= ranked[2][1]


def test_only_the_newest_search_answers(qtbot, service, wait_idle, make_search, model_root):
    install_model(model_root)
    search = make_search(service)
    wait_idle(service)
    got = []

    search.similar("nothing", [], lambda r: got.append(("first", r)))
    search.similar("nothing", [], lambda r: got.append(("second", r)))
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    qtbot.wait(100)

    assert got == [("second", [])]


def test_a_quit_while_indexing_leaves_it_queued(
    qtbot, make_service, wait_idle, make_search, model_root, drive
):
    install_model(model_root)
    entered = []

    class Slow(FakeEncoder):
        def __call__(self, paths):
            # On the indexing thread: long enough for the quit to come in the middle
            entered.append(paths)
            time.sleep(0.3)
            return super().__call__(paths)

    service = make_service(start=False)
    search = make_search(service, Slow())
    service.add_folder(str(drive / "Photos"))
    service.start()
    wait_idle(service)
    qtbot.waitUntil(lambda: bool(entered), timeout=TIMEOUT)
    job = search._indexing

    search.close()
    search.index_future.result(timeout=TIMEOUT / 1000)

    assert service.db.job(job).state == QUEUED


def test_a_scan_cancel_leaves_indexing_alone(
    qtbot, make_service, wait_idle, make_search, model_root, drive
):
    install_model(model_root)
    service = make_service(start=False)
    make_search(service)
    job = service.db.add_job("embed", None)

    service.cancel()

    assert service.db.job(job).state == QUEUED
