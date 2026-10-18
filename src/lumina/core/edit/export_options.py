"""What an export is asked for: the format, its bits and quality, the size; and the file
name it suggests. Plain data, which the UI builds."""

import os
from dataclasses import dataclass

FORMATS = {"jpeg": (8,), "png": (8, 16), "tiff": (8, 16)}
EXTENSIONS = {"jpeg": ".jpg", "png": ".png", "tiff": ".tif"}


@dataclass(frozen=True)
class ExportOptions:
    format: str = "jpeg"
    bits: int = 8
    quality: int = 92
    # Longest side in pixels, or None for full size
    max_side: int | None = None

    def check(self):
        if self.format not in FORMATS:
            raise ValueError(f"unknown format {self.format!r}")
        if self.bits not in FORMATS[self.format]:
            raise ValueError(f"{self.format} can't hold {self.bits} bits")


def suggested_name(source_path, options, copy=0):
    """The file name for an export: the photo's, with `-copy2` for virtual copy 2, so a
    copy does not land on the name of the photo's own export."""
    stem = os.path.splitext(os.path.basename(source_path))[0]
    if copy:
        stem += f"-copy{copy}"
    return stem + EXTENSIONS[options.format]
