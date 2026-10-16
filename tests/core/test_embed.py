"""The encoder's own steps without the model, and with it where it is installed:
`$LUMINA_MODELS` names a models folder that has it (as `$XDG_DATA_HOME/lumina/models`
does once the app downloaded it). Without it those tests skip, as on CI."""

import os

import numpy as np
import pytest
from photos import make_jpeg
from PIL import Image

from lumina.core import embed, models
from lumina.core.errors import PhotoError


@pytest.fixture
def tiny_tokenizer(tmp_path):
    """An encoder whose tokenizer knows six words and adds <eos>, as SigLIP 2's does."""
    from tokenizers import Tokenizer, pre_tokenizers, processors
    from tokenizers import models as token_models

    vocabulary = {"<pad>": 0, "<eos>": 1, "<unk>": 3, "a": 4, "red": 5, "photo": 6}
    tokenizer = Tokenizer(token_models.WordLevel(vocabulary, unk_token="<unk>"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="$A <eos>", special_tokens=[("<eos>", 1)]
    )
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    return embed.Encoder(str(tmp_path))


def test_text_is_lowercased_ended_and_padded(tiny_tokenizer):
    ids = tiny_tokenizer.token_ids(["A RED Photo", "red"])

    assert ids.shape == (2, 64) and ids.dtype == np.int64
    assert list(ids[0, :5]) == [4, 5, 6, 1, 0]
    assert list(ids[1, :3]) == [5, 1, 0]


def test_a_long_text_is_cut_to_the_model_length(tiny_tokenizer):
    ids = tiny_tokenizer.token_ids(["red " * 100])

    assert ids.shape == (1, 64) and (ids[0] == 5).all()


def test_a_photo_is_squashed_to_224_and_scaled_to_plus_minus_one():
    picture = Image.new("RGB", (300, 100), (255, 0, 128))

    values = embed.pixel_values(picture)

    assert values.shape == (3, 224, 224) and values.dtype == np.float32
    assert values[0].min() == 1 and values[1].max() == -1
    assert abs(values[2].mean() - (128 / 127.5 - 1)) < 1e-6


def test_vectors_are_kept_as_float16_and_read_back_as_float32():
    vectors = np.random.default_rng(0).standard_normal((3, embed.DIMENSIONS)).astype(np.float32)

    back = embed.from_blobs([embed.to_blob(v) for v in vectors])

    assert back.dtype == np.float32 and back.shape == (3, embed.DIMENSIONS)
    assert np.allclose(back, vectors, atol=2e-3)
    assert embed.from_blobs([]).shape == (0, embed.DIMENSIONS)


def test_the_match_probability_and_its_cosine_agree():
    assert embed.match_probability(embed.cosine_for(0.5)) == pytest.approx(0.5)
    assert embed.match_probability(0.177) > 0.9
    assert embed.match_probability(0.05) < 0.001


def _installed():
    root = os.environ.get("LUMINA_MODELS")
    if not root or not models.is_installed(embed.MODEL, root):
        pytest.skip("$LUMINA_MODELS does not have the search model")
    return models.directory(embed.MODEL, root)


@pytest.fixture
def colours(tmp_path):
    made = {}
    for name, colour in {
        "red": (220, 20, 20),
        "green": (20, 180, 20),
        "blue": (20, 40, 220),
    }.items():
        made[name] = str(make_jpeg(tmp_path / f"{name}.jpg", size=(320, 240), colour=colour))
    return made


@pytest.mark.parametrize(
    "query, colour",
    [("a red picture", "red"), ("punainen kuva", "red"), ("sininen", "blue"), ("green", "green")],
)
def test_the_model_finds_a_colour_in_english_and_finnish(colours, query, colour):
    folder = _installed()

    blobs = embed.encode_files(folder, list(colours.values()))
    vectors = embed.from_blobs(blobs)
    text = embed.encode_texts(folder, [query])[0]

    assert list(colours)[int(np.argmax(vectors @ text))] == colour
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-2)


def test_the_model_names_a_photo_it_could_not_read(tmp_path, colours):
    folder = _installed()
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a photo")

    results = embed.encode_files(folder, [colours["red"], str(broken)])

    assert isinstance(results[0], bytes) and len(results[0]) == embed.DIMENSIONS * 2
    assert isinstance(results[1], PhotoError) and results[1].path == str(broken)
