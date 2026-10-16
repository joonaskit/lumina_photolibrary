"""Keeping the XMP sidecars (core/xmp.py) in step with the database, both ways.

Writing: a photo whose flag, stars, keywords or edits changed since its sidecar was
written (`Library.sidecar_pending`) gets a new one. The database says what is out of date,
so a quit or a crash in the middle resumes by itself. A photo on a drive that is not
connected waits; a folder that cannot be written to, or another program's `.xmp` in the
way, is named and skipped, and the rest go on.

Reading: a scan looks beside each photo for a sidecar whose file time is not the one
noted when it was written or read here; of those, one newer than the library's last
change of the photo is taken in, so a library can be rebuilt from its photos and their
sidecars, and a copied folder keeps its edits. The library's own change wins when it is
newer, and is written to the sidecar.
"""

import os
from dataclasses import dataclass, field

from lumina.core import volumes, xmp

SIDECARS = "sidecars"
_BATCH = 100


@dataclass
class Written:
    written: int = 0
    # (path, why) of sidecars that could not be written
    failures: list = field(default_factory=list)
    # Photos whose drive is not connected, or that are marked missing: left for later
    waiting: int = 0
    stopped: bool = False


def write_pending(library, mounts, should_stop=lambda: False, report=lambda done, total: None):
    """Write the sidecars that are out of date. `report(done, total)` after each."""
    result = Written()
    total = library.count_sidecars_pending()
    done = 0
    after = 0
    while not should_stop():
        batch = library.sidecar_pending(after, _BATCH)
        if not batch:
            return result
        for photo_id, key, path, available, _changed in batch:
            if should_stop():
                result.stopped = True
                return result
            after = photo_id
            done += 1
            where = volumes.resolve(key, path, mounts) if available else None
            if where is None:
                result.waiting += 1
                continue
            state = library.sidecar_state(photo_id)
            try:
                mtime = xmp.write(where, state)
            except xmp.SidecarError as error:
                result.failures.append((error.path, error.message))
                continue
            library.sidecar_written(photo_id, state.updated, mtime)
            result.written += 1
            report(done, total)
    result.stopped = True
    return result


@dataclass
class Read:
    # Sidecars taken in, and those the library's own newer change won over
    used: int = 0
    kept: int = 0
    failures: list = field(default_factory=list)


def read_changed(library, folder_id, locate, should_stop=lambda: False, report=None):
    """Look beside the photos of folder `folder_id` for sidecars that changed since they
    were written or read here, and take in the newer ones. `locate(relative path)` is the
    photo's path now. `report(done, total)` as it goes. Returns a Read."""
    result = Read()
    candidates = library.sidecar_candidates(folder_id)
    for number, (photo_id, relative, noted) in enumerate(candidates, 1):
        if should_stop():
            break
        photo = locate(relative)
        try:
            mtime = os.stat(xmp.sidecar_path(photo)).st_mtime_ns
        except FileNotFoundError:
            if noted is not None:
                library.forget_sidecar(photo_id)
            mtime = None
        except OSError as error:
            result.failures.append((xmp.sidecar_path(photo), error.strerror or str(error)))
            mtime = None
        if mtime is not None and mtime != noted:
            try:
                state = xmp.read(photo)
            except xmp.ForeignSidecar:
                # Another program's: not ours to read, and not looked at again until it changes
                library.note_sidecar(photo_id, mtime)
            except xmp.SidecarError as error:
                result.failures.append((error.path, error.message))
            else:
                if state is not None:
                    if library.apply_sidecar(photo_id, state, mtime):
                        result.used += 1
                    else:
                        result.kept += 1
        if report is not None and number % 200 == 0:
            report(number, len(candidates))
    return result
