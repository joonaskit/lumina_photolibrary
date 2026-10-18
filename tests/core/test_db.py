import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from lumina.core import db as dbm
from lumina.core.db import Filter, Library, NewerDatabase
from lumina.core.metadata import Metadata


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as opened:
        yield opened


def add_photos(library, folder, volume, names, **when):
    rows = [(name, 1, 1, when.get(name, "2020-01-01 00:00:00.000000")) for name in names]
    library.write_scan(folder, volume, new=rows)
    return {path: state[0] for path, state in library.photo_states(folder).items()}


def test_a_new_file_gets_the_current_schema(tmp_path):
    path = tmp_path / "library.sqlite3"
    Library(path).close()

    version = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
    assert version == dbm.SCHEMA_VERSION


def test_what_is_written_is_there_when_opened_again(tmp_path):
    path = tmp_path / "library.sqlite3"
    with Library(path) as library:
        library.add_folder("uuid:A", "Pictures")

    with Library(path) as library:
        assert [f.path for f in library.folders()] == ["Pictures"]


def test_a_file_from_a_newer_lumina_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "library.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {dbm.SCHEMA_VERSION + 1}")
    raw.close()
    before = path.read_bytes()

    with pytest.raises(NewerDatabase):
        Library(path)

    assert path.read_bytes() == before


def test_the_default_file_follows_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert dbm.default_path() == str(tmp_path / "lumina" / "library.sqlite3")


def test_a_folder_is_added_once(library):
    first = library.add_folder("uuid:A", "/Pictures/")

    assert library.add_folder("uuid:A", "Pictures") == first
    assert library.add_folder("uuid:A", "Pictures/2022") == first
    assert library.add_folder("uuid:B", "Pictures") != first


def test_a_folder_around_others_takes_their_photos(library):
    inner = library.add_folder("uuid:A", "Pictures/2022")
    volume = library.volume_id("uuid:A")
    ids = add_photos(library, inner, volume, ["Pictures/2022/a.jpg"])

    outer = library.add_folder("uuid:A", "Pictures")

    assert [f.id for f in library.folders()] == [outer]
    assert library.photo_states(outer)["Pictures/2022/a.jpg"][0] == ids["Pictures/2022/a.jpg"]


def test_the_root_of_a_volume_holds_every_folder_on_it(library):
    root = library.add_folder("uuid:A", "")

    assert library.add_folder("uuid:A", "Pictures") == root


def test_removing_a_folder_forgets_only_its_photos(library):
    volume = library.volume_id("uuid:A")
    one, two = library.add_folder("uuid:A", "one"), library.add_folder("uuid:A", "two")
    add_photos(library, one, volume, ["one/a.jpg"])
    add_photos(library, two, volume, ["two/b.jpg"])
    library.add_job("scan", one)

    library.remove_folder(one)

    assert [p.path for p in library.photos()] == ["two/b.jpg"]
    assert library.unfinished_jobs() == []


