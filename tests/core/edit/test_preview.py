from photos import make_jpeg
from rawfiles import make_dng

from lumina.core.edit.params import Crop, Params
from lumina.core.edit.preview import edited_pixels


def test_an_edit_in_full_is_the_crop_of_the_whole_photo(tmp_path):
    path = make_jpeg(tmp_path / "a.jpg", size=(400, 200))
    edit = Params(crop=Crop(0.0, 0.0, 0.5, 1.0)).to_json()

    small = edited_pixels(path, 100, edit)
    whole = edited_pixels(path, None, edit)

    assert (whole.width, whole.height) == (200, 200)
    assert small.full == whole.full == (200, 200)
    assert max(small.width, small.height) == 100


def test_a_raw_edit_in_full_is_decoded_in_full(tmp_path):
    path = make_dng(tmp_path / "a.dng")
    edit = Params(exposure=0.5).to_json()

    half = edited_pixels(path, 10_000, edit)
    whole = edited_pixels(path, None, edit)

    assert (whole.width, whole.height) == whole.full
    # From the half-size decode, about the full size
    assert abs(half.full[0] - whole.width) <= 2 and abs(half.full[1] - whole.height) <= 2
    assert whole.width > half.width
