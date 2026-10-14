"""Turning core's pixels into Qt images."""

from PySide6.QtGui import QImage


def to_image(pixels):
    """A QImage of `core.images.Pixels`, owning its own copy of the data."""
    image = QImage(
        pixels.data, pixels.width, pixels.height, 3 * pixels.width, QImage.Format.Format_RGB888
    )
    return image.copy()
