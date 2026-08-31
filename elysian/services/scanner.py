"""Background metadata scanning.

v1 read tags synchronously inside add_paths, so adding a folder of a few
thousand files locked the window for the whole scan. Tracks are now added
immediately with just a filename, and a worker thread fills in title, artist,
album and duration afterwards. Results are handed back through a queue that
the UI drains on its normal tick.
"""
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

    def __init__(self):
        self._jobs: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._pending = 0
        self._lock = threading.Lock()

    WORKERS = 4

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

    def submit(self, track_id: int, path: str) -> None:
        with self._lock:
            self._pending += 1
        self._jobs.put((track_id, path))

    def submit_many(self, pairs) -> None:
        for track_id, path in pairs:
            self.submit(track_id, path)

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

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
                track_id, path = self._jobs.get(timeout=0.2)
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
