import shutil

import numpy as np
import pytest
from fakes import FakeEncoder, FakeExifTool, colour_vector
from photos import make_jpeg

from lumina.core import db as dbm
from lumina.core import embed, indexing
from lumina.core import library as scanning
from lumina.core.db import Filter, Library
from lumina.core.search import Index
from lumina.core.volumes import Mount, locate

DRIVE = "uuid:TEST-DRIVE"
MODEL = embed.MODEL.name
RED, GREEN, BLUE = (200, 30, 30), (30, 200, 30), (30, 30, 200)


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeExifTool.reset()


@pytest.fixture
def drive(tmp_path):
    path = tmp_path / "drive"
    (path / "Photos").mkdir(parents=True)
    make_jpeg(path / "Photos" / "red.jpg", colour=RED)
    make_jpeg(path / "Photos" / "green.jpg", colour=GREEN)
    make_jpeg(path / "Photos" / "blue.jpg", colour=BLUE)
    return path


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


def mounted(path):
    return [Mount(DRIVE, str(path), "/", "exfat", "/dev/sdz1", "Photos drive")]


def scanned(library, drive):
    key, inside = locate(drive / "Photos", mounted(drive))
    folder = library.add_folder(key, inside, "Photos drive", str(drive))
    job = library.add_job(dbm.SCAN, folder)
    assert scanning.scan(library, job, folder, mounted(drive), exiftool=FakeExifTool) == dbm.DONE
    return folder


def run(library, mounts, encode, **kwargs):
    job = library.add_job(indexing.EMBED, None)
    state = indexing.index(library, job, mounts, encode, **kwargs)
    return state, library.job(job)


def names(found):
    return [photo.path.rsplit("/", 1)[-1] for photo, _cosine in found]


def test_every_photo_gets_a_vector_and_can_be_found(library, drive):
    scanned(library, drive)

    state, job = run(library, mounted(drive), FakeEncoder())

    assert state == dbm.DONE and job.message == "3 photos indexed for search"
    index = Index()
    assert index.refresh(library) == 3
    found = index.rank(library.photos(), colour_vector(np.array(RED) / 255))
    assert names(found)[0] == "red.jpg"
    assert found[0][1] > 0.99


def test_what_is_indexed_is_not_encoded_again(library, drive):
    scanned(library, drive)
    run(library, mounted(drive), FakeEncoder())
    encoder = FakeEncoder()

    state, _job = run(library, mounted(drive), encoder)

    assert state == dbm.DONE and encoder.calls == []


def test_a_copy_of_a_photo_is_encoded_once(library, drive):
    shutil.copy(drive / "Photos" / "red.jpg", drive / "Photos" / "red copy.jpg")
    scanned(library, drive)
    encoder = FakeEncoder()

    run(library, mounted(drive), encoder)

    assert sum(len(batch) for batch in encoder.calls) == 3
    index = Index()
    index.refresh(library)
    found = index.rank(library.photos(), colour_vector(np.array(RED) / 255))
    assert sorted(names(found)[:2]) == ["red copy.jpg", "red.jpg"]


def test_a_cancel_stops_and_the_next_run_does_the_rest(library, drive):
    scanned(library, drive)
    batches = []

    state, job = run(
        library,
        mounted(drive),
        FakeEncoder(),
        batch=1,
        should_stop=lambda: len(batches) >= 1,
        report=lambda _phase, _done, _total, changed: changed and batches.append(1),
    )

    assert state == dbm.CANCELLED and job.state == dbm.CANCELLED
    assert len(library.embeddings(MODEL)) == 1
    encoder = FakeEncoder()
    assert run(library, mounted(drive), encoder)[0] == dbm.DONE
    assert sum(len(batch) for batch in encoder.calls) == 2


def test_an_unreadable_photo_is_named_and_not_tried_again(library, drive):
    (drive / "Photos" / "broken.jpg").write_bytes(b"not a photo")
    scanned(library, drive)

    state, job = run(library, mounted(drive), FakeEncoder())

    assert state == dbm.DONE
    assert job.message == "3 photos indexed for search, 1 could not be read"
    assert [path for path, _m in library.failures(job.id)] == [str(drive / "Photos" / "broken.jpg")]
    encoder = FakeEncoder()
    run(library, mounted(drive), encoder)
    assert encoder.calls == []


def test_the_photo_that_crashed_the_worker_is_the_one_named(library, drive):
    scanned(library, drive)
    green = str(drive / "Photos" / "green.jpg")

    state, job = run(library, mounted(drive), FakeEncoder(crashes=[green]))

    assert state == dbm.DONE
    assert [path for path, _m in library.failures(job.id)] == [green]
    assert len(library.embeddings(MODEL)) == 2


def test_a_broken_encoder_stops_the_job_and_marks_nothing(library, drive):
    scanned(library, drive)

    state, job = run(library, mounted(drive), FakeEncoder(broken=True))

    assert state == dbm.FAILED_JOB
    assert "the model could not be loaded" in job.message
    assert library.count_pending_embeddings(MODEL, {DRIVE}) == 3


