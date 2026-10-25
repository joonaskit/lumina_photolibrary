"""Helpers of the library database."""

import time

from lumina.core import metadata
from lumina.core.dbtypes import COPIED


def _inside(path, folder):
    """Whether `path` is strictly inside `folder`, both from a filesystem's root."""
    return folder == "" and path != "" or path.startswith(folder + "/")


def _where(shown):
    clauses, args = [], []
    if shown.folder_id is not None:
        clauses.append("p.folder_id = ?")
        args.append(shown.folder_id)
    if shown.year is not None:
        # A range, so that the index on the time is used
        clauses.append(
            "COALESCE(p.captured, p.file_time) >= ? AND COALESCE(p.captured, p.file_time) < ?"
        )
        args.extend([f"{int(shown.year):04d}", f"{int(shown.year) + 1:04d}"])
    if shown.model is not None:
        clauses.append("p.model = ?")
        args.append(shown.model)
    if shown.lens is not None:
        clauses.append("p.lens = ?")
        args.append(shown.lens)
    if shown.collection_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM collection_photos m WHERE m.photo_id = p.id "
            "AND m.collection_id = ?)"
        )
        args.append(shown.collection_id)
    if shown.import_job is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM import_items i WHERE i.job_id = ? AND i.state = ? "
            "AND i.volume_id = p.volume_id AND i.path = p.path)"
        )
        args.extend([shown.import_job, COPIED])
    if shown.time_ranges:
        taken = "COALESCE(p.captured, p.file_time)"
        clauses.append(
            "(" + " OR ".join(f"({taken} >= ? AND {taken} < ?)" for _ in shown.time_ranges) + ")"
        )
        for low, high in shown.time_ranges:
            args.extend([low, high])
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", args


_JOB_COLUMNS = (
    "SELECT j.id, j.kind, j.folder_id, j.state, j.phase, j.done, j.total, j.message, "
    "(SELECT COUNT(*) FROM job_failures WHERE job_id = j.id), j.derived_id FROM jobs j"
)


def _pending_embeddings(model, volume_keys):
    keys = list(volume_keys)
    marks = ", ".join("?" * len(keys))
    return (
        "FROM photos p JOIN volumes v ON v.id = p.volume_id "
        f"WHERE p.available = 1 AND p.content_id IS NOT NULL AND v.key IN ({marks}) "
        "AND NOT EXISTS (SELECT 1 FROM embeddings e "
        "WHERE e.content_id = p.content_id AND e.model = ?)",
        (*keys, model),
    )


def _pending_sharpness(volume_keys):
    keys = list(volume_keys)
    marks = ", ".join("?" * len(keys))
    return (
        "FROM photos p JOIN volumes v ON v.id = p.volume_id "
        f"WHERE p.available = 1 AND p.content_id IS NOT NULL AND v.key IN ({marks}) "
        "AND NOT EXISTS (SELECT 1 FROM sharpness h WHERE h.content_id = p.content_id)",
        keys,
    )


def _metadata_columns(data):
    captured = offset = None
    if data.captured is not None:
        if data.captured.utcoffset() is not None:
            offset = _format_offset(data.captured.utcoffset())
        captured = data.captured.replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
    return (
        captured,
        offset,
        data.make,
        data.model,
        data.lens,
        data.focal_length,
        data.aperture,
        data.exposure_time,
        data.iso,
        data.orientation,
        data.latitude,
        data.longitude,
        data.file_type,
        metadata.damage_warning(data),
    )


def _format_offset(delta):
    minutes = round(delta.total_seconds() / 60)
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def local_time(mtime_ns):
    """A file time as local wall-clock ISO text, as `captured` is."""
    seconds, nanos = divmod(mtime_ns, 1_000_000_000)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
    return f"{stamp}.{nanos // 1000:06d}"
