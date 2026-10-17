"""Finding bursts: photos a moment apart that look alike."""

from types import SimpleNamespace

import numpy as np

from lumina.core import bursts
from lumina.core.db import PICKED, REJECTED


def photo(number, second, vector, flag=0, rating=0, stack=None, content=None):
    row = SimpleNamespace(
        id=number,
        content_id=content or f"c{number}",
        taken=f"2024-05-01 12:00:{second:06.3f}",
        flag=flag,
        rating=rating,
        stack=stack,
    )
    return row, (row.content_id, np.array(vector, np.float32))


def run(*made, **options):
    rows = [row for row, _pair in made]
    vectors = dict(pair for _row, pair in made)
    return bursts.find(rows, vectors, **options)


SKY, SEA = (1.0, 0.0), (0.0, 1.0)


def test_frames_close_in_time_and_alike_are_a_burst_in_time_order():
    assert run(photo(1, 0, SKY), photo(2, 0.1, SKY), photo(3, 0.2, SKY)) == [[1, 2, 3]]


def test_a_gap_in_time_or_a_different_picture_ends_the_burst():
    made = [
        photo(1, 0, SKY),
        photo(2, 0.5, SKY),
        photo(3, 1.0, SEA),
        photo(4, 1.4, SEA),
        photo(5, 30, SEA),
        photo(6, 30.2, SEA),
    ]

    assert run(*made) == [[1, 2], [3, 4], [5, 6]]


def test_a_photo_alone_is_no_burst():
    assert run(photo(1, 0, SKY), photo(2, 10, SKY)) == []


def test_the_run_follows_the_last_frame_so_a_slow_change_stays_together():
    slow = [(1.0, 0.0), (0.9, 0.44), (0.7, 0.71)]

    assert run(*(photo(i + 1, i, v) for i, v in enumerate(slow)), similarity=0.85) == [[1, 2, 3]]


def test_a_stacked_photo_or_one_without_a_vector_is_left_alone_and_ends_the_run():
    stacked = photo(2, 1, SKY, stack=7)
    unread = photo(5, 4, SKY)[0]
    made = [photo(1, 0, SKY), stacked, photo(3, 2, SKY), photo(4, 3, SKY), photo(6, 5, SKY)]
    rows = [row for row, _pair in made] + [unread]
    vectors = dict(pair for _row, pair in made)

    assert bursts.find(rows, vectors) == [[3, 4]]


def test_the_best_photo_is_on_top_a_thumbs_up_then_the_stars_then_the_first():
    assert run(photo(1, 0, SKY), photo(2, 1, SKY, flag=PICKED), photo(3, 2, SKY, rating=5)) == [
        [2, 1, 3]
    ]
    assert run(photo(1, 0, SKY, rating=2), photo(2, 1, SKY, rating=4), photo(3, 2, SKY)) == [
        [2, 1, 3]
    ]
    assert run(photo(1, 0, SKY, flag=REJECTED), photo(2, 1, SKY), photo(3, 2, SKY)) == [[2, 1, 3]]
    assert run(photo(1, 0, SKY), photo(2, 1, SKY)) == [[1, 2]]


def test_the_order_of_the_rows_given_does_not_matter():
    made = [photo(3, 2, SKY), photo(1, 0, SKY), photo(2, 1, SKY)]

    assert run(*made) == [[1, 2, 3]]
