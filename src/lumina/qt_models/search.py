"""Search for the UI: the model's download, the indexing job, and the searches.

Nothing is fetched until the user asks: `download()` is the only way the model
comes. Once it is there, an indexing job (core/indexing.py) runs whenever there may
be photos without a vector: at the start, after every scan, and when a drive comes.
It runs on a thread of its own (by the JobRunner of qt_models/jobs.py) and encodes in a
worker process of its own at low priority, apart from the scans, so a drive plugged in
is scanned at once, not after hours of indexing. A quit in the middle leaves it queued
for the next start.

Searches run on one thread that keeps the vectors in memory (core/search.py). The
words of a search are encoded in a worker process that keeps the text half of the
model loaded; the first search loads it, which takes a second or two. The newest
search wins: an older one's results are dropped.
"""

import time
from functools import partial

from PySide6.QtCore import QObject, Signal

from lumina.core import db as dbm
from lumina.core import embed, indexing, models
from lumina.core import search as searching
from lumina.qt_models import workers
from lumina.qt_models.jobs import JobRunner

# How often the download's progress reaches the UI, in seconds
_PROGRESS_EVERY = 0.2


class SearchService(QObject):
    # Whether the model is there or coming changed
    model_changed = Signal()
    # Bytes done and in all
    download_progress = Signal(int, int)
    # The reason, naming the file
    download_failed = Signal(str)
    # A JobStatus, while indexing runs and when it ends
    indexing_changed = Signal(object)
    # More photos can be found
    index_changed = Signal()

    def __init__(self, library, model_root=None, encode=None, encode_text=None, parent=None):
        """`library` is the LibraryService. For the tests: `encode(folder, paths)`
        replaces the worker process, `encode_text(text)` the text model."""
        super().__init__(parent)
        self.library = library
        self.model_root = model_root
        self.folder = models.directory(embed.MODEL, model_root)
        self._encode = encode or _encode_in_process
        self._quitting = workers.Token()
        self._download_stop = None
        self.jobs = JobRunner(
            self,
            library,
            {indexing.EMBED},
            workers.INDEXING,
            prepare=lambda _job: partial(_index, partial(self._encode, self.folder)),
            name=lambda _job: "",
            changed=self.indexing_changed.emit,
            ended=lambda _job, _status: self.index_changed.emit(),
            progress=self._on_progress,
        )
        self._removing = False
        self._generation = 0
        # Whether a search has loaded the text half of the model yet
        self._text_loaded = False
        self._searcher = _Searcher(library.path, self.folder, encode_text)
        # Kept for the tests, which wait for them
        self.download_future = self.remove_future = None
        library.job_ended.connect(lambda _status: self.index())
        library.mounts_changed.connect(self.index)

    def start(self):
        if self.is_ready():
            self.index()

    def close(self):
        """For the app's exit: a download or indexing in progress stops; indexing is
        queued for the next start."""
        self._quitting.cancel()
        self.jobs.close()
        self.cancel_download()

    # The model

    def is_ready(self):
        return models.is_installed(embed.MODEL, self.model_root)

    def is_downloading(self):
        return self._download_stop is not None

    def download_size(self):
        return embed.MODEL.size

    def download(self):
        """Fetch the model, continuing a download that stopped. Then index."""
        if self.is_downloading() or self.is_ready() or self._removing:
            return
        stop = workers.Token(self._quitting)
        self._download_stop = stop
        told = [0.0]

        def report(done, total):
            now = time.monotonic()
            if done == total or now - told[0] >= _PROGRESS_EVERY:
                told[0] = now
                workers.call_on_main(self, self.download_progress.emit, done, total)

        self.download_future = workers.submit(
            partial(models.download, embed.MODEL, self.model_root, report, stop),
            on_done=lambda _none: self._downloaded(None),
            on_error=self._downloaded,
            owner=self,
            lane=workers.INDEXING,
        )
        self.model_changed.emit()

    def cancel_download(self):
        if self._download_stop is not None:
            self._download_stop.cancel()

    def remove_model(self):
        """Delete the model's files, stopping a download or indexing first: on the
        indexing thread, after what runs there now. The vectors stay, for when the
        same model comes back; search asks for it again until then."""
        if self._removing:
            return
        self._removing = True
        self.cancel_download()
        self.cancel_indexing()
        self.remove_future = workers.submit(
            models.remove,
            embed.MODEL,
            self.model_root,
            on_done=lambda _none: self._removed(),
            on_error=lambda _error: self._removed(),
            owner=self,
            lane=workers.INDEXING,
        )
        self.model_changed.emit()

    def is_removing(self):
        return self._removing

    def _removed(self):
        self._removing = False
        self._text_loaded = False
        # The text model the searches keep loaded goes too
        workers.submit(
            self._searcher.forget_model,
            on_done=lambda _none: None,
            on_error=lambda _error: None,
            owner=self,
            lane=workers.SEARCH,
        )
        if not self._quitting.cancelled():
            self.model_changed.emit()

    def _downloaded(self, error):
        self._download_stop = None
        if self._quitting.cancelled():
            return
        self.model_changed.emit()
        if error is None:
            self.index()
        elif not isinstance(error, models.Cancelled):
            self.download_failed.emit(str(error))

    # Indexing

    def is_indexing(self):
        return self.jobs.is_busy()

    def index(self):
        """Give every photo without a vector one. Run again when it ends if it is
        running now: what a scan added since it started is not in its count."""
        if (
            self._quitting.cancelled()
            or self._removing
            or not self.is_ready()
            or self.is_downloading()
        ):
            return
        self.jobs.add(indexing.EMBED)

    def cancel_indexing(self):
        """Stop it until the next start or scan."""
        self.jobs.cancel()

    def _on_progress(self, _job, status, changed):
        self.indexing_changed.emit(status)
        if changed:
            self.index_changed.emit()

    # Searching

    def search_text(self, text, photos, on_done):
        """`on_done([(photo, cosine)])` with `photos` (the grid's rows) that match
        `text`, best first, unless a newer search was asked for by then."""

        def done(result):
            self._text_loaded = True
            on_done(result)

        self._submit(self._searcher.text, text, photos, done, failed=on_done)

    def is_text_loaded(self):
        """Whether the text half of the model is loaded, so a search is quick; until the
        first search comes back, it is not."""
        return self._text_loaded

    def similar(self, content_id, photos, on_done):
        """As search_text, for the photos most like the one with `content_id`, itself
        first. No results when it has no vector yet."""
        self._submit(self._searcher.similar, content_id, photos, on_done)

    def _submit(self, fn, query, photos, on_done, failed=None):
        self._generation += 1
        generation = self._generation
        failed = failed or on_done

        def done(result):
            if generation == self._generation:
                on_done(result)

        def error(_error):
            if generation == self._generation:
                failed([])

        workers.submit(
            fn,
            query,
            photos,
            on_done=done,
            on_error=error,
            owner=self,
            lane=workers.SEARCH,
        )


class _Searcher:
    """On the search thread only: the index, a connection, and the text encoder."""

    def __init__(self, path, folder, encode_text=None):
        self.path = path
        self.folder = folder
        self._encode_text = encode_text
        self.index = searching.Index()
        self._library = None

    def text(self, text, photos):
        if not text.strip():
            return []
        self._refresh()
        if self._encode_text is not None:
            query = self._encode_text(text)
        else:
            query = workers.call(workers.QUERY, embed.encode_texts, self.folder, [text])[0]
        return self.index.rank(photos, query, floor=searching.text_floor())

    def forget_model(self):
        if self._encode_text is None:
            workers.call(workers.QUERY, embed.forget_texts)

    def similar(self, content_id, photos):
        self._refresh()
        query = self.index.vector(content_id)
        return [] if query is None else self.index.rank(photos, query)

    def _refresh(self):
        if self._library is None:
            self._library = dbm.Library(self.path)
        self.index.refresh(self._library)


def _encode_in_process(folder, paths):
    return workers.call(workers.EMBED, embed.encode_files, folder, paths)


def _index(encode, library, job_id, mounts, should_stop, report):
    """On the indexing thread."""
    return indexing.index(library, job_id, mounts, encode, should_stop=should_stop, report=report)
