"""Search: the photos' vectors in memory, ranked against a query by cosine.

No vector store (PLAN.md, Phase 4 decisions): the vectors are rows of the library
database, read once into one float32 matrix, and a search is one matrix product,
about 12 ms for 200k photos. `refresh` reads only the rows written since the last
one, so the index follows an indexing job as it goes.

Ranking is within what the grid shows: the rows of the current filter, best first.
A photo with no vector yet is not in the results.
"""

import numpy as np

from lumina.core import embed

# Below this match probability a photo is not a result for a text (PLAN.md: tuned
# on a real library in 4b)
TEXT_FLOOR = 0.001


class Index:
    def __init__(self, model=embed.MODEL.name):
        self.model = model
        self._matrix = np.zeros((0, embed.DIMENSIONS), np.float32)
        self._count = 0
        self._last = 0
        # content id -> row
        self._rows = {}

    def __len__(self):
        return self._count

    def refresh(self, library):
        """Read the vectors written since the last refresh. Returns how many."""
        new = library.embeddings(self.model, self._last)
        if not new:
            return 0
        vectors = embed.from_blobs([blob for _id, _cid, blob in new])
        self._grow(self._count + len(new))
        for (row_id, cid, _blob), vector in zip(new, vectors, strict=True):
            row = self._rows.get(cid)
            if row is None:
                row = self._rows[cid] = self._count
                self._count += 1
            self._matrix[row] = vector
            self._last = row_id
        return len(new)

    def vector(self, cid):
        row = self._rows.get(cid)
        return None if row is None else self._matrix[row].copy()

    def cosines(self, query):
        """The cosine of every vector with the vector `query`, by row."""
        return self._matrix[: self._count] @ np.asarray(query, np.float32)

    def rank(self, photos, query, floor=None):
        """[(photo, cosine)] of `photos` (rows with a `content_id`), best first. With
        `floor`, only those of at least that cosine."""
        cosines = self.cosines(query)
        found = []
        for photo in photos:
            row = self._rows.get(photo.content_id)
            if row is not None:
                found.append((photo, float(cosines[row])))
        if floor is not None:
            found = [pair for pair in found if pair[1] >= floor]
        found.sort(key=lambda pair: -pair[1])
        return found

    def _grow(self, needed):
        if needed <= len(self._matrix):
            return
        bigger = np.zeros((max(needed, 2 * len(self._matrix), 1024), embed.DIMENSIONS), np.float32)
        bigger[: self._count] = self._matrix[: self._count]
        self._matrix = bigger


def text_floor():
    """The cosine at which a text match reaches TEXT_FLOOR's probability."""
    return embed.cosine_for(TEXT_FLOOR)
