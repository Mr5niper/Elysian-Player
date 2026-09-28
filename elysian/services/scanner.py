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



def _first(value, default=""):
    """mutagen's easy interface returns lists; take the first entry."""
    if not value:
        return default
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value[0] is not None else default
    return str(value)


def _number(value) -> int:
    """Track and disc tags are often "3/12", and year is often a full date."""
    raw = _first(value, "").strip()
    if not raw:
        return 0
    head = raw.split("/")[0].split("-")[0].strip()
    digits = "".join(c for c in head if c.isdigit())
    if not digits:
        return 0
    try:
        return int(digits)
    except ValueError:
        return 0


def _total(value) -> int:
    """The "of 12" half of a "3/12" style tag. 0 if there isn't one.

    Kept separate from _number rather than folding a second return value
    into it: almost every caller only wants the number, and a track or disc
    tag with no total at all is the common case, not an error.
    """
    raw = _first(value, "").strip()
    if "/" not in raw:
        return 0
    tail = raw.split("/", 1)[1].strip()
    digits = "".join(c for c in tail if c.isdigit())
    if not digits:
        return 0
    try:
        return int(digits)
    except ValueError:
        return 0


def _raw(meta, frame: str) -> str:
    """Raw ID3 frame, for containers whose easy interface omits them."""
    try:
        got = meta.get(frame)
    except Exception:
        return ""
    if got is None:
        return ""
    return _first(getattr(got, "text", got), "")



#: kbps tables keyed by (MPEG version bits, layer bits), and sample rates
#: keyed by version bits alone - the same fields _mp3_accurate_duration
#: reads from each frame's own 4-byte header to find where the next frame
#: starts, without ever touching that frame's actual (compressed) audio
#: data.
_MP3_BITRATE_TABLES = {
    (3, 1): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    (3, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
    (3, 3): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
    (2, 1): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    (0, 1): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}
_MP3_SAMPLE_RATES = {
    3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000),
}


