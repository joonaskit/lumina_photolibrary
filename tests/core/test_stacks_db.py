"""Stacks of photos: the database, the sidecar that carries them, and removing a folder."""

import pytest

from lumina.core import xmp
from lumina.core.db import Library


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


@pytest.fixture
def photos(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    names = [f"p/{letter}.jpg" for letter in "abcd"]
    library.write_scan(folder, volume, new=[(n, 1, 1, "2020-01-01 00:00:00.000000") for n in names])
    library.folder = folder
    return [library.photo_at("uuid:A", name) for name in names]


def stacked(library):
    """{photo id: (stack id, position)} of the photos in a stack."""
    return {p.id: (p.stack, p.stack_position) for p in library.photos() if p.stack is not None}


def test_photos_are_stacked_in_the_order_given_with_the_first_on_top(library, photos):
    a, b, c, _d = photos

    stack = library.make_stack([b, a, c])

    assert stacked(library) == {b: (stack, 0), a: (stack, 1), c: (stack, 2)}
    assert library.stack_members(stack) == [b, a, c]
    assert all(p.stack_collapsed for p in library.photos() if p.stack)


def test_fewer_than_two_photos_make_no_stack(library, photos):
    assert library.make_stack([photos[0]]) is None
    assert library.make_stack([photos[0], photos[0]]) is None
    assert stacked(library) == {}


def test_a_photo_stacked_again_leaves_its_old_stack(library, photos):
    a, b, c, d = photos
    first = library.make_stack([a, b, c])

    second = library.make_stack([c, d])

    assert stacked(library) == {a: (first, 0), b: (first, 1), c: (second, 0), d: (second, 1)}


def test_a_stack_left_with_one_photo_is_no_stack(library, photos):
    a, b, c, d = photos
    library.make_stack([a, b])

    library.make_stack([b, c, d])

    assert a not in stacked(library)
    assert set(stacked(library)) == {b, c, d}


def test_unstacking_takes_the_whole_stack_apart(library, photos):
    a, b, c, _d = photos
    library.make_stack([a, b, c])

    library.unstack([b])

    assert stacked(library) == {}
    assert library.db.execute("SELECT COUNT(*) FROM stacks").fetchone()[0] == 0


def test_a_new_top_moves_to_the_front_and_the_others_keep_their_order(library, photos):
    a, b, c, _d = photos
    stack = library.make_stack([a, b, c])

    library.set_stack_top(c)

    assert library.stack_members(stack) == [c, a, b]


def test_collapsing_and_expanding_changes_no_sidecar(library, photos):
    a, b, _c, _d = photos
    stack = library.make_stack([a, b])
    library.db.execute("UPDATE photos SET changed_at = 1.0")
    library.db.commit()

    library.set_stacks_collapsed([stack], False)

    assert all(not p.stack_collapsed for p in library.photos() if p.stack)
    assert {r[0] for r in library.db.execute("SELECT changed_at FROM photos")} == {1.0}


def test_stacking_notes_every_photo_it_touches_for_the_sidecars(library, photos):
    a, b, _c, d = photos

    library.make_stack([a, b])

    changed = {row[0]: row[1] for row in library.db.execute("SELECT id, changed_at FROM photos")}
    assert changed[a] and changed[b]
    assert changed[d] is None


def test_a_sidecar_carries_the_stack_and_puts_it_together_again(library, photos):
    a, b, c, _d = photos
    library.make_stack([a, b, c])
    states = {photo: library.sidecar_state(photo) for photo in (a, b, c)}
    keys = {state.stack[0] for state in states.values()}
    assert len(keys) == 1
    assert [states[p].stack[1] for p in (a, b, c)] == [0, 1, 2]
    again = {photo: xmp.parse(xmp.build(state)) for photo, state in states.items()}
    assert all(again[p].stack == states[p].stack for p in again)
    # A library that lost its database: the photos know nothing of the stack
    library.unstack([a])
    library.db.execute("UPDATE photos SET changed_at = NULL")
    library.db.commit()
    assert stacked(library) == {}

    for photo in (a, b, c):
        library.apply_sidecar(photo, again[photo], 1)

    members = stacked(library)
    assert set(members) == {a, b, c}
    assert len({stack for stack, _place in members.values()}) == 1
    assert [members[p][1] for p in (a, b, c)] == [0, 1, 2]


def test_a_sidecar_without_a_stack_takes_a_photo_out_of_it(library, photos):
    a, b, _c, _d = photos
    library.make_stack([a, b])
    state = library.sidecar_state(a)
    newer = xmp.SidecarState(updated=(state.updated or 0) + 10)

    assert library.apply_sidecar(a, newer, 1)

    assert a not in stacked(library)


def test_an_old_sidecar_format_without_stacks_still_reads():
    data = xmp.build(xmp.SidecarState(updated=1.0, flag=1))

    assert b"Stack" not in data
    assert xmp.parse(data).stack is None


def test_removing_a_folder_dissolves_stacks_it_leaves_with_one_photo(library, photos):
    a, b, c, _d = photos
    other = library.add_folder("uuid:A", "q")
    library.write_scan(
        other, library.volume_id("uuid:A"), new=[("q/e.jpg", 1, 1, "2020-01-01 00:00:00.000000")]
    )
    e = library.photo_at("uuid:A", "q/e.jpg")
    library.make_stack([a, e])
    library.make_stack([b, c])

    library.remove_folder(other)

    assert set(stacked(library)) == {b, c}
    assert library.db.execute("SELECT COUNT(*) FROM stacks").fetchone()[0] == 1


def test_a_missing_file_stays_in_its_stack(library, photos):
    a, b, _c, _d = photos
    library.make_stack([a, b])
    library.db.execute("UPDATE photos SET available = 0 WHERE id = ?", (a,))
    library.db.commit()

    assert set(stacked(library)) == {a, b}


def test_a_photo_taken_out_leaves_the_rest_stacked_and_the_next_becomes_the_top(library, photos):
    a, b, c, _d = photos
    stack = library.make_stack([a, b, c])

    library.leave_stack([a])

    assert set(stacked(library)) == {b, c}
    assert library.stack_members(stack) == [b, c]
    assert library.db.execute("SELECT changed_at FROM photos WHERE id = ?", (a,)).fetchone()[0]


def test_taking_out_of_a_stack_of_two_dissolves_it(library, photos):
    a, b, _c, _d = photos
    library.make_stack([a, b])

    library.leave_stack([b])

    assert stacked(library) == {}
    assert library.db.execute("SELECT COUNT(*) FROM stacks").fetchone()[0] == 0


def test_taking_out_a_photo_that_is_in_no_stack_changes_nothing(library, photos):
    a, b, c, d = photos
    library.make_stack([a, b])

    library.leave_stack([c, d])

    assert set(stacked(library)) == {a, b}
    assert (
        library.db.execute("SELECT changed_at FROM photos WHERE id = ?", (c,)).fetchone()[0] is None
    )
