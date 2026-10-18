"""Embeddings: the part of `Library` for it (see db.py)."""

import json

from lumina.core.dbutil import _pending_embeddings


class EmbeddingsStore:
    # Embeddings

    def pending_embeddings(self, model, volume_keys, limit, skip=()):
        """[(content_id, volume_key, path)] of photos with no vector for `model` yet,
        one per content id, on the volumes `volume_keys` (the connected ones), newest
        first: recent photos are searchable soonest. `skip` are the content ids a job
        passed over, left out (by id, not by position: photos added meanwhile may sort
        before them)."""
        where, args = _pending_embeddings(model, volume_keys)
        rows = self.db.execute(
            "SELECT p.content_id, v.key, p.path, MAX(COALESCE(p.captured, p.file_time)) AS t "
            f"{where} AND p.content_id NOT IN (SELECT value FROM json_each(?)) "
            "GROUP BY p.content_id ORDER BY t DESC, p.content_id LIMIT ?",
            (*args, json.dumps(sorted(skip)), limit),
        )
        return [tuple(row[:3]) for row in rows]

    def count_pending_embeddings(self, model, volume_keys):
        where, args = _pending_embeddings(model, volume_keys)
        return self.db.execute(f"SELECT COUNT(DISTINCT p.content_id) {where}", args).fetchone()[0]

    def write_embeddings(self, model, vectors=(), failed=()):
        """`vectors`: (content_id, float16 blob); `failed`: (content_id, message)."""
        with self.transaction():
            self.db.executemany(
                "INSERT OR REPLACE INTO embeddings (content_id, model, vector, error) "
                "VALUES (?, ?, ?, NULL)",
                [(cid, model, blob) for cid, blob in vectors],
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO embeddings (content_id, model, vector, error) "
                "VALUES (?, ?, NULL, ?)",
                [(cid, model, message) for cid, message in failed],
            )

    def embeddings(self, model, after=0):
        """[(row id, content_id, float16 blob)] of `model`'s vectors written after row
        `after`, in order: a search index reads only what is new."""
        rows = self.db.execute(
            "SELECT id, content_id, vector FROM embeddings "
            "WHERE model = ? AND id > ? AND vector IS NOT NULL ORDER BY id",
            (model, after),
        )
        return [tuple(row) for row in rows]

    def embedding(self, content_id, model):
        row = self.db.execute(
            "SELECT vector FROM embeddings WHERE content_id = ? AND model = ?",
            (content_id, model),
        ).fetchone()
        return row[0] if row is not None else None

    def vectors_of(self, model, content_ids):
        """{content_id: float16 blob} of those of `content_ids` that have a vector."""
        ids = list(dict.fromkeys(content_ids))
        found = {}
        for start in range(0, len(ids), 500):
            part = ids[start : start + 500]
            rows = self.db.execute(
                "SELECT content_id, vector FROM embeddings WHERE model = ? "
                f"AND vector IS NOT NULL AND content_id IN ({', '.join('?' * len(part))})",
                (model, *part),
            )
            found.update((row[0], row[1]) for row in rows)
        return found
