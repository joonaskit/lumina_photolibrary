"""A content id for a photo: the same bytes give the same id wherever the file is, so
a moved or renamed file, or a drive mounted somewhere else, keeps its thumbnails.

The id hashes the size and the first and last 64 KiB rather than the whole file:
two reads, however large the RAW. A photo's first bytes hold its EXIF (capture
time to the second, camera serial), so two different photos agree on all of it
only in contrived cases. Opened for reading only.
"""

import hashlib
import os

from lumina.core.errors import PhotoError

_CHUNK = 64 * 1024
# Changes every id when the recipe changes, so no old id is taken for a new one
_RECIPE = b"lumina-content-id-1"


def content_id(path):
    """A 32-character hex id of the file at `path`. Raises PhotoError naming it."""
    try:
        with open(path, "rb") as file:
            size = os.fstat(file.fileno()).st_size
            digest = hashlib.sha256(_RECIPE)
            digest.update(size.to_bytes(8, "little"))
            digest.update(file.read(_CHUNK))
            if size > _CHUNK:
                file.seek(max(_CHUNK, size - _CHUNK))
                digest.update(file.read(_CHUNK))
    except OSError as error:
        raise PhotoError(path, f"cannot read ({error.strerror or error})") from error
    return digest.hexdigest()[:32]
