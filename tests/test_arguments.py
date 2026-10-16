"""What `lumina PATH...` makes of its arguments (core of lumina.app: no Qt needed)."""

import os

from lumina.app import local_path


def test_a_path_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert local_path("photo.jpg") == str(tmp_path / "photo.jpg")
    assert local_path(str(tmp_path / "a" / ".." / "b.jpg")) == str(tmp_path / "b.jpg")


def test_a_file_url_is_decoded_to_its_path():
    assert local_path("file:///home/me/My%20Photos/a%C3%A4.jpg") == "/home/me/My Photos/aä.jpg"
    assert local_path("file://localhost/tmp/a.jpg") == "/tmp/a.jpg"


def test_other_urls_are_not_files():
    assert local_path("https://example.com/a.jpg") is None
    assert local_path("file://otherhost/tmp/a.jpg") is None
    assert local_path("smb://nas/photos/a.jpg") is None


def test_home_is_expanded():
    assert local_path("~/a.jpg") == os.path.join(os.path.expanduser("~"), "a.jpg")
