"""Running core work away from the UI thread: one scheduler, and the lanes it runs on.

A lane is a pool of worker processes or threads for one kind of work, made when first
used. Decoding runs in processes, so the GIL does not stall the UI; what waits on the
disk or the database runs on threads. Work that keeps something loaded between calls (the
open photo, the search model) has a process of its own, and so does work that must never
wait behind a scan or a thumbnail. Each lane takes its requests in order of priority,
then in the order they came; a request still waiting can be cancelled.

When a worker process dies (a decoder crashes on a broken file), every call in flight in
its lane fails with it, and only one of them did it. Those are run again one at a time,
before anything else in that lane: the one that kills its worker again fails, with
BrokenProcessPool; the others succeed.

The result comes back to the main thread through a queued signal, and a callback runs
there, but only while the QObject that asked for it still exists: a window can be
closed while its work runs.
"""

import heapq
import itertools
import multiprocessing
import os
import threading
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import QObject, Qt, Signal, Slot
from shiboken6 import isValid

# Priorities, the first the soonest: what the user looks at, what is in view, and work
# that may wait (an export's files, a profile fitted from many photos)
NOW = 0
SOON = 1
LATER = 2


@dataclass(frozen=True)
class Lane:
    name: str
    processes: bool
    size: int


# One process is left for the UI and the system
DECODE = Lane("decode", True, max(1, (os.cpu_count() or 2) - 1))
# Short work for the window: queries, stacks, keyword counts, reading a card
IO = Lane("io", False, 2)
# One process for the Develop view: it keeps the open photo decoded between renders
# (core/edit/session.py), and thumbnails never queue in front of a render
DEVELOP = Lane("develop", True, 1)
# One process for indexing for search, keeping the model loaded between batches
# (core/embed.py); it never holds up thumbnails or a render
EMBED = Lane("embed", True, 1)
# One process for scoring photos' sharpness (core/sharpness.py), at low priority: it never
# holds up thumbnails or a render
SCORING = Lane("scoring", True, 1)
# One process for the text half of the model, which a search's words are encoded by,
# kept loaded between searches (core/embed.py)
QUERY = Lane("query", True, 1)
# One process for the steps of merges and their previews (core/hdr.py,
# core/panorama.py): a merge never holds up a scan, a thumbnail or a render
MERGING = Lane("merging", True, 1)
# One thread for searches, which keeps the vectors in memory and its connection
SEARCH = Lane("search", False, 1)
# A thread for each kind of job (qt_models/jobs.py), which run one at a time and wait
# on the disk or on a process for minutes or hours: none waits for another. The
# indexing thread fetches and removes the model too, in turn with the indexing
SCAN = Lane("scan", False, 1)
INDEXING = Lane("indexing", False, 1)
SHARPNESS = Lane("sharpness", False, 1)
IMPORT = Lane("import", False, 1)
MERGE = Lane("merge", False, 1)
EXPORT = Lane("export", False, 1)
# A thread for each chore that runs on its own: looking after the thumbnail cache,
# walking a folder to watch it, writing sidecars, backing up the database, and a
# tethered folder's photos, which never wait for a scan
CACHE = Lane("cache", False, 1)
WATCH = Lane("watch", False, 1)
SIDECARS = Lane("sidecars", False, 1)
BACKUP = Lane("backup", False, 1)
TETHER = Lane("tether", False, 1)


class Token:
    """Asks work to stop. Work checks `cancelled()`, or is handed the token itself as its
    `should_stop`. A token made with a `parent` is cancelled when the parent is: a job's
    with the app's quitting."""

    def __init__(self, parent=None):
        self._event = threading.Event()
        self._parent = parent

    def cancel(self):
        self._event.set()

    def cancelled(self):
        return self._event.is_set() or (self._parent is not None and self._parent.cancelled())

    __call__ = cancelled


class Coalescing:
    """Work that runs one at a time: asked for again while it runs, it runs once more
    when it ends, however often it was asked. On the main thread."""

    def __init__(self):
        self._busy = False
        self._again = False

    def begin(self):
        """Whether to start it now; when not, it runs again after this one."""
        if self._busy:
            self._again = True
            return False
        self._busy, self._again = True, False
        return True

    def end(self):
        """It ended: whether to start it again."""
        again, self._busy, self._again = self._again, False, False
        return again

    def busy(self):
        return self._busy

    def drop_again(self):
        self._again = False


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


@dataclass
class _Request:
    fn: object
    args: tuple
    # What the caller holds: done when the work is, cancelled while it waits
    future: Future


