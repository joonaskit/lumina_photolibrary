"""Running core work away from the UI thread.

Decoding runs in worker processes, so the GIL does not stall the UI; scanning, which
waits on the disk, runs on threads. Either way the result comes back to the main
thread through a queued signal, and a callback runs there, but only while the
QObject that asked for it still exists: a window can be closed while its work runs.
"""

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial

from PySide6.QtCore import QObject, Signal, Slot
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
        self.deliver.connect(self._run)

    @Slot(object, object)
    def _run(self, owner, call):
        # Deleted since the work was asked for: there is nobody to tell
        if isValid(owner):
            call()


_relay = None
_processes = None
_threads = None


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


def _thread_pool():
    global _threads
    if _threads is None:
        _threads = ThreadPoolExecutor(max_workers=_THREAD_COUNT, thread_name_prefix="lumina")
    return _threads


def submit(fn, *args, on_done, on_error, owner, process=True):
    """Run `fn(*args)` in a worker process (or a thread, with `process=False`), then
    `on_done(result)` or `on_error(exception)` on the main thread, unless the QObject
    `owner` has been deleted by then. Call from the main thread. Returns the future;
    cancelling it before it starts calls neither."""
    global _processes
    relay = _the_relay()
    pool = _process_pool() if process else _thread_pool()
    try:
        future = pool.submit(fn, *args)
    except BrokenProcessPool:
        # A worker died (a decoder crashed on a broken file): start over with a new pool
        _processes = None
        pool = _process_pool()
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


def _forget_pool(pool):
    global _processes
    if _processes is pool:
        _processes = None


def call_on_main(owner, fn, *args):
    """From a worker thread: run `fn(*args)` on the main thread, soon, unless the QObject
    `owner` has been deleted by then."""
    _the_relay().deliver.emit(owner, partial(fn, *args))


def shutdown():
    """Drop what has not started and let the workers go. For the app's exit."""
    global _processes, _threads
    for pool in (_processes, _threads):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    _processes = _threads = None
