"""What the app remembers between runs, in QSettings under the app's name."""

from PySide6.QtCore import QSettings

from lumina import APP_ID


def _settings():
    return QSettings(APP_ID, APP_ID)


def last_folder():
    """The folder last added, to start the folder chooser in."""
    value = _settings().value("library/last_folder")
    return value if isinstance(value, str) and value else None


def set_last_folder(path):
    _settings().setValue("library/last_folder", path)


def selected_folder():
    """The library folder shown last, or None for all photos."""
    value = _settings().value("library/selected_folder")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def set_selected_folder(folder_id):
    _settings().setValue("library/selected_folder", "" if folder_id is None else folder_id)


def clear():
    _settings().clear()


def export_choices():
    """What the export dialog had last time: a dict, empty at first."""
    value = _settings().value("export/choices")
    return dict(value) if isinstance(value, dict) else {}


def set_export_choices(choices):
    _settings().setValue("export/choices", choices)


def preset_sections():
    """The parts of an edit the preset dialog had chosen last time (names from the
    edit's sections), or None at first. Kept as one string: a list of one comes back
    from QSettings as a plain string."""
    value = _settings().value("develop/preset_sections")
    if not isinstance(value, str):
        return None
    return [name for name in value.split(",") if name]


def set_preset_sections(sections):
    _settings().setValue("develop/preset_sections", ",".join(sections))


def copy_sections():
    """The parts of an edit the Copy Settings dialog had chosen last time, or None at
    first (the dialog then offers the look and the exposure)."""
    value = _settings().value("develop/copy_sections")
    if not isinstance(value, str):
        return None
    return [name for name in value.split(",") if name]


def set_copy_sections(sections):
    _settings().setValue("develop/copy_sections", ",".join(sections))


def grid_size():
    """The grid's thumbnail size: "small", "medium" or "large"."""
    value = _settings().value("library/grid_size")
    return value if value in ("small", "medium", "large") else "small"


def set_grid_size(name):
    _settings().setValue("library/grid_size", name)


def rejected_photos():
    """What the grid does with thumbs-down photos: "show", "dim" or "hide"."""
    value = _settings().value("library/rejected")
    return value if value in ("show", "dim", "hide") else "dim"


def set_rejected_photos(mode):
    _settings().setValue("library/rejected", mode)


# The most the thumbnail cache may take, in gigabytes; 0 is no limit
THUMBNAIL_LIMIT_DEFAULT = 4


def thumbnail_limit_gb():
    value = _settings().value("cache/thumbnail_limit_gb")
    try:
        return max(0, int(value)) if value not in (None, "") else THUMBNAIL_LIMIT_DEFAULT
    except (TypeError, ValueError):
        return THUMBNAIL_LIMIT_DEFAULT


def set_thumbnail_limit_gb(gigabytes):
    _settings().setValue("cache/thumbnail_limit_gb", max(0, int(gigabytes)))


def thumbnail_limit():
    """The limit in bytes, or None for none."""
    gigabytes = thumbnail_limit_gb()
    return gigabytes * 1024**3 if gigabytes else None


def gentle_scans():
    """Whether a large scan rests between batches and reads at a low priority. On at
    first."""
    return _settings().value("scan/gentle", True) in (True, "true")


def set_gentle_scans(on):
    _settings().setValue("scan/gentle", bool(on))


def watch_folders():
    """Whether the library's folders are watched for changes while Lumina runs. On at
    first."""
    return _settings().value("scan/watch", True) in (True, "true")


def set_watch_folders(on):
    _settings().setValue("scan/watch", bool(on))


def density():
    """How much room the interface takes: "comfortable" or "compact"."""
    value = _settings().value("view/density")
    return value if value in ("comfortable", "compact") else "compact"


def set_density(name):
    _settings().setValue("view/density", name)


def advance_after_flag():
    """Whether the viewer steps to the next photo after one is flagged. Off at first."""
    return _settings().value("viewer/advance_after_flag") in (True, "true")


def set_advance_after_flag(on):
    _settings().setValue("viewer/advance_after_flag", bool(on))


def merged_order(stored, default):
    """The order of the Develop panel's sections: `stored` (names, in the user's order)
    without those that are no more, and a section that is new at its place in
    `default`: after the one before it there."""
    order = []
    for name in stored:
        if name in default and name not in order:
            order.append(name)
    for index, name in enumerate(default):
        if name in order:
            continue
        before = next((n for n in reversed(default[:index]) if n in order), None)
        order.insert(0 if before is None else order.index(before) + 1, name)
    return order


def _names(key):
    value = _settings().value(key)
    return [name for name in value.split(",") if name] if isinstance(value, str) else []


def develop_order(default):
    """The Develop panel's sections (`default` is their names in the default order) in
    the order the user chose."""
    return merged_order(_names("develop/section_order"), tuple(default))


def set_develop_order(order):
    _settings().setValue("develop/section_order", ",".join(order))


def hidden_sections():
    """The names of the Develop panel's sections the user hid; they stay in the edit."""
    return set(_names("develop/hidden_sections"))


def set_hidden_sections(names):
    _settings().setValue("develop/hidden_sections", ",".join(sorted(names)))


def collapsed_sections():
    """The names of the Develop panel's sections folded on the panel."""
    return set(_names("develop/collapsed_sections"))


def set_collapsed_sections(names):
    _settings().setValue("develop/collapsed_sections", ",".join(sorted(names)))


def reset_develop_panel():
    """Back to the default order with every section shown (folding is kept)."""
    _settings().remove("develop/section_order")
    _settings().remove("develop/hidden_sections")


def merge_deghost():
    """The deghosting an HDR merge was last asked for: off at first."""
    value = _settings().value("merge/deghost")
    return value if value in ("off", "low", "medium", "high") else "off"


def merge_align():
    """Whether an HDR merge was last asked to align the photos. On at first."""
    return _settings().value("merge/align", True) in (True, "true")


def set_merge_choices(deghost, align):
    _settings().setValue("merge/deghost", deghost)
    _settings().setValue("merge/align", bool(align))


def panorama_projection():
    """The projection a panorama was last asked for: cylindrical at first."""
    value = _settings().value("panorama/projection")
    return value if value in ("cylindrical", "spherical", "perspective") else "cylindrical"


def panorama_crop():
    """Whether a panorama was last asked to be cropped to a rectangle. On at first."""
    return _settings().value("panorama/crop", True) in (True, "true")


def set_panorama_choices(projection, crop):
    _settings().setValue("panorama/projection", projection)
    _settings().setValue("panorama/crop", bool(crop))
