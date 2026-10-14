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


def make_jpeg_with_exif(path, orientation=1, gps=True):
    """A JPEG with the metadata a camera writes: capture time with sub-seconds and
    offset, camera, lens, exposure and, with `gps`, a position south and east."""
    from PIL.TiffImagePlugin import IFDRational

    image = Image.new("RGB", (48, 32), (90, 90, 90))
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
    image.save(path, "JPEG", exif=exif.tobytes())
    return path
