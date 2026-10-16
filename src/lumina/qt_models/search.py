"""Search for the UI: the model's download, the indexing job, and the searches.

Nothing is fetched until the user asks: `download()` is the only way the model
comes. Once it is there, an indexing job (core/indexing.py) runs whenever there may
be photos without a vector: at the start, after every scan, and when a drive comes.
It runs on a thread of its own and encodes in a worker process of its own at low
priority, apart from the scans, so a drive plugged in is scanned at once, not
after hours of indexing. A quit in the middle leaves it queued for the next start.

Searches run on one thread that keeps the vectors in memory (core/search.py). The
words of a search are encoded in a worker process that keeps the text half of the
model loaded; the first search loads it, which takes a second or two. The newest
search wins: an older one's results are dropped.
"""

import threading
import time
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import QObject, Signal

from lumina.core import db as dbm
from lumina.core import embed, indexing, models
from lumina.core import search as searching
from lumina.qt_models import workers

# How often the download's progress reaches the UI, in seconds
_PROGRESS_EVERY = 0.2


@dataclass(frozen=True)
class IndexStatus:
    state: str
    done: int
    total: int | None
    message: str | None
    failures: tuple


class SearchService(QObject):
    # Whether the model is there or coming changed
    model_changed = Signal()
    # Bytes done and in all
    download_progress = Signal(int, int)
    # The reason, naming the file
    download_failed = Signal(str)
    # An IndexStatus, while indexing runs and when it ends
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
        self._quitting = threading.Event()
        self._download_stop = None
        self._index_stop = None
        self._indexing = None
        self._again = False
        self._removing = False
        self._generation = 0
        # Whether a search has loaded the text half of the model yet
        self._text_loaded = False
        self._searcher = _Searcher(library.path, self.folder, encode_text)
        # Kept for the tests, which wait for them
        self.download_future = self.index_future = self.remove_future = None
        library.job_ended.connect(lambda _status: self.index())
        library.mounts_changed.connect(self.index)

    def start(self):
        if self.is_ready():
            self.index()

    def close(self):
        """For the app's exit: a download or indexing in progress stops; indexing is
        queued for the next start."""
        self._quitting.set()
        for stop in (self._download_stop, self._index_stop):
            if stop is not None:
                stop.set()

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
        stop = threading.Event()
        self._download_stop = stop
        told = [0.0]

        def report(done, total):
            now = time.monotonic()
            if done == total or now - told[0] >= _PROGRESS_EVERY:
                told[0] = now
                workers.call_on_main(self, self.download_progress.emit, done, total)

        self.download_future = workers.submit(
            partial(models.download, embed.MODEL, self.model_root, report, stop.is_set),
            on_done=lambda _none: self._downloaded(None),
            on_error=self._downloaded,
            owner=self,
            process=workers.INDEXING,
        )
        self.model_changed.emit()

    def cancel_download(self):
        if self._download_stop is not None:
            self._download_stop.set()

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
            process=workers.INDEXING,
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
            process=workers.SEARCH,
        )
        if not self._quitting.is_set():
            self.model_changed.emit()

    def _downloaded(self, error):
        self._download_stop = None
        if self._quitting.is_set():
            return
        self.model_changed.emit()
        if error is None:
            self.index()
        elif not isinstance(error, models.Cancelled):
            self.download_failed.emit(str(error))

    # Indexing

    def is_indexing(self):
        return self._indexing is not None

    def index(self):
        """Give every photo without a vector one. Run again when it ends if it is
        running now: what a scan added since it started is not in its count."""
        if (
            self._quitting.is_set()
            or self._removing
            or not self.is_ready()
            or self.is_downloading()
        ):
            return
        if self._indexing is not None:
            self._again = True
            return
        self._again = False
        job_id = self.library.db.add_job(indexing.EMBED, None)
        stop = threading.Event()
        self._index_stop = stop
        self._indexing = job_id
        quitting = self._quitting
        self.index_future = workers.submit(
            _run_index,
            self.library.path,
            job_id,
            self.library.mounts,
            partial(self._encode, self.folder),
            stop,
            quitting,
            lambda *args: workers.call_on_main(self, self._on_progress, job_id, *args),
            on_done=lambda _state: self._on_ended(job_id),
            on_error=lambda error: self._on_failed(job_id, error),
            owner=self,
            process=workers.INDEXING,
        )

    def cancel_indexing(self):
        """Stop it until the next start or scan."""
        if self._index_stop is not None:
            self._again = False
            self._index_stop.set()

    def _on_progress(self, job_id, _phase, _done, _total, changed):
        if self._quitting.is_set() or self._indexing != job_id:
            return
        self.indexing_changed.emit(self._status(job_id))
        if changed:
            self.index_changed.emit()

    def _on_ended(self, job_id):
        self._indexing = self._index_stop = None
        if self._quitting.is_set():
            return
        self.indexing_changed.emit(self._status(job_id))
        self.index_changed.emit()
        if self._again:
            self.index()

    def _on_failed(self, job_id, error):
        if not self._quitting.is_set():
            self.library.db.update_job(
                job_id, state=dbm.FAILED_JOB, message=f"{type(error).__name__}: {error}"
            )
        self._on_ended(job_id)

    def _status(self, job_id):
        job = self.library.db.job(job_id)
        failures = tuple(self.library.db.failures(job_id)[:20]) if job.failures else ()
        return IndexStatus(job.state, job.done, job.total, job.message, failures)

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
            process=workers.SEARCH,
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


def _run_index(path, job_id, mounts, encode, stop, quitting, report):
    """On the indexing thread, with a connection of its own."""
    with dbm.Library(path) as library:
        state = indexing.index(
            library, job_id, mounts, encode, should_stop=stop.is_set, report=report
        )
        if state == dbm.CANCELLED and quitting.is_set():
            # Stopped by a quit, not by the user: run again at the next start
            library.update_job(job_id, state=dbm.QUEUED, message=None)
        return state
