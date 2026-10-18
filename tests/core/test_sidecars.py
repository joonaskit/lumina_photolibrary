import errno
import os
import stat

import pytest
from fakes import FakeExifTool
from photos import make_jpeg

from lumina.core import db as dbm
from lumina.core import library as scanning
from lumina.core import sidecars, xmp
from lumina.core.db import Library
from lumina.core.volumes import Mount, locate

DRIVE = "uuid:TEST-DRIVE"
PARAMS = '{"version": 1, "exposure": 0.5, "note": "a \\"quoted\\" <tag> & more\\nnew line ä"}'


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeExifTool.reset()


@pytest.fixture
def drive(tmp_path):
    path = tmp_path / "drive"
    (path / "Photos").mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        make_jpeg(path / "Photos" / name)
    return path


def mounted(path):
    return [Mount(DRIVE, str(path), "/", "exfat", "/dev/sdz1", "Photos drive")]


def open_library(path, drive):
    library = Library(path)
    key, inside = locate(drive / "Photos", mounted(drive))
    folder = library.add_folder(key, inside, "Photos drive", str(drive))
    return library, folder


@pytest.fixture
def library(tmp_path, drive):
    library, folder = open_library(tmp_path / "library.sqlite3", drive)
    library.folder_id = folder
    yield library
    library.close()


def scan(library, drive, **kwargs):
    job = library.add_job(scanning.SCAN, library.folder_id)
    scanning.scan(library, job, library.folder_id, mounted(drive), exiftool=FakeExifTool, **kwargs)
    return library.job(job)


def ids(library):
    return {path.rsplit("/", 1)[-1]: state[0] for path, state in library.photo_states(1).items()}


def state(**fields):
    return xmp.SidecarState(updated=1000.5, **fields)


# The file format


def test_a_sidecar_round_trips_with_awkward_text():
    original = state(
        flag=-1,
        rating=4,
        keywords=("Places > Finland > Helsinki", 'Cat & <Dog "quoted"', "Käpylä"),
        edits=((0, PARAMS), (2, '{"version": 1}')),
    )

    assert xmp.parse(xmp.build(original)) == original


def test_other_programs_get_the_stars_and_every_keyword_name():
    data = xmp.build(state(rating=3, keywords=("Places > Finland > Helsinki", "Cat", "places")))

    text = data.decode()
    assert 'xmp:Rating="3"' in text
    # Each name once, however many keywords it is in, for programs that know no levels
    assert text.count("<rdf:li>Places</rdf:li>") == 1
    for name in ("Finland", "Helsinki", "Cat"):
        assert f"<rdf:li>{name}</rdf:li>" in text


def test_a_state_with_nothing_is_a_small_sidecar_still(tmp_path):
    assert xmp.parse(xmp.build(state())) == state()


def test_a_file_from_another_program_is_not_ours():
    other = b'<?xml version="1.0"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>'

    with pytest.raises(xmp.ForeignSidecar):
        xmp.parse(other, "x.xmp")


@pytest.mark.parametrize(
    "data",
    [
        xmp.build(state()).replace(b"?>", b'?><!DOCTYPE a [<!ENTITY b "c">]>', 1),
        xmp.build(state())[:80],
        xmp.NAMESPACE.encode() + b" not xml",
        xmp.build(state()).replace(b'lumina:Version="1"', b'lumina:Version="9"'),
        xmp.build(state()).replace(b'lumina:Updated="1000.5"', b'lumina:Updated="soon"'),
    ],
)
def test_a_damaged_sidecar_of_ours_is_an_error_naming_the_file(data):
    with pytest.raises(xmp.SidecarError) as caught:
        xmp.parse(data, "/p/a.jpg.xmp")

    assert caught.value.path == "/p/a.jpg.xmp"


# Writing


def test_writing_leaves_the_photo_alone_and_no_part_behind(tmp_path):
    photo = make_jpeg(tmp_path / "a.jpg")
    before = (photo.read_bytes(), photo.stat().st_mtime_ns)

    mtime = xmp.write(str(photo), state(rating=2))

    assert (photo.read_bytes(), photo.stat().st_mtime_ns) == before
    assert sorted(os.listdir(tmp_path)) == ["a.jpg", "a.jpg.xmp"]
    assert mtime == (tmp_path / "a.jpg.xmp").stat().st_mtime_ns
    assert xmp.read(str(photo)) == state(rating=2)


