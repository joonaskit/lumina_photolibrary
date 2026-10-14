import os

from lumina.core import volumes
from lumina.core.volumes import locate, mount_of, parse_mountinfo, resolve

# As this machine has them: / and /home two subvolumes of one btrfs filesystem, /tmp a
# tmpfs, and a USB drive with a label, mounted at a path with a space in it
MOUNTINFO = """\
60 1 0:31 /root / rw,relatime shared:1 - btrfs /dev/nvme0n1p3 rw,seclabel
70 60 0:31 /home /home rw,relatime shared:2 - btrfs /dev/nvme0n1p3 rw,seclabel
80 60 0:35 / /tmp rw,nosuid shared:3 - tmpfs tmpfs rw,seclabel
90 60 8:17 / /run/media/me/My\\040Photos rw,nosuid shared:4 - exfat /dev/sdb1 rw
"""
UUIDS = {"/dev/nvme0n1p3": "c84ac11f", "/dev/sdb1": "E8A3-70FC"}


def mounts(text=MOUNTINFO):
    return parse_mountinfo(text, uuid_of=UUIDS.get, label_of={"/dev/sdb1": "My Photos"}.get)


def test_mountinfo_is_read_with_its_escapes():
    found = mounts()

    assert [m.mount_point for m in found] == ["/", "/home", "/tmp", "/run/media/me/My Photos"]
    assert found[3].volume_id == "uuid:E8A3-70FC"
    assert found[3].label == "My Photos"
    # No UUID: known by where it is mounted
    assert found[2].volume_id == "mount:/tmp"


def test_two_subvolumes_of_one_filesystem_do_not_collide():
    home = locate("/home/me/Pictures/a.jpg", mounts())
    root = locate("/me/Pictures/a.jpg", mounts())

    assert home == ("uuid:c84ac11f", "home/me/Pictures/a.jpg")
    assert root == ("uuid:c84ac11f", "root/me/Pictures/a.jpg")
    assert home != root


def test_the_longest_mount_point_wins():
    assert mount_of("/home/me", mounts()).mount_point == "/home"
    assert mount_of("/homework", mounts()).mount_point == "/"


def test_a_path_comes_back_to_where_it_is_mounted_now():
    found = mounts()

    assert resolve("uuid:c84ac11f", "home/me/a.jpg", found) == "/home/me/a.jpg"
    assert resolve("uuid:c84ac11f", "root/etc", found) == "/etc"
    volume, inside = locate("/run/media/me/My Photos/2022/a.CR3", found)
    assert (volume, inside) == ("uuid:E8A3-70FC", "2022/a.CR3")


def test_a_drive_mounted_somewhere_else_keeps_its_paths():
    volume, inside = locate("/run/media/me/My Photos/2022/a.CR3", mounts())
    elsewhere = MOUNTINFO.replace("/run/media/me/My\\040Photos", "/mnt/photos")

    assert resolve(volume, inside, mounts(elsewhere)) == "/mnt/photos/2022/a.CR3"


def test_a_drive_not_plugged_in_resolves_to_nothing():
    unplugged = "\n".join(line for line in MOUNTINFO.splitlines() if "sdb1" not in line)

    assert resolve("uuid:E8A3-70FC", "2022/a.CR3", mounts(unplugged)) is None


def test_the_root_of_a_volume():
    assert locate("/run/media/me/My Photos", mounts()) == ("uuid:E8A3-70FC", "")
    assert resolve("uuid:E8A3-70FC", "", mounts()) == "/run/media/me/My Photos"


def test_this_machine_round_trips(tmp_path):
    found = volumes.read_mounts()
    folder = tmp_path / "photos"
    folder.mkdir()

    volume, inside = locate(folder, found)

    assert os.path.samefile(resolve(volume, inside, found), folder)