def test_missing_photos_keep_what_is_known_of_them(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    ids = add_photos(library, folder, volume, ["p/a.jpg"])
    library.write_metadata(read=[(ids["p/a.jpg"], "cid", Metadata(model="Canon EOS 90D"))])

    library.mark_missing([ids["p/a.jpg"]])

    photo = library.photo(ids["p/a.jpg"])
    assert photo["available"] == 0 and photo["missing_since"] is not None
    assert photo["model"] == "Canon EOS 90D" and photo["content_id"] == "cid"


def test_capture_time_is_kept_as_local_time_and_offset(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    ids = add_photos(library, folder, volume, ["p/a.jpg"])
    taken = datetime(2022, 5, 9, 21, 3, 25, 960000, tzinfo=timezone(timedelta(hours=2)))

    library.write_metadata(read=[(ids["p/a.jpg"], "cid", Metadata(captured=taken))])

    photo = library.photo(ids["p/a.jpg"])
    assert (photo["captured"], photo["captured_offset"]) == ("2022-05-09 21:03:25.960000", "+02:00")


def test_photos_are_in_time_order_and_filtered(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    ids = add_photos(
        library,
        folder,
        volume,
        ["p/new.jpg", "p/old.jpg", "p/none.jpg"],
        **{"p/none.jpg": "2021-06-01 12:00:00.000000"},
    )
    library.write_metadata(
        read=[
            (ids["p/new.jpg"], "1", Metadata(captured=datetime(2024, 1, 1), model="R6", lens="RF")),
            (
                ids["p/old.jpg"],
                "2",
                Metadata(captured=datetime(2019, 1, 1), model="90D", lens="EF"),
            ),
        ]
    )

    assert [p.path for p in library.photos()] == ["p/old.jpg", "p/none.jpg", "p/new.jpg"]
    assert [p.path for p in library.photos(Filter(year="2021"))] == ["p/none.jpg"]
    assert [p.path for p in library.photos(Filter(model="90D"))] == ["p/old.jpg"]
    assert [p.path for p in library.photos(Filter(lens="RF", folder_id=folder))] == ["p/new.jpg"]
    assert library.choices() == (["2024", "2021", "2019"], ["90D", "R6"], ["EF", "RF"])


def test_an_unfinished_job_is_not_queued_twice(library):
    folder = library.add_folder("uuid:A", "p")
    job = library.add_job("scan", folder)

    assert library.add_job("scan", folder) == job
    library.update_job(job, state=dbm.DONE)
    assert library.add_job("scan", folder) != job


def test_a_library_from_schema_1_is_brought_up_to_date_with_its_data(tmp_path, monkeypatch):
    path = tmp_path / "library.sqlite3"
    # As the first release wrote it
    monkeypatch.setattr(dbm, "_MIGRATIONS", dbm._MIGRATIONS[:1])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 1)
    with Library(path) as old:
        folder = old.add_folder("uuid:A", "p")
        old.write_scan(folder, old.volume_id("uuid:A"), new=[("p/a.jpg", 1, 1, "2020")])
    monkeypatch.undo()

    with Library(path) as library:
        assert (
            sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == dbm.SCHEMA_VERSION
        )
        photo = library.photos()[0]
        assert photo.path == "p/a.jpg"
        library.save_edit(photo.id, '{"version": 1}')
        assert library.edit(photo.id) == '{"version": 1}'
        # Schema 4: not flagged
        assert photo.flag == dbm.UNFLAGGED


def test_a_migration_that_fails_half_way_leaves_the_library_as_it_was(tmp_path, monkeypatch):
    path = tmp_path / "library.sqlite3"
    monkeypatch.setattr(dbm, "_MIGRATIONS", dbm._MIGRATIONS[:1])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 1)
    with Library(path):
        pass
    monkeypatch.undo()

    def columns():
        db = sqlite3.connect(path)
        try:
            return (
                [row[1] for row in db.execute("PRAGMA table_info(photos)")],
                db.execute("PRAGMA user_version").fetchone()[0],
            )
        finally:
            db.close()

    before = columns()

    def broken(db):
        dbm._script(db, "ALTER TABLE photos ADD COLUMN half INTEGER; NOT SQL;")

    monkeypatch.setattr(dbm, "_MIGRATIONS", [dbm._MIGRATIONS[0], broken])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 2)
    with pytest.raises(sqlite3.Error):
        Library(path)
    assert columns() == before

    # The next start, with the migration fixed, goes through
    monkeypatch.setattr(dbm, "_MIGRATIONS", dbm._MIGRATIONS[:1] + [lambda db: None])
    with Library(path):
        pass
    assert columns()[1] == 2


def test_a_library_from_schema_5_reads_its_cr3s_again_to_find_damage(tmp_path, monkeypatch):
    path = tmp_path / "library.sqlite3"
    monkeypatch.setattr(dbm, "_MIGRATIONS", dbm._MIGRATIONS[:5])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 5)
    with Library(path) as old:
        volume = old.volume_id("uuid:A")
        folder = old.add_folder("uuid:A", "p")
        ids = add_photos(old, folder, volume, ["p/a.cr3", "p/b.jpg", "p/c.cr3"])
        # As schema 5 wrote details: a CR3 and a JPEG read, a CR3 that failed
        with old.db:
            old.db.executemany(
                "UPDATE photos SET metadata_state = ?, file_type = ?, model = ? WHERE id = ?",
                [
                    (1, "CR3", "Canon EOS 90D", ids["p/a.cr3"]),
                    (1, "JPEG", None, ids["p/b.jpg"]),
                    (2, None, None, ids["p/c.cr3"]),
                ],
            )
    monkeypatch.undo()

    with Library(path) as library:
        # Only the CR3 read before is read again; what was known of it stays meanwhile
        assert library.pending_metadata(folder, 10) == [(ids["p/a.cr3"], "p/a.cr3")]
        assert library.photo(ids["p/a.cr3"])["model"] == "Canon EOS 90D"
        assert all(photo.damage_warning is None for photo in library.photos())


