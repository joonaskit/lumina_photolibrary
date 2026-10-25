"""Sharpness: the part of `Library` for it (see db.py)."""

import json

from lumina.core import sharpness
from lumina.core.dbtypes import UNFLAGGED
from lumina.core.dbutil import _pending_sharpness


class SharpnessStore:
    # Sharpness

    def pending_sharpness(self, volume_keys, limit, skip=()):
        """[(content_id, volume_key, path)] of photos with no score yet, one per content
        id, on the volumes `volume_keys` (the connected ones), newest first. `skip` are the
        content ids a job passed over, left out."""
        where, args = _pending_sharpness(volume_keys)
        rows = self.db.execute(
            "SELECT p.content_id, v.key, p.path, MAX(COALESCE(p.captured, p.file_time)) AS t "
            f"{where} AND p.content_id NOT IN (SELECT value FROM json_each(?)) "
            "GROUP BY p.content_id ORDER BY t DESC, p.content_id LIMIT ?",
            (*args, json.dumps(sorted(skip)), limit),
        )
        return [tuple(row[:3]) for row in rows]

    def count_pending_sharpness(self, volume_keys):
        where, args = _pending_sharpness(volume_keys)
        return self.db.execute(f"SELECT COUNT(DISTINCT p.content_id) {where}", args).fetchone()[0]

    def write_sharpness(self, scores=(), failed=()):
        """`scores`: (content_id, score); `failed`: (content_id, message)."""
        with self.transaction():
            self.db.executemany(
                "INSERT OR REPLACE INTO sharpness (content_id, score, error) VALUES (?, ?, NULL)",
                list(scores),
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO sharpness (content_id, score, error) VALUES (?, NULL, ?)",
                list(failed),
            )

    def pick_best(self, stack_ids):
        """Put the sharpest photo on top of each of these stacks (core/sharpness.py `pick`
        says which). A stack with a photo not scored yet is left as it is. Returns
        ({stack id: the photo now on top}, how many stacks were left)."""
        picked, skipped = {}, 0
        with self.transaction():
            for stack in dict.fromkeys(stack_ids):
                members = self._scored_members(stack)
                best = sharpness.pick(members)
                if best is None:
                    skipped += 1
                    continue
                picked[stack] = best
                self.set_stack_top(best)
        return picked, skipped

    def _scored_members(self, stack_id):
        rows = self.db.execute(
            "SELECT p.id, p.content_id, COALESCE(p.captured, p.file_time), p.flag, p.rating, "
            "h.score FROM photos p LEFT JOIN sharpness h ON h.content_id = p.content_id "
            "WHERE p.stack_id = ? ORDER BY p.stack_position, p.id",
            (stack_id,),
        )
        return [
            sharpness.Member(r[0], r[2], r[3] if r[3] is not None else UNFLAGGED, r[4], r[5])
            for r in rows
        ]
