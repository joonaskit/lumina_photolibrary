"""Running core work away from the UI thread.

Decoding runs in worker processes, so the GIL does not stall the UI; scanning, which
waits on the disk, runs on threads. Either way the result comes back to the main
thread through a queued signal, and a callback runs there, but only while the
QObject that asked for it still exists: a window can be closed while its work runs.
"""

import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial

from PySide6.QtCore import QObject, Qt, Signal, Slot
from shiboken6 import isValid

# One process is left for the UI and the system
_PROCESS_COUNT = max(1, (os.cpu_count() or 2) - 1)
_THREAD_COUNT = 2


class _Relay(QObject):
    # The QObject a call is for, and the call, to run on the thread the relay lives on:
    # the main thread
    deliver = Signal(object, object)

    def __init__(self):
        super().__init__()
        # Queued even from the main thread: work that is done before submit() returns
        # must not call back from inside it
        self.deliver.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object, object)
    def _run(self, owner, call):
        # Deleted since the work was asked for: there is nobody to tell
        if isValid(owner):
            call()


_relay = None
_processes = None
_threads = None
# Pools of their own, by name: made when first used, from any thread
_named = {}
_named_lock = threading.Lock()
# One process for the Develop view: it keeps the open photo decoded between renders
# (core/edit/session.py), and thumbnails never queue in front of a render
DEVELOP = "develop"
# One process for indexing for search, at low priority, keeping the model loaded
# between batches (core/embed.py); it never holds up thumbnails or a render
EMBED = "embed"
# One process for the text half of the model, which a search's words are encoded by,
# kept loaded between searches (core/embed.py)
QUERY = "query"
# One thread for indexing jobs, which wait on EMBED for hours, and one for searches,
# so neither waits behind a scan or the grid's reads
INDEXING = "indexing"
SEARCH = "search"
# One thread for looking after the thumbnail cache, apart from the scans
CACHE = "cache"
# One thread for walking a library folder to watch it
WATCH = "watch"
_NAMED_PROCESSES = {DEVELOP, EMBED, QUERY}


def _the_relay():
    # Made by the first submit(), which is on the main thread
    global _relay
    if _relay is None:
        _relay = _Relay()
    return _relay


def _process_pool():
    global _processes
    if _processes is None:
        # Not fork: a forked copy of a process with Qt's threads in it can hang
        context = multiprocessing.get_context("forkserver")
        _processes = ProcessPoolExecutor(max_workers=_PROCESS_COUNT, mp_context=context)
    return _processes


def _named_pool(name):
    def pick():
        with _named_lock:
            return _named.get(name) or _make(name)

    return pick


def _make(name):
    if name in _NAMED_PROCESSES:
        context = multiprocessing.get_context("forkserver")
        pool = ProcessPoolExecutor(max_workers=1, mp_context=context)
    else:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"lumina-{name}")
    _named[name] = pool
    return pool


def _thread_pool():
    global _threads
    if _threads is None:
        _threads = ThreadPoolExecutor(max_workers=_THREAD_COUNT, thread_name_prefix="lumina")
    return _threads


def submit(fn, *args, on_done, on_error, owner, process=True):
    """Run `fn(*args)` in a worker process (or a thread, with `process=False`, or one
    of the named pools above, with `process=DEVELOP` and so on), then `on_done(result)` or
    `on_error(exception)` on the main thread, unless the QObject `owner` has been
    deleted by then. Call from the main thread. Returns the future; cancelling it
    before it starts calls neither."""
    relay = _the_relay()
    pick = _pool_for(process)
    pool = pick()
    try:
        future = pool.submit(fn, *args)
    except BrokenProcessPool:
        # A worker died (a decoder crashed on a broken file): start over with a new pool
        _forget_pool(pool)
        pool = pick()
        future = pool.submit(fn, *args)

    def finished(done):
        if done.cancelled():
            return
        error = done.exception()
        if isinstance(error, BrokenProcessPool):
            _forget_pool(pool)
        call = partial(on_error, error) if error else partial(on_done, done.result())
        relay.deliver.emit(owner, call)

    future.add_done_callback(finished)
    return future


def _pool_for(process):
    if isinstance(process, str):
        return _named_pool(process)
    return _process_pool if process else _thread_pool


def _forget_pool(pool):
    global _processes
    if _processes is pool:
        _processes = None
    with _named_lock:
        for name, named in list(_named.items()):
            if named is pool:
                del _named[name]


def call(process, fn, *args):
    """From a worker thread: run `fn(*args)` in the pool `process` (as submit()'s) and
    wait for it. A worker that died is replaced for the next call, and its
    BrokenProcessPool raised for this one."""
    pick = _pool_for(process)
    pool = pick()
    try:
        return pool.submit(fn, *args).result()
    except BrokenProcessPool:
        _forget_pool(pool)
        raise


def call_on_main(owner, fn, *args):
    """From a worker thread: run `fn(*args)` on the main thread, soon, unless the QObject
    `owner` has been deleted by then."""
    _the_relay().deliver.emit(owner, partial(fn, *args))


def shutdown():
    """Drop what has not started and let the workers go. For the app's exit."""
    global _processes, _threads
    for pool in (_processes, _threads, *_named.values()):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    _processes = _threads = None
    with _named_lock:
        _named.clear()