def test_a_merge_made_outlives_its_sources_leaving_the_library(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    add_photos(library, folder, volume, ["p/a.cr3", "p/b.cr3"])
    sources = [dbm.Source("uuid:A", "p/a.cr3", "1"), dbm.Source("uuid:A", "/p/b.cr3", "2")]
    job = library.add_derived_job("hdr", '{"version": 1}', sources, folder)
    derived = library.job(job).derived_id
    library.set_derived_result(derived, "uuid:A", "p/a-HDR.tif")
    library.update_job(job, state=dbm.DONE)

    library.remove_folder(folder)

    made = library.derived_at("uuid:A", "p/a-HDR.tif")
    assert made.id == derived and made.kind == "hdr" and made.settings == '{"version": 1}'
    assert made.sources == (
        dbm.Source("uuid:A", "p/a.cr3", "1"),
        dbm.Source("uuid:A", "p/b.cr3", "2"),
    )


def test_a_merge_never_made_goes_with_its_folder(library):
    folder = library.add_folder("uuid:A", "p")
    job = library.add_derived_job("hdr", "{}", [dbm.Source("uuid:A", "p/a.cr3")], folder)
    derived = library.job(job).derived_id

    library.remove_folder(folder)

    assert library.job(job) is None and library.derived(derived) is None


def test_a_library_from_schema_6_gets_derived_photos_and_keeps_its_jobs(tmp_path, monkeypatch):
    path = tmp_path / "library.sqlite3"
    monkeypatch.setattr(dbm, "_MIGRATIONS", dbm._MIGRATIONS[:6])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 6)
    with Library(path) as old:
        folder = old.add_folder("uuid:A", "p")
        scan = old.add_job("scan", folder)
    monkeypatch.undo()

    with Library(path) as library:
        assert library.job(scan).derived_id is None and library.job(scan).kind == "scan"
        job = library.add_derived_job("hdr", "{}", [dbm.Source("uuid:A", "p/a.cr3")], folder)
        assert library.derived(library.job(job).derived_id).sources[0].path == "p/a.cr3"


def an_import(library, folder, **item):
    fields = dict(
        source_key="uuid:CARD",
        source_path="DCIM/100CANON/IMG_0001.CR3",
        size=10,
        content_id="cid-1",
        key="uuid:A",
        folder="p/2026",
        name="IMG_0001.CR3",
    )
    return library.add_import_job(folder, [dbm.ImportItem(**(fields | item))])


def test_an_import_job_keeps_its_photos_to_copy_in_order(library):
    folder = library.add_folder("uuid:A", "p")

    job = an_import(library, folder, backup_key="uuid:B", backup_folder="/copies/2026/")

    record = library.job(job)
    assert (record.kind, record.state, record.total, record.folder_id) == (
        "import",
        dbm.QUEUED,
        1,
        folder,
    )
    [item] = library.import_items(job)
    assert (item.source_key, item.source_path, item.key, item.folder, item.name) == (
        "uuid:CARD",
        "DCIM/100CANON/IMG_0001.CR3",
        "uuid:A",
        "p/2026",
        "IMG_0001.CR3",
    )
    assert (item.backup_key, item.backup_folder, item.state) == ("uuid:B", "copies/2026", 0)
    library.set_import_item(item.id, dbm.COPIED, path="/p/2026/IMG_0001.CR3")
    assert library.import_items(job, dbm.TO_COPY) == []
    assert library.import_items(job)[0].path == "p/2026/IMG_0001.CR3"


def test_a_photo_is_imported_once_copied_or_in_the_library(library):
    folder = library.add_folder("uuid:A", "p")
    job = an_import(library, folder)
    assert library.imported(["cid-1", "cid-2", None]) == set()

    library.set_import_item(library.import_items(job)[0].id, dbm.COPIED, path="p/a.cr3")
    ids = add_photos(library, folder, library.volume_id("uuid:A"), ["p/b.jpg"])
    library.write_metadata(read=[(ids["p/b.jpg"], "cid-2", Metadata())])

    assert library.imported(["cid-1", "cid-2", "cid-3"]) == {"cid-1", "cid-2"}


