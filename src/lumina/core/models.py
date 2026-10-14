"""The search model's files: where they are kept, whether they are there, and fetching them.

This is the only code in Lumina that uses the network, and it runs only when the
user asked for the download. The files are pinned to one revision and checked by
size and SHA-256. Each is written to a `.part` file beside where it goes, continued
from where it stopped when there is one, checked, and only then renamed into place:
a file under its own name is always whole.
"""

import hashlib
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Model:
    # Stored with each vector: vectors of another model are not compared with these
    name: str
    repository: str
    revision: str
    files: tuple

    @property
    def size(self):
        return sum(f.size for f in self.files)


# PLAN.md, Phase 4 decisions: SigLIP 2 base, multilingual, fp16 ONNX
SIGLIP2 = Model(
    name="siglip2-base-patch16-224",
    repository="onnx-community/siglip2-base-patch16-224-ONNX",
    revision="ba1f3b0843f24bc5417d38e19c37b287d719b2f4",
    files=(
        ModelFile(
            "onnx/vision_model_fp16.onnx",
            186039516,
            "a1959f7bd3993a607e48839f6d01e25b876fe76afda301b028b78eef68aabd95",
        ),
        ModelFile(
            "onnx/text_model_fp16.onnx",
            564862230,
            "711da56ada0a4aa11c7dd3320df741081a3cae4f0ae1b5e5c6d5b294738d0eb0",
        ),
        ModelFile(
            "tokenizer.json",
            34363039,
            "cb9140fae3ac5122c972d37adf83e1248471a38147ad76f8215c8872c6fd8322",
        ),
    ),
)

BASE_URL = "https://huggingface.co"
_CHUNK = 1 << 20
_TIMEOUT = 30


class DownloadError(Exception):
    """The download stopped; what was fetched is kept and the next one continues."""


class Cancelled(Exception):
    pass


def default_root():
    """`$XDG_DATA_HOME/lumina/models`, read when it is called."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "lumina", "models")


def directory(model, root=None):
    return os.path.join(root or default_root(), model.name, model.revision)


def is_installed(model, root=None):
    """Whether every file is there at its size. They are only renamed into place
    after their hash was checked."""
    folder = directory(model, root)
    for f in model.files:
        try:
            if os.path.getsize(os.path.join(folder, f.path)) != f.size:
                return False
        except OSError:
            return False
    return True


def download(
    model,
    root=None,
    report=lambda done, total: None,
    should_stop=lambda: False,
    base_url=BASE_URL,
):
    """Fetch what is missing of `model`. `report(done, total)` in bytes. Raises
    DownloadError naming the file, or Cancelled; either way what was fetched stays
    for the next try."""
    folder = directory(model, root)
    done = 0
    report(done, model.size)
    for f in model.files:
        target = os.path.join(folder, f.path)
        if _size(target) == f.size:
            done += f.size
            report(done, model.size)
            continue
        url = f"{base_url}/{model.repository}/resolve/{model.revision}/{f.path}"
        start = done
        _fetch(url, target, f, lambda n, start=start: report(start + n, model.size), should_stop)
        done += f.size
    report(done, model.size)


def _fetch(url, target, f, report, should_stop):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"
    have = _size(part) or 0
    if have > f.size:
        os.remove(part)
        have = 0
    if have < f.size:
        request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                # A server that ignores the range sends the whole file
                if have and response.status != 206:
                    have = 0
                with open(part, "ab" if have else "wb") as out:
                    report(have)
                    while chunk := response.read(_CHUNK):
                        if should_stop():
                            raise Cancelled()
                        out.write(chunk)
                        have += len(chunk)
                        report(have)
        except (urllib.error.URLError, OSError) as error:
            raise DownloadError(f"{f.path}: {_reason(error)}") from error
    if not _matches(part, f):
        os.remove(part)
        raise DownloadError(f"{f.path}: the file that came is not the expected one")
    os.replace(part, target)


def _matches(path, f):
    if _size(path) != f.size:
        return False
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest() == f.sha256


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _reason(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code} {error.reason}"
    if isinstance(error, urllib.error.URLError):
        return str(error.reason)
    return error.strerror or str(error)
