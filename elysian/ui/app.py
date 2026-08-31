"""Main window."""
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import dearpygui.dearpygui as dpg

from .. import config
from ..models.playlist import Playlist
from ..models.track import format_time
from ..playback.engine import PlaybackEngine, PlaybackError
from ..services import settings as settings_store
from ..services.art import ArtProvider
from ..services.discovery import DiscoveryProvider
from ..services.lyrics import LyricsProvider
from ..services.scanner import MetadataScanner, apply_metadata
from . import theme as theming
from .playlist_view import PlaylistView

ART_TEXTURE = "album_art_texture"
REPEAT_LABELS = {"none": "Repeat off", "all": "Repeat all", "one": "Repeat one"}
REPEAT_CYCLE = {"none": "all", "all": "one", "one": "none"}


class ElysianApp:
    def __init__(self):
        self.playlist = Playlist()
        self.engine = PlaybackEngine()
        self.art = ArtProvider()
        self.lyrics = LyricsProvider()
        self.discovery = DiscoveryProvider(config.AUDIO_EXTENSIONS)
        self.scanner = MetadataScanner()

        self.settings = settings_store.load()
        self.current_id = -1
        self.shuffle = bool(self.settings["shuffle"])
        self.repeat = self.settings["repeat"]
        self.discovery_on = bool(self.settings["discovery"])
        self.lyrics_on = bool(self.settings["lyrics"])

        self.shuffle_bag: list[int] = []
        self.history: list[int] = []
        self.seeking = False
        self.sleep_deadline: float | None = None
        self._status_until = 0.0
        self._last_lyric = ""
        self._discovery_busy = False
        self._pending_discovery: str | None = None
        self._scan_dirty = False

        self.view = PlaylistView(
            self.playlist,
            on_activate=self.play_id,
            on_reorder=self._reorder,
            on_context=lambda _: None,
        )

    # ---- lifecycle ----------------------------------------------------

    def run(self) -> None:
        dpg.create_context()
        self._build_textures()
        dpg.create_viewport(
            title=config.APP_NAME,
            width=config.WINDOW_WIDTH,
            height=config.WINDOW_HEIGHT,
            min_width=config.MIN_WIDTH,
            min_height=config.MIN_HEIGHT,
        )
        icon = config.resource_path("icon.ico")
        if os.path.isfile(icon):
            try:
                dpg.set_viewport_small_icon(icon)
                dpg.set_viewport_large_icon(icon)
            except Exception:
                pass

        self._build_ui()
        dpg.bind_theme(theming.build_theme())
        self._bind_keys()

        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main_window", True)

        self.scanner.start()
        self._restore_session()

        if not self.engine.available:
            self._status(f"No audio device: {self.engine.error}")

        while dpg.is_dearpygui_running():
            self._tick()
            dpg.render_dearpygui_frame()

        self._save_session()
        self.scanner.shutdown()
        self.engine.stop()
        dpg.destroy_context()

    def _build_textures(self) -> None:
        size = config.ART_SIZE
        blank = [0.13, 0.13, 0.16, 1.0] * (size * size)
        with dpg.texture_registry():
            dpg.add_dynamic_texture(size, size, blank, tag=ART_TEXTURE)

    # ---- layout -------------------------------------------------------

    def _build_ui(self) -> None:
        accent = theming.build_accent_button_theme()
        dim = theming.build_dim_text_theme()
        seek_theme = theming.build_seek_theme()
        self.toggle_theme = theming.build_toggle_on_theme()

        with dpg.window(tag="main_window"):
            self._build_menu()
            with dpg.group(horizontal=True):
                with dpg.child_window(width=config.ART_SIZE + 32, border=False):
                    dpg.add_image(ART_TEXTURE, tag="art_image",
                                  width=config.ART_SIZE, height=config.ART_SIZE)
                    dpg.add_spacer(height=6)
                    dpg.add_text("Nothing playing", tag="now_title", wrap=config.ART_SIZE)
                    dpg.add_text("", tag="now_artist", wrap=config.ART_SIZE)
                    dpg.bind_item_theme("now_artist", dim)
                    dpg.add_spacer(height=8)

                    dpg.add_slider_float(tag="seek_slider", width=config.ART_SIZE,
                                         default_value=0.0, max_value=1.0,
                                         format="", callback=self._on_seek_drag)
                    dpg.bind_item_theme("seek_slider", seek_theme)
                    with dpg.group(horizontal=True):
                        dpg.add_text("0:00", tag="time_now")
                        dpg.add_spacer(width=config.ART_SIZE - 96)
                        dpg.add_text("0:00", tag="time_total")
                    dpg.bind_item_theme("time_now", dim)
                    dpg.bind_item_theme("time_total", dim)

                    dpg.add_spacer(height=8)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="<<", width=52, callback=self.previous)
                        dpg.add_button(label="Play", tag="play_button", width=76,
                                       callback=self.toggle_play)
                        dpg.bind_item_theme("play_button", accent)
                        dpg.add_button(label=">>", width=52, callback=self.next_track)

                    dpg.add_spacer(height=6)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Shuffle", tag="shuffle_button",
                                       width=76, callback=self.toggle_shuffle)
                        dpg.add_button(label="Repeat off", tag="repeat_button",
                                       width=98, callback=self.cycle_repeat)
                    dpg.add_button(label="Discover off", tag="discover_button",
                                   width=config.ART_SIZE, callback=self.toggle_discovery)

                    dpg.add_spacer(height=8)
                    dpg.add_text("Volume", tag="volume_label")
                    dpg.bind_item_theme("volume_label", dim)
                    dpg.add_slider_float(tag="volume_slider", width=config.ART_SIZE,
                                         default_value=self.settings["volume"],
                                         max_value=1.0, format="%.0f%%",
                                         callback=self._on_volume)

                with dpg.child_window(border=False, tag="playlist_pane"):
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Add files", callback=self.add_files)
                        dpg.add_button(label="Add folder", callback=self.add_folder)
                        dpg.add_button(label="Load M3U", callback=self.load_m3u)
                        dpg.add_button(label="Save M3U", callback=self.save_m3u)
                        dpg.add_input_text(tag="filter_box", hint="Filter",
                                           width=-1, callback=self._on_filter)
                    dpg.add_spacer(height=4)
                    dpg.add_text("", tag="lyric_line", wrap=640)
                    dpg.add_spacer(height=2)
                    self.view.build("playlist_pane")
            dpg.add_text("", tag="status_bar")
            dpg.bind_item_theme("status_bar", dim)

        self._sync_toggles()

    def _build_menu(self) -> None:
        with dpg.menu_bar():
            with dpg.menu(label="File"):
                dpg.add_menu_item(label="Add files...", callback=self.add_files)
                dpg.add_menu_item(label="Add folder...", callback=self.add_folder)
                dpg.add_separator()
                dpg.add_menu_item(label="Load M3U...", callback=self.load_m3u)
                dpg.add_menu_item(label="Save M3U...", callback=self.save_m3u)
                dpg.add_separator()
                dpg.add_menu_item(label="Exit", callback=lambda: dpg.stop_dearpygui())
            with dpg.menu(label="Playback"):
                dpg.add_menu_item(label="Play / pause", callback=self.toggle_play)
                dpg.add_menu_item(label="Stop", callback=self.stop)
                dpg.add_separator()
                dpg.add_menu_item(label="Show lyrics", check=True,
                                  tag="menu_lyrics", default_value=self.lyrics_on,
                                  callback=self.toggle_lyrics)
                dpg.add_menu_item(label="Discovery mode", check=True,
                                  tag="menu_discovery",
                                  default_value=self.discovery_on,
                                  callback=self.toggle_discovery)
                dpg.add_separator()
                with dpg.menu(label="Sleep timer"):
                    for minutes in (15, 30, 45, 60, 90):
                        dpg.add_menu_item(
                            label=f"{minutes} minutes",
                            user_data=minutes,
                            callback=lambda s, a, u: self.set_sleep_timer(u))
                    dpg.add_menu_item(label="Cancel",
                                      callback=lambda: self.set_sleep_timer(0))
            with dpg.menu(label="Tools"):
                dpg.add_menu_item(label="Remove missing files",
                                  callback=self.remove_missing)
                dpg.add_menu_item(label="Remove selected",
                                  callback=self.remove_selected)
                dpg.add_separator()
                dpg.add_menu_item(label="Open file location",
                                  callback=self.open_location)
                dpg.add_separator()
                dpg.add_menu_item(label="Shortcuts", callback=self._show_shortcuts)

    # ---- keyboard ------------------------------------------------------

    def _bind_keys(self) -> None:
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_Spacebar, callback=self._key_space)
            dpg.add_key_press_handler(dpg.mvKey_Left, callback=self._key_left)
            dpg.add_key_press_handler(dpg.mvKey_Right, callback=self._key_right)
            dpg.add_key_press_handler(dpg.mvKey_Up, callback=self._key_up)
            dpg.add_key_press_handler(dpg.mvKey_Down, callback=self._key_down)
            dpg.add_key_press_handler(dpg.mvKey_Return, callback=self._key_return)
            dpg.add_key_press_handler(dpg.mvKey_Delete, callback=self._key_delete)
            dpg.add_key_press_handler(dpg.mvKey_Slash, callback=self._key_slash)

    def _typing(self) -> bool:
        """True while the filter box has focus.

        v1 bound its shortcuts on the root window, so a space typed into the
        search box toggled playback and the arrow keys seeked. Every shortcut
        below defers to this check.
        """
        return dpg.does_item_exist("filter_box") and dpg.is_item_focused("filter_box")

    def _key_space(self, *_):
        if not self._typing():
            self.toggle_play()

    def _key_left(self, *_):
        if self._typing():
            return
        if dpg.is_key_down(dpg.mvKey_ModCtrl):
            self.previous()
        else:
            self.engine.nudge(-5)

    def _key_right(self, *_):
        if self._typing():
            return
        if dpg.is_key_down(dpg.mvKey_ModCtrl):
            self.next_track()
        else:
            self.engine.nudge(5)

    def _key_up(self, *_):
        if not self._typing():
            self._step_volume(0.05)

    def _key_down(self, *_):
        if not self._typing():
            self._step_volume(-0.05)

    def _key_return(self, *_):
        if not self._typing():
            self.view.activate_selected()

    def _key_delete(self, *_):
        if not self._typing():
            self.remove_selected()

    def _key_slash(self, *_):
        if not self._typing():
            dpg.focus_item("filter_box")

    # ---- file operations ------------------------------------------------

    def add_files(self, *_):
        paths = _ask_open_files()
        if paths:
            self._ingest(paths)

    def add_folder(self, *_):
        folder = _ask_folder()
        if not folder:
            return
        found = [str(p) for p in sorted(Path(folder).rglob("*"))
                 if p.suffix.lower() in config.AUDIO_EXTENSIONS]
        if not found:
            self._status("No audio files in that folder")
            return
        self._ingest(found)

    def _ingest(self, paths) -> None:
        added = self.playlist.add_paths(paths)
        if not added:
            self._status("Already in the playlist")
            return
        pairs = []
        for track in added:
            position = self.playlist.tracks.index(track)
            pairs.append((self.playlist.id_at(position), track.path))
        self.scanner.submit_many(pairs)
        self._rebuild_bag()
        self.view.refresh()
        self._status(f"Added {len(added)} track(s), reading tags...")

    def load_m3u(self, *_):
        path = _ask_open_files(m3u=True)
        if not path:
            return
        added = self.playlist.load_m3u(path[0], config.AUDIO_EXTENSIONS)
        if added:
            pairs = [(self.playlist.id_at(self.playlist.tracks.index(t)), t.path)
                     for t in added]
            self.scanner.submit_many(pairs)
            self._rebuild_bag()
            self.view.refresh()
        self._status(f"Loaded {len(added)} track(s)")

    def save_m3u(self, *_):
        if not len(self.playlist):
            self._status("Playlist is empty")
            return
        path = _ask_save_file()
        if not path:
            return
        try:
            self.playlist.save_m3u(path)
            self._status(f"Saved {os.path.basename(path)}")
        except OSError as exc:
            self._status(f"Could not save: {exc}")

    def remove_selected(self, *_):
        ids = set(self.view.selected)
        if not ids:
            return
        if self.current_id in ids:
            self.stop()
        self.playlist.remove_ids(ids)
        self.view.selected.clear()
        self._rebuild_bag()
        self.view.refresh()
        self._status(f"Removed {len(ids)} track(s)")

    def remove_missing(self, *_):
        removed = self.playlist.remove_missing()
        if removed:
            self._rebuild_bag()
            self.view.refresh()
        self._status(f"Removed {removed} missing file(s)" if removed
                     else "No missing files")

    def open_location(self, *_):
        track_id = next(iter(self.view.selected), self.current_id)
        track = self.playlist.by_id(track_id)
        if not track:
            return
        folder = str(Path(track.path).parent)
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            self._status(f"Could not open folder: {exc}")

    # ---- playback -------------------------------------------------------

    def play_id(self, track_id: int, start: float = 0.0) -> None:
        track = self.playlist.by_id(track_id)
        if track is None:
            return
        try:
            self.engine.play(track.path, start)
        except PlaybackError as exc:
            self._status(f"Cannot play {Path(track.path).name}: {exc}")
            return
        self.current_id = track_id
        if not self.history or self.history[-1] != track_id:
            self.history.append(track_id)
            del self.history[:-200]
        if not track.scanned:
            track.length = self.engine.duration
        self.view.set_current(track_id)
        self._update_now_playing(track)
        self._last_lyric = ""

    def toggle_play(self, *_):
        if not len(self.playlist):
            self._status("Add some tracks first")
            return
        if self.engine.active:
            self.engine.toggle()
        else:
            target = self.current_id if self.current_id >= 0 else self.playlist.id_at(0)
            self.play_id(target)
        self._sync_play_button()

    def stop(self, *_):
        self.engine.stop()
        dpg.set_value("seek_slider", 0.0)
        dpg.set_value("time_now", "0:00")
        dpg.set_value("lyric_line", "")
        self._sync_play_button()

    def next_track(self, *_, auto: bool = False):
        if not len(self.playlist):
            return
        if auto and self.repeat == "one":
            self.play_id(self.current_id)
            return
        nxt = self._next_id(auto)
        if nxt is None:
            if auto and self.discovery_on:
                self._start_discovery()
            else:
                self.stop()
            return
        self.play_id(nxt)

    def previous(self, *_):
        if self.engine.position > 3.0:
            self.engine.seek(0.0)
            return
        if self.shuffle and len(self.history) >= 2:
            self.history.pop()
            self.play_id(self.history[-1])
            return
        position = self.playlist.index_of(self.current_id)
        if position > 0:
            self.play_id(self.playlist.id_at(position - 1))
        elif self.repeat == "all" and len(self.playlist):
            self.play_id(self.playlist.id_at(len(self.playlist) - 1))
        else:
            self.engine.seek(0.0)

    def _next_id(self, auto: bool) -> int | None:
        if self.shuffle:
            if not self.shuffle_bag:
                if self.repeat == "all" or not auto:
                    self._rebuild_bag()
                else:
                    return None
            while self.shuffle_bag:
                candidate = self.shuffle_bag.pop()
                if self.playlist.index_of(candidate) >= 0:
                    return candidate
            return None
        position = self.playlist.index_of(self.current_id)
        if position + 1 < len(self.playlist):
            return self.playlist.id_at(position + 1)
        if self.repeat == "all" and len(self.playlist):
            return self.playlist.id_at(0)
        return None

    def _rebuild_bag(self) -> None:
        ids = [i for i in self.playlist.ids if i != self.current_id]
        random.shuffle(ids)
        self.shuffle_bag = ids

    def _reorder(self, dragged_id: int, target_id: int) -> None:
        self.playlist.move(dragged_id, target_id)
        self.view.refresh()

    # ---- discovery ------------------------------------------------------

    def _start_discovery(self) -> None:
        if self._discovery_busy or not len(self.playlist):
            self.stop()
            return
        last = self.playlist.at(len(self.playlist) - 1)
        if last is None:
            self.stop()
            return
        seed = last.path
        exclude = [t.path for t in self.playlist.tracks]
        self._discovery_busy = True
        self._status("Looking for something new...")

        def work():
            try:
                found = self.discovery.suggest(seed, exclude)
            except Exception:
                found = None
            self._pending_discovery = found or ""
            self._discovery_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _collect_discovery(self) -> None:
        if self._pending_discovery is None:
            return
        found, self._pending_discovery = self._pending_discovery, None
        if not found:
            self._status("Nothing new found nearby")
            self.stop()
            return
        added = self.playlist.add_paths([found])
        if not added:
            self.stop()
            return
        position = self.playlist.tracks.index(added[0])
        track_id = self.playlist.id_at(position)
        self.scanner.submit(track_id, added[0].path)
        self.view.refresh()
        self.play_id(track_id)

    # ---- toggles ---------------------------------------------------------

    def toggle_shuffle(self, *_):
        self.shuffle = not self.shuffle
        self._rebuild_bag()
        self._sync_toggles()

    def cycle_repeat(self, *_):
        self.repeat = REPEAT_CYCLE[self.repeat]
        self._sync_toggles()

    def toggle_discovery(self, *_):
        self.discovery_on = not self.discovery_on
        self._sync_toggles()

    def toggle_lyrics(self, *_):
        self.lyrics_on = not self.lyrics_on
        if not self.lyrics_on:
            dpg.set_value("lyric_line", "")
        self._sync_toggles()

    def set_sleep_timer(self, minutes: int) -> None:
        if minutes:
            self.sleep_deadline = time.monotonic() + minutes * 60
            self._status(f"Sleep timer set for {minutes} minutes")
        else:
            self.sleep_deadline = None
            self._status("Sleep timer cancelled")

    def _sync_toggles(self) -> None:
        """Menu check state and button labels both read from the same fields,
        so they cannot drift apart the way v1's variable-less checkbuttons did."""
        dpg.configure_item("shuffle_button",
                           label="Shuffle on" if self.shuffle else "Shuffle")
        dpg.configure_item("repeat_button", label=REPEAT_LABELS[self.repeat])
        dpg.configure_item("discover_button",
                           label="Discover on" if self.discovery_on else "Discover off")
        for tag, state in (("menu_lyrics", self.lyrics_on),
                           ("menu_discovery", self.discovery_on)):
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, state)
        for tag, state in (("shuffle_button", self.shuffle),
                           ("discover_button", self.discovery_on)):
            if state:
                dpg.bind_item_theme(tag, self.toggle_theme)
            else:
                dpg.bind_item_theme(tag, 0)

    def _sync_play_button(self) -> None:
        playing = self.engine.playing
        dpg.configure_item("play_button", label="Pause" if playing else "Play")

    # ---- widget callbacks -------------------------------------------------

    def _on_seek_drag(self, sender, value):
        duration = self.engine.duration
        if duration > 0:
            dpg.set_value("time_now", format_time(value * duration))
        self.seeking = True

    def _on_volume(self, sender, value):
        self.engine.set_volume(value)

    def _on_filter(self, sender, value):
        self.view.set_filter(value)

    # ---- frame loop --------------------------------------------------------

    def _tick(self) -> None:
        applied = 0
        for track_id, info in self.scanner.drain():
            track = self.playlist.by_id(track_id)
            if track:
                apply_metadata(track, info)
                applied += 1
        if applied:
            self._scan_dirty = True
        if self._scan_dirty and self.scanner.pending == 0:
            self._scan_dirty = False
            self.view.refresh()
            if not self._status_until:
                dpg.set_value("status_bar", self._footer_text())

        if self.seeking and not dpg.is_item_active("seek_slider"):
            duration = self.engine.duration
            if duration > 0:
                self.engine.seek(dpg.get_value("seek_slider") * duration)
            self.seeking = False

        if self.engine.active and not self.seeking:
            duration = self.engine.duration
            position = self.engine.position
            if duration > 0:
                dpg.set_value("seek_slider", min(1.0, position / duration))
                dpg.set_value("time_total", format_time(duration))
            dpg.set_value("time_now", format_time(position))
            if self.lyrics_on:
                track = self.playlist.by_id(self.current_id)
                if track:
                    line = self.lyrics.line_at(track.path, position)
                    if line != self._last_lyric:
                        self._last_lyric = line
                        dpg.set_value("lyric_line", line)
        elif self.engine.finished():
            self.engine.stop()
            self.next_track(auto=True)

        self._collect_discovery()

        if self.sleep_deadline and time.monotonic() >= self.sleep_deadline:
            self.sleep_deadline = None
            self.stop()
            self._status("Sleep timer elapsed")

        if self._status_until and time.monotonic() > self._status_until:
            dpg.set_value("status_bar", self._footer_text())
            self._status_until = 0.0

        self._sync_play_button()

    def _footer_text(self) -> str:
        total = len(self.playlist)
        pending = self.scanner.pending
        parts = [f"{total} track(s)", format_time(self.playlist.total_length)]
        if pending:
            parts.append(f"reading tags: {pending} left")
        return "   |   ".join(parts)

    def _status(self, text: str) -> None:
        dpg.set_value("status_bar", text)
        self._status_until = time.monotonic() + 3.0

    def _update_now_playing(self, track) -> None:
        dpg.set_value("now_title", track.title)
        dpg.set_value("now_artist", track.artist or "Unknown artist")
        dpg.set_value("time_total", format_time(self.engine.duration))
        texture = self.art.get(track.path)
        size = config.ART_SIZE
        if texture:
            dpg.set_value(ART_TEXTURE, texture[2])
        else:
            dpg.set_value(ART_TEXTURE, [0.13, 0.13, 0.16, 1.0] * (size * size))

    def _show_shortcuts(self, *_):
        lines = [
            "Space          Play or pause",
            "Ctrl + Left    Previous track",
            "Ctrl + Right   Next track",
            "Left / Right   Seek 5 seconds",
            "Up / Down      Volume",
            "Enter          Play selected",
            "Delete         Remove selected",
            "/              Jump to filter",
            "Ctrl + click   Add to selection",
            "Drag a row     Reorder",
        ]
        if dpg.does_item_exist("shortcuts_window"):
            dpg.delete_item("shortcuts_window")
        with dpg.window(label="Shortcuts", tag="shortcuts_window",
                        width=340, height=300, pos=(200, 120)):
            for line in lines:
                dpg.add_text(line)

    # ---- session ----------------------------------------------------------

    def _restore_session(self) -> None:
        paths = [p for p in self.settings.get("playlist", []) if os.path.isfile(p)]
        if paths:
            added = self.playlist.add_paths(paths)
            pairs = [(self.playlist.id_at(i), t.path)
                     for i, t in enumerate(self.playlist.tracks)]
            self.scanner.submit_many(pairs)
            self.view.refresh()
            self._rebuild_bag()
        self.engine.set_volume(self.settings.get("volume", 0.8))
        dpg.set_value("volume_slider", self.engine.volume)

        last = self.settings.get("last_path", "")
        if last and os.path.isfile(last):
            for i, track in enumerate(self.playlist.tracks):
                if os.path.normcase(track.path) == os.path.normcase(last):
                    self.current_id = self.playlist.id_at(i)
                    self.view.set_current(self.current_id)
                    self._update_now_playing(track)
                    break
        dpg.set_value("status_bar", self._footer_text())

    def _save_session(self) -> None:
        track = self.playlist.by_id(self.current_id)
        self.settings.update({
            "volume": self.engine.volume,
            "shuffle": self.shuffle,
            "repeat": self.repeat,
            "discovery": self.discovery_on,
            "lyrics": self.lyrics_on,
            "playlist": [t.path for t in self.playlist.tracks],
            "last_path": track.path if track else "",
            "last_position": self.engine.position if self.engine.active else 0.0,
        })
        settings_store.save(self.settings)

    def _step_volume(self, delta: float) -> None:
        self.engine.set_volume(self.engine.volume + delta)
        dpg.set_value("volume_slider", self.engine.volume)