def test_a_sidecar_of_ours_is_replaced(tmp_path):
    photo = make_jpeg(tmp_path / "a.jpg")
    xmp.write(str(photo), state(rating=2))

    xmp.write(str(photo), xmp.SidecarState(updated=2000.0, rating=5))

    assert xmp.read(str(photo)).rating == 5


def test_another_programs_xmp_is_never_overwritten(tmp_path):
    photo = make_jpeg(tmp_path / "a.jpg")
    other = tmp_path / "a.jpg.xmp"
    other.write_bytes(b"<x:xmpmeta>darktable's</x:xmpmeta>")

    with pytest.raises(xmp.ForeignSidecar) as caught:
        xmp.write(str(photo), state())

    assert caught.value.path == str(other)
    assert other.read_bytes() == b"<x:xmpmeta>darktable's</x:xmpmeta>"
    assert sorted(os.listdir(tmp_path)) == ["a.jpg", "a.jpg.xmp"]


def test_a_file_that_appears_while_writing_is_not_written_over(tmp_path, monkeypatch):
    photo = make_jpeg(tmp_path / "a.jpg")
    path = tmp_path / "a.jpg.xmp"
    real = xmp._existing

    def appears(name):
        found = real(name)
        path.write_bytes(b"theirs")
        return found

    monkeypatch.setattr(xmp, "_existing", appears)

    with pytest.raises(xmp.ForeignSidecar):
        xmp.write(str(photo), state())

    assert path.read_bytes() == b"theirs"
    assert sorted(os.listdir(tmp_path)) == ["a.jpg", "a.jpg.xmp"]


