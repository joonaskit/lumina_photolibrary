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


def colour_vector(colour):
    """A unit vector from an RGB colour, in the model's dimensions: photos of one
    colour are alike, and a query is a colour."""
    import numpy as np

    from lumina.core import embed

    vector = np.zeros(embed.DIMENSIONS, np.float32)
    vector[:3] = colour
    vector[3] = 1
    return vector / np.linalg.norm(vector)


class FakeEncoder:
    """encode(paths) as core/indexing.py takes it, without the model: a photo's vector
    is its mean colour. `calls` records the batches; a path in `crashes` makes a call
    that has it raise, as a worker a decoder crashed in does; `broken` raises always."""

    def __init__(self, crashes=(), broken=False):
        self.calls = []
        self.crashes = set(map(str, crashes))
        self.broken = broken

    def __call__(self, paths):
        from lumina.core import embed, images

        self.calls.append(list(paths))
        if self.broken:
            raise RuntimeError("the model could not be loaded")
        if self.crashes & set(paths):
            raise RuntimeError("A process in the process pool was terminated abruptly")
        results = []
        for path in paths:
            try:
                pixels = images.load_pixels(path, 16)
            except PhotoError as error:
                results.append(error)
                continue
            data = pixels.data
            colour = [sum(data[i::3]) / (len(data) / 3) / 255 for i in range(3)]
            results.append(embed.to_blob(colour_vector(colour)))
        return results


def install_model(root):
    """The search model as is_installed sees it: every file at its size, sparse, so
    nothing the size of the real one is written."""
    import os

    from lumina.core import embed, models

    folder = models.directory(embed.MODEL, root)
    for f in embed.MODEL.files:
        path = os.path.join(folder, f.path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(f.size)


# What the fake text encoder understands: a colour by name
COLOURS = {"red": (0.8, 0.15, 0.15), "green": (0.04, 0.8, 0.04), "blue": (0.1, 0.1, 0.8)}


def encode_colour_text(text):
    return colour_vector(COLOURS.get(text.lower(), (0.5, 0.5, 0.5)))