class _Pool:
    """A lane's workers and its queue."""

    def __init__(self, lane):
        self.lane = lane
        self.executor = None
        self.waiting = []
        # Calls that were in flight when a worker died, to run again one at a time
        self.suspects = deque()
        self.in_flight = 0
        self.lock = threading.Lock()

    def make(self):
        if self.executor is None:
            if self.lane.processes:
                # Not fork: a forked copy of a process with Qt's threads in it can hang
                context = multiprocessing.get_context("forkserver")
                self.executor = ProcessPoolExecutor(max_workers=self.lane.size, mp_context=context)
            else:
                self.executor = ThreadPoolExecutor(
                    max_workers=self.lane.size, thread_name_prefix=f"lumina-{self.lane.name}"
                )
        return self.executor

    def broken(self, executor):
        """Its worker died: a new one for what comes next."""
        with self.lock:
            if self.executor is executor:
                self.executor = None

    def next(self):
        """The requests to start now, with the lock held: a suspect alone, or the
        soonest waiting up to the lane's size."""
        if self.suspects:
            if self.in_flight:
                return []
            self.in_flight = 1
            return [(self.suspects.popleft(), True)]
        chosen = []
        while self.waiting and self.in_flight < self.lane.size:
            request = heapq.heappop(self.waiting)[2]
            # False when it was cancelled while it waited
            if request.future.set_running_or_notify_cancel():
                self.in_flight += 1
                chosen.append((request, False))
        return chosen


_relay = None
_pools = {}
_pools_lock = threading.Lock()
_order = itertools.count()


def _the_relay():
    # Made by the first submit(), which is on the main thread
    global _relay
    if _relay is None:
        _relay = _Relay()
    return _relay


def _pool(lane):
    with _pools_lock:
        pool = _pools.get(lane)
        if pool is None:
            pool = _pools[lane] = _Pool(lane)
        return pool


def schedule(lane, fn, *args, priority=SOON):
    """Run `fn(*args)` on `lane`, from any thread: returns its Future, which can be
    cancelled while it waits."""
    future = Future()
    pool = _pool(lane)
    with pool.lock:
        heapq.heappush(pool.waiting, (priority, next(_order), _Request(fn, args, future)))
    _dispatch(pool)
    return future


def _dispatch(pool):
    with pool.lock:
        chosen = pool.next()
    for request, alone in chosen:
        _start(pool, request, alone)


def _start(pool, request, alone):
    with pool.lock:
        executor = pool.make()
    try:
        inner = executor.submit(request.fn, *request.args)
    except BrokenProcessPool:
        # It died before this was given to it
        pool.broken(executor)
        _start(pool, request, alone)
        return
    except RuntimeError as error:
        # Shut down: the app is quitting
        _settle(pool, request, error=error)
        return
    inner.add_done_callback(lambda done: _finished(pool, request, alone, executor, done))


def _finished(pool, request, alone, executor, done):
    if done.cancelled():
        _settle(pool, request, error=RuntimeError("Shut down"))
        return
    error = done.exception()
    if isinstance(error, BrokenProcessPool):
        pool.broken(executor)
        if not alone:
            # One of those in flight did it: this runs again, alone
            with pool.lock:
                pool.suspects.append(request)
                pool.in_flight -= 1
            _dispatch(pool)
            return
    if error is not None:
        _settle(pool, request, error=error)
    else:
        _settle(pool, request, result=done.result())


def _settle(pool, request, result=None, error=None):
    with pool.lock:
        pool.in_flight -= 1
    if error is not None:
        request.future.set_exception(error)
    else:
        request.future.set_result(result)
    _dispatch(pool)


def submit(fn, *args, on_done, on_error, owner, lane=DECODE, priority=SOON):
    """Run `fn(*args)` on `lane`, then `on_done(result)` or `on_error(exception)` on the
    main thread, unless the QObject `owner` has been deleted by then. Call from the main
    thread. Returns the Future; cancelling it before it starts calls neither."""
    relay = _the_relay()

    def deliver(done):
        if done.cancelled():
            return
        error = done.exception()
        call = partial(on_error, error) if error else partial(on_done, done.result())
        relay.deliver.emit(owner, call)

    future = schedule(lane, fn, *args, priority=priority)
    future.add_done_callback(deliver)
    return future


def call(lane, fn, *args, priority=SOON):
    """From a worker thread: run `fn(*args)` on `lane` and wait for it. Raises what it
    raised: BrokenProcessPool when it killed its worker on its own."""
    return schedule(lane, fn, *args, priority=priority).result()


def start_process(target, *args):
    """Run `target(*args)` in a process of its own, for work that lasts (a camera session)
    and is spoken to through queues from `make_queue()`. Started as the lanes' are (not
    forked: Qt's threads); a daemon, so it never outlives the app. Returns the Process."""
    context = multiprocessing.get_context("forkserver")
    process = context.Process(target=target, args=args, daemon=True)
    process.start()
    return process


def make_queue():
    """A queue to and from a process of `start_process`, which a thread can use too."""
    return multiprocessing.get_context("forkserver").Queue()


def call_on_main(owner, fn, *args):
    """From a worker thread: run `fn(*args)` on the main thread, soon, unless the QObject
    `owner` has been deleted by then."""
    _the_relay().deliver.emit(owner, partial(fn, *args))


def shutdown():
    """Drop what has not started and let the workers go. For the app's exit."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        with pool.lock:
            waiting = [entry[2] for entry in pool.waiting] + list(pool.suspects)
            pool.waiting.clear()
            pool.suspects.clear()
            executor, pool.executor = pool.executor, None
        for request in waiting:
            request.future.cancel()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