def test_a_filesystem_without_hard_links_is_written_too(tmp_path, monkeypatch):
    photo = make_jpeg(tmp_path / "a.jpg")

    def no_links(*_args):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)

    xmp.write(str(photo), state(rating=1))

    assert xmp.read(str(photo)).rating == 1
    assert sorted(os.listdir(tmp_path)) == ["a.jpg", "a.jpg.xmp"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_folder_that_cannot_be_written_to_is_an_error_naming_the_sidecar(tmp_path):
    folder = tmp_path / "card"
    folder.mkdir()
    photo = make_jpeg(folder / "a.jpg")
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(xmp.SidecarError) as caught:
            xmp.write(str(photo), state())
    finally:
        folder.chmod(stat.S_IRWXU)

    assert caught.value.path == str(folder / "a.jpg.xmp")


def test_no_sidecar_reads_as_none(tmp_path):
    assert xmp.read(str(make_jpeg(tmp_path / "a.jpg"))) is None


# What notes a change


def test_a_change_of_flag_stars_keywords_or_edit_makes_the_sidecar_out_of_date(library, drive):
    scan(library, drive)
    a, b, c = ids(library).values()
    assert library.sidecar_pending() == [] and library.count_sidecars_pending() == 0

    library.set_flags({a: 1})
    library.set_ratings({b: 3})
    library.set_photo_keywords({c: tuple(library.ensure_keywords(["Cat"]).values())})

    assert [row[0] for row in library.sidecar_pending()] == [a, b, c]
    library.save_edit(a, PARAMS)
    library.sidecar_written(a, library.sidecar_state(a).updated, 1)
    assert [row[0] for row in library.sidecar_pending()] == [b, c]
    # Changed again after it was written
    library.save_edit(a, '{"version": 1}')
    assert a in [row[0] for row in library.sidecar_pending()]
    # Back to the original, or a copy, or a keyword renamed or deleted
    library.sidecar_written(a, library.sidecar_state(a).updated, 1)
    library.delete_edit(a)
    assert a in [row[0] for row in library.sidecar_pending()]
    library.sidecar_written(c, library.sidecar_state(c).updated, 1)
    library.rename_keyword("Cat", "Dog")
    assert c in [row[0] for row in library.sidecar_pending()]


def test_photos_never_marked_have_no_sidecar_to_write(library, drive):
    scan(library, drive)

    assert library.sidecar_pending() == []


def test_the_state_of_a_photo_is_what_the_sidecar_holds(library, drive):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    kept = library.ensure_keywords(["Places > Helsinki", "Cat"])
    library.set_flags({a: -1})
    library.set_ratings({a: 4})
    library.set_photo_keywords({a: tuple(kept.values())})
    library.save_edit(a, PARAMS)
    library.new_copy(a, '{"version": 1}')

    held = library.sidecar_state(a)

    assert (held.flag, held.rating) == (-1, 4)
    assert held.keywords == ("Cat", "Places > Helsinki")
    assert held.edits == ((0, PARAMS), (1, '{"version": 1}'))
    assert held.updated > 0


# Writing the pending ones


def test_pending_sidecars_are_written_beside_the_photos_and_not_again(library, drive):
    scan(library, drive)
    a, b, _c = ids(library).values()
    library.set_ratings({a: 5})
    library.set_flags({b: 1})

    result = sidecars.write_pending(library, mounted(drive))

    assert (result.written, result.failures, result.waiting) == (2, [], 0)
    assert sorted(os.listdir(drive / "Photos")) == [
        "a.jpg",
        "a.jpg.xmp",
        "b.jpg",
        "b.jpg.xmp",
        "c.jpg",
    ]
    assert library.sidecar_pending() == []
    assert sidecars.write_pending(library, mounted(drive)).written == 0


def test_a_photo_on_a_drive_that_is_not_connected_waits(library, drive):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    library.set_ratings({a: 5})

    result = sidecars.write_pending(library, [])

    assert (result.written, result.waiting) == (0, 1)
    assert [row[0] for row in library.sidecar_pending()] == [a]
    assert sidecars.write_pending(library, mounted(drive)).written == 1


def test_a_missing_photo_waits_and_keeps_its_state(library, drive):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    library.set_ratings({a: 5})
    library.mark_missing([a])

    result = sidecars.write_pending(library, mounted(drive))

    assert result.waiting == 1 and not (drive / "Photos" / "a.jpg.xmp").exists()


def test_one_that_cannot_be_written_is_named_and_the_rest_go_on(library, drive):
    scan(library, drive)
    a, b, _c = ids(library).values()
    library.set_ratings({a: 5, b: 2})
    theirs = drive / "Photos" / "a.jpg.xmp"
    theirs.write_bytes(b"another program's")

    result = sidecars.write_pending(library, mounted(drive))

    assert result.written == 1
    assert [path for path, _why in result.failures] == [str(theirs)]
    assert theirs.read_bytes() == b"another program's"
    assert (drive / "Photos" / "b.jpg.xmp").exists()


def test_writing_stops_when_asked_and_resumes_from_the_database(library, drive):
    scan(library, drive)
    library.set_ratings(dict.fromkeys(ids(library).values(), 3))
    stops = iter([False, False, True, True, True, True])

    result = sidecars.write_pending(library, mounted(drive), lambda: next(stops, True))

    assert 0 < result.written < 3 and result.stopped
    sidecars.write_pending(library, mounted(drive))
    assert library.sidecar_pending() == []
    assert len(list((drive / "Photos").glob("*.xmp"))) == 3


# Reading back


def test_a_library_is_rebuilt_from_the_photos_and_their_sidecars(tmp_path, drive, library):
    scan(library, drive)
    a, b, _c = ids(library).values()
    kept = library.ensure_keywords(["Places > Helsinki", "Cat"])
    library.set_flags({a: -1})
    library.set_ratings({a: 4})
    library.set_photo_keywords({a: tuple(kept.values())})
    library.save_edit(a, PARAMS)
    library.new_copy(a, '{"version": 1, "exposure": 1}')
    library.set_ratings({b: 2})
    sidecars.write_pending(library, mounted(drive))
    photo_bytes = {p.name: p.read_bytes() for p in (drive / "Photos").glob("*.jpg")}

    # A new library that has never heard of any of it
    fresh, folder = open_library(tmp_path / "fresh.sqlite3", drive)
    fresh.folder_id = folder
    try:
        job = scan(fresh, drive, read_sidecars=True)

        assert "2 sidecars read in" in job.message
        rebuilt = fresh.photos()
        by_name = {p.path.rsplit("/", 1)[-1]: p for p in rebuilt}
        first = by_name["a.jpg"]
        assert (first.flag, first.rating) == (-1, 4)
        paths = {i: p for i, p, _c in fresh.keyword_list()}
        assert sorted(paths[k] for k in first.keywords) == ["Cat", "Places > Helsinki"]
        assert first.edit == PARAMS
        assert fresh.copies(first.id) == [0, 1]
        assert by_name["b.jpg"].rating == 2 and by_name["c.jpg"].rating == 0
        # Nothing to write back: it is as the sidecars said
        assert fresh.sidecar_pending() == []
    finally:
        fresh.close()
    assert {p.name: p.read_bytes() for p in (drive / "Photos").glob("*.jpg")} == photo_bytes


def test_without_the_setting_sidecars_are_not_read(tmp_path, drive, library):
    scan(library, drive)
    library.set_ratings({ids(library)["a.jpg"]: 4})
    sidecars.write_pending(library, mounted(drive))
    fresh, folder = open_library(tmp_path / "fresh.sqlite3", drive)
    fresh.folder_id = folder
    try:
        scan(fresh, drive)

        assert {p.rating for p in fresh.photos()} == {0}
    finally:
        fresh.close()


def test_the_library_wins_when_its_change_is_newer_and_is_written_to_the_sidecar(library, drive):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    path = str(drive / "Photos" / "a.jpg")
    xmp.write(path, xmp.SidecarState(updated=10.0, rating=1))
    library.set_ratings({a: 5})

    job = scan(library, drive, read_sidecars=True)

    assert library.photo(a).rating == 5
    assert "older than the library's own, kept" in job.message
    assert [row[0] for row in library.sidecar_pending()] == [a]
    sidecars.write_pending(library, mounted(drive))
    assert xmp.read(path).rating == 5


def test_a_newer_sidecar_wins_and_replaces_the_edits(library, drive):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    library.save_edit(a, '{"version": 1, "old": true}')
    library.new_copy(a, '{"version": 1, "copy": true}')
    path = str(drive / "Photos" / "a.jpg")
    newer = xmp.SidecarState(
        updated=library.sidecar_state(a).updated + 100,
        flag=1,
        rating=2,
        keywords=("Cat",),
        edits=((0, '{"version": 1, "new": true}'),),
    )
    xmp.write(path, newer)

    job = scan(library, drive, read_sidecars=True)

    assert "1 sidecar read in" in job.message
    assert library.edit(a) == '{"version": 1, "new": true}'
    # Both sides had changed: what was replaced stays, as copies after the sidecar's
    assert library.copies(a) == [0, 1, 2]
    assert {library.edit(a, 1), library.edit(a, 2)} == {
        '{"version": 1, "old": true}',
        '{"version": 1, "copy": true}',
    }
    held = library.sidecar_state(a)
    assert (held.flag, held.rating, held.keywords) == (1, 2, ("Cat",))
    assert library.sidecar_pending() == []


def test_a_newer_sidecar_replacing_what_was_already_written_keeps_no_extra_copies(library, drive):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    path = str(drive / "Photos" / "a.jpg")
    library.save_edit(a, '{"version": 1, "mine": true}')
    sidecars.write_pending(library, mounted(drive))
    newer = xmp.SidecarState(
        updated=library.sidecar_state(a).updated + 100,
        edits=((0, '{"version": 1, "theirs": true}'),),
    )
    xmp.write(path, newer)

    scan(library, drive, read_sidecars=True)

    assert library.edit(a) == '{"version": 1, "theirs": true}'
    assert library.copies(a) == [0]


@pytest.mark.parametrize(
    ("edit", "why"),
    [
        ("{not json", "damaged"),
        ("[1, 2]", "damaged"),
        ('{"exposure": 1}', "damaged"),
        ('{"version": 9999}', "newer Lumina"),
    ],
)
def test_a_sidecar_with_a_bad_edit_is_named_and_the_librarys_edits_stay(library, drive, edit, why):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    library.save_edit(a, '{"version": 1, "mine": true}')
    newer = xmp.SidecarState(
        updated=library.sidecar_state(a).updated + 100,
        rating=4,
        edits=((0, edit),),
    )
    xmp.write(str(drive / "Photos" / "a.jpg"), newer)

    job = scan(library, drive, read_sidecars=True)

    assert job.state == dbm.DONE
    [(failed, message)] = library.failures(job.id)
    assert failed == str(drive / "Photos" / "a.jpg.xmp") and why in message
    assert library.edit(a) == '{"version": 1, "mine": true}'
    assert library.photo(a).rating == 0


def test_a_sidecar_is_read_once_until_the_file_changes(library, drive):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    path = str(drive / "Photos" / "a.jpg")
    xmp.write(path, xmp.SidecarState(updated=library.sidecar_state(a).updated + 10, rating=3))
    assert "1 sidecar read in" in scan(library, drive, read_sidecars=True).message

    # The user clears it in the library; the same file is not read over it again
    library.set_ratings({a: 0})
    again = scan(library, drive, read_sidecars=True)

    assert "sidecar" not in (again.message or "") and library.photo(a).rating == 0


def test_a_foreign_or_damaged_sidecar_never_stops_a_scan(library, drive):
    scan(library, drive)
    a, b, _c = ids(library).values()
    (drive / "Photos" / "a.jpg.xmp").write_bytes(b"<x:xmpmeta>darktable</x:xmpmeta>")
    (drive / "Photos" / "b.jpg.xmp").write_bytes(xmp.NAMESPACE.encode() + b" <broken")

    job = scan(library, drive, read_sidecars=True)

    assert job.state == dbm.DONE
    assert [row[0] for row in library.failures(job.id)] == [str(drive / "Photos" / "b.jpg.xmp")]
    # Another program's is left as it is, here and when written
    assert (drive / "Photos" / "a.jpg.xmp").read_bytes() == b"<x:xmpmeta>darktable</x:xmpmeta>"
    assert library.photo(a).rating == 0


def test_a_sidecar_that_is_gone_is_written_again(library, drive):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    library.set_ratings({a: 4})
    sidecars.write_pending(library, mounted(drive))
    os.unlink(drive / "Photos" / "a.jpg.xmp")

    scan(library, drive, read_sidecars=True)

    assert [row[0] for row in library.sidecar_pending()] == [a]


def test_a_sidecar_read_in_marks_nothing_as_the_librarys_own_change_to_rewrite(library, drive):
    scan(library, drive)
    a = ids(library)["a.jpg"]
    xmp.write(str(drive / "Photos" / "a.jpg"), xmp.SidecarState(updated=5e9, rating=2))

    scan(library, drive, read_sidecars=True)

    assert library.photo(a).changed_at == 5e9 and library.sidecar_pending() == []


# The schema


def test_a_schema_9_library_notes_what_was_marked_before(tmp_path):
    import sqlite3

    path = tmp_path / "library.sqlite3"
    with Library(path) as library:
        volume = library.volume_id("uuid:A")
        folder = library.add_folder("uuid:A", "p")
        library.write_scan(
            folder, volume, new=[(f"p/{n}.jpg", 1, 1, "2020-01-01 00:00:00.000000") for n in "abc"]
        )
        a, b, _c = (library.photo_at("uuid:A", f"p/{n}.jpg") for n in "abc")
        library.set_flags({a: 1})
        library.save_edit(b, "{}")
    raw = sqlite3.connect(path)
    raw.executescript(
        "UPDATE photos SET changed_at = NULL;"
        "ALTER TABLE photos DROP COLUMN changed_at; ALTER TABLE photos DROP COLUMN sidecar_at; "
        "ALTER TABLE photos DROP COLUMN sidecar_mtime_ns; "
        "DROP INDEX photos_stack; DROP INDEX photos_folder_time; "
        "ALTER TABLE photos DROP COLUMN stack_id; "
        "ALTER TABLE photos DROP COLUMN stack_position; DROP TABLE stacks; "
        "DROP TABLE tether_items; DROP TABLE tether_sessions; "
        "DROP TABLE export_items; DROP TABLE export_jobs; "
        "PRAGMA user_version = 9;"
    )
    raw.close()

    with Library(path) as library:
        assert [row[0] for row in library.sidecar_pending()] == [a, b]
