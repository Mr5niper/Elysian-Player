"""The object exposed to JavaScript as pywebview.api.

Every method here is callable from the frontend. All application state lives on
the Python side; the frontend polls get_state() and renders whatever it is
given, so there is exactly one source of truth.
"""
import os
import queue
import random
import threading
import time
from pathlib import Path

from . import config
from . import paths as pathutil
from .models.playlist import Playlist
from .models.track import format_time
from .playback.engine import PlaybackEngine, PlaybackError
from .services import settings as settings_store
from .services.art import ArtProvider
from .services.scanner import MetadataScanner, apply_metadata
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
        self._shuffle = bool(self._settings["shuffle"])
        self._repeat = self._settings["repeat"]
        self._engine.set_volume(self._settings.get("volume", 0.8))

        self._shuffle_bag: list[int] = []
        self._history: list[int] = []
        self._revision = 0
        self._status = ""
        self._status_until = 0.0
        self._peaks: list[float] = []
        self._peaks_for: str | None = None
        self._closing = False
        self._queued: dict[int, int] = {}
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

        self._snapshot: dict = {
            "current_id": -1, "playing": False, "paused": False,
            "position": 0.0, "duration": 0.0, "volume": self._engine.volume,
            "shuffle": self._shuffle, "repeat": self._repeat,
            "status": "", "maximized": False, "revision": 0,
            "meta_revision": 0, "scan_pending": 0,
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
        for track_id, info in self._scanner.drain():
            # Release the queue slot whether or not the track still exists, or
            # a removed track would hold its id forever.
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

        Everything that can block -- loading a track, reading tags, extracting
        album art -- happens on the worker thread and lands in _snapshot. A
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
            # full send, and Path is ~9x slower -- 120ms versus 11ms for 50k.
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
                "volume": round(self._engine.volume, 3),
                "shuffle": self._shuffle,
                "repeat": self._repeat,
                "status": self._footer(),
                "maximized": self._maximized,
                "revision": self._revision,
                "meta_revision": self._meta_revision,
                # Lets the frontend keep the queue topped up without ever
                # dumping a whole playlist into it.
                "scan_pending": self._scanner.pending,
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
        """Walk a folder on a worker thread, adding tracks in batches.

        The playlist grows while you watch instead of after the whole share
        has been enumerated.
        """
        def work():
            batch, total = [], 0
            for path in self._walk_folder(root):
                batch.append(path)
                if len(batch) >= 150:
                    total += self._add_batch(batch)
                    batch = []
                    self._set_status(f"Scanning folder... {total} found", 30.0)
            if batch:
                total += self._add_batch(batch)
            if total:
                self._set_status(f"Added {total} track{'s' if total != 1 else ''}")
            else:
                self._set_status("No audio files found")

        self._set_status("Scanning folder...", 30.0)
        threading.Thread(target=work, name="elysian-folder", daemon=True).start()

    def _add_batch(self, paths) -> int:
        with self._lock:
            added = self._playlist.add_paths(paths)
            if added:
                self._rebuild_bag()
        if added:
            self._bump()
        return len(added)

    def _ingest(self, paths) -> int:
        audio, folders = [], []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                folders.append(str(p))
            elif p.suffix.lower() in config.AUDIO_EXTENSIONS:
                audio.append(str(p))
        for folder in folders:
            self._ingest_folder_async(folder)
        if not audio:
            return 0
        with self._lock:
            added = self._playlist.add_paths(audio)
        if added:
            self._rebuild_bag()
            self._bump()
            self._set_status(f"Added {len(added)} track{'s' if len(added) != 1 else ''}")
        elif audio:
            self._set_status("Already in the playlist")
        else:
            self._set_status("No audio files found")
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
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Playlists (*.m3u;*.m3u8)", "All files (*.*)"))
        if not result:
            return 0
        added = self._playlist.load_m3u(result[0], config.AUDIO_EXTENSIONS)
        if added:
            self._rebuild_bag()
            self._bump()
        self._set_status(f"Loaded {len(added)} track{'s' if len(added) != 1 else ''}")
        return len(added)

    def save_m3u(self) -> bool:
        import webview

        if not len(self._playlist):
            self._set_status("Playlist is empty")
            return False
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename="playlist.m3u")
        if not result:
            return False
        path = result if isinstance(result, str) else result[0]
        try:
            self._playlist.save_m3u(path)
            self._set_status(f"Saved {os.path.basename(path)}")
            return True
        except OSError as exc:
            self._set_status(f"Could not save: {exc}")
            return False

    def _do_remove(self, ids) -> None:
        ids = {int(i) for i in ids or []}
        if not ids:
            return
        if self._current_id in ids:
            self._do_stop()
        self._playlist.remove_ids(ids)
        self._rebuild_bag()
        self._bump()
        self._set_status(f"Removed {len(ids)} track{'s' if len(ids) != 1 else ''}")

    def _do_reorder(self, dragged_id: int, target_id: int) -> None:
        self._playlist.move(int(dragged_id), int(target_id))
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

    def set_volume(self, value: float) -> None:
        self._engine.set_volume(float(value))

    def toggle_shuffle(self) -> None:
        self._shuffle = not self._shuffle
        self._rebuild_bag()
        self._bump()

    def cycle_repeat(self) -> None:
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
        ids = [i for i in self._playlist.ids if i != self._current_id]
        random.shuffle(ids)
        self._shuffle_bag = ids

    # ---- bridge: enqueue and return immediately -------------------------
    # Each of these can touch a file on a network share, so none of them may
    # run on the call from JavaScript. The frontend already updates itself
    # optimistically, so the round trip is invisible.

    def play_id(self, track_id: int, start: float = 0.0) -> bool:
        self._post("play_id", int(track_id), float(start))
        return True

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
        attribute on it was always False -- and an empty dict is falsy, so it
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

    # ---- session -------------------------------------------------------

    def _restore_session(self) -> None:
        # Deliberately no os.path.isfile() here. On a network share that is
        # one round trip per saved path before the window has even drawn.
        # Missing files surface when something tries to play them, or via
        # Tools > Remove missing.
        paths = self._settings.get("playlist", [])
        if paths:
            self._playlist.add_paths(paths)
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
                    break
        self._bump()

    def _save_session(self) -> None:
        track = self._playlist.by_id(self._current_id)
        self._settings.update({
            "volume": self._engine.volume,
            "shuffle": self._shuffle,
            "repeat": self._repeat,
            "playlist": [t.path for t in self._playlist.tracks],
            "last_path": track.path if track else "",
            "last_position": self._engine.position if self._engine.active else 0.0,
        })
        settings_store.save(self._settings)

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._worker.is_alive():
            self._worker.join(timeout=0.8)
        self._save_session()
        self._scanner.shutdown()
        self._engine.stop()


    # ---- entry points used by the host, kept off the JS bridge ----------
    # Every public attribute of this object is walked by pywebview when it
    # builds window.pywebview.api, and it recurses into non-callables. A
    # public reference to the Window made it descend into window.dom.document,
    # which blocks until the page has loaded -- so the API object was never
    # created and every call from JavaScript failed.

    #: Everything JavaScript is allowed to call. Anything public and not in
    #: this set is a mistake -- see _assert_bridge_surface.
    BRIDGE = frozenset({
        "get_tick", "get_full", "get_meta", "get_peaks",
        "request_scan", "request_ahead", "request_prefetch",
        "drop_prefetch", "reset_scan_queue",
        "add_files", "add_folder", "load_m3u", "save_m3u",
        "remove", "reorder", "open_paths",
        "play_id", "toggle_play", "stop", "next_track", "previous",
        "seek", "nudge", "set_volume", "toggle_shuffle", "cycle_repeat",
        "win_minimise", "win_maximise", "win_close",
        # host-side entry points, not called from JS but necessarily public
        "attach", "boot", "ingest", "close", "set_maximized", "BRIDGE",
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
        extra = {n for n in dir(self) if not n.startswith("_")} - self.BRIDGE
        if extra:
            raise RuntimeError(
                "Api exposes unexpected public attributes to pywebview: "
                + ", ".join(sorted(extra))
                + ". Prefix them with an underscore, or add them to "
                  "Api.BRIDGE if JavaScript is meant to call them.")

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
        self._restore_session()

    def ingest(self, paths) -> int:
        return self._add_paths(paths)

    def close(self) -> None:
        self._shutdown()

    def open_paths(self, paths) -> int:
        """Add these files and return the id of the FIRST one.

        Returns the right id whether the file was just added or was already in
        the playlist, so double-clicking a track in Explorer plays that track
        rather than whatever happens to sit at position one.
        """
        paths = [p for p in (paths or []) if p]
        if not paths:
            return -1
        self._add_paths(paths)
        with self._lock:
            index = self._playlist.index_of_path(paths[0])
            return self._playlist.id_at(index) if index >= 0 else -1