def test_photos_on_an_unplugged_drive_wait(library, drive):
    scanned(library, drive)
    encoder = FakeEncoder()

    state, job = run(library, [], encoder)

    assert state == dbm.DONE and encoder.calls == []
    assert library.count_pending_embeddings(MODEL, {DRIVE}) == 3


def test_a_photo_gone_while_indexing_is_not_marked_unreadable(library, drive):
    scanned(library, drive)
    (drive / "Photos" / "blue.jpg").unlink()

    state, job = run(library, mounted(drive), FakeEncoder())

    assert state == dbm.DONE and job.failures == 0
    assert "1 not reachable" in job.message
    assert library.count_pending_embeddings(MODEL, {DRIVE}) == 1


def test_a_photo_added_while_indexing_is_not_passed_over(library, drive):
    scanned(library, drive)
    with library.db:
        library.db.execute("UPDATE photos SET captured = '2024-01-01' WHERE path LIKE '%blue%'")
    (drive / "Photos" / "blue.jpg").unlink()
    inner = FakeEncoder()
    calls = []

    def encode(paths):
        calls.append(paths)
        if len(calls) == 1:
            # A scan finds a new, newer photo while the job runs
            make_jpeg(drive / "Photos" / "new.jpg", colour=(90, 90, 90))
            scanned(library, drive)
            with library.db:
                library.db.execute(
                    "UPDATE photos SET captured = '2030-01-01' WHERE path LIKE '%new%'"
                )
        return inner(paths)

    state, job = run(library, mounted(drive), encode, batch=1)

    assert state == dbm.DONE
    # The new photo has its vector in this run, not the next
    assert library.count_pending_embeddings(MODEL, {DRIVE}) == 0
    assert len(library.embeddings(MODEL)) == 3


def test_a_missing_photo_keeps_its_vector_and_a_forgotten_folder_drops_it(library, drive):
    folder = scanned(library, drive)
    run(library, mounted(drive), FakeEncoder())
    (drive / "Photos" / "red.jpg").unlink()
    scanned(library, drive)

    assert len(library.embeddings(MODEL)) == 3
    library.remove_folder(folder)
    assert library.embeddings(MODEL) == []


def test_the_newest_photos_are_indexed_first(library, drive):
    scanned(library, drive)
    with library.db:
        library.db.execute("UPDATE photos SET captured = '2024-01-01' WHERE path LIKE '%green%'")

    pending = library.pending_embeddings(MODEL, {DRIVE}, 10)

    assert pending[0][2].endswith("green.jpg")


def test_search_follows_the_filter(library, drive):
    scanned(library, drive)
    run(library, mounted(drive), FakeEncoder())
    with library.db:
        library.db.execute("UPDATE photos SET model = 'R5' WHERE path LIKE '%red%'")
    index = Index()
    index.refresh(library)

    found = index.rank(
        library.photos(Filter(model="Canon EOS 90D")), colour_vector(np.array(RED) / 255)
    )

    assert "red.jpg" not in names(found) and len(found) == 2


def test_the_index_reads_only_what_is_new(library, drive):
    scanned(library, drive)
    index = Index()
    run(
        library,
        mounted(drive),
        FakeEncoder(),
        batch=1,
        should_stop=lambda: len(index) > 0 or bool(index.refresh(library)),
    )

    assert len(index) == 1
    run(library, mounted(drive), FakeEncoder())
    assert index.refresh(library) == 2 and len(index) == 3
    assert index.refresh(library) == 0


def test_rank_is_best_first_keeps_ties_in_order_and_leaves_out_photos_without_vectors():
    from collections import namedtuple

    Row = namedtuple("Row", "id content_id")
    index = Index()
    vectors = {"a": [1.0, 0.0], "b": [0.6, 0.8], "c": [1.0, 0.0], "d": [0.0, 1.0]}
    index._grow(len(vectors))
    for row, (cid, vector) in enumerate(vectors.items()):
        index._matrix[row, :2] = vector
        index._rows[cid] = row
    index._count = len(vectors)
    query = np.zeros(embed.DIMENSIONS, np.float32)
    query[0] = 1.0
    photos = [Row(1, "d"), Row(2, "c"), Row(3, None), Row(4, "b"), Row(5, "a"), Row(6, "x")]

    found = index.rank(photos, query)

    # "c" and "a" are equally good: in the order they were given
    assert [(photo.id, cosine) for photo, cosine in found] == [
        (2, 1.0),
        (5, 1.0),
        (4, pytest.approx(0.6)),
        (1, 0.0),
    ]
    assert all(type(cosine) is float for _photo, cosine in found)
    assert [photo.id for photo, _ in index.rank(photos, query, floor=0.5)] == [2, 5, 4]
    assert index.rank([], query) == []
