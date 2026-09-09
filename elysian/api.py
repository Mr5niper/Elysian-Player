"""The object exposed to JavaScript as pywebview.api.

Every method here is callable from the frontend. All application state lives on
the Python side; the frontend polls get_tick()/get_full() and renders whatever
it is given, so there is exactly one source of truth.
"""
import os
import queue
import random
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path

from webview.window import FixPoint

from . import config
from . import paths as pathutil
from .models.playlist import Playlist
from .models.track import format_time
from .playback.engine import PlaybackEngine, PlaybackError
from .services import settings as settings_store
from .services.art import ArtProvider
from .services.library import LibraryService
from .services.scanner import MetadataScanner, apply_metadata
from .services.tag_editor import write_many as _write_tags
from .services.waveform import peaks_for

from .logs import get as _get_logger

log = _get_logger("api")


REPEAT_CYCLE = {"none": "all", "all": "one", "one": "none"}


class Api:
    def __init__(self):
        self._window = None
        self._playlist = Playlist()
        self._engine = PlaybackEngine()
        self._art = ArtProvider()
        self._scanner = MetadataScanner()
        self._scanner.start()

        self._settings = settings_store.load()
        self._current_id = -1
        # Set only while a tag save is releasing the file the engine has
        # open for the track it's currently playing or paused on; consumed
        # once that save finishes, whichever way, to put playback back.
        self._tag_save_resume = None
        self._shuffle = bool(self._settings["shuffle"])
        self._repeat = self._settings["repeat"]
        self._engine.set_volume(self._settings.get("volume", 0.8))
        # Mute is session-local: the engine goes to 0 but _premute_volume is
        # what the slider shows, what unmute restores, and what gets saved,
        # so closing muted cannot trap the next launch at volume zero.
        self._muted = False
        self._premute_volume = float(self._settings.get("volume", 0.8))

        self._shuffle_bag: list[int] = []
        self._history: list[int] = []
        self._revision = 0
        self._status = ""
        self._status_until = 0.0
        self._peaks: list[float] = []
        self._peaks_for: str | None = None
        self._closing = False
        self._queued: dict[int, int] = {}
        # Running count for the folder-scan status line, owned by the worker.
        self._folder_added = 0
        # Bumped by clear_playlist so folder walks started before the clear
        # stop adding to the playlist the user just emptied.
        self._ingest_gen = 0
        # Offset to resume at, applied the first time the restored track is
        # played. Seeking at startup would mean opening the file over the
        # network before the window has even drawn.
        self._resume_at = 0.0
        self._resume_id = -1
        self._maximized = False
        self._lock = threading.RLock()
        self._scan_dirty = False
        self._last_scan_bump = 0.0
        # Ids whose metadata changed since the frontend last collected them.
        # Sending the whole track list every time a tag arrived meant well
        # over a megabyte crossing the bridge each second on a large playlist,
        # to communicate a couple of dozen changed rows.
        self._dirty: set[int] = set()
        self._meta_revision = 0

        # Library state. Three counters rather than one, so the frontend
        # refetches only the pane that actually changed: the index itself
        # (a scan finished, a root was added), the browser list, and the
        # detail list are all independent of each other and of the playlist.
        self._library = LibraryService(config.LIBRARY_DB_FILE)
        self._library_revision = 0
        self._library_browser_revision = 0
        self._library_detail_revision = 0
        self._library_scanning = False
        self._library_refresh_at = 0.0
        self._library_summary = {"tracks": 0, "artists": 0, "albums": 0,
                                 "genres": 0, "duration": 0.0, "roots": []}
        self._library_browser = {"view": "albums", "items": [], "needle": "",
                                 "revision": 0}
        self._library_detail = {"kind": "", "key": "", "key2": "",
                                "title": "", "items": [], "revision": 0}
        # Cover art, resolved only for the cards actually on screen. A
        # library of a thousand albums is a thousand image decodes, and
        # most of them are for cards nobody has scrolled to.
        # Each entry carries the sequence number it arrived at, so the
        # frontend can ask for only what it has not seen. Returning the
        # whole map meant several megabytes of base64 crossing the bridge
        # every time a single cover resolved, which is what made scrolling
        # a large library stutter.
        self._library_art = {}          # key -> [seq, data url or ""]
        self._library_art_seq = 0
        self._library_art_revision = 0
        # Bumped on every browse or detail request so a query thread that
        # is still running when a newer one starts can tell it has been
        # superseded. Without this, a slow query fired first could finish
        # after a fast one fired later and overwrite it - a filter typed
        # quickly, a tab switched quickly, or a scan-triggered refresh
        # landing after a manual request could all show stale results.
        self._library_browser_gen = 0
        self._library_detail_gen = 0
        self._library_art_pending = set()
        # The tag editor. Unlike everything else the library owns, opening
        # or saving here means writing to the user's actual files, which is
        # why it gets its own revision rather than riding the browser or
        # detail ones: those two describe what SQLite says, this describes
        # a real file operation that can fail per file and takes a moment.
        self._library_editor_revision = 0
        self._library_editor = {
            "open": False, "loading": False, "saving": False,
            "paths": [], "count": 0, "data": {}, "mixed": {},
            "errors": [], "saved": 0, "failed": 0,
        }
        # Covers are resolved by a fixed set of workers reading one queue,
        # not by a pool taking whatever was submitted first. What matters is
        # the cards on screen now: a queue that cannot be reordered means
        # they wait behind every album already scrolled past.
        # Two queues, as the tag scanner uses. Everything is decoded
        # eventually, but whatever is on screen jumps the whole backlog:
        # one queue that can only be appended to would put the covers being
        # looked at behind every album already swept past.
        self._art_urgent: queue.Queue = queue.Queue()
        self._art_bulk: queue.Queue = queue.Queue()
        self._art_workers = []
        # Keys sitting in the background queue and not yet started. A key
        # here can be promoted to the urgent queue when it scrolls into
        # view; the background copy is skipped when a worker reaches it.
        self._art_bulk_pending = set()
        # The playlist scanner consults the index before opening a file. A
        # track the library already knows costs a local lookup instead of a
        # tag read over a share, which is the expensive thing the whole
        # prefetch scheme exists to ration.
        self._scanner.resolver = self._library.lookup_one

        # Set only by _do_library_play_context, and cleared back to
        # "playlist" by anything that is an explicit playlist edit (add,
        # remove, reorder, clear, load an m3u). Informational: transport
        # itself never branches on it, since the queue it describes IS the
        # playlist by the time this is set. It exists so the status line
        # and, later, the UI can say what is actually playing.
        self._play_context = {"source": "playlist", "kind": "", "title": ""}

        self._snapshot: dict = {
            "current_id": -1, "playing": False, "paused": False,
            "position": 0.0, "duration": 0.0, "volume": self._engine.volume,
            "shuffle": self._shuffle, "repeat": self._repeat,
            "status": "", "maximized": False, "revision": 0,
            "meta_revision": 0, "scan_pending": 0,
            "library_revision": 0, "library_browser_revision": 0,
            "library_detail_revision": 0, "library_art_revision": 0,
            "library_scanning": False,
            "library_editor_revision": 0,
            "play_context_source": "playlist", "play_context_kind": "",
            "play_context_title": "", "current_path": "",
        }
        self._full: dict = {"tracks": [], "title": "", "artist": "",
                            "art": None, "revision": -1}
        self._full_revision = -1
        self._art_cache: dict[str, str | None] = {}
        self._cmd: queue.Queue = queue.Queue()
        self._snap_lock = threading.RLock()
        self._worker = threading.Thread(target=self._run, name="elysian-core",
                                        daemon=True)
        self._worker.start()

        if not self._engine.available:
            self._set_status(f"No audio device: {self._engine.error}")

    def _run(self) -> None:
        """Owns every operation that can block.

        The UI thread only ever reads _snapshot, so a slow network share can
        never stall a button press.
        """
        while not self._closing:
            try:
                cmd = self._cmd.get(timeout=0.04)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                try:
                    self._dispatch(cmd)
                except Exception:
                    log.exception("command failed: %r", cmd)
                while True:
                    try:
                        self._dispatch(self._cmd.get_nowait())
                    except queue.Empty:
                        break
                    except Exception:
                        log.exception("queued command failed")
            try:
                self._drain_scanner()
                self._advance_if_finished()
                with self._lock:
                    track = self._playlist.by_id(self._current_id)
                self._ensure_art(track)
                self._ensure_peaks(track)
                self._rebuild_snapshot()
            except Exception:
                # An invariant failure here would otherwise repeat silently
                # every 40ms forever.
                log.exception("worker maintenance pass failed")

    def _dispatch(self, cmd) -> None:
        name, args = cmd[0], cmd[1:]
        fn = getattr(self, "_do_" + name, None)
        if fn:
            fn(*args)

    def _post(self, name, *args) -> None:
        self._cmd.put((name,) + args)

    # ---- helpers -------------------------------------------------------

    def _bump(self) -> None:
        self._revision += 1

    def _set_status(self, text: str, seconds: float = 4.0) -> None:
        self._status = text
        self._status_until = time.monotonic() + seconds

    def _footer(self) -> str:
        if self._status and time.monotonic() < self._status_until:
            return self._status
        n = len(self._playlist)
        if not n:
            return ""
        parts = [f"{n} track{'s' if n != 1 else ''}",
                 format_time(self._playlist.total_length)]
        if self._scanner.pending:
            parts.append(f"reading tags: {self._scanner.pending}")
        return "   |   ".join(parts)

    def _drain_scanner(self) -> None:
        """Apply finished tag reads.

        Bumping the revision on every drain made the frontend refetch and
        rebuild the whole track list on every 200ms tick for the entire
        duration of a scan. Batched to at most once a second instead.
        """
        changed = False
        # Under _lock: get_meta iterates _dirty from the bridge thread while
        # holding it, and a set that changes size mid-iteration raises there.
        # Everything inside is in-memory, so the hold is microseconds.
        with self._lock:
            for track_id, info in self._scanner.drain():
                # Release the queue slot whether or not the track still
                # exists, or a removed track would hold its id forever.
                self._queued.pop(track_id, None)
                track = self._playlist.by_id(track_id)
                if track:
                    before = track.length
                    apply_metadata(track, info)
                    self._playlist.adjust_length(track.length - before)
                    self._dirty.add(track_id)
                    changed = True
        if changed:
            self._scan_dirty = True
        now = time.monotonic()
        if self._scan_dirty and now - self._last_scan_bump >= 0.4:
            self._scan_dirty = False
            self._last_scan_bump = now
            # A metadata revision, not a structural one: the set of rows has
            # not changed, so the frontend only needs the rows that did.
            self._meta_revision += 1

    def request_scan(self, ids, priority: int = MetadataScanner.VISIBLE) -> int:
        """Queue tag reads for these tracks.

        Files may live on a network share where every read is a round trip, so
        nothing is opened until something needs it. The frontend asks for the
        rows on screen at VISIBLE, and fills otherwise-idle time with
        prefetching at a lower priority; the queue is ordered so a row
        scrolling into view never waits behind that prefetch.
        """
        queued = 0
        priority = int(priority)
        for raw in (ids or []):
            track_id = int(raw)
            # A row already queued for prefetch must be able to jump to the
            # front when it scrolls into view. Without this it stays stuck
            # behind the whole background sweep.
            existing = self._queued.get(track_id)
            if existing is not None and existing <= priority:
                continue
            track = self._playlist.by_id(track_id)
            if track is None or track.scanned:
                continue
            self._queued[track_id] = priority
            self._scanner.submit(track_id, track.path, priority)
            queued += 1
        return queued

    def reset_scan_queue(self) -> int:
        """Discard everything queued, at any priority.

        The frontend calls this whenever the visible rows change: work queued
        for a screen you have scrolled past is not displayed, so it must not
        be in front of the screen you are looking at now.
        """
        dropped = self._scanner.drop_all()
        for track_id in dropped:
            self._queued.pop(track_id, None)
        # The playing track feeds the Now Playing card no matter which view
        # is on screen, so its scan must survive this reset: with the card
        # showing and no playlist rows visible, nothing else would ever
        # re-request it. _scan_one no-ops if it was already read.
        self._scan_one(self._current_id)
        return len(dropped)

    def drop_prefetch(self) -> int:
        """Forget queued prefetch that the view has scrolled away from."""
        dropped = self._scanner.drop_prefetch()
        for track_id in dropped:
            self._queued.pop(track_id, None)
        return len(dropped)

    def request_prefetch(self, ids) -> int:
        """Prefetch, at a priority that yields to anything on screen."""
        return self.request_scan(ids, MetadataScanner.BACKGROUND)

    def request_ahead(self, ids) -> int:
        """Prefetch in the direction of travel while scrolling."""
        return self.request_scan(ids, MetadataScanner.AHEAD)

    def _scan_one(self, track_id: int) -> None:
        track = self._playlist.by_id(track_id)
        if track is not None and not track.scanned and track_id not in self._queued:
            self._queued[track_id] = MetadataScanner.VISIBLE
            self._scanner.submit(track_id, track.path, MetadataScanner.VISIBLE)

    # ---- state ---------------------------------------------------------

    def get_tick(self) -> dict:
        """Pure read. Never touches the disk or the network.

        Everything that can block, such as loading a track, reading tags or
        extracting album art, happens on the worker thread and lands in
        _snapshot. A
        bridge call that opens a file on a network share freezes the whole
        interface, which is what made the app feel dead.
        """
        with self._snap_lock:
            return dict(self._snapshot)

    def get_full(self) -> dict:
        with self._snap_lock:
            full = dict(self._full)
        return full

    @staticmethod
    def _track_row(track_id: int, t) -> dict:
        return {
            "id": track_id,
            "title": t.title,
            "artist": t.artist,
            "album": t.album,
            # os.path.basename, not Path().name: this runs once per track per
            # full send, and Path is about 9x slower here: 120ms against
            # 11ms for fifty thousand tracks.
            "name": os.path.basename(t.path),
            "length": round(t.length, 1),
            "scanned": t.scanned,
        }

    def get_meta(self) -> dict:
        """Only the rows whose metadata changed since the last collection.

        Bounded by how many tracks were scanned, not by playlist size, so this
        stays a few kilobytes whether the playlist holds fifty tracks or fifty
        thousand.
        """
        with self._lock:
            rows = []
            for track_id in self._dirty:
                track = self._playlist.by_id(track_id)
                if track is None:
                    continue
                rows.append(self._track_row(track_id, track))
            self._dirty.clear()
            return {"tracks": rows, "meta_revision": self._meta_revision}

    def get_peaks(self) -> list:
        return self._peaks

    def _rebuild_snapshot(self) -> None:
        with self._lock:
            track = self._playlist.by_id(self._current_id)
            tick = {
                "current_id": self._current_id,
                "playing": self._engine.playing,
                "paused": self._engine.paused,
                "position": round(self._engine.position, 2),
                "duration": round(self._engine.duration, 2),
                "volume": round(self._premute_volume if self._muted
                                else self._engine.volume, 3),
                "muted": self._muted,
                "shuffle": self._shuffle,
                "repeat": self._repeat,
                "status": self._footer(),
                "maximized": self._maximized,
                "revision": self._revision,
                "meta_revision": self._meta_revision,
                # Lets the frontend keep the queue topped up without ever
                # dumping a whole playlist into it.
                "scan_pending": self._scanner.pending,
                "library_revision": self._library_revision,
                "library_browser_revision": self._library_browser_revision,
                "library_detail_revision": self._library_detail_revision,
                "library_art_revision": self._library_art_revision,
                "library_scanning": self._library_scanning,
                "library_editor_revision": self._library_editor_revision,
                "play_context_source": self._play_context["source"],
                "play_context_kind": self._play_context["kind"],
                "play_context_title": self._play_context["title"],
                # So the library views can show the same play indicator the
                # playlist does, next to whichever row matches this path,
                # without leaving the library to see what is playing.
                "current_path": track.path if track else "",
            }
            if self._revision != self._full_revision:
                self._full_revision = self._revision
                self._dirty.clear()
                full = {
                    "tracks": [
                        self._track_row(self._playlist.id_at(i), t)
                        for i, t in enumerate(self._playlist.tracks)
                    ],
                    "title": track.title if track else "",
                    "artist": track.artist if track else "",
                    "art": self._art_cache.get(track.path) if track else None,
                    "revision": self._revision,
                }
            else:
                full = None
        with self._snap_lock:
            self._snapshot = tick
            if full is not None:
                self._full = full

    def _ensure_art(self, track) -> None:
        """Extract album art on a thread of its own.

        Not on the command worker: reading art from a file on a network share
        takes long enough that a play or next press would sit in the queue
        behind it.
        """
        if track is None or track.path in self._art_cache:
            return
        path = track.path
        self._art_cache[path] = None

        def work():
            try:
                url = self._art.data_url(path)
            except Exception:
                # A track with no artwork is normal and returns None; reaching
                # here means the extraction itself broke.
                log.warning("album art extraction failed for %s", path,
                            exc_info=True)
                url = None
            # Hand the result back to the worker rather than touching shared
            # state and the revision counter from this thread.
            self._post("art_ready", path, url)

        threading.Thread(target=work, name="elysian-art", daemon=True).start()

    def _do_art_ready(self, path: str, url) -> None:
        self._art_cache[path] = url
        while len(self._art_cache) > 48:
            self._art_cache.pop(next(iter(self._art_cache)), None)
        if url:
            self._bump()

    def _ensure_peaks(self, track) -> None:
        if track is None or self._peaks_for == track.path:
            return
        self._peaks_for = track.path
        self._peaks = []
        path = track.path
        def work():
            try:
                found = peaks_for(path)
            except Exception:
                log.warning("waveform failed for %s", path, exc_info=True)
                found = []
            self._post("peaks_ready", path, found)
        threading.Thread(target=work, name="elysian-peaks", daemon=True).start()

    def _do_peaks_ready(self, path: str, found) -> None:
        if self._peaks_for == path:
            self._peaks = found

    # ---- adding --------------------------------------------------------

    def _walk_folder(self, root: str):
        """Yield audio paths as they are discovered.

        os.scandir with a stack, not Path.rglob, and no sort of the whole
        result. rglob materialised the entire tree before returning a single
        path, so a large network share showed nothing for minutes.
        """
        stack = [root]
        while stack:
            if self._closing:
                return
            folder = stack.pop()
            try:
                entries = sorted(os.scandir(folder), key=lambda e: e.name)
            except OSError:
                continue
            subdirs = []
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.path)
                    # splitext, not Path().suffix: runs per file while
                    # walking a share, and is ~5x faster.
                    elif os.path.splitext(entry.name)[1].lower() \
                            in config.AUDIO_EXTENSIONS:
                        yield entry.path
                except OSError:
                    continue
            stack.extend(reversed(subdirs))

    def _ingest_folder_async(self, root: str) -> None:
        """Walk a folder on a helper thread, adding tracks in batches.

        The playlist grows while you watch instead of after the whole share
        has been enumerated. The helper thread only discovers paths and posts
        them; the worker thread applies every mutation, so playlist state,
        the revision counter and the status line keep a single owner.
        """
        gen = self._ingest_gen

        def work():
            batch = []
            for path in self._walk_folder(root):
                if gen != self._ingest_gen:
                    # The playlist was cleared after this walk began. Its
                    # batches would repopulate the list the user just
                    # emptied, so stop rather than keep reading the share.
                    return
                batch.append(path)
                if len(batch) >= 150:
                    self._post("add_batch", batch, gen)
                    batch = []
            if batch:
                self._post("add_batch", batch, gen)
            self._post("folder_scan_done", gen)

        self._post("status", "Scanning folder...", 30.0)
        threading.Thread(target=work, name="elysian-folder", daemon=True).start()

    def _do_add_batch(self, paths, gen=None) -> None:
        if gen is not None and gen != self._ingest_gen:
            return
        # Mutation is worker-only now, but _lock still matters: get_meta
        # iterates playlist state under it from the bridge thread, so every
        # worker-side mutation must hold it for that read to be safe.
        with self._lock:
            before = len(self._playlist)
            added = self._playlist.add_paths(paths)
            added_ids = self._added_ids_from_tail(before, len(added))
        if added and self._shuffle:
            self._extend_shuffle_bag(added_ids)
        if added:
            self._play_context = {"source": "playlist", "kind": "", "title": ""}
            self._bump()
            # Dropping several folders at once merges their counts. That is
            # rare, and "Added N tracks" for the lot is still the truth.
            self._folder_added += len(added)
            self._set_status(f"Scanning folder... {self._folder_added} found",
                             30.0)

    def _do_folder_scan_done(self, gen=None) -> None:
        if gen is not None and gen != self._ingest_gen:
            return
        total = self._folder_added
        self._folder_added = 0
        if total:
            self._set_status(f"Added {total} track{'s' if total != 1 else ''}")
        else:
            self._set_status("No audio files found")

    def _do_status(self, text: str, seconds: float = 4.0) -> None:
        self._set_status(text, seconds)

    def _split_paths(self, paths):
        """Sort raw paths into audio files and folders.

        is_dir() is a stat per path, a round trip each on a network share, so
        this only ever runs on the worker thread.
        """
        audio, folders = [], []
        for raw in paths or []:
            p = Path(raw)
            if p.is_dir():
                folders.append(str(p))
            elif p.suffix.lower() in config.AUDIO_EXTENSIONS:
                audio.append(str(p))
        return audio, folders

    def _ingest(self, paths) -> int:
        """Hand raw paths to the worker and return immediately.

        Even classifying the paths is I/O (is_dir stats each one), so the
        bridge and drop threads get to do none of it. Returns the number of
        paths handed off, not the number added; nothing reads this value, and
        dedup results arrive through the status line.
        """
        paths = [str(p) for p in (paths or []) if p]
        if not paths:
            return 0
        self._post("ingest", paths)
        return len(paths)

    def _do_ingest(self, paths) -> None:
        audio, folders = self._split_paths(paths)
        for folder in folders:
            self._ingest_folder_async(folder)
        if audio:
            self._do_add_audio_paths(audio)
        elif not folders:
            self._set_status("No audio files found")

    def _do_add_audio_paths(self, paths) -> int:
        # _lock guards readers that iterate playlist state from the bridge
        # thread (get_meta), now that mutation itself is worker-only.
        with self._lock:
            before = len(self._playlist)
            added = self._playlist.add_paths(paths)
            added_ids = self._added_ids_from_tail(before, len(added))
        if added and self._shuffle:
            self._extend_shuffle_bag(added_ids)
        if added:
            self._play_context = {"source": "playlist", "kind": "", "title": ""}
            self._bump()
            self._set_status(f"Added {len(added)} track{'s' if len(added) != 1 else ''}")
        else:
            self._set_status("Already in the playlist")
        # Returned so callers can say something more specific than the
        # generic status above; _do_library_enqueue was already written as
        # though this reported a count.
        return len(added)

    def add_files(self) -> int:
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("Audio (*.mp3;*.flac;*.wav;*.ogg)", "All files (*.*)"))
        return self._ingest(result or [])

    def add_folder(self) -> int:
        import webview

        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return self._ingest(result or [])

    def _add_paths(self, paths) -> int:
        return self._ingest(paths or [])

    def load_m3u(self) -> int:
        """Pick a playlist file; the worker does the reading.

        The playlist may sit on a network share, and reading it on the bridge
        call would freeze the interface exactly the way this app is built not
        to. Returns 1 if a file was chosen, 0 if the dialog was cancelled; the
        outcome arrives through the status line like every other slow result.
        """
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Playlists (*.m3u;*.m3u8)", "All files (*.*)"))
        if not result:
            return 0
        self._post("load_m3u", result[0])
        return 1

    def _do_load_m3u(self, path: str) -> None:
        try:
            # Parsing reads the file and stats candidates, so it happens
            # outside _lock; only the mutation needs it.
            found = self._playlist.read_m3u_paths(path, config.AUDIO_EXTENSIONS)
        except OSError:
            log.error("could not load playlist %s", path, exc_info=True)
            self._set_status("Could not load playlist")
            return
        with self._lock:
            before = len(self._playlist)
            added = self._playlist.add_paths(found)
            added_ids = self._added_ids_from_tail(before, len(added))
        if added and self._shuffle:
            self._extend_shuffle_bag(added_ids)
        if added:
            self._play_context = {"source": "playlist", "kind": "", "title": ""}
            self._bump()
        self._set_status(f"Loaded {len(added)} track{'s' if len(added) != 1 else ''}")

    def save_m3u(self) -> bool:
        """Pick a destination; the worker writes the file.

        Writing on the bridge call could block on a slow share the same way
        loading could. Returns True if a destination was chosen.
        """
        import webview

        if not len(self._playlist):
            self._post("status", "Playlist is empty")
            return False
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename="playlist.m3u")
        if not result:
            return False
        path = result if isinstance(result, str) else result[0]
        self._post("save_m3u", path)
        return True

    def _do_save_m3u(self, path: str) -> None:
        try:
            self._playlist.save_m3u(path)
            self._set_status(f"Saved {os.path.basename(path)}")
        except OSError as exc:
            self._set_status(f"Could not save: {exc}")

    def clear_playlist(self) -> None:
        self._post("clear_playlist")

    def _reset_playlist_state(self) -> None:
        """Empty the playlist and everything that describes it.

        Shared by _do_clear_playlist and _replace_playlist_with_paths so the
        two cannot drift: both need every piece of per-track state - the
        shuffle bag, play history, waveform cache, resume point and art
        cache - left matching an empty playlist before anything new goes
        in. Invalidating running folder walks and queued tag reads here too
        stops them spending share round trips on tracks that are about to
        stop existing.
        """
        self._engine.stop()
        self._ingest_gen += 1
        self._folder_added = 0
        self._scanner.drop_all()
        self._queued.clear()
        with self._lock:
            self._playlist.clear()
            self._current_id = -1
            self._dirty.clear()
            self._meta_revision += 1
        self._shuffle_bag = []
        self._history.clear()
        self._peaks = []
        self._peaks_for = None
        self._resume_id = -1
        self._resume_at = 0.0
        self._art_cache.clear()

    def _do_clear_playlist(self) -> None:
        """Remove every track and reset playlist-related state.

        Worker-owned, like every other mutation. The frontend clears its
        local selection immediately, but the source of truth is here.
        """
        self._reset_playlist_state()
        self._play_context = {"source": "playlist", "kind": "", "title": ""}
        self._bump()
        self._set_status("Playlist cleared")

    def _replace_playlist_with_paths(self, paths) -> int:
        """Replace the active playlist with exactly these paths, in order.

        Used for library-context playback: the paths are already known
        good, since they came from the library index rather than a folder
        walk, so there is nothing here to validate. Returns the number of
        tracks loaded.
        """
        clean = [str(p) for p in (paths or []) if p]
        if not clean:
            return 0
        self._reset_playlist_state()
        with self._lock:
            added = self._playlist.add_paths(clean)
        if self._shuffle:
            self._rebuild_bag()
        self._bump()
        return len(added)

    def _do_remove(self, ids) -> None:
        ids = {int(i) for i in ids or []}
        if not ids:
            return
        if self._current_id in ids:
            self._do_stop()
        self._playlist.remove_ids(ids)
        self._play_context = {"source": "playlist", "kind": "", "title": ""}
        # Only worth reshuffling if shuffle is on. Correctness never depended
        # on this rebuild anyway: _next_id skips ids that no longer exist.
        if self._shuffle:
            self._rebuild_bag()
        self._bump()
        self._set_status(f"Removed {len(ids)} track{'s' if len(ids) != 1 else ''}")

    def _do_reorder(self, dragged_id: int, target_id: int) -> None:
        self._playlist.move(int(dragged_id), int(target_id))
        self._play_context = {"source": "playlist", "kind": "", "title": ""}
        self._bump()

    # ---- transport -----------------------------------------------------

    def _do_play_id(self, track_id: int, start: float = 0.0) -> bool:
        track = self._playlist.by_id(int(track_id))
        if track is None:
            return False
        # Pick up where the last session left off, once, for that one track.
        if int(track_id) == self._resume_id:
            if start <= 0.0:
                start = self._resume_at
            self._resume_id = -1
            self._resume_at = 0.0
        try:
            self._engine.play(track.path, start)
        except PlaybackError as exc:
            self._set_status(f"Cannot play {os.path.basename(track.path)}: {exc}")
            return False
        self._current_id = int(track_id)
        self._scan_one(self._current_id)
        if not track.scanned or not track.length:
            # Writing length directly means the cached total is now wrong.
            # _lock for the _dirty add: get_meta iterates that set under it.
            with self._lock:
                before = track.length
                track.length = self._engine.duration
                self._playlist.adjust_length(track.length - before)
                self._dirty.add(self._current_id)
                self._meta_revision += 1
        if not self._history or self._history[-1] != self._current_id:
            self._history.append(self._current_id)
            del self._history[:-200]
        self._bump()
        return True

    def _do_toggle_play(self) -> None:
        if not len(self._playlist):
            self._set_status("Add some music first")
            return
        if self._engine.active:
            self._engine.toggle()
        else:
            target = self._current_id if self._playlist.by_id(self._current_id) \
                else self._playlist.id_at(0)
            self._do_play_id(target)

    def _do_stop(self) -> None:
        self._engine.stop()
        self._bump()

    def _do_next_track(self, auto: bool = False) -> None:
        if not len(self._playlist):
            return
        if auto and self._repeat == "one":
            self._do_play_id(self._current_id)
            return
        nxt = self._next_id(auto)
        if nxt is None:
            self._do_stop()
            return
        self._do_play_id(nxt)

    def _do_previous(self) -> None:
        if self._engine.position > 3.0:
            self._engine.seek(0.0)
            return
        if self._shuffle and len(self._history) >= 2:
            self._history.pop()
            self._do_play_id(self._history[-1])
            return
        index = self._playlist.index_of(self._current_id)
        if index > 0:
            self._do_play_id(self._playlist.id_at(index - 1))
        elif self._repeat == "all" and len(self._playlist):
            self._do_play_id(self._playlist.id_at(len(self._playlist) - 1))
        else:
            self._engine.seek(0.0)

    def _do_seek(self, seconds: float) -> None:
        self._engine.seek(float(seconds))

    def _do_nudge(self, delta: float) -> None:
        self._engine.nudge(float(delta))

    def _do_set_volume(self, value: float) -> None:
        # Touching the volume while muted unmutes, as the system mixer does;
        # otherwise the slider moves and nothing audible happens.
        self._muted = False
        self._engine.set_volume(float(value))
        self._premute_volume = self._engine.volume

    def _do_toggle_mute(self) -> None:
        if self._muted:
            self._muted = False
            # Unmuting to silence reads as a dead button, so a zero premute
            # volume restores to something audible instead.
            restore = self._premute_volume if self._premute_volume > 0 else 0.5
            self._engine.set_volume(restore)
        else:
            self._premute_volume = self._engine.volume
            self._muted = True
            self._engine.set_volume(0.0)
        self._bump()

    def _do_toggle_shuffle(self) -> None:
        self._shuffle = not self._shuffle
        if self._shuffle:
            self._rebuild_bag()
        else:
            # No point generating a fresh random permutation nobody will draw
            # from. Toggling back on rebuilds.
            self._shuffle_bag = []
        self._bump()

    def _do_cycle_repeat(self) -> None:
        self._repeat = REPEAT_CYCLE[self._repeat]
        self._bump()

    def _advance_if_finished(self) -> None:
        if self._current_id >= 0 and self._engine.finished():
            self._engine.stop()
            self._do_next_track(auto=True)

    def _next_id(self, auto: bool) -> int | None:
        if self._shuffle:
            if not self._shuffle_bag:
                if self._repeat == "all" or not auto:
                    self._rebuild_bag()
                else:
                    return None
            while self._shuffle_bag:
                candidate = self._shuffle_bag.pop()
                if self._playlist.index_of(candidate) >= 0:
                    return candidate
            return None
        index = self._playlist.index_of(self._current_id)
        if index + 1 < len(self._playlist):
            return self._playlist.id_at(index + 1)
        if self._repeat == "all" and len(self._playlist):
            return self._playlist.id_at(0)
        return None

    def _rebuild_bag(self) -> None:
        """Regenerate the shuffle draw order. Callers gate on self._shuffle,
        so while shuffle is off the bag stays empty and add/remove/load paths
        skip this entirely.

        Only the shared reads happen under _lock; the filter and shuffle of
        what can be a large id list run outside it, so bridge-thread readers
        are held up for a copy, not a shuffle. The bag itself is worker-owned.
        """
        with self._lock:
            current = self._current_id
            ids = self._playlist.ids
        ids = [i for i in ids if i != current]
        random.shuffle(ids)
        self._shuffle_bag = ids

    def _added_ids_from_tail(self, before_len: int, added_count: int) -> list[int]:
        """Ids of the tracks the most recent add_paths() appended.

        add_paths always appends new tracks at the tail, so the new ids are
        exactly the positions [before_len, before_len + added_count).
        Callers invoke this under _lock, right after the add.
        """
        if added_count <= 0:
            return []
        return [self._playlist.id_at(i)
                for i in range(before_len, before_len + added_count)]

    def _extend_shuffle_bag(self, new_ids) -> None:
        """Mix newly appended ids into the existing bag without a rebuild.

        Each id goes to a uniformly random position, NOT the end: _next_id
        pops from the end, so appending would make "shuffle" keep drawing
        the newest batch first for the whole length of a folder scan.
        Random insertion keeps draws uniform across the library and leaves
        the relative order of everything already in the bag untouched.
        Unlike the full rebuild this never re-admits already-played tracks,
        which also stops long scans from repeating songs you just heard.
        """
        bag = self._shuffle_bag
        for tid in new_ids:
            if tid == self._current_id:
                continue
            bag.insert(random.randrange(len(bag) + 1), tid)

    # ---- library: worker side -------------------------------------------
    #
    # Every query runs on a thread of its own and posts its result back,
    # rather than running on the worker. A GROUP BY over a large library is
    # not free, and the worker is what services play, seek and next: a
    # browse should never be able to stall transport. This is the same
    # shape already used for album art and waveform peaks.

    def _bump_library(self) -> None:
        self._library_revision += 1

    def _refresh_library_summary(self) -> None:
        def work():
            try:
                data = self._library.summary()
            except Exception:
                log.exception("library summary failed")
                return
            self._post("library_summary_ready", data)
        threading.Thread(target=work, name="elysian-lib-summary",
                         daemon=True).start()

    def _do_library_summary_ready(self, data) -> None:
        self._library_summary = data
        self._settings["library_roots"] = list(data.get("roots", []))
        settings_store.save(self._settings)
        self._bump_library()

    def _do_library_add_root(self, path) -> None:
        if self._library.add_root(path):
            self._bump_library()
            self._do_library_scan(path)
        else:
            self._set_status("Could not add that folder to the library")

    def _do_library_remove_root(self, path) -> None:
        removed = self._library.remove_root(path)
        self._forget_art_misses()
        self._set_status(f"Removed {removed} track(s) from the library")
        self._bump_library()
        self._refresh_library_summary()
        self._do_library_browser(
            self._library_browser.get("view", "albums"),
            self._library_browser.get("needle", ""))

    def _do_library_scan(self, root=None) -> None:
        if self._library_scanning:
            self._set_status("A library scan is already running")
            return
        self._library_scanning = True
        self._set_status("Scanning library...", 60.0)

        def work():
            try:
                if root:
                    result = self._library.scan_root(root, progress=report)
                else:
                    result = self._library.rescan_all(progress=report)
            except Exception as exc:
                log.exception("library scan failed")
                self._post("library_scan_failed", str(exc))
                return
            self._post("library_scan_done", result)

        def report(totals, folder):
            # Posted rather than written directly: the counters are read by
            # the snapshot the frontend polls, and that belongs to the
            # worker like everything else it reads.
            self._post("library_scan_progress", dict(totals), str(folder))

        threading.Thread(target=work, name="elysian-lib-scan",
                         daemon=True).start()

    def _do_library_scan_progress(self, totals, folder) -> None:
        found = int(totals.get("scanned", 0) or 0)
        folders = int(totals.get("folders", 0) or 0)
        name = os.path.basename(str(folder).rstrip("\\/")) or folder
        self._set_status(
            f"Scanning library: {found} tracks in {folders} folders, {name}",
            60.0)
        # Show the new albums as they arrive rather than at the end. Both
        # queries cost more as the library grows, so they are throttled;
        # a long scan should not spend its time re-aggregating a table it
        # is still writing to.
        now = time.monotonic()
        if now - self._library_refresh_at >= 2.0:
            self._library_refresh_at = now
            self._forget_art_misses()
            self._library_revision += 1
            self._refresh_library_summary()
            self._do_library_browser(
                self._library_browser.get("view", "albums"),
                self._library_browser.get("needle", ""))

    def _do_library_scan_done(self, result) -> None:
        self._library_scanning = False
        scanned = int(result.get("scanned", 0) or 0)
        updated = int(result.get("updated", 0) or 0)
        removed = int(result.get("removed", 0) or 0)
        self._library_refresh_at = 0.0
        if result.get("cancelled"):
            self._set_status("Library scan cancelled")
        elif updated or removed:
            self._set_status(f"Library: {scanned} seen, {updated} added or "
                             f"updated, {removed} gone")
        else:
            self._set_status(f"Library up to date, {scanned} tracks")
        self._bump_library()
        self._refresh_library_summary()
        self._do_library_browser(
            self._library_browser.get("view", "albums"),
            self._library_browser.get("needle", ""))

    def _do_library_scan_failed(self, message) -> None:
        self._library_scanning = False
        self._set_status(f"Library scan failed: {message}")

    def _do_library_browser(self, view, needle="") -> None:
        view = (view if view in ("albums", "artists", "genres", "songs")
                else "albums")
        needle = str(needle or "")
        self._library_browser_gen += 1
        gen = self._library_browser_gen

        def work():
            # Filtering happens here rather than in the frontend, which can
            # only match what it has already been sent: a song title is not
            # in the album list, so searching for one found nothing.
            try:
                if view == "artists":
                    items = self._library.artists(needle)
                elif view == "genres":
                    items = self._library.genres(needle)
                elif view == "songs":
                    items = self._library.songs(needle)
                else:
                    items = self._library.albums(needle)
            except Exception:
                log.exception("library browser query failed")
                items = []
            if gen != self._library_browser_gen:
                return  # a newer request has since been made; this result
                        # is not wrong, just late, and showing it now would
                        # silently undo whatever the newer one produced
            self._post("library_browser_ready", view, items, needle)

        threading.Thread(target=work, name="elysian-lib-browse",
                         daemon=True).start()

    def _do_library_browser_ready(self, view, items, needle="") -> None:
        if view == "albums" and not needle:
            # Fill in the whole library in the background. Anything already
            # cached or queued is skipped, so a refresh during a scan does
            # not re-queue what is already done.
            self._do_library_fill_art(
                [f"{i.get('album_artist','')}\u0000{i.get('album','')}"
                 for i in (items or [])])
        self._library_browser_revision += 1
        self._library_browser = {"view": view, "items": items,
                                 "needle": needle,
                                 "revision": self._library_browser_revision}
        self._settings["library_view"] = view
        settings_store.save(self._settings)

    def _do_library_detail(self, kind, key, key2="") -> None:
        self._library_detail_gen += 1
        gen = self._library_detail_gen

        def work():
            try:
                if kind == "album":
                    items = self._library.album_tracks(key, key2)
                    title = key
                elif kind == "artist":
                    items = self._library.artist_tracks(key)
                    title = key
                elif kind == "genre":
                    items = self._library.genre_tracks(key)
                    title = key
                elif kind == "search":
                    items = self._library.search(key)
                    title = f'Search: {key}'
                else:
                    items, title = [], ""
            except Exception:
                log.exception("library detail query failed")
                items, title = [], ""
            if gen != self._library_detail_gen:
                return  # superseded by a later drill-in or double-click
            self._post("library_detail_ready", kind, key, key2, title, items)

        threading.Thread(target=work, name="elysian-lib-detail",
                         daemon=True).start()

    def _do_library_detail_ready(self, kind, key, key2, title, items) -> None:
        self._library_detail_revision += 1
        self._library_detail = {"kind": kind, "key": key, "key2": key2,
                                "title": title, "items": items,
                                "revision": self._library_detail_revision}

    def _start_art_workers(self) -> None:
        if self._art_workers:
            return
        for i in range(4):
            t = threading.Thread(target=self._art_worker,
                                 name=f"elysian-lib-art-{i}", daemon=True)
            t.start()
            self._art_workers.append(t)

    def _art_worker(self) -> None:
        while not self._closing:
            try:
                key, album, artist = self._art_urgent.get_nowait()
            except queue.Empty:
                try:
                    key, album, artist = self._art_bulk.get(timeout=0.25)
                except queue.Empty:
                    continue
                if key not in self._art_bulk_pending:
                    continue        # promoted to urgent, or already done
                self._art_bulk_pending.discard(key)
            url = None
            try:
                for path in self._library.album_paths(album, artist):
                    url = self._art.data_url(path)
                    if url:
                        break
            except Exception:
                log.warning("library art failed for %s", album, exc_info=True)
                url = None
            self._post("library_art_ready", key, url)

    def _do_library_visible_art(self, keys) -> None:
        """Resolve exactly these albums next, in this order.

        Only the urgent queue is emptied. Work for cards that have scrolled
        away should not be done ahead of what is on screen, but it is still
        worth doing, so it stays in the background queue rather than being
        thrown away.
        """
        while True:
            try:
                dropped = self._art_urgent.get_nowait()
            except queue.Empty:
                break
            self._library_art_pending.discard(dropped[0])
        self._start_art_workers()
        for key in keys or []:
            if key in self._library_art:
                continue
            promoted = key in self._art_bulk_pending
            if promoted:
                # Waiting in the background queue behind the rest of the
                # library. Move it to the front rather than skipping it as
                # already pending, which is what left a scrolled to row
                # waiting for everything queued ahead of it.
                self._art_bulk_pending.discard(key)
            elif key in self._library_art_pending:
                continue            # already urgent, or being decoded now
            artist, _, album = str(key).partition("\u0000")
            self._library_art_pending.add(key)
            self._art_urgent.put((key, album, artist))

    def _do_library_fill_art(self, keys) -> None:
        """Queue the rest of the library, behind anything on screen."""
        self._start_art_workers()
        for key in keys or []:
            if key in self._library_art or key in self._library_art_pending:
                continue
            artist, _, album = str(key).partition("\u0000")
            self._library_art_pending.add(key)
            self._art_bulk_pending.add(key)
            self._art_bulk.put((key, album, artist))

    def _do_library_art(self, album, album_artist) -> None:
        key = f"{album_artist}\u0000{album}"
        if key in self._library_art or key in self._library_art_pending:
            return
        self._library_art_pending.add(key)
        self._start_art_workers()
        self._art_urgent.put((key, album, album_artist))

    def _forget_art_misses(self) -> None:
        """Drop remembered "this album has no cover" answers.

        A miss is only true for the tracks indexed at the time it was
        asked. Folders are committed one at a time, so an album can be half
        indexed when its card first appears and the track carrying the
        artwork may not have arrived yet. Keeping that answer meant the
        cover never appeared however much of the album turned up later.
        Successful lookups are kept: those cannot become wrong.
        """
        missing = [k for k, v in self._library_art.items() if not v[1]]
        if not missing:
            return
        for key in missing:
            self._library_art.pop(key, None)
        self._library_art_revision += 1

    def _do_library_art_ready(self, key, url) -> None:
        self._library_art_pending.discard(key)
        self._art_bulk_pending.discard(key)
        # Cached even when nothing was found, so a coverless album is not
        # searched again every time it scrolls past.
        self._library_art_seq += 1
        self._library_art[key] = [self._library_art_seq, url or ""]
        while len(self._library_art) > 4000:
            self._library_art.pop(next(iter(self._library_art)), None)
        self._library_art_revision += 1

    def _do_library_enqueue(self, paths) -> None:
        # _do_add_audio_paths already sets an "Added N tracks" status; a
        # second, near-identical one here just overwrites it a moment
        # later for no benefit.
        self._do_add_audio_paths(paths)

    def _do_library_play_context(self, paths, start_path, kind="", title="") -> None:
        """Replace the active playlist with a library-derived queue and play.

        paths is the library view's own current order - an album's track
        list, an artist's or genre's detail list, or the songs list exactly
        as displayed - so double-clicking one track in the middle of it
        continues through the rest of what was on screen rather than
        stopping or falling back to whatever the playlist held before.
        start_path is the track actually activated; kind and title describe
        the source for the status line only, transport never branches on
        them.
        """
        ordered = [str(p) for p in (paths or []) if p]
        start_path = str(start_path or "")
        if not ordered or not start_path:
            return
        loaded = self._replace_playlist_with_paths(ordered)
        if not loaded:
            self._set_status("Nothing to play")
            return
        self._play_context = {"source": "library", "kind": str(kind or ""),
                              "title": str(title or "")}
        with self._lock:
            index = self._playlist.index_of_path(start_path)
            target = (self._playlist.id_at(index) if index >= 0
                     else self._playlist.id_at(0))
        if target >= 0:
            self._do_play_id(target)
            if title:
                self._set_status(f"Playing: {title}")

    def _refresh_playlist_paths(self, paths) -> int:
        """Apply freshly written tags to any playlist rows for these files.

        Without this, a track already sitting in the playlist would keep
        showing its old title and artist until it was rescanned some other
        way. apply_metadata only touches title, artist, album and length,
        which is also all the playlist row ever displays; genre, track
        number and the rest are library-only and need nothing here.
        """
        fresh = self._library.lookup(paths)
        if not fresh:
            return 0
        wanted = {pathutil.key(p) for p in paths}
        changed = 0
        with self._lock:
            for track_id, track in zip(self._playlist.ids, self._playlist.tracks):
                if pathutil.key(track.path) not in wanted:
                    continue
                info = fresh.get(track.path)
                if info is None:
                    # lookup() keys its result by the exact path it was
                    # asked with, which may differ in case or separators
                    # from how this row's path happens to be spelled.
                    info = next((v for p, v in fresh.items()
                                if pathutil.same(p, track.path)), None)
                if info is None:
                    continue
                before = track.length
                apply_metadata(track, info)
                self._playlist.adjust_length(track.length - before)
                self._dirty.add(track_id)
                changed += 1
            if changed:
                self._meta_revision += 1
        return changed

    def _do_library_open_editor(self, paths) -> None:
        """Load current tags for these files into the editor, off the worker.

        Reads each file directly rather than the index: the index can be a
        scan behind on purpose, so this is the one place that has to show
        what the file actually holds right now. The same read also
        refreshes the index for these exact paths as a side effect.
        """
        clean = [str(p) for p in (paths or []) if p]
        if not clean:
            return
        self._library_editor = {
            "open": True, "loading": True, "saving": False,
            "paths": clean, "count": len(clean), "data": {}, "mixed": {},
            "errors": [], "saved": 0, "failed": 0,
        }
        self._bump_library_editor()

        def work():
            try:
                payload = self._library.edit_payload(clean)
            except Exception:
                log.exception("could not build the tag editor payload")
                payload = {"count": 0, "paths": [], "data": {}, "mixed": {}}
            self._post("library_editor_ready", payload)

        threading.Thread(target=work, name="elysian-lib-editor-open",
                         daemon=True).start()

    def _do_library_editor_ready(self, payload) -> None:
        if not self._library_editor.get("open"):
            return  # closed before the lookup finished; nothing to show
        self._library_editor = {
            "open": True, "loading": False, "saving": False,
            "paths": payload.get("paths", []), "count": payload.get("count", 0),
            "data": payload.get("data", {}), "mixed": payload.get("mixed", {}),
            "errors": [], "saved": 0, "failed": 0,
        }
        self._bump_library_editor()

    def _do_library_close_editor(self) -> None:
        self._library_editor = {
            "open": False, "loading": False, "saving": False,
            "paths": [], "count": 0, "data": {}, "mixed": {},
            "errors": [], "saved": 0, "failed": 0,
        }
        self._bump_library_editor()

    def _do_library_save_editor(self, paths, changes) -> None:
        """Write the edited tags to disk, off the worker, then reindex.

        The write itself can touch a file on a slow share, which is why it
        runs on its own thread rather than here: the worker also owns
        transport, and a write that took a second would be a second of
        nothing else in this app responding either.
        """
        clean = [str(p) for p in (paths or []) if p]
        changes = dict(changes or {})
        if not clean or not changes:
            self._do_library_close_editor()
            return
        self._library_editor["saving"] = True
        self._bump_library_editor()

        # Writing tags needs to open the file for write, and on Windows a
        # file the engine already has open for playback can make that fail
        # outright rather than partially succeed. stop() alone does not
        # release it - the decoder underneath keeps the file open
        # regardless - so release_file() is what actually does;
        # _resume_after_tag_save puts it back once the write, whichever
        # way it goes, is actually done.
        self._tag_save_resume = None
        if self._engine.active and self._engine.path:
            if pathutil.key(self._engine.path) in pathutil.keys(clean):
                self._tag_save_resume = {
                    "id": self._current_id,
                    "position": self._engine.position,
                    "playing": self._engine.playing,
                }
                self._engine.release_file()
                self._bump()

        def work():
            try:
                result = _write_tags(clean, changes)
            except Exception as exc:
                log.exception("tag save failed outright")
                self._post("library_save_failed", str(exc))
                return
            written = [r["path"] for r in result["results"] if r["ok"]]
            errors = [r for r in result["results"] if not r["ok"]]
            reindexed = 0
            if written:
                try:
                    reindexed = self._library.refresh_paths(written)
                except Exception:
                    log.exception("could not reindex after saving tags")
            self._post("library_save_done", result, written, errors)

        threading.Thread(target=work, name="elysian-lib-editor-save",
                         daemon=True).start()

    def _do_library_save_done(self, result, written, errors) -> None:
        ok = result.get("ok", 0)
        failed = result.get("failed", 0)
        if written:
            self._refresh_playlist_paths(written)
            self._library_revision += 1
            self._refresh_library_summary()
            self._do_library_browser(self._library_browser.get("view", "albums"),
                                     self._library_browser.get("needle", ""))
            if self._library_detail.get("kind"):
                d = self._library_detail
                self._do_library_detail(d.get("kind", ""), d.get("key", ""),
                                        d.get("key2", ""))
        self._resume_after_tag_save()
        if failed:
            # Keep the editor open so the failures are visible, rather than
            # closing over a partial save the user never saw happen.
            self._library_editor["saving"] = False
            self._library_editor["errors"] = [str(e.get("error", ""))
                                              for e in errors]
            self._library_editor["saved"] = ok
            self._library_editor["failed"] = failed
            self._bump_library_editor()
            self._set_status(f"Saved {ok}, failed {failed}")
        else:
            self._do_library_close_editor()
            self._set_status(f"Saved tags for {ok} track"
                             f"{'s' if ok != 1 else ''}")

    def _do_library_save_failed(self, message) -> None:
        self._resume_after_tag_save()
        self._library_editor["saving"] = False
        self._library_editor["errors"] = [message]
        self._bump_library_editor()
        self._set_status("Could not save tags")

    def _resume_after_tag_save(self) -> None:
        """Put playback back the way a tag save's file release found it.

        Runs whether the save succeeded, partially failed, or failed
        outright: whatever happened to the write, stopping the engine to
        release the file is not something the save should leave behind.
        Skipped if something else already changed which track is current
        while the save was in flight - a user who moved on in the meantime
        should not be pulled back to a track they left.
        """
        resume = self._tag_save_resume
        self._tag_save_resume = None
        if resume is None or resume["id"] != self._current_id:
            return
        if self._playlist.by_id(resume["id"]) is None:
            return
        self._do_play_id(resume["id"], resume["position"])
        if not resume["playing"]:
            self._engine.pause()
        self._bump()

    def _bump_library_editor(self) -> None:
        self._library_editor_revision += 1

    # ---- library: bridge side -------------------------------------------

    def library_add_folder(self) -> int:
        """Picks a folder, then hands the work to the worker."""
        import webview

        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return 0
        folder = result[0] if isinstance(result, (list, tuple)) else result
        if not folder:
            return 0
        self._post("library_add_root", str(folder))
        return 1

    def library_remove_root(self, path) -> None:
        self._post("library_remove_root", str(path))

    def library_rescan(self) -> None:
        self._post("library_scan", None)

    def library_cancel_scan(self) -> None:
        self._library.cancel()

    def library_request_browser(self, view, needle="") -> None:
        self._post("library_browser", str(view), str(needle or ""))

    def library_request_detail(self, kind, key, key2="") -> None:
        self._post("library_detail", str(kind), str(key), str(key2 or ""))

    def library_get_state(self) -> dict:
        """Small, pure read: counters and the summary, never a query."""
        data = dict(self._library_summary)
        data["revision"] = self._library_revision
        data["browser_revision"] = self._library_browser_revision
        data["detail_revision"] = self._library_detail_revision
        data["scanning"] = self._library_scanning
        # The tab the library was left on. Saved when a browse result lands,
        # but nothing read it back, so the frontend always opened on albums.
        data["view"] = self._settings.get("library_view", "albums")
        return data

    def library_get_browser(self) -> dict:
        return dict(self._library_browser)

    def library_get_detail(self) -> dict:
        return dict(self._library_detail)

    def library_request_art(self, album, album_artist="") -> None:
        self._post("library_art", str(album or ""), str(album_artist or ""))

    def library_visible_art(self, keys) -> None:
        """The albums on screen right now, nearest the view first."""
        self._post("library_visible_art",
                   [str(k) for k in (keys or []) if k])

    def library_get_art(self, since=0) -> dict:
        """Covers resolved after the caller's last sequence number.

        A pure read of what the worker has already produced, and bounded by
        how many covers have arrived rather than by library size.
        """
        try:
            mark = int(since or 0)
        except (TypeError, ValueError):
            mark = 0
        fresh = {k: v[1] for k, v in self._library_art.items() if v[0] > mark}
        return {"seq": self._library_art_seq, "art": fresh,
                "reset": mark > self._library_art_seq}

    def library_enqueue(self, paths) -> None:
        self._post("library_enqueue", [str(p) for p in (paths or []) if p])

    def library_play_context(self, paths, start_path, kind="", title="") -> None:
        self._post(
            "library_play_context",
            [str(p) for p in (paths or []) if p],
            str(start_path or ""), str(kind or ""), str(title or ""),
        )

    def library_open_editor(self, paths) -> None:
        self._post("library_open_editor", [str(p) for p in (paths or []) if p])

    def library_close_editor(self) -> None:
        self._post("library_close_editor")

    def library_save_editor(self, paths, changes) -> None:
        self._post("library_save_editor",
                   [str(p) for p in (paths or []) if p], dict(changes or {}))

    def library_get_editor_state(self) -> dict:
        src = self._library_editor
        return {
            "open": bool(src.get("open", False)),
            "loading": bool(src.get("loading", False)),
            "saving": bool(src.get("saving", False)),
            "paths": list(src.get("paths", [])),
            "count": int(src.get("count", 0) or 0),
            "data": dict(src.get("data", {})),
            "mixed": dict(src.get("mixed", {})),
            "errors": list(src.get("errors", [])),
            "saved": int(src.get("saved", 0) or 0),
            "failed": int(src.get("failed", 0) or 0),
        }

    # ---- bridge: enqueue and return immediately -------------------------
    # Each of these can touch a file on a network share, so none of them may
    # run on the call from JavaScript. The frontend already updates itself
    # optimistically, so the round trip is invisible.

    def play_id(self, track_id: int, start: float = 0.0) -> None:
        """Queue playback of this track.

        Fire-and-forget: the outcome lands in the snapshot (or the status
        line on failure) after the worker processes the command. Nothing in
        the frontend or host reads a return value here.
        """
        self._post("play_id", int(track_id), float(start))

    def toggle_play(self) -> None:
        self._post("toggle_play")

    def stop(self) -> None:
        self._post("stop")

    def next_track(self) -> None:
        self._post("next_track", False)

    def previous(self) -> None:
        self._post("previous")

    def seek(self, seconds: float) -> None:
        self._post("seek", float(seconds))

    def nudge(self, delta: float) -> None:
        self._post("nudge", float(delta))

    def set_volume(self, value: float) -> None:
        self._post("set_volume", float(value))

    def toggle_shuffle(self) -> None:
        self._post("toggle_shuffle")

    def cycle_repeat(self) -> None:
        self._post("cycle_repeat")

    def toggle_mute(self) -> None:
        self._post("toggle_mute")

    def remove(self, ids) -> None:
        self._post("remove", [int(i) for i in (ids or [])])

    def reorder(self, dragged_id: int, target_id: int) -> None:
        self._post("reorder", int(dragged_id), int(target_id))

    # ---- window --------------------------------------------------------

    def win_minimise(self) -> None:
        if self._window:
            self._window.minimize()

    def win_maximise(self) -> None:
        """Toggle between maximised and normal.

        window.state is a dict pywebview uses for sharing values with the
        frontend, not window geometry, so the old check for a `maximized`
        attribute on it was always False, and an empty dict is falsy, so it
        short-circuited before even looking. The button only ever maximised.
        The real state is tracked from pywebview's own maximized/restored
        events, which also catches Win+Up and a title bar double-click.
        """
        if not self._window:
            return
        try:
            if self._maximized:
                self._window.restore()
                self._maximized = False
            else:
                self._window.maximize()
                self._maximized = True
        except Exception:
            log.warning("could not toggle the window state", exc_info=True)

    def win_close(self) -> None:
        if self._window:
            self._window.destroy()

    # Which corner/edge pywebview's own resize() keeps fixed while the
    # opposite one follows the mouse, for each of the eight drag handles.
    # window.resize() only takes a target width/height - it has no notion
    # of "drag this edge" - so the fix point is what makes, say, dragging
    # the left edge grow the window leftward instead of resizing in place
    # from the top-left corner, which is resize()'s own default.
    _RESIZE_FIX_POINTS = {
        "right":       FixPoint.NORTH | FixPoint.WEST,
        "bottom":      FixPoint.NORTH | FixPoint.WEST,
        "bottomright": FixPoint.NORTH | FixPoint.WEST,
        "left":        FixPoint.NORTH | FixPoint.EAST,
        "bottomleft":  FixPoint.NORTH | FixPoint.EAST,
        "top":         FixPoint.SOUTH | FixPoint.WEST,
        "topright":    FixPoint.SOUTH | FixPoint.WEST,
        "topleft":     FixPoint.SOUTH | FixPoint.EAST,
    }

    def win_resize_to(self, edge: str, width: float, height: float) -> None:
        """Resize toward a target size while dragging one edge or corner.

        A frameless window has no native resize border at all, so this is
        called continuously from JS while the mouse moves rather than
        started once and left to the OS: window.resize() only understands
        "be this size", not "the user is dragging". This is the same
        window.resize()/SetWindowPos call pywebview's own move() already
        uses for the working title-bar drag, not a raw WM_SYSCOMMAND -
        that approach looked right but never actually took over the mouse,
        since the WebView2 content keeps its own capture in a separate
        process that ReleaseCapture() on the top-level window never
        touches.
        """
        if not self._window:
            return
        fix_point = self._RESIZE_FIX_POINTS.get(str(edge or "").lower())
        if fix_point is None:
            return
        try:
            w = max(1, int(round(width)))
            h = max(1, int(round(height)))
            self._window.resize(w, h, fix_point)
        except Exception:
            log.warning("could not resize toward %r (%s x %s)",
                        edge, width, height, exc_info=True)

    # ---- session -------------------------------------------------------

    def _do_restore_session(self) -> None:
        # Deliberately no os.path.isfile() here. On a network share that is
        # one round trip per saved path before the window has even drawn.
        # Missing files surface only when something later tries to open them.
        paths = self._settings.get("playlist", [])
        if paths:
            self._playlist.add_paths(paths)
            # _shuffle was loaded from the same settings in __init__, so this
            # honours the saved state rather than assuming shuffle is on.
            if self._shuffle:
                self._rebuild_bag()
        last = self._settings.get("last_path", "")
        if last:
            self._resume_at = float(
                self._settings.get("last_position", 0.0) or 0.0)
            for i, track in enumerate(self._playlist.tracks):
                if pathutil.same(track.path, last):
                    self._current_id = self._playlist.id_at(i)
                    if self._resume_at > 1.0:
                        self._resume_id = self._current_id
                    # The restored track is shown in Now Playing immediately,
                    # so queue its tags now. Without this it sat on its
                    # filename until it was played again or scrolled into
                    # view, since only _do_play_id scanned the current track.
                    # _scan_one only submits to the background queue, so
                    # startup still opens no files.
                    self._scan_one(self._current_id)
                    break
        self._bump()

        # Re-register saved roots so the library is browsable straight away.
        # No scan is started here: opening the app should not go and read a
        # share, and the index already on disk answers every query.
        for root in self._settings.get("library_roots", []):
            try:
                self._library.add_root(root)
            except Exception:
                log.warning("could not restore library root %s", root,
                            exc_info=True)
        self._refresh_library_summary()

    def _save_session(self) -> None:
        track = self._playlist.by_id(self._current_id)
        self._settings.update({
            "volume": self._premute_volume if self._muted
                      else self._engine.volume,
            "shuffle": self._shuffle,
            "repeat": self._repeat,
            "playlist": [t.path for t in self._playlist.tracks],
            "last_path": track.path if track else "",
            "last_position": self._engine.position if self._engine.active else 0.0,
        })
        settings_store.save(self._settings)

    #: Queued commands whose effects the user expects to survive exit,
    #: applied by _flush_persisted before the session is saved.
    #: restore_session is here so a close moments after launch can never save
    #: an empty playlist over the previous session. library_play_context
    #: replaces the whole playlist exactly like clear_playlist, remove and
    #: reorder, and costs no file I/O to apply here, since add_paths only
    #: compares strings. save_m3u is an explicit
    #: user action; silently discarding a save the user believes happened is
    #: worse than a slow exit. load_m3u, ingest and open_paths are deliberately
    #: absent: applying them here would stat or read files, possibly over a
    #: dead network share, with the window already gone, and their loss costs
    #: nothing that reopening the app cannot redo. Anything else still queued
    #: at exit is transport, and running it on the way out would only start
    #: work nobody is waiting for.
    #: Underscored because pywebview walks every public attribute of this
    #: object when it builds the JS API.
    _PERSISTED_COMMANDS = frozenset({
        "library_add_root", "library_remove_root",
        "restore_session", "remove", "reorder", "add_batch",
        "set_volume", "toggle_shuffle", "cycle_repeat", "save_m3u",
        "clear_playlist", "library_play_context",
    })

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._worker.is_alive():
            self._worker.join(timeout=0.8)
        self._flush_persisted()
        try:
            self._drain_scanner()
        except Exception:
            log.exception("could not apply the last tag results")
        self._save_session()
        self._scanner.shutdown()

        self._engine.stop()

    def _flush_persisted(self) -> None:
        """Apply queued changes the worker never got to.

        Setting the closing flag stops the worker loop on its next pass, so a
        reorder or a removal made a moment before closing was still sitting in
        the queue and the session was then saved without it.

        Only called from _shutdown, and only for commands that affect what is
        saved.
        """
        while True:
            try:
                cmd = self._cmd.get_nowait()
            except queue.Empty:
                return
            if cmd[0] not in self._PERSISTED_COMMANDS:
                continue
            try:
                self._dispatch(cmd)
            except Exception:
                log.exception("could not apply %r while closing", cmd[0])


    # ---- entry points used by the host, kept off the JS bridge ----------
    # Every public attribute of this object is walked by pywebview when it
    # builds window.pywebview.api, and it recurses into non-callables. A
    # public reference to the Window made it descend into window.dom.document,
    # which blocks until the page has loaded, so the API object was never
    # created and every call from JavaScript failed.

    #: Everything JavaScript is allowed to call. Anything public and not in
    #: this set or HOST_PUBLIC is a mistake. See _assert_bridge_surface.
    JS_BRIDGE = frozenset({
        "get_tick", "get_full", "get_meta", "get_peaks",
        "request_scan", "request_ahead", "request_prefetch",
        "drop_prefetch", "reset_scan_queue",
        "add_files", "add_folder", "load_m3u", "save_m3u",
        "clear_playlist",
        "remove", "reorder",
        "play_id", "toggle_play", "stop", "next_track", "previous",
        "seek", "nudge", "set_volume", "toggle_shuffle", "cycle_repeat",
        "toggle_mute",
        "win_minimise", "win_maximise", "win_close", "win_resize_to",
        "library_add_folder", "library_remove_root", "library_rescan",
        "library_cancel_scan", "library_request_browser",
        "library_request_detail", "library_get_state",
        "library_get_browser", "library_get_detail",
        "library_enqueue", "library_play_context",
        "library_request_art", "library_visible_art", "library_get_art",
        "library_open_editor", "library_close_editor",
        "library_save_editor", "library_get_editor_state",
    })

    #: Public for the host process only, never called from JavaScript, but
    #: necessarily unprefixed. The two set names are here because pywebview
    #: sees them as public attributes of this object too.
    HOST_PUBLIC = frozenset({
        "attach", "boot", "ingest", "open_paths", "close", "set_maximized",
        "JS_BRIDGE", "HOST_PUBLIC",
    })

    def _assert_bridge_surface(self) -> None:
        """Fail loudly if a public attribute has crept onto this object.

        pywebview builds window.pywebview.api by walking public attributes and
        recursing into non-callables. A public reference to the window once
        made it descend into window.dom.document, which blocks until the page
        loads, so the API object was never created and every call from the
        frontend silently failed. This turns that class of mistake into an
        error at startup instead of a dead interface.
        """
        allowed = self.JS_BRIDGE | self.HOST_PUBLIC
        extra = {n for n in dir(self) if not n.startswith("_")} - allowed
        if extra:
            raise RuntimeError(
                "Api exposes unexpected public attributes to pywebview: "
                + ", ".join(sorted(extra))
                + ". Prefix them with an underscore, or add them to "
                  "Api.JS_BRIDGE (JavaScript may call them) or "
                  "Api.HOST_PUBLIC (host-side only).")
        methods = (self.JS_BRIDGE
                   | (self.HOST_PUBLIC - {"JS_BRIDGE", "HOST_PUBLIC"}))
        dud = sorted(n for n in methods
                     if not callable(getattr(self, n, None)))
        if dud:
            raise RuntimeError(
                "Api bridge sets list names that are not callable methods: "
                + ", ".join(dud)
                + ". A constant or attribute has taken a method's place.")

    def attach(self, window) -> None:
        self._window = window

    def set_maximized(self, flag: bool) -> None:
        """Called from pywebview's own window events, so the toggle stays
        correct when the user maximises by some other means."""
        # No _bump here: `maximized` rides on every tick, and bumping the
        # revision would make the frontend refetch the whole track list for a
        # window resize.
        self._maximized = bool(flag)

    def boot(self) -> None:
        # Enqueued, not called: the worker is already running its maintenance
        # loop by now, and restoring mutated the playlist from the host's
        # bind thread while _rebuild_snapshot was iterating it. The queue is
        # FIFO, so restore still lands before any open_paths posted after it.
        self._post("restore_session")

    def ingest(self, paths) -> int:
        return self._add_paths(paths)

    def close(self) -> None:
        self._shutdown()

    def open_paths(self, paths) -> int:
        """Queue these files to be added, and the FIRST one played.

        Explorer open used to need the id back synchronously so the host
        could call play_id, which meant playlist mutation on the host thread.
        The worker now owns the whole add-and-play step; it plays the file you
        opened whether or not it was already in the playlist. Returns 1 if
        anything was queued, 0 otherwise.
        """
        paths = [str(p) for p in (paths or []) if p]
        if not paths:
            return 0
        self._post("open_paths", paths)
        return 1

    def _do_open_paths(self, paths) -> None:
        audio, folders = self._split_paths(paths)
        for folder in folders:
            self._ingest_folder_async(folder)
        if not audio:
            if not folders:
                self._set_status("No audio files found")
            return
        self._do_add_audio_paths(audio)
        with self._lock:
            index = self._playlist.index_of_path(audio[0])
            target = self._playlist.id_at(index) if index >= 0 else -1
        if target >= 0:
            self._do_play_id(target)
