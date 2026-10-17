"""Finding near-duplicates and copies across the library."""

from types import SimpleNamespace

import numpy as np

from lumina.core import similar
from lumina.core.db import PICKED


def photo(number, content=None, flag=0, rating=0):
    return SimpleNamespace(
        id=number,
        content_id=content or f"c{number}",
        taken=f"2024-05-01 12:00:{number:02d}",
        flag=flag,
        rating=rating,
    )


def vec(*head):
    vector = np.zeros(4, np.float32)
    vector[: len(head)] = head
    return vector / np.linalg.norm(vector)


def ids(groups):
    return [g.ids for g in groups]


def test_photos_that_look_alike_are_a_group_and_the_others_are_not():
    rows = [photo(1), photo(2), photo(3)]
    vectors = {"c1": vec(1, 0), "c2": vec(1, 0.05), "c3": vec(0, 1)}

    groups = similar.find(rows, vectors)

    assert ids(groups) == [(1, 2)]
    assert not groups[0].exact


def test_a_lower_threshold_joins_more():
    rows = [photo(1), photo(2)]
    vectors = {"c1": vec(1, 0), "c2": vec(1, 0.5)}

    assert similar.find(rows, vectors) == []
    assert ids(similar.find(rows, vectors, similarity=0.85)) == [(1, 2)]


def test_copies_of_one_file_are_a_group_even_without_a_vector():
    rows = [photo(1, "same"), photo(2), photo(3, "same")]

    groups = similar.find(rows, {})

    assert ids(groups) == [(1, 3)]
    assert groups[0].exact


def test_a_copy_joins_what_its_vector_is_close_to_and_the_group_is_not_exact():
    rows = [photo(1, "same"), photo(2, "same"), photo(3)]
    vectors = {"same": vec(1, 0), "c3": vec(1, 0.05)}

    groups = similar.find(rows, vectors)

    assert ids(groups) == [(1, 2, 3)]
    assert not groups[0].exact


def test_a_chain_of_slow_changes_is_one_group():
    rows = [photo(i + 1) for i in range(3)]
    vectors = {"c1": vec(1, 0), "c2": vec(1, 0.3), "c3": vec(1, 0.6)}

    assert ids(similar.find(rows, vectors, similarity=0.95)) == [(1, 2, 3)]


def test_the_best_is_first_and_groups_go_largest_first():
    rows = [photo(1), photo(2, flag=PICKED), photo(3), photo(4), photo(5)]
    vectors = {
        "c1": vec(1, 0),
        "c2": vec(1, 0),
        "c3": vec(0, 1),
        "c4": vec(0, 1),
        "c5": vec(0, 1),
    }

    assert ids(similar.find(rows, vectors)) == [(3, 4, 5), (2, 1)]


def test_blocks_of_the_matrix_find_pairs_across_them(monkeypatch):
    monkeypatch.setattr(similar, "_BLOCK", 2)
    rows = [photo(i) for i in range(1, 6)]
    vectors = {f"c{i}": vec(i % 2, 1 - i % 2) for i in range(1, 6)}

    assert ids(similar.find(rows, vectors)) == [(1, 3, 5), (2, 4)]
