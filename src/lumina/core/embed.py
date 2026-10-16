"""Photos and text as vectors in one space, by SigLIP 2 through ONNX Runtime.

A photo and a query that mean the same thing get vectors pointing the same way:
search is the cosine between them. Vectors are L2-normalised float32 here and kept
as float16 in the database.

Preprocessing is the model's own (PLAN.md, Phase 4 decisions): the photo squashed to
224 x 224 and scaled to [-1, 1]; the text lowercased, tokenized with `<eos>` and
padded with zeros to 64 tokens.
"""

import math
import os

import numpy as np
from PIL import Image

from lumina.core import images, models
from lumina.core.errors import PhotoError

MODEL = models.SIGLIP2
DIMENSIONS = 768
_SIDE = 224
_TEXT_LENGTH = 64
# Decoded this large before squashing to 224: the short side of most photos stays above
_DECODE_SIDE = 512

# SigLIP's learned scale and bias, not in the split ONNX files: from the original
# weights (google/siglip2-base-patch16-224, model.safetensors)
_LOGIT_SCALE = math.exp(4.724453449249268)
_LOGIT_BIAS = -16.771724700927734


def match_probability(cosine):
    """How likely SigLIP thinks a photo shows what a text says, from their cosine."""
    return 1 / (1 + np.exp(-(_LOGIT_SCALE * np.asarray(cosine) + _LOGIT_BIAS)))


def cosine_for(probability):
    """The cosine at which match_probability is `probability`."""
    return float((math.log(probability / (1 - probability)) - _LOGIT_BIAS) / _LOGIT_SCALE)


def to_blob(vector):
    return np.asarray(vector, np.float16).tobytes()


def from_blobs(blobs):
    """float16 blobs as one float32 matrix, a row each."""
    if not blobs:
        return np.zeros((0, DIMENSIONS), np.float32)
    joined = np.frombuffer(b"".join(blobs), np.float16).reshape(len(blobs), -1)
    return joined.astype(np.float32)


class Encoder:
    """The model's two halves, each loaded when first used. `threads` limits how many
    cores one call uses (None: ONNX Runtime's choice, all of them)."""

    def __init__(self, folder, threads=None):
        self.folder = folder
        self.threads = threads
        self._vision = self._text = self._tokenizer = None

    def images(self, pictures):
        """Vectors of PIL images, one row each."""
        if not pictures:
            return np.zeros((0, DIMENSIONS), np.float32)
        batch = np.stack([pixel_values(p) for p in pictures])
        session = self._vision or self._load_vision()
        return _normalise(session.run(["pooler_output"], {"pixel_values": batch})[0])

    def texts(self, queries):
        session = self._text or self._load_text()
        return _normalise(session.run(["pooler_output"], {"input_ids": self.token_ids(queries)})[0])

    def token_ids(self, queries):
        tokenizer = self._tokenizer or self._load_tokenizer()
        ids = np.zeros((len(queries), _TEXT_LENGTH), np.int64)
        for row, query in enumerate(queries):
            tokens = tokenizer.encode(query.lower()).ids[:_TEXT_LENGTH]
            ids[row, : len(tokens)] = tokens
        return ids

    def _load_vision(self):
        self._vision = self._session("onnx/vision_model_fp16.onnx")
        return self._vision

    def _load_text(self):
        self._text = self._session("onnx/text_model_fp16.onnx")
        return self._text

    def _load_tokenizer(self):
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_file(os.path.join(self.folder, "tokenizer.json"))
        return self._tokenizer

    def _session(self, name):
        import onnxruntime

        options = onnxruntime.SessionOptions()
        if self.threads:
            options.intra_op_num_threads = self.threads
        return onnxruntime.InferenceSession(
            os.path.join(self.folder, name), options, providers=["CPUExecutionProvider"]
        )


def pixel_values(picture):
    """A PIL image as the model's input: 3 x 224 x 224 float32 in [-1, 1]."""
    square = picture.convert("RGB").resize((_SIDE, _SIDE), Image.Resampling.BILINEAR)
    return (np.asarray(square, np.float32) / 127.5 - 1).transpose(2, 0, 1)


def _normalise(vectors):
    vectors = np.asarray(vectors, np.float32)
    return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)


# In the embedding worker process: the encoder stays loaded between batches
_encoder = None


def encode_files(folder, paths, threads=None):
    """For a worker process: [float16 blob or PhotoError] for each path, in order.
    The process lowers its own priority the first time, so the UI and the Develop
    view go first."""
    global _encoder
    if _encoder is None:
        # Once: os.nice adds to the niceness it has
        _lower_priority()
    if _encoder is None or _encoder.folder != folder:
        _encoder = Encoder(folder, threads)
    results, pictures, places = [None] * len(paths), [], []
    for n, path in enumerate(paths):
        try:
            pixels = images.load_pixels(path, _DECODE_SIDE)
        except PhotoError as error:
            results[n] = error
            continue
        pictures.append(Image.frombytes("RGB", (pixels.width, pixels.height), pixels.data))
        places.append(n)
    for n, vector in zip(places, _encoder.images(pictures), strict=True):
        results[n] = to_blob(vector)
    return results


# In the query worker process: the text half stays loaded between searches. Loading
# its tokenizer holds the GIL for most of a second, which on a thread of the app's
# own process stops the UI too
_query_encoder = None


def encode_texts(folder, queries):
    """For a worker process: the vectors of `queries`, one row each."""
    global _query_encoder
    if _query_encoder is None or _query_encoder.folder != folder:
        # Two threads: a query is short, and indexing may be using the rest
        _query_encoder = Encoder(folder, threads=2)
    return _query_encoder.texts(queries)


def forget_texts():
    """For the query worker process: let go of the text half (the model was deleted)."""
    global _query_encoder
    _query_encoder = None


def _lower_priority():
    try:
        os.nice(10)
    except OSError:
        pass
