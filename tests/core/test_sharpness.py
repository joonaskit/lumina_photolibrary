"""Sharpness: the score, the pick of a stack, the job that scores the library."""

import numpy as np
import pytest
from fakes import FakeExifTool
from photos import make_jpeg
from PIL import Image, ImageFilter

from lumina.core import db as dbm
from lumina.core import focus, sharpness
from lumina.core import library as scanning
from lumina.core.db import Library
from lumina.core.errors import PhotoError
from lumina.core.sharpness import Member
from lumina.core.volumes import Mount, locate

DRIVE = "uuid:TEST-DRIVE"


def textured(path, blur=0.0, size=(640, 480)):
    """A JPEG of random blocks, softened by `blur` pixels."""
    random = np.random.default_rng(1)
    cells = random.integers(0, 256, (size[1] // 8, size[0] // 8, 3), dtype=np.uint8)
    image = Image.fromarray(cells).resize(size, Image.Resampling.NEAREST)
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    image.save(path, "JPEG", quality=95)
    return path


def test_a_blurred_copy_scores_below_the_sharp_one(tmp_path):
    sharp = textured(tmp_path / "sharp.jpg")
    soft = textured(tmp_path / "soft.jpg", blur=3)

    assert sharpness.score_file(sharp) > 4 * sharpness.score_file(soft)


def test_a_blurred_copy_ranks_below_the_sharp_one_at_any_size(tmp_path):
    for size in ((320, 240), (2048, 1536)):
        sharp = textured(tmp_path / f"sharp{size[0]}.jpg", size=size)
        soft = textured(tmp_path / f"soft{size[0]}.jpg", blur=size[0] / 200, size=size)

        assert sharpness.score_file(sharp) > 2 * sharpness.score_file(soft)


def test_a_sharp_subject_on_a_soft_background_scores_as_sharp(tmp_path):
    whole = textured(tmp_path / "whole.jpg")
    image = Image.open(textured(tmp_path / "bg.jpg", blur=6))
    image.paste(Image.open(whole).crop((0, 0, 200, 150)), (0, 0))
    image.save(tmp_path / "subject.jpg", "JPEG", quality=95)

    assert sharpness.score_file(tmp_path / "subject.jpg") > 0.5 * sharpness.score_file(whole)


def test_a_flat_photo_scores_zero(tmp_path):
    assert sharpness.score_file(make_jpeg(tmp_path / "flat.jpg", size=(200, 100))) == 0


def test_score_files_names_a_file_it_cannot_read(tmp_path):
    good = textured(tmp_path / "good.jpg")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a photo")

    scores = sharpness.score_files([str(good), str(bad)])

    assert isinstance(scores[0], float)
    assert isinstance(scores[1], PhotoError) and str(bad) in scores[1].path


def test_a_photo_is_blurry_beside_a_much_sharper_one_only():
    assert sharpness.is_blurry(40, 100)
    assert not sharpness.is_blurry(60, 100)
    assert not sharpness.is_blurry(None, 100)
    assert not sharpness.is_blurry(40, None)


def member(id, score, flag=0, rating=0, taken="2024-01-01 10:00:00"):
    return Member(id, taken, flag, rating, score)


def test_the_sharpest_photo_is_picked():
    assert sharpness.pick([member(1, 10), member(2, 90), member(3, 50)]) == 2


def test_within_a_few_percent_a_thumbs_up_then_stars_then_the_first_taken_decide():
    near = [member(1, 100), member(2, 97, flag=1), member(3, 98, rating=5)]
    assert sharpness.pick(near) == 2
    assert sharpness.pick([member(1, 100), member(3, 98, rating=5)]) == 3
    late = [member(1, 100, taken="2024-01-01 10:00:02"), member(2, 99)]
    assert sharpness.pick(late) == 2


def test_a_much_sharper_photo_wins_over_a_starred_one():
    assert sharpness.pick([member(1, 100), member(2, 80, flag=1, rating=5)]) == 1


def test_a_stack_with_a_photo_not_scored_is_not_picked():
    assert sharpness.pick([member(1, 100), member(2, None)]) is None
    assert sharpness.pick([]) is None


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


@pytest.fixture
def stack(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    names = [f"p/{letter}.jpg" for letter in "abc"]
    library.write_scan(folder, volume, new=[(n, 1, 1, "2020-01-01 00:00:00.000000") for n in names])
    ids = [library.photo_at("uuid:A", name) for name in names]
    for photo in ids:
        library.db.execute("UPDATE photos SET content_id = ? WHERE id = ?", (f"c{photo}", photo))
    library.db.commit()
    library.make_stack(ids)
    return library.photo(ids[0]).stack_id, ids


def test_pick_best_puts_the_sharpest_on_top_and_keeps_the_order_of_the_rest(library, stack):
    stack_id, (a, b, c) = stack
    library.write_sharpness([(f"c{a}", 10.0), (f"c{b}", 20.0), (f"c{c}", 90.0)])

    picked, skipped = library.pick_best([stack_id])

    assert (picked, skipped) == ({stack_id: c}, 0)
    assert library.stack_members(stack_id) == [c, a, b]


def test_pick_best_leaves_a_stack_with_an_unscored_photo_and_counts_it(library, stack):
    stack_id, (a, b, c) = stack
    library.write_sharpness([(f"c{a}", 10.0), (f"c{c}", 90.0)])

    assert library.pick_best([stack_id]) == ({}, 1)
    assert library.stack_members(stack_id) == [a, b, c]


def test_pick_best_does_not_touch_the_files_or_flags(library, stack):
    stack_id, ids = stack
    library.write_sharpness([(f"c{i}", float(n)) for n, i in enumerate(ids)])

    library.pick_best([stack_id])

    assert set(library.flags(ids).values()) == {dbm.UNFLAGGED}


def test_the_rows_carry_their_score(library, stack):
    _stack_id, (a, b, _c) = stack
    library.write_sharpness([(f"c{a}", 12.5)], [(f"c{b}", "broken")])

    scores = {p.id: p.sharpness for p in library.photos()}

    assert scores[a] == 12.5 and scores[b] is None


# The job


def mounted(path):
    return [Mount(DRIVE, str(path), "/", "exfat", "/dev/sdz1", "Photos drive")]


@pytest.fixture
def drive(tmp_path):
    path = tmp_path / "drive"
    (path / "Photos").mkdir(parents=True)
    textured(path / "Photos" / "sharp.jpg")
    textured(path / "Photos" / "soft.jpg", blur=3)
    return path


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeExifTool.reset()


def scanned(library, drive):
    key, inside = locate(drive / "Photos", mounted(drive))
    folder = library.add_folder(key, inside, "Photos drive", str(drive))
    job = library.add_job(dbm.SCAN, folder)
    assert scanning.scan(library, job, folder, mounted(drive), exiftool=FakeExifTool) == dbm.DONE


def run(library, mounts, score, **kwargs):
    job = library.add_job(focus.SHARPNESS, None)
    state = focus.run(library, job, mounts, score, **kwargs)
    return state, library.job(job)


def test_the_job_scores_every_photo_once(library, drive):
    scanned(library, drive)

    state, job = run(library, mounted(drive), sharpness.score_files)

    assert state == dbm.DONE and job.message == "2 photos scored for sharpness"
    scores = {p.path.rsplit("/", 1)[-1]: p.sharpness for p in library.photos()}
    assert scores["sharp.jpg"] > scores["soft.jpg"] > 0
    assert run(library, mounted(drive), sharpness.score_files)[1].message == (
        "0 photos scored for sharpness"
    )


def test_a_stopped_job_resumes_with_what_is_left(library, drive):
    scanned(library, drive)
    calls = []

    def stop_after_one():
        return len(calls) >= 1

    def score(paths):
        calls.append(paths)
        return sharpness.score_files(paths)

    state, _job = run(library, mounted(drive), score, should_stop=stop_after_one, batch=1)

    assert state == dbm.CANCELLED
    assert sum(p.sharpness is not None for p in library.photos()) == 1
    calls.clear()
    state, job = run(library, mounted(drive), score, batch=1)
    assert state == dbm.DONE and job.message == "1 photo scored for sharpness"
    assert len(calls) == 1
    assert all(p.sharpness is not None for p in library.photos())


def test_a_photo_that_cannot_be_read_is_named_and_not_tried_again(library, drive):
    scanned(library, drive)
    (drive / "Photos" / "soft.jpg").write_bytes(b"broken")

    state, job = run(library, mounted(drive), sharpness.score_files)

    assert state == dbm.DONE
    assert "1 could not be read" in job.message
    assert [path for path, _message in library.failures(job.id)] == [
        str(drive / "Photos" / "soft.jpg")
    ]
    assert run(library, mounted(drive), sharpness.score_files)[1].message == (
        "0 photos scored for sharpness"
    )


def test_a_batch_that_raises_is_tried_one_photo_at_a_time(library, drive):
    scanned(library, drive)

    def score(paths):
        if len(paths) > 1:
            raise RuntimeError("worker died")
        return sharpness.score_files(paths)

    state, job = run(library, mounted(drive), score)

    assert state == dbm.DONE and job.message == "2 photos scored for sharpness"


def test_photos_on_a_drive_not_connected_wait(library, drive):
    scanned(library, drive)

    state, job = run(library, [], sharpness.score_files)

    assert state == dbm.DONE and job.message == "0 photos scored for sharpness"
    assert all(p.sharpness is None for p in library.photos())
