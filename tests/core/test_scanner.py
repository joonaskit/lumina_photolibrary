from photos import make_jpeg

from lumina.core.scanner import find_photos


def test_finds_photos_in_name_order_folder_by_folder(tmp_path):
    make_jpeg(tmp_path / "b.jpg")
    make_jpeg(tmp_path / "a.JPG")
    (tmp_path / "IMG_1.CR3").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("not a photo")
    (tmp_path / "sub").mkdir()
    make_jpeg(tmp_path / "sub" / "c.jpg")

    found = [p.removeprefix(str(tmp_path) + "/") for p in find_photos(tmp_path)]

    assert found == ["IMG_1.CR3", "a.JPG", "b.jpg", "sub/c.jpg"]


def test_hidden_folders_and_files_are_skipped(tmp_path):
    (tmp_path / ".cache").mkdir()
    make_jpeg(tmp_path / ".cache" / "x.jpg")
    make_jpeg(tmp_path / ".hidden.jpg")

    assert list(find_photos(tmp_path)) == []


def test_stops_when_asked(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        make_jpeg(tmp_path / name / "x.jpg")

    assert list(find_photos(tmp_path, should_stop=lambda: True)) == []


def test_scanning_writes_nothing(tmp_path):
    make_jpeg(tmp_path / "a.jpg")
    before = sorted(p.name for p in tmp_path.rglob("*"))

    list(find_photos(tmp_path))

    assert sorted(p.name for p in tmp_path.rglob("*")) == before
