"""An edited photo at screen or thumbnail size: for the grid and the viewer.

Rendered from a half-size decode, which is plenty for anything up to about 3000
pixels and four times faster than the full one. Runs in worker processes.
"""

from lumina.core.edit import params as edit_params
from lumina.core.edit import pipeline, source
from lumina.core.images import Pixels


def edited_pixels(path, max_side, edit_json):
    """The photo at `path` with the edit `edit_json`, at most `max_side` on its longest
    side, as 8-bit Pixels. Raises PhotoError naming the photo, or NewerParams."""
    edit = edit_params.from_json(edit_json)
    linear = source.load_linear(path, half=True)
    image = pipeline.render(source.fitted(linear, edit, max_side), edit)
    return Pixels(image.shape[1], image.shape[0], image.tobytes())
