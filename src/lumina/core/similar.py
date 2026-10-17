"""Near-duplicates across the library: photos whose search vectors are close, and copies
of one file (#61).

Groups are the connected pieces of "at least this alike", so a chain of slowly changing
photos is one group. Photos with the same content id are exact copies (a file imported
twice, or in two folders) and need no vector to be found. Pure: nothing here reads the
library or changes it; nothing is ever deleted (PLAN.md, safety rule 1).
"""

from dataclasses import dataclass

import numpy as np

from lumina.core import bursts

# Cosine at which two photos are the same picture saved twice (a re-export, a resize);
# the dialog lets the user go lower. To be tuned on real libraries
SIMILARITY = 0.95
# Rows of the similarity matrix made at once: 1024 x 100k float32 is 400 MB at worst
_BLOCK = 1024


@dataclass(frozen=True)
class Group:
    # Photo ids, the best first (as a burst's), the rest in time order
    ids: tuple[int, ...]
    # Every photo is the same file
    exact: bool


def find(rows, vectors, similarity=SIMILARITY):
    """The groups of two or more among `rows` (photos with id, content_id, taken, flag and
    rating), largest first. `vectors` is {content id: normalised vector}."""
    rows = list(rows)
    parent = list(range(len(rows)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(a, b):
        parent[root(a)] = root(b)

    # Copies of one file
    first = {}
    for i, row in enumerate(rows):
        if row.content_id is not None:
            join(i, first.setdefault(row.content_id, i))
    # One vector for each content id: the others of its copies are joined to it already
    reps = [i for i in dict.fromkeys(first.values()) if rows[i].content_id in vectors]
    if len(reps) > 1:
        matrix = np.stack([vectors[rows[i].content_id] for i in reps]).astype(np.float32)
        for start in range(0, len(reps), _BLOCK):
            block = matrix[start : start + _BLOCK] @ matrix.T
            for a, b in zip(*np.nonzero(block >= similarity), strict=True):
                if start + a < b:
                    join(reps[start + a], reps[b])
    members = {}
    for i in range(len(rows)):
        members.setdefault(root(i), []).append(rows[i])
    found = []
    for group in members.values():
        if len(group) < 2:
            continue
        top = bursts.best(group)
        rest = sorted((r for r in group if r is not top), key=lambda r: (r.taken, r.id))
        exact = top.content_id is not None and all(r.content_id == top.content_id for r in rest)
        found.append(Group(tuple(r.id for r in (top, *rest)), exact))
    found.sort(key=lambda g: (-len(g.ids), g.ids))
    return found
