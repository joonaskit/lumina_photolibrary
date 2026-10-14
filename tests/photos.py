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
