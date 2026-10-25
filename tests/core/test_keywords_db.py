import json
import sqlite3

import pytest
from photos import make_jpeg

from lumina.core import db as dbm
from lumina.core import keywords
from lumina.core import metadata as metadatam
from lumina.core.db import Filter, Library
from lumina.core.metadata import Metadata
from lumina.core.smart import SmartQuery


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


@pytest.fixture
def photos(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    names = ["p/a.jpg", "p/b.jpg", "p/c.jpg"]
    library.write_scan(folder, volume, new=[(n, 1, 1, "2020-01-01 00:00:00.000000") for n in names])
    return [library.photo_at("uuid:A", name) for name in names]


# Names


def test_a_keyword_is_a_path_of_names():
    assert keywords.parse("  places>Finland |  Helsinki ") == "places > Finland > Helsinki"
    assert keywords.parse("Cat") == "Cat"
    assert keywords.parse(" > ") is None and keywords.parse("") is None
    assert keywords.parse(None) is None


def test_the_levels_of_a_path():
    path = "Places > Finland > Helsinki"

    assert keywords.leaf(path) == "Helsinki"
    assert keywords.parent(path) == "Places > Finland"
    assert keywords.parent("Places") is None
    assert keywords.ancestors(path) == ["Places", "Places > Finland"]
    assert keywords.within(path, "places > finland")
    assert not keywords.within("Places > Finlandia", "Places > Finland")
    assert keywords.within("Places", "Places")


# The database


def test_a_new_keyword_makes_the_levels_above_it(library):
    ids = library.ensure_keywords(["Places > Finland > Helsinki"])

    assert [path for _id, path, _count in library.keyword_list()] == [
        "Places",
        "Places > Finland",
        "Places > Finland > Helsinki",
    ]
    assert ids["Places > Finland > Helsinki"] in {i for i, _p, _c in library.keyword_list()}


def test_a_keyword_is_one_whatever_the_case(library):
    first = library.ensure_keywords(["Cat"])["Cat"]

    assert library.ensure_keywords(["cat"])["cat"] == first
    assert [path for _i, path, _c in library.keyword_list()] == ["Cat"]


def test_photos_are_given_keywords_and_counted_under_each_level(library, photos):
    ids = library.ensure_keywords(["Places > Finland > Helsinki", "Places > Finland > Espoo"])
    helsinki, espoo = ids["Places > Finland > Helsinki"], ids["Places > Finland > Espoo"]
    a, b, c = photos

    library.set_photo_keywords({a: (helsinki, espoo), b: (espoo,)})

    counts = {path: count for _i, path, count in library.keyword_list()}
    # a counts once under Finland although it has two keywords there
    assert counts == {
        "Places": 2,
        "Places > Finland": 2,
        "Places > Finland > Espoo": 2,
        "Places > Finland > Helsinki": 1,
    }
    assert library.photo_keywords([a, b, c]) == {
        a: tuple(sorted((helsinki, espoo))),
        b: (espoo,),
        c: (),
    }
    assert {row.id: row.keywords for row in library.photos()}[b] == (espoo,)


def test_setting_keywords_replaces_what_a_photo_had(library, photos):
    ids = library.ensure_keywords(["Cat", "Dog"])

    library.set_photo_keywords({photos[0]: (ids["Cat"],)})
    library.set_photo_keywords({photos[0]: (ids["Dog"],)})

    assert library.photo_keywords([photos[0]])[photos[0]] == (ids["Dog"],)


def test_renaming_a_keyword_moves_what_is_below_it_and_keeps_the_photos(library, photos):
    ids = library.ensure_keywords(["Trip > Spain"])
    library.set_photo_keywords({photos[0]: (ids["Trip > Spain"],)})

    assert library.rename_keyword("Trip", "Holiday")

    assert [path for _i, path, _c in library.keyword_list()] == ["Holiday", "Holiday > Spain"]
    assert library.photo_keywords([photos[0]])[photos[0]] == (ids["Trip > Spain"],)
    assert not library.rename_keyword("Nowhere", "Anywhere")


def test_renaming_onto_another_keyword_is_refused(library):
    library.ensure_keywords(["A", "B"])

    with pytest.raises(ValueError):
        library.rename_keyword("A", "B")

    assert [path for _i, path, _c in library.keyword_list()] == ["A", "B"]


def test_deleting_a_keyword_takes_it_and_those_below_off_the_photos(library, photos):
    ids = library.ensure_keywords(["Trip > Spain", "Cat"])
    library.set_photo_keywords({photos[0]: (ids["Trip > Spain"], ids["Cat"])})

    assert library.delete_keyword("Trip") == 2

    assert library.photo_keywords([photos[0]])[photos[0]] == (ids["Cat"],)


def test_keywords_in_the_file_are_a_starting_set_taken_in_once(library, photos):
    a = photos[0]
    data = Metadata(keywords=("Places > Helsinki", "Cat"))

    library.write_metadata(read=[(a, "cid", data)])

    names = {i: path for i, path, _c in library.keyword_list()}
    assert sorted(names[k] for k in library.photo_keywords([a])[a]) == ["Cat", "Places > Helsinki"]
    # The user takes one off; reading the file again (it changed) does not put it back
    cat = next(i for i, path in names.items() if path == "Cat")
    library.set_photo_keywords({a: ()})
    library.write_metadata(read=[(a, "cid", data)])
    assert library.photo_keywords([a])[a] == ()
    assert cat in names


def test_keywords_and_collections_survive_a_missing_file(library, photos):
    a = photos[0]
    ids = library.ensure_keywords(["Cat"])
    library.set_photo_keywords({a: (ids["Cat"],)})
    box = library.add_collection("Best")
    library.add_to_collection(box, [a])

    library.mark_missing([a])

    assert library.photo_keywords([a])[a] == (ids["Cat"],)
    assert library.collection_photo_ids(box) == {a}
    row = next(r for r in library.photos() if r.id == a)
    assert not row.available and row.keywords == (ids["Cat"],)


def test_removing_a_folder_forgets_its_photos_from_keywords_and_collections(library, photos):
    ids = library.ensure_keywords(["Cat"])
    library.set_photo_keywords({photos[0]: (ids["Cat"],)})
    box = library.add_collection("Best")
    library.add_to_collection(box, photos)

    library.remove_folder(library.folders()[0].id)

    assert library.collection_photo_ids(box) == set()
    assert [count for _i, _p, count in library.keyword_list()] == [0]
    assert [c.name for c in library.collections()] == ["Best"]


# Collections


def test_a_manual_collection_holds_photos_once(library, photos):
    box = library.add_collection("  Best   of 2020 ")

    assert library.add_to_collection(box, photos[:2]) == 2
    assert library.add_to_collection(box, photos[1:]) == 1

    assert [(c.name, c.count, c.query) for c in library.collections()] == [
        ("Best of 2020", 3, None)
    ]
    assert {p.id for p in library.photos(Filter(collection_id=box))} == set(photos)

    library.remove_from_collection(box, [photos[0]])
    assert library.collection_photo_ids(box) == set(photos[1:])


def test_collection_names_are_unique_whatever_the_case(library):
    library.add_collection("Best")

    with pytest.raises(ValueError):
        library.add_collection("best")
    with pytest.raises(ValueError):
        library.add_collection("   ")
    other = library.add_collection("Other")
    with pytest.raises(ValueError):
        library.rename_collection(other, "BEST")
    library.rename_collection(other, "Second")
    assert [c.name for c in library.collections()] == ["Best", "Second"]


def test_a_smart_collection_holds_no_photos_and_deleting_a_collection_keeps_the_photos(
    library, photos
):
    smart = library.add_collection("Picks", SmartQuery(flag=1).to_json())
    manual = library.add_collection("Manual")
    assert library.add_to_collection(smart, photos) == 0
    library.add_to_collection(manual, photos)

    library.delete_collection(manual)

    assert len(library.photos()) == 3
    assert [(c.name, c.count) for c in library.collections()] == [("Picks", None)]


def test_a_collection_filter_combines_with_the_others(library, photos):
    library.write_metadata(
        read=[
            (photos[0], "1", Metadata(model="A")),
            (photos[1], "2", Metadata(model="B")),
        ]
    )
    box = library.add_collection("Box")
    library.add_to_collection(box, photos[:2])

    shown = library.photos(Filter(collection_id=box, model="B"))

    assert [p.id for p in shown] == [photos[1]]


# Smart collections


def test_a_smart_query_round_trips_and_ignores_what_it_does_not_know():
    query = SmartQuery(flag=1, rating=3, keyword="Cat", year="2020", text="beach")

    assert SmartQuery.from_json(query.to_json()) == query
    later = json.dumps({"version": 9, "flag": 1, "something new": True})
    assert SmartQuery.from_json(later) == SmartQuery(flag=1)
    assert SmartQuery().is_empty() and not query.is_empty()


def test_a_smart_query_picks_photos_by_flag_stars_edit_and_keyword(library, photos):
    ids = library.ensure_keywords(["Places > Helsinki", "Places > Espoo", "Cat"])
    a, b, c = photos
    library.set_flags({a: 1, b: 1})
    library.set_ratings({a: 4, b: 2, c: 5})
    library.save_edit(a, "{}")
    library.set_photo_keywords({a: (ids["Places > Helsinki"],), b: (ids["Places > Espoo"],)})
    rows = {row.id: row for row in library.photos()}
    paths = {i: path for i, path, _c in library.keyword_list()}

    def picks(**fields):
        query = SmartQuery(**fields)
        return sorted(p for p, row in rows.items() if query.passes(row, paths))

    assert picks(flag=1) == [a, b]
    assert picks(flag=1, rating=3) == [a]
    assert picks(rating=0) == []
    assert picks(edit="edited") == [a]
    assert picks(edit="unedited") == [b, c]
    assert picks(keyword="places") == [a, b]
    assert picks(keyword="Places > Espoo") == [b]
    assert picks(keyword="Cat") == []
    assert picks() == [a, b, c]


def test_a_smart_query_gives_the_filter_the_database_runs(library, photos):
    library.write_metadata(read=[(photos[0], "1", Metadata(model="A", lens="L"))])

    query = SmartQuery(model="A", lens="L", flag=1)

    assert query.to_filter() == Filter(model="A", lens="L")
    assert [p.id for p in library.photos(query.to_filter())] == [photos[0]]


# Migration


def test_a_schema_8_library_gets_keywords_and_collections_with_everything_kept(tmp_path):
    from lumina.core import db as dbm

    path = tmp_path / "library.sqlite3"
    with Library(path) as library:
        volume = library.volume_id("uuid:A")
        folder = library.add_folder("uuid:A", "p")
        library.write_scan(folder, volume, new=[("p/a.jpg", 1, 1, "2020-01-01 00:00:00.000000")])
        photo = library.photo_at("uuid:A", "p/a.jpg")
        library.set_flags({photo: 1})
    raw = sqlite3.connect(path)
    raw.executescript(
        "DROP TABLE photo_keywords; DROP TABLE keywords; DROP TABLE collection_photos; "
        "DROP TABLE collections; ALTER TABLE photos DROP COLUMN keywords_read; "
        "ALTER TABLE photos DROP COLUMN changed_at; ALTER TABLE photos DROP COLUMN sidecar_at; "
        "ALTER TABLE photos DROP COLUMN sidecar_mtime_ns; "
        "DROP INDEX photos_stack; DROP INDEX photos_folder_time; "
        "ALTER TABLE photos DROP COLUMN stack_id; "
        "ALTER TABLE photos DROP COLUMN stack_position; DROP TABLE stacks; "
        "DROP TABLE tether_items; DROP TABLE tether_sessions; "
        "DROP TABLE export_items; DROP TABLE export_jobs; DROP TABLE sharpness; "
        "PRAGMA user_version = 8;"
    )
    raw.close()

    with Library(path) as library:
        assert library.db.execute("PRAGMA user_version").fetchone()[0] == dbm.SCHEMA_VERSION
        assert [(p.id, p.flag, p.keywords) for p in library.photos()] == [(photo, 1, ())]
        library.add_collection("New")


# Reading the file's keywords


def test_keywords_come_out_of_exiftool_entries():
    entry = {
        "HierarchicalSubject": ["Places|Finland|Helsinki", "Cat"],
        "Keywords": ["Helsinki", "cat", "Dog", 2020],
        "Subject": "Dog",
    }

    assert metadatam._keywords(entry) == (
        "Places > Finland > Helsinki",
        "Cat",
        "Dog",
        "2020",
    )
    assert metadatam._keywords({"Keywords": "single"}) == ("single",)
    assert metadatam._keywords({}) == ()


def test_exiftool_reads_the_keywords_of_a_file(exiftool, tmp_path):
    import subprocess

    path = make_jpeg(tmp_path / "k.jpg")
    subprocess.run(
        [
            "exiftool",
            "-q",
            "-overwrite_original",
            "-IPTC:Keywords=Cat",
            "-IPTC:Keywords=Helsinki",
            "-XMP-lr:HierarchicalSubject=Places|Helsinki",
            str(path),
        ],
        check=True,
    )

    assert exiftool.read(path).keywords == ("Places > Helsinki", "Cat")


def test_a_level_is_spelt_as_the_keyword_there_already_spells_it(library):
    library.ensure_keywords(["Places > Finland"])

    ids = library.ensure_keywords(["places > FINLAND > Oslo"])

    assert [path for _i, path, _c in library.keyword_list()] == [
        "Places",
        "Places > Finland",
        "Places > Finland > Oslo",
    ]
    assert ids["places > FINLAND > Oslo"] is not None


# The last import


def test_the_last_import_is_the_photos_the_newest_import_copied(library, photos):
    folder = library.folders()[0].id
    item = dict(
        source_key="uuid:CARD",
        size=10,
        key="uuid:A",
        folder="p",
    )
    assert library.last_import() is None
    first = library.add_import_job(
        folder,
        [dbm.ImportItem(source_path="DCIM/1.CR3", content_id="1", name="a.jpg", **item)],
    )
    library.set_import_item(library.import_items(first)[0].id, dbm.COPIED, path="p/a.jpg")
    second = library.add_import_job(
        folder,
        [
            dbm.ImportItem(source_path="DCIM/2.CR3", content_id="2", name="b.jpg", **item),
            dbm.ImportItem(source_path="DCIM/3.CR3", content_id="3", name="c.jpg", **item),
        ],
    )
    first_b, second_c = library.import_items(second)
    library.set_import_item(first_b.id, dbm.COPIED, path="p/b.jpg")
    library.set_import_item(second_c.id, dbm.NOT_COPIED, message="no room")

    assert library.last_import() == second
    shown = library.photos(Filter(import_job=second))
    assert [p.path for p in shown] == ["p/b.jpg"]
    assert [p.path for p in library.photos(Filter(import_job=first))] == ["p/a.jpg"]


def test_an_import_that_copied_nothing_is_not_the_last_import(library, photos):
    folder = library.folders()[0].id
    job = library.add_import_job(
        folder,
        [
            dbm.ImportItem(
                source_key="uuid:CARD",
                source_path="DCIM/1.CR3",
                size=1,
                content_id="1",
                key="uuid:A",
                folder="p",
                name="a.jpg",
            )
        ],
    )

    assert library.last_import() is None
    assert library.import_items(job)
