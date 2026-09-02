"""Background metadata scanning.

v1 read tags synchronously inside add_paths, so adding a folder of a few
thousand files locked the window for the whole scan. Tracks are now added
immediately with just a filename, and a worker thread fills in title, artist,
album and duration afterwards. Results are handed back through a queue that
the UI drains on its normal tick.
"""
import itertools
import queue
import threading
from pathlib import Path

from ..models.track import Track

from ..logs import get as _get_logger

log = _get_logger("scanner")



def read_metadata(path: str) -> dict:
    """Read tags for one file. Never raises."""
    info = {"title": "", "artist": "", "album": "", "length": 0.0}
    try:
        from mutagen import File

        meta = File(path, easy=True)
        if meta is None:
            return info
        if meta.info is not None:
            info["length"] = float(getattr(meta.info, "length", 0.0) or 0.0)
        for key, field in (("title", "title"), ("artist", "artist"),
                           ("album", "album")):
            value = meta.get(key)
            if value:
                info[field] = str(value[0])
    except Exception:
        log.warning("could not read tags from %s", path, exc_info=True)
    if not info["title"]:
        info["title"] = Path(path).stem
    return info


class MetadataScanner:
    """Owns a single worker thread and a result queue."""

    #: Lower runs first. Visible rows must never wait behind prefetch.
    VISIBLE = 0
    AHEAD = 1
    BACKGROUND = 2

    def __init__(self):
        # A priority queue, not FIFO: prefetching fills the queue with
        # thousands of rows nobody is looking at, and a row that scrolls into
        # view has to jump ahead of all of them.
        self._jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._results: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._pending = 0
        self._lock = threading.Lock()

    # Tag reads on a network share are latency-bound, not bandwidth-bound:
    # each is a round trip that spends most of its time waiting. More
    # concurrency fills a screenful proportionally faster, and SMB handles
    # this many outstanding opens without complaint.
    WORKERS = 8

    def start(self) -> None:
        if self._threads and any(t.is_alive() for t in self._threads):
            return
        self._stop.clear()
        self._threads = []
        for i in range(self.WORKERS):
            t = threading.Thread(target=self._run,
                                 name=f"elysian-scanner-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, track_id: int, path: str,
               priority: int = BACKGROUND) -> None:
        with self._lock:
            self._pending += 1
        # The counter keeps ordering stable within a priority and stops the
        # tuple comparison ever reaching the path string.
        self._jobs.put((priority, next(self._seq), track_id, path))

    def submit_many(self, pairs, priority: int = BACKGROUND) -> None:
        for track_id, path in pairs:
            self.submit(track_id, path, priority)

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def drop_all(self) -> list[int]:
        """Empty the queue completely.

        Used when the view moves: rows queued for a screen that has scrolled
        away are no longer displayed, so they no longer have any claim on the
        reader, whatever priority they were given at the time.
        """
        dropped = []
        while True:
            try:
                dropped.append(self._jobs.get_nowait()[2])
            except queue.Empty:
                break
        if dropped:
            with self._lock:
                self._pending -= len(dropped)
                if self._pending < 0:
                    self._pending = 0
        return dropped

    def drop_prefetch(self) -> list[int]:
        """Discard queued prefetch, keeping anything marked VISIBLE.

        A long scroll makes prefetched rows worthless before they are read.
        On a network share each one is a round trip, so throwing them away is
        the point rather than a tidy-up. Returns the ids dropped so the caller
        can forget it ever asked for them.
        """
        keep, dropped = [], []
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            if job[0] <= self.VISIBLE:
                keep.append(job)
            else:
                dropped.append(job[2])
        for job in keep:
            self._jobs.put(job)
        if dropped:
            with self._lock:
                self._pending -= len(dropped)
                if self._pending < 0:
                    self._pending = 0
        return dropped

    def drain(self, limit: int = 200):
        """Yield (track_id, info) pairs ready to apply. Call from the UI loop."""
        for _ in range(limit):
            try:
                yield self._results.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                _prio, _seq, track_id, path = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            # A raising read_metadata used to kill this worker outright, and
            # with four workers a handful of bad files would end all scanning
            # for the session with no trace.
            try:
                info = read_metadata(path)
            except Exception:
                log.exception("scanner worker recovered from %s", path)
                info = {"title": Path(path).stem, "artist": "",
                        "album": "", "length": 0.0}
            self._results.put((track_id, info))
            with self._lock:
                self._pending -= 1
                # Same floor the drop paths apply: the count only feeds the
                # footer, and a double-decrement should read as done, not as
                # a negative tag count that never clears.
                if self._pending < 0:
                    self._pending = 0

    def shutdown(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=0.6)


def apply_metadata(track: Track, info: dict) -> None:
    track.title = info.get("title") or track.title
    track.artist = info.get("artist", "")
    track.album = info.get("album", "")
    track.length = info.get("length", 0.0)
    track.scanned = True