def test_a_folder_around_another_takes_its_import_and_drops_its_scan(library):
    inner = library.add_folder("uuid:A", "p/2026")
    library.add_job("scan", inner)
    job = an_import(library, inner)

    outer = library.add_folder("uuid:A", "p")

    assert [(j.kind, j.folder_id) for j in library.unfinished_jobs()] == [("import", outer)]
    assert len(library.import_items(job)) == 1


def test_a_library_from_schema_7_gets_imports(tmp_path, monkeypatch):
    path = tmp_path / "library.sqlite3"
    monkeypatch.setattr(dbm, "_MIGRATIONS", dbm._MIGRATIONS[:7])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 7)
    with Library(path) as old:
        folder = old.add_folder("uuid:A", "p")
    monkeypatch.undo()

    with Library(path) as library:
        job = an_import(library, folder)
        assert library.import_items(job)[0].name == "IMG_0001.CR3"


def test_a_warning_of_damage_is_kept_and_goes_when_the_file_reads_clean(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    photo = add_photos(library, folder, volume, ["p/a.cr3"])["p/a.cr3"]

    damaged = Metadata(file_type="CR3", warning="Truncated CTMD record")
    library.write_metadata(read=[(photo, "1", damaged)])
    assert library.photos()[0].damage_warning == "Truncated CTMD record"

    library.write_metadata(read=[(photo, "2", Metadata(file_type="CR3"))])
    assert library.photos()[0].damage_warning is None


def test_edits_and_virtual_copies(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    photo = add_photos(library, folder, volume, ["p/a.jpg"])["p/a.jpg"]

    assert library.edit(photo) is None
    library.save_edit(photo, '{"exposure": 1}')
    library.save_edit(photo, '{"exposure": 2}')
    copy = library.new_copy(photo, '{"exposure": -1}')

    assert library.edit(photo) == '{"exposure": 2}'
    assert library.edit(photo, copy) == '{"exposure": -1}'
    assert library.copies(photo) == [0, 1]
    library.delete_edit(photo)
    assert library.edit(photo) is None and library.copies(photo) == [1]


def test_virtual_copies_of_many_photos_are_made_all_or_none(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    ids = add_photos(library, folder, volume, ["p/a.jpg", "p/b.jpg"])
    a, b = ids["p/a.jpg"], ids["p/b.jpg"]

    assert library.new_copies({a: '{"exposure": 1}', b: '{"exposure": 2}'}) == {a: 1, b: 1}
    assert library.edit(b, 1) == '{"exposure": 2}'

    with pytest.raises(sqlite3.Error):
        library.new_copies({a: '{"exposure": 3}', b: None})

    assert library.copies(a) == [1] and library.copies(b) == [1]


def test_edits_are_saved_for_many_photos_at_once(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    ids = add_photos(library, folder, volume, ["p/a.jpg", "p/b.jpg", "p/c.jpg"])
    a, b, c = ids["p/a.jpg"], ids["p/b.jpg"], ids["p/c.jpg"]
    library.save_edit(a, '{"exposure": 1}')
    library.save_edit(b, '{"exposure": 2}')
    copy = library.new_copy(b, '{"exposure": -1}')

    # New, replaced, and (None) back to the original: the photo's own edit, not its copies
    library.save_edits({a: None, b: '{"exposure": 3}', c: '{"exposure": 4}'})

    assert library.edit(a) is None
    assert library.edit(b) == '{"exposure": 3}'
    assert library.edit(c) == '{"exposure": 4}'
    assert library.edit(b, copy) == '{"exposure": -1}'


def test_saving_many_edits_is_all_or_nothing(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    a = add_photos(library, folder, volume, ["p/a.jpg"])["p/a.jpg"]
    library.save_edit(a, '{"exposure": 1}')

    with pytest.raises(sqlite3.Error):
        # The second is no photo of the library
        library.save_edits({a: '{"exposure": 2}', 9999: '{"exposure": 3}'})

    assert library.edit(a) == '{"exposure": 1}'


def test_a_photo_forgotten_with_its_folder_takes_its_edits(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    photo = add_photos(library, folder, volume, ["p/a.jpg"])["p/a.jpg"]
    library.save_edit(photo, "{}")

    library.remove_folder(folder)

    assert library.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0


def test_a_missing_photo_keeps_its_edit(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    photo = add_photos(library, folder, volume, ["p/a.jpg"])["p/a.jpg"]
    library.save_edit(photo, '{"exposure": 1}')

    library.mark_missing([photo])

    assert library.edit(photo) == '{"exposure": 1}'


def test_presets_by_name(library):
    library.save_preset("Matte", '{"matte": {}}')
    library.save_preset("bright", '{"exposure": 1}')
    library.save_preset("Matte", '{"matte": {"black": 0.1}}')

    assert library.presets() == [
        ("bright", '{"exposure": 1}'),
        ("Matte", '{"matte": {"black": 0.1}}'),
    ]
    library.delete_preset("bright")
    assert [name for name, _ in library.presets()] == ["Matte"]


def test_flags_are_kept_and_read_back(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    ids = add_photos(library, folder, volume, ["p/a.jpg", "p/b.jpg", "p/c.jpg"])

    library.set_flags({ids["p/a.jpg"]: dbm.PICKED, ids["p/b.jpg"]: dbm.REJECTED})

    assert {photo.path: photo.flag for photo in library.photos()} == {
        "p/a.jpg": dbm.PICKED,
        "p/b.jpg": dbm.REJECTED,
        "p/c.jpg": dbm.UNFLAGGED,
    }
    assert library.flags([ids["p/a.jpg"], ids["p/c.jpg"], 999]) == {
        ids["p/a.jpg"]: dbm.PICKED,
        ids["p/c.jpg"]: dbm.UNFLAGGED,
    }
    with pytest.raises(ValueError):
        library.set_flags({ids["p/a.jpg"]: 2})
    assert library.flags([ids["p/a.jpg"]]) == {ids["p/a.jpg"]: dbm.PICKED}


def test_flags_are_read_for_more_photos_than_sqlite_takes_at_once(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    ids = list(add_photos(library, folder, volume, [f"p/{n}.jpg" for n in range(1200)]).values())

    library.set_flags(dict.fromkeys(ids, dbm.REJECTED))

    assert library.flags(ids) == dict.fromkeys(ids, dbm.REJECTED)


def test_a_flag_outlives_a_missing_file_and_a_changed_one(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    photo = add_photos(library, folder, volume, ["p/a.jpg"])["p/a.jpg"]
    library.set_flags({photo: dbm.REJECTED})

    library.mark_missing([photo])
    library.write_scan(folder, volume, changed=[(photo, 2, 2, "2021")])

    assert library.flags([photo]) == {photo: dbm.REJECTED}


def test_ratings_are_kept_and_read_back_apart_from_flags(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    ids = add_photos(library, folder, volume, ["p/a.jpg", "p/b.jpg", "p/c.jpg"])

    library.set_ratings({ids["p/a.jpg"]: 5, ids["p/b.jpg"]: 2})
    library.set_flags({ids["p/b.jpg"]: dbm.PICKED})

    assert {photo.path: (photo.rating, photo.flag) for photo in library.photos()} == {
        "p/a.jpg": (5, dbm.UNFLAGGED),
        "p/b.jpg": (2, dbm.PICKED),
        "p/c.jpg": (0, dbm.UNFLAGGED),
    }
    assert library.ratings([ids["p/a.jpg"], ids["p/c.jpg"], 999]) == {
        ids["p/a.jpg"]: 5,
        ids["p/c.jpg"]: 0,
    }
    for bad in (6, -1, 2.5):
        with pytest.raises(ValueError):
            library.set_ratings({ids["p/a.jpg"]: bad})
    assert library.ratings([ids["p/a.jpg"]]) == {ids["p/a.jpg"]: 5}


def test_ratings_are_read_for_more_photos_than_sqlite_takes_at_once(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    ids = list(add_photos(library, folder, volume, [f"p/{n}.jpg" for n in range(1200)]).values())

    library.set_ratings(dict.fromkeys(ids, 3))

    assert library.ratings(ids) == dict.fromkeys(ids, 3)


def test_a_rating_outlives_a_missing_file_and_a_changed_one(library):
    volume = library.volume_id("uuid:A")
    folder = library.add_folder("uuid:A", "p")
    photo = add_photos(library, folder, volume, ["p/a.jpg"])["p/a.jpg"]
    library.set_ratings({photo: 4})

    library.mark_missing([photo])
    library.write_scan(folder, volume, changed=[(photo, 2, 2, "2021")])

    assert library.ratings([photo]) == {photo: 4}
