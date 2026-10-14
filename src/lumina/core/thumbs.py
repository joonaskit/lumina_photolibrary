"""Thumbnails, kept as WebP files in Lumina's own cache folder.

A thumbnail is filed under its photo's content id, so it is found again after the
photo is moved or its drive remounted, and a photo whose bytes change gets a new
one. Nothing is ever written next to a photo: only under the cache root. The cache
is a convenience: when it cannot be read or written the thumbnail is made anyway.
"""

import os
import tempfile
from pathlib import Path

from PIL import Image

from lumina.core import images
from lumina.core.edit import params as edit_params
from lumina.core.edit.preview import edited_pixels
from lumina.core.fingerprint import content_id
from lumina.core.images import Pixels

# Goes up when the files change format or look: a new folder, the old one unused
FORMAT_VERSION = 1
_QUALITY = 85


def default_cache_dir():
    """`$XDG_CACHE_HOME/lumina/thumbnails`, read when it is called."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "lumina", "thumbnails")


class ThumbnailCache:
    def __init__(self, root):
        self.root = Path(root) / f"v{FORMAT_VERSION}"

    def path(self, cid, side):
        return self.root / cid[:2] / f"{cid}-{side}.webp"

    def get(self, cid, side):
        """The cached thumbnail as Pixels, or None. A damaged file is removed."""
        path = self.path(cid, side)
        try:
            with Image.open(path) as image:
                image = image.convert("RGB")
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            _remove(path)
            return None
        return Pixels(image.width, image.height, image.tobytes())

    def put(self, cid, side, pixels):
        """Keep `pixels` for `cid`. Whole or not at all: written aside, then renamed.
        Returns whether it was kept."""
        path = self.path(cid, side)
        image = Image.frombytes("RGB", (pixels.width, pixels.height), pixels.data)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".part")
        except OSError:
            return False
        try:
            with os.fdopen(handle, "wb") as file:
                image.save(file, "WEBP", quality=_QUALITY)
            os.replace(temporary, path)
        except OSError:
            _remove(temporary)
            return False
        return True

    def invalidate(self, cid):
        """Forget every thumbnail of `cid`: for when its look changes (an edit)."""
        folder = self.root / cid[:2]
        for path in folder.glob(f"{cid}-*.webp") if folder.is_dir() else ():
            _remove(path)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def load_thumbnail(path, side, cache_root, edit_json=None):
    """The thumbnail of the photo at `path`, with its edit if it has one, from the cache
    under `cache_root` or made and kept there. Raises PhotoError naming the photo when
    it cannot be read.

    An edited photo's thumbnail is filed under its content id and the edit's
    fingerprint, so changing the edit makes a new one. An edit from a newer Lumina
    can't be rendered here: the photo's own preview is shown."""
    cache = ThumbnailCache(cache_root)
    cid = content_id(path)
    key = cid
    edit = None
    if edit_json is not None:
        try:
            edit = edit_params.from_json(edit_json)
        except edit_params.NewerParams:
            edit = None
        if edit is not None and not edit.is_identity():
            key = f"{cid}-{edit.fingerprint()}"
        else:
            edit = None
    pixels = cache.get(key, side)
    if pixels is None:
        if edit is not None:
            pixels = edited_pixels(path, side, edit_json)
        else:
            pixels = images.load_pixels(path, side)
        cache.put(key, side, pixels)
    return pixels


def cached_thumbnail(cid, side, cache_root, edit_json=None):
    """The cached thumbnail of content id `cid` (with its edit), or None: for a photo
    whose drive is not connected, which can't be read but may have been seen before.
    Without one for the edit, the unedited one."""
    cache = ThumbnailCache(cache_root)
    if edit_json is not None:
        try:
            edit = edit_params.from_json(edit_json)
        except edit_params.NewerParams:
            edit = None
        if edit is not None and not edit.is_identity():
            found = cache.get(f"{cid}-{edit.fingerprint()}", side)
            if found is not None:
                return found
    return cache.get(cid, side)
