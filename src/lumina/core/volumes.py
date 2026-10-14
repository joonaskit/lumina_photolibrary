"""Which filesystem a path is on, and where a filesystem is mounted now.

A photo is stored as a volume id and its path from that filesystem's own root, so
an external drive keeps its photos however and wherever it is mounted next time,
and a drive that is not plugged in is "not connected" rather than "deleted".

The id is the filesystem's UUID where it has one. The path is from the root of the
filesystem, not of the mount: on btrfs `/` and `/home` are often two subvolumes of
one filesystem with one UUID, and mountinfo's root field ("/root", "/home") keeps
their files apart. A filesystem without a UUID (tmpfs, most network mounts) is
known by where it is mounted.

Linux only for now: everything comes from /proc/self/mountinfo and /dev/disk.
"""

import os
import re
from dataclasses import dataclass

MOUNTINFO = "/proc/self/mountinfo"
BY_UUID = "/dev/disk/by-uuid"
BY_LABEL = "/dev/disk/by-label"

_ESCAPE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class Mount:
    # The volume's id: "uuid:<UUID>" or, without one, "mount:<mount point>"
    volume_id: str
    # Where it is mounted, and which directory of the filesystem is mounted there
    mount_point: str
    root: str
    fstype: str
    source: str
    label: str | None = None


def _unescape(field):
    # mountinfo writes space, tab, newline and backslash as octal escapes
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text, uuid_of=None, label_of=None):
    """The mounts in mountinfo `text`, in its order. `uuid_of(source)` and
    `label_of(source)` give a device's UUID and label, or None."""
    uuid_of = uuid_of or device_uuid
    label_of = label_of or device_label
    mounts = []
    for line in text.splitlines():
        fields = line.split(" ")
        if "-" not in fields:
            continue
        dash = fields.index("-")
        if dash < 6 or len(fields) < dash + 3:
            continue
        root, mount_point = _unescape(fields[3]), _unescape(fields[4])
        fstype, source = fields[dash + 1], _unescape(fields[dash + 2])
        uuid = uuid_of(source)
        volume_id = f"uuid:{uuid}" if uuid else f"mount:{mount_point}"
        mounts.append(Mount(volume_id, mount_point, root, fstype, source, label_of(source)))
    return mounts


def read_mounts():
    """The mounts of this system now. An empty list when mountinfo cannot be read."""
    try:
        with open(MOUNTINFO, encoding="utf-8", errors="surrogateescape") as file:
            return parse_mountinfo(file.read())
    except OSError:
        return []


def _devices(folder):
    """{device number: name} of the links in a /dev/disk folder."""
    found = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return found
    for name in names:
        try:
            found[os.stat(os.path.join(folder, name)).st_rdev] = _unescape_udev(name)
        except OSError:
            continue
    return found


def _unescape_udev(name):
    # udev writes a space in a label as \x20
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)


def _device_number(source):
    if not source.startswith("/dev/"):
        return None
    try:
        return os.stat(source).st_rdev
    except OSError:
        return None


def device_uuid(source):
    number = _device_number(source)
    return _devices(BY_UUID).get(number) if number is not None else None


def device_label(source):
    number = _device_number(source)
    return _devices(BY_LABEL).get(number) if number is not None else None


def _below(path, top):
    return path == top or path.startswith(top.rstrip("/") + "/")


def mount_of(path, mounts):
    """The mount `path` is on: the one with the longest mount point above it, the last
    one mounted where several share it. None when nothing is."""
    best = None
    for mount in mounts:
        if _below(path, mount.mount_point):
            if best is None or len(mount.mount_point) >= len(best.mount_point):
                best = mount
    return best


def locate(path, mounts):
    """(volume id, path from the filesystem's root without a leading "/") of the
    directory or file at `path`, an absolute path. None when no mount holds it."""
    path = os.path.realpath(path)
    mount = mount_of(path, mounts)
    if mount is None:
        return None
    below = os.path.relpath(path, mount.mount_point)
    inside = os.path.normpath(os.path.join(mount.root, "" if below == "." else below))
    return mount.volume_id, inside.strip("/")


def resolve(volume_id, relative, mounts):
    """Where `relative` on `volume_id` is now, or None when that volume is not mounted
    or no mount of it shows that part of it."""
    relative = relative.strip("/")
    best = None
    for mount in mounts:
        if mount.volume_id != volume_id:
            continue
        root = mount.root.strip("/")
        if root == "" or relative == root or relative.startswith(root + "/"):
            # Of several mounts (a bind mount, a subvolume twice), the one mounting the
            # deepest part of the filesystem, then the shortest path
            key = (len(root), -len(mount.mount_point))
            if best is None or key > best[0]:
                best = (key, mount, root)
    if best is None:
        return None
    _key, mount, root = best
    rest = relative[len(root) :].lstrip("/") if root else relative
    return os.path.join(mount.mount_point, rest) if rest else mount.mount_point


def mounted_ids(mounts):
    return {mount.volume_id for mount in mounts}
