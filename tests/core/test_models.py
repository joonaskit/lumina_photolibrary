import hashlib
import http.server
import threading

import pytest

from lumina.core import models


def model_of(files):
    """A Model of `files` ({path: bytes}) and the bytes the server has."""
    entries = tuple(
        models.ModelFile(path, len(data), hashlib.sha256(data).hexdigest())
        for path, data in files.items()
    )
    return models.Model("tiny", "test/tiny", "abc123", entries)


class Server:
    """Serves `files` under /test/tiny/resolve/abc123/, honouring Range. `ranges`
    records the Range headers it got; `served` replaces what is sent."""

    def __init__(self, files):
        self.files = dict(files)
        self.served = dict(files)
        self.ranges = []
        self.missing = set()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.removeprefix("/test/tiny/resolve/abc123/")
                server.ranges.append(self.headers.get("Range"))
                if path in server.missing or path not in server.served:
                    self.send_error(404)
                    return
                data = server.served[path]
                start = 0
                if self.headers.get("Range"):
                    start = int(self.headers["Range"].removeprefix("bytes=").rstrip("-"))
                    self.send_response(206)
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:])

            def log_message(self, *_args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


FILES = {"onnx/model.onnx": b"m" * 300_000, "tokenizer.json": b'{"t": 1}'}


@pytest.fixture
def server():
    running = Server(FILES)
    yield running
    running.close()


def test_the_files_are_fetched_checked_and_put_in_place(tmp_path, server):
    model = model_of(FILES)
    seen = []

    assert not models.is_installed(model, tmp_path)
    models.download(model, tmp_path, report=lambda d, t: seen.append((d, t)), base_url=server.url)

    folder = tmp_path / "tiny" / "abc123"
    assert (folder / "onnx" / "model.onnx").read_bytes() == FILES["onnx/model.onnx"]
    assert models.is_installed(model, tmp_path)
    assert not list(folder.rglob("*.part"))
    assert seen[-1] == (model.size, model.size)
    assert [d for d, _t in seen] == sorted(d for d, _t in seen)


def test_a_stopped_download_continues_where_it_stopped(tmp_path, server):
    model = model_of(FILES)
    part = tmp_path / "tiny" / "abc123" / "onnx" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(FILES["onnx/model.onnx"][:1000])

    models.download(model, tmp_path, base_url=server.url)

    assert server.ranges[0] == "bytes=1000-"
    assert models.is_installed(model, tmp_path)


def test_installed_files_are_not_fetched_again(tmp_path, server):
    model = model_of(FILES)
    models.download(model, tmp_path, base_url=server.url)
    server.ranges.clear()

    models.download(model, tmp_path, base_url=server.url)

    assert server.ranges == []


def test_a_file_that_is_not_the_expected_one_is_not_kept(tmp_path, server):
    model = model_of(FILES)
    server.served["tokenizer.json"] = b'{"t": 2}'

    with pytest.raises(models.DownloadError, match="tokenizer.json"):
        models.download(model, tmp_path, base_url=server.url)

    folder = tmp_path / "tiny" / "abc123"
    assert not (folder / "tokenizer.json").exists()
    assert not (folder / "tokenizer.json.part").exists()
    assert not models.is_installed(model, tmp_path)


def test_a_server_error_names_the_file_and_keeps_what_came(tmp_path, server):
    model = model_of(FILES)
    server.missing.add("tokenizer.json")

    with pytest.raises(models.DownloadError, match=r"tokenizer.json: HTTP 404"):
        models.download(model, tmp_path, base_url=server.url)

    assert (tmp_path / "tiny" / "abc123" / "onnx" / "model.onnx").exists()


def test_a_cancel_keeps_the_part_for_the_next_try(tmp_path, server):
    model = model_of(FILES)

    with pytest.raises(models.Cancelled):
        models.download(model, tmp_path, should_stop=lambda: True, base_url=server.url)

    folder = tmp_path / "tiny" / "abc123"
    assert not (folder / "onnx" / "model.onnx").exists()
    assert (folder / "onnx" / "model.onnx.part").exists()


def test_no_server_is_a_download_error(tmp_path, server):
    model = model_of(FILES)
    server.close()

    with pytest.raises(models.DownloadError, match="onnx/model.onnx"):
        models.download(model, tmp_path, base_url=server.url)


def test_the_models_folder_follows_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    folder = models.directory(models.SIGLIP2)

    assert folder == str(
        tmp_path / "lumina" / "models" / models.SIGLIP2.name / models.SIGLIP2.revision
    )
