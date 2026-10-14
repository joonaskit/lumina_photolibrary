"""Stand-ins for the tests: an exiftool that answers at once and can be told to fail."""

import time
from datetime import datetime

from lumina.core.errors import PhotoError
from lumina.core.metadata import Metadata, MetadataUnavailable


class FakeExifTool:
    """Says every photo is from a 90D taken on 2022-05-09, except: a name in `models`
    gets that camera, a path in `broken` fails, and after `dies_after` batches it stops.
    `delay` seconds per batch make a scan slow enough to stop in the middle."""

    calls = []
    models = {}
    delay = 0.0

    def __init__(self, broken=(), dies_after=None):
        self.broken = set(map(str, broken))
        self.dies_after = dies_after

    def read_many(self, paths):
        FakeExifTool.calls.append(list(paths))
        if FakeExifTool.delay:
            time.sleep(FakeExifTool.delay)
        if self.dies_after is not None and len(FakeExifTool.calls) > self.dies_after:
            raise MetadataUnavailable("exiftool stopped")
        return {p: self._one(p) for p in paths}

    def _one(self, path):
        if path in self.broken:
            return PhotoError(path, "File format error")
        name = path.rsplit("/", 1)[-1]
        model = FakeExifTool.models.get(name, "Canon EOS 90D")
        return Metadata(captured=datetime(2022, 5, 9, 21, 3), model=model, lens="EF-S55-250mm")

    def close(self):
        pass

    @classmethod
    def reset(cls):
        cls.calls, cls.models, cls.delay = [], {}, 0.0