# ---- native file dialogs ---------------------------------------------------
# DearPyGui's built-in file dialog is weak; tkinter's are the OS-native ones
# and tkinter ships with CPython, so it costs nothing to bundle.

def _tk_root():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def _ask_open_files(m3u: bool = False):
    try:
        from tkinter import filedialog

        root = _tk_root()
        if m3u:
            result = filedialog.askopenfilename(
                title="Open playlist",
                filetypes=[("M3U playlist", "*.m3u *.m3u8"), ("All files", "*.*")])
            result = [result] if result else []
        else:
            result = list(filedialog.askopenfilenames(
                title="Add audio files",
                filetypes=[("Audio", "*.mp3 *.flac *.wav *.ogg"),
                           ("All files", "*.*")]))
        root.destroy()
        return result
    except Exception:
        return []


def _ask_folder():
    try:
        from tkinter import filedialog

        root = _tk_root()
        result = filedialog.askdirectory(title="Add a folder")
        root.destroy()
        return result
    except Exception:
        return ""


def _ask_save_file():
    try:
        from tkinter import filedialog

        root = _tk_root()
        result = filedialog.asksaveasfilename(
            title="Save playlist", defaultextension=".m3u",
            filetypes=[("M3U playlist", "*.m3u"), ("All files", "*.*")])
        root.destroy()
        return result
    except Exception:
        return ""