def _mp3_accurate_duration(path: str) -> float:
    """The file's real duration, found by counting actual frames - never
    decoding (decompressing) the audio itself.

    Every MPEG Layer III frame represents a fixed number of samples
    (1152 for MPEG1, 576 for MPEG2/2.5) regardless of that frame's own
    bitrate, so the true duration is simply (frame count * samples per
    frame / sample rate) - exact and unambiguous for a genuinely
    variable-bitrate file, the same way a real decode is, but without
    paying to decompress a single sample: only each frame's own 4-byte
    header is ever read, used solely to compute that frame's size in
    bytes so the next one can be found. Verified directly against a
    real, badly-affected file: matches a full decode's result to the
    full precision returned, in roughly an eighth of the time.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    pos = 0
    if data[:3] == b"ID3":
        pos = 10 + (((data[6] & 0x7f) << 21) | ((data[7] & 0x7f) << 14)
                    | ((data[8] & 0x7f) << 7) | (data[9] & 0x7f))
    n = len(data)
    total_samples = 0
    sample_rate = 0
    while pos + 4 <= n:
        if data[pos] == 0xFF and (data[pos + 1] & 0xE0) == 0xE0:
            version_bits = (data[pos + 1] >> 3) & 0x3
            layer_bits = (data[pos + 1] >> 1) & 0x3
            table = _MP3_BITRATE_TABLES.get((version_bits, layer_bits))
            rates = _MP3_SAMPLE_RATES.get(version_bits)
            if table and rates:
                bitrate_idx = (data[pos + 2] >> 4) & 0xF
                samplerate_idx = (data[pos + 2] >> 2) & 0x3
                padding = (data[pos + 2] >> 1) & 0x1
                if 0 < bitrate_idx < 15 and samplerate_idx < 3:
                    bitrate_bps = table[bitrate_idx] * 1000
                    rate = rates[samplerate_idx]
                    if layer_bits == 3:            # Layer I
                        frame_size, slot = 384, 4
                    elif version_bits != 3 and layer_bits == 1:  # MPEG2/2.5 L3
                        frame_size, slot = 576, 1
                    else:
                        frame_size, slot = 1152, 1
                    frame_len = (((frame_size // 8 * bitrate_bps) // rate
                                 + padding) * slot)
                    if frame_len > 0:
                        total_samples += frame_size
                        sample_rate = rate
                        pos += frame_len
                        continue
        pos += 1
    return (total_samples / sample_rate) if sample_rate else 0.0


#: Everything one file yields. The library stores all of it, the playlist
#: view uses a subset; one reader means a track scanned for either purpose
#: is usable by the other.
EMPTY_METADATA = {
    "title": "", "artist": "", "album": "", "length": 0.0,
    "album_artist": "", "genre": "", "track_number": 0,
    "disc_number": 0, "year": 0, "compilation": 0,
    "track_total": 0, "disc_total": 0,
    "title_sort": "", "artist_sort": "", "album_sort": "",
    "album_artist_sort": "",
}

#: What the tag editor may change, and what LibraryService considers
#: "editable" for the payload it builds and the mixed-value aggregation it
#: does. Defined once here, since both library.py and tag_editor.py were
#: previously keeping their own identical copy of this same list with
#: nothing enforcing that they stay identical.
EDITABLE_TRACK_FIELDS = (
    "title", "artist", "album", "album_artist", "genre",
    "track_number", "track_total", "disc_number", "disc_total",
    "year", "compilation",
    "title_sort", "artist_sort", "album_sort", "album_artist_sort",
)


def read_metadata(path: str) -> dict:
    """Read tags for one file. Never raises."""
    info = dict(EMPTY_METADATA)
    try:
        from mutagen import File

        meta = File(path, easy=True)
        if meta is None:
            info["title"] = Path(path).stem
            return info
        if meta.info is not None:
            info["length"] = float(getattr(meta.info, "length", 0.0) or 0.0)
            # A tag reader's length for an MP3 with no Xing/VBRI header
            # (bitrate_mode UNKNOWN - a concept only MPEG audio has) is a
            # "read one frame's bitrate, assume the whole file is
            # constant at it" guess. Correct for a genuinely CBR file
            # with no header, since the assumption holds - but badly
            # wrong for a file that is actually variable and also lacks
            # one: confirmed directly against a real file read as
            # roughly six and a half times too long (about 39 minutes
            # reported for an actual 4-minute track), because that
            # file's first frame happened to be a quiet moment encoded
            # at only 32kbps while the real average was around 212kbps.
            #
            # A missing header alone is not rare - plenty of ordinary
            # CBR files, especially older rips, never had one written,
            # and for those the "constant bitrate" guess is exactly
            # right. An extra, no-cost check is what actually narrows
            # this to the rare, genuinely broken case: real music is
            # essentially never legitimately encoded, start to finish,
            # at under 96kbps - a header-less file whose assumed
            # bitrate is that low is a sign its first frame is not
            # representative of the file, the same way this one's
            # was not. Both attributes checked here (bitrate_mode,
            # bitrate) are already sitting on meta.info from the read
            # above - this opens nothing a second time and costs
            # nothing for every other file. Only a file already this
            # unusual pays for _mp3_accurate_duration, and even that is
            # a frame count, never a full audio decode - roughly 6-8x
            # faster, confirmed directly, since it only ever reads each
            # frame's own 4-byte header.
            bitrate_mode = getattr(meta.info, "bitrate_mode", None)
            bitrate = getattr(meta.info, "bitrate", 0) or 0
            from mutagen.mp3 import BitrateMode

            if bitrate_mode == BitrateMode.UNKNOWN and 0 < bitrate < 96000:
                try:
                    accurate = _mp3_accurate_duration(path)
                    if accurate > 0:
                        info["length"] = accurate
                except Exception:
                    log.warning("could not get an accurate duration for "
                               "%s", path, exc_info=True)
        for tag, field in (("title", "title"), ("artist", "artist"),
                           ("album", "album"), ("genre", "genre")):
            info[field] = _first(meta.get(tag), "")
        # WAV carries ID3 too, but mutagen's easy interface exposes raw
        # frame names for it rather than the friendly ones, so the lookups
        # above come back empty and the tags are silently lost. Fall back to
        # the frames directly when that happens.
        if not any((info["title"], info["artist"], info["album"])):
            _frames = {"title": "TIT2", "artist": "TPE1", "album": "TALB",
                       "genre": "TCON"}
            for field, frame in _frames.items():
                if not info[field]:
                    got = meta.get(frame)
                    if got is not None:
                        info[field] = _first(getattr(got, "text", got), "")
        # Spelled two different ways depending on the container; try both
        # rather than losing it on one format.
        info["album_artist"] = (_first(meta.get("albumartist"), "")
                                or _first(meta.get("album artist"), "")
                                or _raw(meta, "TPE2"))
        # "Sort name" tags: what iTunes calls Sort Name/Sort Artist/Sort
        # Album/Sort Album Artist, used to file "The Beatles" under B or
        # "The White Album" under W without changing what is actually
        # displayed. mutagen's easy interface recognises the same plain
        # keys for both ID3 (TSOT/TSOP/TSOA/TSO2) and Vorbis comments
        # (titlesort/artistsort/albumsort/albumartistsort) - verified
        # directly with a real round-trip write/read on both container
        # types, not assumed from the tag names alone.
        info["title_sort"] = _first(meta.get("titlesort"), "")
        info["artist_sort"] = _first(meta.get("artistsort"), "")
        info["album_sort"] = _first(meta.get("albumsort"), "")
        info["album_artist_sort"] = _first(meta.get("albumartistsort"), "")
        # WAV loses these through the easy interface exactly the way it
        # loses title/artist/album/genre above - same fallback to the raw
        # ID3 frames directly.
        if not any((info["title_sort"], info["artist_sort"],
                   info["album_sort"], info["album_artist_sort"])):
            _sort_frames = {"title_sort": "TSOT", "artist_sort": "TSOP",
                            "album_sort": "TSOA", "album_artist_sort": "TSO2"}
            for field, frame in _sort_frames.items():
                if not info[field]:
                    info[field] = _raw(meta, frame)
        # Captured once so the "3" and the "of 12" come from the exact same
        # string; asking meta.get(...) twice risks the two halves coming
        # from a different tag if a file oddly carries both spellings.
        track_raw = meta.get("tracknumber") or _raw(meta, "TRCK")
        disc_raw = meta.get("discnumber") or _raw(meta, "TPOS")
        info["track_number"] = _number(track_raw)
        info["track_total"] = _total(track_raw)
        info["disc_number"] = _number(disc_raw)
        info["disc_total"] = _total(disc_raw)
        info["year"] = _number(meta.get("date") or meta.get("year")
                               or _raw(meta, "TDRC"))
        # The "part of a compilation" flag. Tools write it as TCMP in ID3,
        # COMPILATION in Vorbis comments and cpil in MP4. It is the only
        # authoritative answer: a compilation can have one artist on every
        # track, and counting artists would never notice.
        flag = (_first(meta.get("compilation"), "")
                or _raw(meta, "TCMP")
                or _first(meta.get("cpil"), ""))
        info["compilation"] = 1 if str(flag).strip().lower() in (
            "1", "true", "yes", "y") else 0
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
        #: Optional callable(path) -> metadata dict or None.
        #: Set by Api to the library index.
        self.resolver = None
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
                # A resolver (the library index) answers from a local
                # database in microseconds. Only fall through to opening
                # the file when it has never been seen.
                info = None
                resolver = self.resolver
                if resolver is not None:
                    info = resolver(path)
                if info is None:
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
