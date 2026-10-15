"""Small photos made for the tests, in tmp_path: never a real library."""

from PIL import Image

_EXIF_ORIENTATION = 0x0112


def make_jpeg(path, size=(64, 32), colour=(200, 40, 40), orientation=None):
    """A JPEG of one colour, `size` as stored, with an EXIF orientation if given."""
    image = Image.new("RGB", size, colour)
    exif = Image.Exif()
    if orientation is not None:
        exif[_EXIF_ORIENTATION] = orientation
    image.save(path, "JPEG", exif=exif.tobytes())
    return path


def make_png(path, size=(40, 30), colour=(20, 120, 220)):
    Image.new("RGB", size, colour).save(path, "PNG")
    return path


def camera_exif(orientation=1, gps=True):
    """The metadata a camera writes: capture time with sub-seconds and offset, camera,
    lens, exposure and, with `gps`, a position south and east."""
    from PIL.TiffImagePlugin import IFDRational

    exif = Image.Exif()
    exif[0x010F] = "Canon"
    exif[0x0110] = "Canon EOS R6m2"
    exif[_EXIF_ORIENTATION] = orientation
    details = exif.get_ifd(0x8769)
    details[0x9003] = "2024:06:01 18:30:05"
    details[0x9291] = "25"
    details[0x9011] = "+03:00"
    details[0xA434] = "RF24-105mm F4 L IS USM"
    details[0x920A] = IFDRational(50, 1)
    details[0x829D] = IFDRational(28, 10)
    details[0x829A] = IFDRational(1, 250)
    details[0x8827] = 400
    if gps:
        position = exif.get_ifd(0x8825)
        position[1] = "S"
        position[2] = (IFDRational(33), IFDRational(51), IFDRational(54))
        position[3] = "E"
        position[4] = (IFDRational(151), IFDRational(12), IFDRational(36))
    return exif


def make_jpeg_with_exif(path, orientation=1, gps=True):
    """A JPEG with the metadata of `camera_exif`."""
    image = Image.new("RGB", (48, 32), (90, 90, 90))
    image.save(path, "JPEG", exif=camera_exif(orientation, gps).tobytes())
    return path


def make_heic(
    path, size=(64, 32), colour=(200, 40, 40), orientation=None, icc=None, exif=None, thumbnail=None
):
    """A HEIC of one colour, `size` as stored, with an EXIF orientation, an ICC profile
    or a whole `Exif` if given, and a thumbnail inside it `thumbnail` pixels on its
    longer side. Lossy, as they all are: colours are right to a few steps."""
    import pillow_heif

    pillow_heif.register_heif_opener()
    image = Image.new("RGB", size, colour)
    if exif is None:
        exif = Image.Exif()
        if orientation is not None:
            exif[_EXIF_ORIENTATION] = orientation
    options = {"exif": exif.tobytes(), "quality": 95}
    if icc is not None:
        options["icc_profile"] = icc
    if thumbnail is not None:
        options["thumbnails"] = [thumbnail]
    image.save(path, "HEIF", **options)
    return path


# Colorants of RGB spaces adapted to D50, as a profile holds them: (red, green, blue) XYZ
SRGB_COLORANTS = ((0.4360, 0.2225, 0.0139), (0.3851, 0.7169, 0.0971), (0.1431, 0.0606, 0.7141))
DISPLAY_P3_COLORANTS = (
    (0.5151, 0.2412, -0.0011),
    (0.2920, 0.6922, 0.0419),
    (0.1571, 0.0666, 0.7841),
)


def icc_profile(description, colorants):
    """A matrix/TRC ICC profile (version 2.4) for an RGB space with sRGB's curve and
    the given `colorants`: made here, so the tests need no profile on the machine."""
    import struct

    def fixed(value):
        return struct.pack(">i", round(value * 65536))

    def xyz(values):
        return b"XYZ \0\0\0\0" + b"".join(fixed(v) for v in values)

    def text(string):
        return b"text\0\0\0\0" + string.encode() + b"\0"

    ascii_description = description.encode() + b"\0"
    desc = (
        b"desc\0\0\0\0"
        + struct.pack(">I", len(ascii_description))
        + ascii_description
        + b"\0" * 4
        + b"\0" * 4
        + b"\0" * 2
        + b"\0"
        + b"\0" * 67
    )
    # The sRGB curve, as a parametric curve of type 3
    curve = (
        b"para\0\0\0\0"
        + struct.pack(">HH", 3, 0)
        + b"".join(fixed(v) for v in (2.4, 1 / 1.055, 0.055 / 1.055, 1 / 12.92, 0.04045))
    )
    d50 = (0.9642, 1.0, 0.8249)
    tags = [
        (b"desc", desc),
        (b"cprt", text("Made for the tests")),
        (b"wtpt", xyz(d50)),
        (b"rXYZ", xyz(colorants[0])),
        (b"gXYZ", xyz(colorants[1])),
        (b"bXYZ", xyz(colorants[2])),
        (b"rTRC", curve),
        (b"gTRC", curve),
        (b"bTRC", curve),
    ]
    table = struct.pack(">I", len(tags))
    body, offset = b"", 128 + 4 + 12 * len(tags)
    for signature, data in tags:
        data += b"\0" * (-len(data) % 4)
        table += signature + struct.pack(">II", offset + len(body), len(data))
        body += data
    size = 128 + len(table) + len(body)
    header = (
        struct.pack(">I", size)
        + b"\0\0\0\0"
        + struct.pack(">I", 0x02400000)
        + b"mntrRGB XYZ "
        + struct.pack(">6H", 2024, 1, 1, 0, 0, 0)
        + b"acsp"
        # Platform, flags, manufacturer, model, attributes, rendering intent
        + b"\0" * 28
        + b"".join(fixed(v) for v in d50)
        + b"\0" * 48
    )
    assert len(header) == 128
    return header + table + body
