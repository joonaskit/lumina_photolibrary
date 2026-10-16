"""An edited photo at screen or thumbnail size, for the grid and the viewer, or in
full for the viewer's 100 %.

Rendered from a half-size decode, which is plenty for anything up to about 3000
pixels and four times faster than the full one; in full from the full decode.
Runs in worker processes.
"""

from lumina.core import linear_tiff, raw
from lumina.core.edit import params as edit_params
from lumina.core.edit import pipeline, source
from lumina.core.images import Pixels


def edited_pixels(path, max_side, edit_json):
    """The photo at `path` with the edit `edit_json`, at most `max_side` on its longest
    side, or in full with `max_side` None, as 8-bit Pixels. Raises PhotoError naming
    the photo, or NewerParams."""
    edit = edit_params.from_json(edit_json)
    half = max_side is not None
    linear = source.load_linear(path, half=half)
    height, width = linear.shape[:2]
    if half and linear_tiff.is_linear(path):
        width, height = linear_tiff.size(path)
    elif half and raw.is_raw(path):
        # About what the full decode gives: the viewer's 100 % before it comes
        width, height = width * 2, height * 2
    full = pipeline.output_size(width, height, edit)
    if max_side is not None:
        linear = source.fitted(linear, edit, max_side)
    image = pipeline.render(linear, edit, full=(width, height))
    return Pixels(image.shape[1], image.shape[0], image.tobytes(), full)
