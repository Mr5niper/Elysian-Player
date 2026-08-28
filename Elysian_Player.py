import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import pygame
from pathlib import Path
from dataclasses import dataclass
import os
import sys
import time
import json
import random
from io import BytesIO
import platform
import subprocess
import webbrowser

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1
from PIL import Image, ImageTk

APP_NAME = "Elysian Player"
SETTINGS_FILE = str(Path.home() / ".elysian_player_final.json")

# Theme
BG = "#1f1f1f"
FG = "#f0f0f0"
HL = "#2b2b2b"
ACCENT = "#2f6fed"
PLAYING_BG = "#004e56"
ROW_BG = "#151515"


@dataclass
class Track:
    path: str
    title: str
    artist: str
    length: float  # seconds

    @property
    def display(self):
        return f"{self.title}" if not self.artist else f"{self.title} - {self.artist}"


class PlaylistModel:
    def __init__(self):
        self.tracks: list[Track] = []

    def has_path(self, path: str) -> bool:
        p = os.path.normcase(path)
        return any(os.path.normcase(t.path) == p for t in self.tracks)

    def add_paths(self, paths: list[str]) -> int:
        added = 0
        for p in paths:
            if not self.has_path(p):
                self.tracks.append(self._extract_track(p))
                added += 1
        return added

    def remove_indices(self, indices: list[int]):
        for i in sorted(set(indices), reverse=True):
            if 0 <= i < len(self.tracks):
                del self.tracks[i]

    def move_block(self, selected_indices: list[int], direction: int):
        if not selected_indices or direction not in (-1, 1):
            return
        sel = sorted(set(selected_indices))
        if direction < 0:
            if sel[0] == 0:
                return
            insert_at = sel[0] - 1
            block = [self.tracks[i] for i in sel]
            for i in reversed(sel):
                del self.tracks[i]
            for off, t in enumerate(block):
                self.tracks.insert(insert_at + off, t)
        else:
            if sel[-1] == len(self.tracks) - 1:
                return
            insert_at = sel[-1] + 1
            block = [self.tracks[i] for i in sel]
            for i in reversed(sel):
                del self.tracks[i]
            for t in block:
                self.tracks.insert(insert_at, t)
                insert_at += 1

    def save_m3u(self, path: str):
        base = Path(path).parent
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for t in self.tracks:
                f.write(f"#EXTINF:{int(t.length)},{t.display}\n")
                p = Path(t.path)
                try:
                    rel = p.relative_to(base)
                    f.write(str(rel).replace("\\", "/") + "\n")
                except Exception:
                    f.write(str(p) + "\n")

    def load_m3u(self, path: str) -> int:
        base = Path(path).parent
        to_add = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                p = Path(s)
                if not p.is_file():
                    p2 = (base / s).resolve()
                    if p2.is_file():
                        p = p2
                if p.is_file() and p.suffix.lower() == ".mp3":
                    to_add.append(str(p))
        return self.add_paths(to_add)

    def _extract_track(self, path: str) -> Track:
        title = None
        artist = None
        length = 0.0
        try:
            audio = MP3(path)
            length = float(audio.info.length) if audio and audio.info and audio.info.length else 0.0
            try:
                tags = ID3(path)
                if tags:
                    t = tags.get("TIT2")
                    a = tags.get("TPE1")
                    if isinstance(t, TIT2) and t.text:
                        title = str(t.text[0])
                    if isinstance(a, TPE1) and a.text:
                        artist = str(a.text[0])
            except Exception:
                pass
        except Exception:
            pass
        if not title:
            title = Path(path).stem
        if not artist:
            artist = ""
        return Track(path=path, title=title, artist=artist, length=length)


class ArtProvider:
    def __init__(self):
        self.cache: dict[str, ImageTk.PhotoImage | None] = {}

    def get(self, path: str, size=(260, 260)) -> ImageTk.PhotoImage | None:
        if path in self.cache:
            return self.cache[path]
        img_tk = None
        try:
            tags = ID3(path)
            for k in tags.keys():
                frame = tags.get(k)
                if isinstance(frame, APIC) and frame.data:
                    img = Image.open(BytesIO(frame.data))
                    img.thumbnail(size, Image.LANCZOS)
                    img_tk = ImageTk.PhotoImage(img)
                    break
        except Exception:
            img_tk = None
        if img_tk is None:
            folder = Path(path).parent
            for name in ["cover.jpg", "folder.jpg", "front.jpg", "album.jpg", "cover.png", "folder.png"]:
                f = folder / name
                if f.exists():
                    try:
                        img = Image.open(f)
                        img.thumbnail(size, Image.LANCZOS)
                        img_tk = ImageTk.PhotoImage(img)
                        break
                    except Exception:
                        pass
        self.cache[path] = img_tk
        return img_tk


class LyricsProvider:
    def __init__(self):
        self.cache: dict[str, list[tuple[int, str]]] = {}

    def parse(self, path: str) -> list[tuple[int, str]]:
        if path in self.cache:
            return self.cache[path]
        lrc = Path(path).with_suffix(".lrc")
        lines = []
        if not lrc.exists():
            self.cache[path] = lines
            return lines
        try:
            for raw in lrc.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = raw.strip()
                if not s:
                    continue
                while s.startswith("[") and "]" in s:
                    tag = s[1:s.index("]")]
                    rest = s[s.index("]") + 1:].strip()
                    try:
                        mm, sec = tag.split(":")
                        secs = int(mm) * 60 + int(float(sec))
                        lines.append((secs, rest))
                    except Exception:
                        pass
                    s = rest
            lines.sort(key=lambda x: x[0])
        except Exception:
            lines = []
        self.cache[path] = lines
        return lines

    def current_line(self, path: str, cur_sec: int) -> str:
        seq = self.parse(path)
        if not seq:
            return ""
        lo, hi, ans = 0, len(seq) - 1, -1
        while lo <= hi:
            m = (lo + hi) // 2
            if seq[m][0] <= cur_sec:
                ans = m
                lo = m + 1
            else:
                hi = m - 1
        return seq[ans][1] if ans >= 0 else ""


class DiscoveryProvider:
    def __init__(self):
        self.index_cache: dict[str, list[str]] = {}

    def suggest(self, base_path: str, exclude: set[str]) -> str | None:
        base = Path(base_path)
        dirs = []
        # Candidate dirs: same album dir, siblings under artist dir, parent and its siblings
        dirs.append(base.parent)
        artist_dir = base.parent.parent if base.parent.parent.exists() else None
        if artist_dir and artist_dir.is_dir():
            try:
                for d in artist_dir.iterdir():
                    if d.is_dir():
                        dirs.append(d)
            except Exception:
                pass
        parent = base.parent.parent
        if parent and parent.is_dir():
            dirs.append(parent)
            try:
                for d in parent.iterdir():
                    if d.is_dir():
                        dirs.append(d)
            except Exception:
                pass
        candidates = []
        for d in dirs:
            key = str(d.resolve())
            if key not in self.index_cache:
                try:
                    mp3s = [str(p) for p in d.rglob("*.mp3")]
                except Exception:
                    mp3s = []
                self.index_cache[key] = mp3s
            for p in self.index_cache[key]:
                if p not in exclude:
                    candidates.append(p)
        if not candidates:
            return None
        return random.choice(candidates)


class PlaybackEngine:
    def __init__(self):
        pygame.mixer.init()
        self._base_pos = 0.0
        self._t0 = None
        self.is_playing = False
        self.is_paused = False
        self.volume = 0.8
        pygame.mixer.music.set_volume(self.volume)

    def load_and_play(self, path: str, start_sec: float = 0.0, fade_ms: int = 180):
        pygame.mixer.music.load(path)
        self._base_pos = start_sec
        self._t0 = time.monotonic()
        self.is_playing = True
        self.is_paused = False
        pygame.mixer.music.set_volume(self.volume)
        pygame.mixer.music.play(fade_ms=fade_ms)
        if start_sec > 0:
            try:
                pygame.mixer.music.set_pos(start_sec)
            except Exception:
                pygame.mixer.music.play(start=start_sec)

    def pause(self):
        if not self.is_playing or self.is_paused:
            return
        self._base_pos = self.position()
        self.is_paused = True
        pygame.mixer.music.pause()

    def resume(self):
        if not self.is_playing or not self.is_paused:
            return
        self._t0 = time.monotonic()
        self.is_paused = False
        pygame.mixer.music.unpause()

    def stop(self, fade_ms: int = 150):
        try:
            pygame.mixer.music.fadeout(fade_ms)
        except Exception:
            pygame.mixer.music.stop()
        self.is_playing = False
        self.is_paused = False
        self._base_pos = 0.0
        self._t0 = None

    def seek(self, seconds: float):
        if not self.is_playing:
            return
        was_paused = self.is_paused
        self._base_pos = float(seconds)
        self._t0 = time.monotonic()
        try:
            pygame.mixer.music.play()
            try:
                pygame.mixer.music.set_pos(seconds)
            except Exception:
                pygame.mixer.music.play(start=seconds)
        except Exception:
            pygame.mixer.music.play()
        if was_paused:
            pygame.mixer.music.pause()
            self.is_paused = True

    def set_volume(self, vol: float):
        self.volume = max(0.0, min(1.0, vol))
        pygame.mixer.music.set_volume(self.volume)

    def busy(self) -> bool:
        return pygame.mixer.music.get_busy()

    def position(self) -> float:
        if not self.is_playing:
            return 0.0
        if self.is_paused or self._t0 is None:
            return self._base_pos
        return max(0.0, self._base_pos + (time.monotonic() - self._t0))


class ElysianApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._setup_style()

        # Services
        self.model = PlaylistModel()
        self.engine = PlaybackEngine()
        self.art = ArtProvider()
        self.lyrics = LyricsProvider()
        self.discovery = DiscoveryProvider()

        # State
        self.current_index = -1
        self.shuffle = False
        self.repeat = "none"  # none | one | all
        self.shuffle_bag: list[int] = []
        self.shuffle_pos = -1
        self.history: list[int] = []
        self.user_seeking = False
        self.lyrics_enabled = True
        self.lyrics_current = ""
        self.discovery_enabled = False
        self.resume_index = -1
        self.resume_position = 0.0
        self.sleep_deadline = None

        # Drag init (fix)
        self._dragging = False
        self._drag_start_iid = None
        self._drag_start_y = 0

        # UI
        self._build_ui()
        self._build_menus()
        self._build_context_menu()

        # Settings/session
        self._load_settings()
        self._restore_last_session_playlist()
        self._restore_last_marker()
        self._rebuild_tree(reset_bag=True)

        # Timer
        self.tick_ms = 200
        self.tick_job = self.root.after(self.tick_ms, self._ui_tick)

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ---------- Style/UI ----------

    def _setup_style(self):
        self.root.title(APP_NAME)
        self.root.geometry("980x620")
        self.root.minsize(840, 540)
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 22, "bold"))
        style.configure("Now.TLabel", background=BG, foreground="#00e676", font=("Segoe UI", 11))
        style.configure("Status.TLabel", background=BG, foreground="#a9a9a9", font=("Segoe UI", 9))
        style.configure("Lyrics.TLabel", background=BG, foreground="#dddd88", font=("Segoe UI", 10, "italic"))
        style.configure("TButton", background=HL, foreground=FG, font=("Segoe UI", 10, "bold"), borderwidth=0)
        style.map("TButton", background=[("active", ACCENT)], foreground=[("active", "#ffffff")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
        style.configure("Treeview",
                        background=ROW_BG,
                        fieldbackground=ROW_BG,
                        foreground=FG,
                        rowheight=26,
                        font=("Segoe UI", 10))
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=HL, foreground=FG, font=("Segoe UI", 10, "bold"), borderwidth=0)

    def _build_ui(self):
        # Header
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=12, pady=(8, 4))
        ttk.Label(header, text="🎵 Elysian Player", style="Title.TLabel").pack(side="left")
        self.status_label = ttk.Label(header, text="", style="Status.TLabel")
        self.status_label.pack(side="right")

        self.now_label = ttk.Label(self.root, text="No song playing", style="Now.TLabel", wraplength=940)
        self.now_label.pack(pady=(0, 4))

        self.lyrics_label = ttk.Label(self.root, text="", style="Lyrics.TLabel", wraplength=940, anchor="center", justify="center")
        self.lyrics_label.pack(pady=(0, 6))

        # Main panes
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        left = ttk.Frame(main)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ttk.Frame(main)
        right.pack(side="right", fill="both", expand=True)

        # Album art
        art_frame = ttk.Frame(left)
        art_frame.pack(pady=(4, 8))
        self.album_art = ttk.Label(art_frame)
        self.album_art.pack()
        self._set_album_art(None)

        # Progress/time
        prog = ttk.Frame(left)
        prog.pack(fill="x", pady=(2, 6))
        self.time_start = ttk.Label(prog, text="0:00")
        self.time_start.pack(side="left")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Scale(prog, from_=0, to=100, orient="horizontal", variable=self.progress_var, command=self._on_progress_drag)
        self.progress.bind("<ButtonPress-1>", self._on_progress_press)
        self.progress.bind("<ButtonRelease-1>", self._on_progress_release)
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.time_end = ttk.Label(prog, text="0:00")
        self.time_end.pack(side="right")

        # Controls
        controls = ttk.Frame(left)
        controls.pack(pady=(4, 6))
        ttk.Button(controls, text="⏮ Prev", command=self.previous).grid(row=0, column=0, padx=3, pady=2)
        self.play_btn = ttk.Button(controls, text="▶ Play", command=self.play_pause, style="Accent.TButton")
        self.play_btn.grid(row=0, column=1, padx=3, pady=2)
        ttk.Button(controls, text="⏹ Stop", command=self.stop).grid(row=0, column=2, padx=3, pady=2)
        ttk.Button(controls, text="Next ⏭", command=self.next).grid(row=0, column=3, padx=3, pady=2)

        # Modes + Volume
        modes = ttk.Frame(left)
        modes.pack(pady=(2, 6))
        self.shuffle_btn = ttk.Button(modes, text="🔀 Shuffle", command=self.toggle_shuffle)
        self.shuffle_btn.grid(row=0, column=0, padx=5)
        self.repeat_btn = ttk.Button(modes, text="🔁 Repeat: Off", command=self.cycle_repeat)
        self.repeat_btn.grid(row=0, column=1, padx=5)
        self.discovery_btn = ttk.Button(modes, text="✨ Discover: Off", command=self.toggle_discovery)
        self.discovery_btn.grid(row=0, column=2, padx=5)

        vol = ttk.Frame(left)
        vol.pack(pady=(2, 6))
        ttk.Label(vol, text="🔊 Volume: ").pack(side="left")
        self.volume_var = tk.DoubleVar(value=self.engine.volume * 100)
        self.volume = ttk.Scale(vol, from_=0, to=100, orient="horizontal", variable=self.volume_var, command=self._on_volume, length=180)
        self.volume.pack(side="left")
        self.volume_label = ttk.Label(vol, text=f"{int(self.volume_var.get())}%")
        self.volume_label.pack(side="left", padx=(6, 0))

        # Right: tools
        tools = ttk.Frame(right)
        tools.pack(fill="x", pady=(2, 4))
        ttk.Button(tools, text="+ Add Songs", command=self.add_songs).pack(side="left", padx=3)
        ttk.Button(tools, text="📁 Add Folder", command=lambda: self.add_folder(False)).pack(side="left", padx=3)
        ttk.Button(tools, text="💾 Save M3U", command=self.save_m3u).pack(side="left", padx=3)
        ttk.Button(tools, text="📂 Load M3U", command=self.load_m3u).pack(side="left", padx=3)
        ttk.Label(tools, text="Filter:").pack(side="right")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._rebuild_tree(reset_bag=False))
        self.filter_entry = ttk.Entry(tools, textvariable=self.filter_var, width=28)
        self.filter_entry.pack(side="right", padx=(6, 10))

        # Playlist
        tree_frame = ttk.Frame(right)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("num", "title", "artist", "duration"), show="headings", selectmode="extended")
        for col, text, width, anchor in [
            ("num", "#", 52, "center"),
            ("title", "Title", 420, "w"),
            ("artist", "Artist", 240, "w"),
            ("duration", "Duration", 100, "center"),
        ]:
            self.tree.heading(col, text=text, command=lambda c=col: self._sort_by_column(c))
            self.tree.column(col, width=width, anchor=anchor, stretch=(col != "duration"))
        self.tree.tag_configure("playing", background=PLAYING_BG, foreground="#ffffff")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Bindings
        self.tree.bind("<Double-Button-1>", self.play_selected)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        # Drag reorder
        self.tree.bind("<ButtonPress-1>", self._on_tree_press, add="+")
        self.tree.bind("<B1-Motion>", self._on_tree_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_release)

        # Keyboard
        self.root.bind("<space>", lambda e: self.play_pause())
        self.root.bind("<Control-Left>", lambda e: self.previous())
        self.root.bind("<Control-Right>", lambda e: self.next())
        self.root.bind("<Left>", lambda e: self.seek_relative(-5))
        self.root.bind("<Right>", lambda e: self.seek_relative(5))
        self.root.bind("<Up>", lambda e: self._step_volume(5))
        self.root.bind("<Down>", lambda e: self._step_volume(-5))
        self.root.bind("<Control-o>", lambda e: self.add_songs())
        self.root.bind("<Control-f>", lambda e: self.add_folder(False))
        self.root.bind("<Control-s>", lambda e: self.save_m3u())
        self.root.bind("<Control-l>", lambda e: self.load_m3u())
        self.root.bind("<Control-h>", lambda e: self.toggle_shuffle())
        self.root.bind("<Control-r>", lambda e: self.cycle_repeat())
        self.root.bind("/", lambda e: self._focus_filter())

        # Footer
        footer = ttk.Frame(self.root)
        footer.pack(fill="x", padx=12, pady=(0, 8))
        self.footer_label = ttk.Label(footer, text="", style="Status.TLabel")
        self.footer_label.pack(side="left")
        self._update_footer()

    def _build_menus(self):
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Add Songs... (Ctrl+O)", command=self.add_songs)
        file_menu.add_command(label="Add Folder... (Ctrl+F)", command=lambda: self.add_folder(False))
        file_menu.add_separator()
        file_menu.add_command(label="Load M3U... (Ctrl+L)", command=self.load_m3u)
        file_menu.add_command(label="Save M3U... (Ctrl+S)", command=self.save_m3u)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_closing)
        menubar.add_cascade(label="File", menu=file_menu)

        playback_menu = tk.Menu(menubar, tearoff=0)
        playback_menu.add_command(label="Play/Pause (Space)", command=self.play_pause)
        playback_menu.add_command(label="Stop", command=self.stop)
        playback_menu.add_separator()
        playback_menu.add_command(label="Previous (Ctrl+Left)", command=self.previous)
        playback_menu.add_command(label="Next (Ctrl+Right)", command=self.next)
        playback_menu.add_separator()
        playback_menu.add_checkbutton(label="Show Lyrics", onvalue=1, offvalue=0, command=self.toggle_lyrics)
        playback_menu.add_checkbutton(label="Discovery Mode", onvalue=1, offvalue=0, command=self.toggle_discovery)
        playback_menu.add_separator()
        playback_menu.add_command(label="Set Sleep Timer...", command=self.set_sleep_timer)
        menubar.add_cascade(label="Playback", menu=playback_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Toggle Mini Player", command=self.toggle_mini)
        menubar.add_cascade(label="View", menu=view_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Remove Missing Files", command=self.remove_missing_files)
        tools_menu.add_separator()
        tools_menu.add_command(label="Shortcuts", command=self._show_shortcuts)
        tools_menu.add_command(label="About", command=lambda: messagebox.showinfo("About", f"{APP_NAME}\nBuilt with tkinter + pygame + mutagen + Pillow"))
        menubar.add_cascade(label="Tools", menu=tools_menu)

        self.root.config(menu=menubar)

    def _build_context_menu(self):
        self.ctx = tk.Menu(self.root, tearoff=0)
        self.ctx.add_command(label="Play", command=self._ctx_play)
        self.ctx.add_command(label="Play Next", command=self.play_next_for_selected)
        self.ctx.add_separator()
        self.ctx.add_command(label="Open File Location", command=self._ctx_open_location)
        self.ctx.add_separator()
        self.ctx.add_command(label="Remove", command=self.remove_selected)
        self.tree.bind("<Button-3>", self._show_context_menu)

    # ---------- Actions ----------

    def add_songs(self):
        files = filedialog.askopenfilenames(title="Select MP3 Files",
                                            filetypes=[("MP3 Files", "*.mp3"), ("All Files", "*.*")])
        if not files:
            return
        added = self.model.add_paths([str(f) for f in files])
        if added:
            self._rebuild_tree(reset_bag=True)
            self._toast(f"Added {added} song(s)")

    def add_folder(self, recursive=False):
        folder = filedialog.askdirectory(title="Select Folder")
        if not folder:
            return
        gl = Path(folder).rglob("*.mp3") if recursive else Path(folder).glob("*.mp3")
        paths = [str(p) for p in gl]
        if not paths:
            self._toast("No MP3 files found")
            return
        added = self.model.add_paths(paths)
        if added:
            self._rebuild_tree(reset_bag=True)
            self._toast(f"Added {added} song(s)")

    def save_m3u(self):
        if not self.model.tracks:
            self._toast("No songs to save")
            return
        path = filedialog.asksaveasfilename(defaultextension=".m3u",
                                            filetypes=[("M3U Playlist", "*.m3u"), ("All Files", "*.*")])
        if not path:
            return
        try:
            self.model.save_m3u(path)
            self._toast(f"Saved: {path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save playlist:\n{e}")

    def load_m3u(self):
        path = filedialog.askopenfilename(title="Open M3U Playlist",
                                          filetypes=[("M3U Playlist", "*.m3u"), ("All Files", "*.*")])
        if not path:
            return
        try:
            added = self.model.load_m3u(path)
            if added:
                self._rebuild_tree(reset_bag=True)
                self._toast(f"Loaded {added} song(s)")
            else:
                self._toast("No new songs in M3U")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load playlist:\n{e}")

    def remove_selected(self):
        sel = [int(i) for i in self.tree.selection()]
        if not sel:
            return
        removing_current = (self.current_index in sel)
        self.model.remove_indices(sel)
        if removing_current:
            self.stop()
            self.current_index = -1
        else:
            dec = sum(1 for i in sel if i < self.current_index)
            self.current_index = max(-1, self.current_index - dec)
        self._rebuild_tree(reset_bag=True)
        self.history.clear()

    def remove_missing_files(self):
        removed = 0
        i = 0
        while i < len(self.model.tracks):
            if not os.path.isfile(self.model.tracks[i].path):
                if i == self.current_index:
                    self.stop()
                    self.current_index = -1
                elif self.current_index > i:
                    self.current_index -= 1
                del self.model.tracks[i]
                removed += 1
                continue
            i += 1
        if removed:
            self._rebuild_tree(reset_bag=True)
            self._toast(f"Removed {removed} missing file(s)")
        else:
            self._toast("No missing files")

    def play_selected(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.current_index = int(sorted(sel, key=lambda x: int(x))[0])
        self.play_current(add_to_history=True)

    def play_current(self, add_to_history: bool, resume_pos: float = 0.0):
        if not (0 <= self.current_index < len(self.model.tracks)):
            return
        t = self.model.tracks[self.current_index]
        try:
            self.engine.load_and_play(t.path, start_sec=resume_pos)
        except Exception as e:
            self._handle_play_error(t.path, e)
            return

        self.play_btn.config(text="⏸ Pause")
        self.now_label.config(text=f"Now Playing: {t.title}" + (f" - {t.artist}" if t.artist else ""))
        self.time_end.config(text=self._fmt_time(t.length))
        self._highlight_row()
        art = self.art.get(t.path)
        self._set_album_art(art)

        if add_to_history and (not self.history or self.history[-1] != self.current_index):
            self.history.append(self.current_index)
            if len(self.history) > 800:
                self.history = self.history[-400:]

    def play_pause(self):
        if not self.model.tracks:
            self._toast("Add songs to the playlist first")
            return
        if not self.engine.is_playing:
            if self.current_index < 0:
                self.current_index = 0
            if self.resume_index == self.current_index and self.resume_position > 0.0:
                self.play_current(add_to_history=True, resume_pos=self.resume_position)
                self.resume_position = 0.0
            else:
                self.play_current(add_to_history=True)
        elif self.engine.is_paused:
            self.engine.resume()
            self.play_btn.config(text="⏸ Pause")
        else:
            self.engine.pause()
            self.play_btn.config(text="▶ Play")

    def stop(self):
        self.engine.stop()
        self.play_btn.config(text="▶ Play")
        self.progress_var.set(0.0)
        self.time_start.config(text="0:00")
        self.time_end.config(text="0:00")
        self._set_album_art(None)
        self.now_label.config(text="No song playing")
        self._highlight_row(clear=True)
        self.lyrics_current = ""
        self.lyrics_label.config(text="")
        self._update_footer()

    def next(self, from_end: bool = False):
        if not self.model.tracks:
            return
        if self.repeat == "one" and self.engine.is_playing and not from_end:
            self.engine.seek(0.0)
            return
        if self.shuffle:
            self.shuffle_pos += 1
            if self.shuffle_pos >= len(self.shuffle_bag):
                if self.repeat == "all":
                    self._rebuild_shuffle_bag()
                    self.shuffle_pos += 1
                elif self.discovery_enabled and from_end:
                    self._discover_next()
                    return
                else:
                    self.stop()
                    return
            self.current_index = self.shuffle_bag[self.shuffle_pos]
        else:
            if self.current_index + 1 < len(self.model.tracks):
                self.current_index += 1
            else:
                if self.repeat == "all":
                    self.current_index = 0
                elif self.discovery_enabled and from_end:
                    self._discover_next()
                    return
                else:
                    self.stop()
                    return
        self.play_current(add_to_history=True)

    def previous(self):
        if not self.model.tracks:
            return
        if self.shuffle and len(self.history) >= 2:
            self.history.pop()
            self.current_index = self.history[-1]
            self.play_current(add_to_history=False)
            self._align_shuffle_cursor()
            return
        if self.current_index > 0:
            self.current_index -= 1
            self.play_current(add_to_history=True)
        else:
            if self.repeat == "all":
                self.current_index = len(self.model.tracks) - 1
                self.play_current(add_to_history=True)
            else:
                self.seek_to(0.0)

    def seek_relative(self, delta):
        if not self.engine.is_playing:
            return
        L = self._current_track_len()
        self.seek_to(max(0.0, min(L, self.engine.position() + delta)))

    def seek_to(self, seconds):
        if not self.engine.is_playing:
            return
        self.engine.seek(seconds)
        self.time_start.config(text=self._fmt_time(seconds))
        L = self._current_track_len()
        if L > 0:
            self.progress_var.set((seconds / L) * 100.0)

    def toggle_shuffle(self):
        self.shuffle = not self.shuffle
        self.shuffle_btn.config(text="🔀 Shuffle: On" if self.shuffle else "🔀 Shuffle")
        self._rebuild_shuffle_bag()
        self.history.clear()
        self._update_footer()

    def cycle_repeat(self):
        self.repeat = {"none": "all", "all": "one", "one": "none"}[self.repeat]
        self.repeat_btn.config(text={"none": "🔁 Repeat: Off", "all": "🔁 Repeat: All", "one": "🔂 Repeat: One"}[self.repeat])
        self._update_footer()

    def toggle_discovery(self):
        self.discovery_enabled = not self.discovery_enabled
        self.discovery_btn.config(text="✨ Discover: On" if self.discovery_enabled else "✨ Discover: Off")
        self._update_footer()

    # ---------- UI helpers ----------

    def _on_volume(self, _):
        v = float(self.volume_var.get()) / 100.0
        self.engine.set_volume(v)
        self.volume_label.config(text=f"{int(self.volume_var.get())}%")

    def _on_progress_press(self, _):
        self.user_seeking = True

    def _on_progress_drag(self, _):
        if not self.engine.is_playing:
            return
        L = self._current_track_len()
        if L > 0:
            cur = float(self.progress_var.get()) / 100.0 * L
            self.time_start.config(text=self._fmt_time(cur))

    def _on_progress_release(self, _):
        if not self.engine.is_playing:
            self.user_seeking = False
            return
        L = self._current_track_len()
        target = float(self.progress_var.get()) / 100.0 * L if L > 0 else 0.0
        self.seek_to(target)
        self.user_seeking = False

    def _sort_by_column(self, column):
        if not self.model.tracks:
            return
        key_map = {
            "num": lambda t, i: i,
            "title": lambda t, i: t.title.lower(),
            "artist": lambda t, i: t.artist.lower(),
            "duration": lambda t, i: t.length,
        }
        key_fn = key_map.get(column, key_map["num"])
        if getattr(self, "_sort_col", None) == column:
            self._sort_rev = not getattr(self, "_sort_rev", False)
        else:
            self._sort_col = column
            self._sort_rev = False
        current_path = self.model.tracks[self.current_index].path if 0 <= self.current_index < len(self.model.tracks) else None
        self.model.tracks = [t for _, t in sorted(enumerate(self.model.tracks),
                                                 key=lambda pair: key_fn(pair[1], pair[0]),
                                                 reverse=self._sort_rev)]
        if current_path:
            for i, t in enumerate(self.model.tracks):
                if os.path.normcase(t.path) == os.path.normcase(current_path):
                    self.current_index = i
                    break
        self._rebuild_tree(reset_bag=True)

    def _rebuild_tree(self, reset_bag: bool):
        filt = self.filter_var.get().strip().lower() if hasattr(self, "filter_var") else ""
        self.tree.delete(*self.tree.get_children())
        for i, t in enumerate(self.model.tracks):
            if filt and not (filt in t.title.lower() or filt in t.artist.lower() or filt in Path(t.path).name.lower()):
                continue
            self.tree.insert("", "end", iid=str(i), values=(i + 1, t.title, t.artist, self._fmt_time(t.length)))
        self._highlight_row()
        if reset_bag:
            self._rebuild_shuffle_bag()
        self._update_footer()

    def _highlight_row(self, clear=False):
        for iid in self.tree.get_children():
            self.tree.item(iid, tags=())
        if not clear and 0 <= self.current_index < len(self.model.tracks):
            iid = str(self.current_index)
            if iid in self.tree.get_children():
                self.tree.item(iid, tags=("playing",))
                self.tree.selection_set(iid)
                self.tree.see(iid)

    def _set_album_art(self, image_tk: ImageTk.PhotoImage | None):
        if image_tk is None:
            placeholder = Image.new("RGB", (260, 260), color=(40, 40, 40))
            self._album_art_hold = ImageTk.PhotoImage(placeholder)
            self.album_art.config(image=self._album_art_hold)
        else:
            self._album_art_hold = image_tk
            self.album_art.config(image=image_tk)

    # Drag reorder
    def _on_tree_press(self, event):
        self._drag_start_iid = self.tree.identify_row(event.y)
        self._drag_start_y = event.y
        self._dragging = False

    def _on_tree_motion(self, event):
        if not self._drag_start_iid:
            return
        if abs(event.y - self._drag_start_y) > 6:
            self._dragging = True

    def _on_tree_release(self, event):
        if not self._dragging or not self._drag_start_iid:
            self._drag_start_iid = None
            self._dragging = False
            return
        target_iid = self.tree.identify_row(event.y)
        if target_iid and target_iid != self._drag_start_iid:
            src = int(self._drag_start_iid)
            dst = int(target_iid)
            item = self.model.tracks.pop(src)
            self.model.tracks.insert(dst, item)
            if self.current_index == src:
                self.current_index = dst
            else:
                if src < self.current_index <= dst:
                    self.current_index -= 1
                elif dst <= self.current_index < src:
                    self.current_index += 1
            self._rebuild_tree(reset_bag=True)
            self.tree.selection_set(str(dst))
            self.tree.see(str(dst))
            self.history.clear()
        self._drag_start_iid = None
        self._dragging = False

    def _show_context_menu(self, event):
        try:
            iid = self.tree.identify_row(event.y)
            if iid:
                if iid not in self.tree.selection():
                    self.tree.selection_set(iid)
                self.ctx.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx.grab_release()

    def _ctx_play(self):
        self.play_selected()

    def _ctx_open_location(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        path = self.model.tracks[idx].path
        folder = str(Path(path).parent)
        try:
            if platform.system() == "Windows":
                os.startfile(folder)
            elif platform.system() == "Darwin":
                subprocess.call(["open", folder])
            else:
                subprocess.call(["xdg-open", folder])
        except Exception:
            webbrowser.open(f"file://{folder}")

    def play_next_for_selected(self):
        sel = [int(i) for i in self.tree.selection()]
        if not sel or self.current_index < 0:
            return
        sel = sorted(set(sel))
        items = [self.model.tracks[i] for i in sel]
        for i in reversed(sel):
            del self.model.tracks[i]
            if self.current_index > i:
                self.current_index -= 1
        insert_at = self.current_index + 1
        for i, t in enumerate(items):
            self.model.tracks.insert(insert_at + i, t)
        self._rebuild_tree(reset_bag=True)
        self.tree.selection_set([str(self.current_index + 1 + i) for i in range(len(items))])
        self.history.clear()

    # Mini mode
    def toggle_mini(self):
        # Non-destructive: just shrink/restore window geometry
        if getattr(self, "_mini_mode", False):
            self._mini_mode = False
            if getattr(self, "_saved_geom", None):
                self.root.geometry(self._saved_geom)
            self._toast("Standard view")
        else:
            self._saved_geom = self.root.winfo_geometry()
            self.root.geometry("420x420")
            self._mini_mode = True
            self._toast("Mini player")

    def toggle_lyrics(self):
        self.lyrics_enabled = not self.lyrics_enabled
        if self.lyrics_enabled:
            self.lyrics_label.config(text=self.lyrics_current)
            self.lyrics_label.pack(pady=(0, 6))
        else:
            self.lyrics_label.config(text="")
            self.lyrics_label.pack_forget()

    # Sleep timer
    def set_sleep_timer(self):
        minutes = simpledialog.askinteger("Sleep Timer", "Stop playback after how many minutes?",
                                          minvalue=1, maxvalue=480)
        if minutes:
            self.sleep_deadline = time.monotonic() + minutes * 60
            self._toast(f"Sleep timer set: {minutes} min")
        else:
            self.sleep_deadline = None

    # Timer loop
    def _ui_tick(self):
        try:
            if self.engine.is_playing and not self.engine.is_paused:
                cur = self.engine.position()
                L = self._current_track_len()
                if not self.user_seeking and L > 0:
                    self.progress_var.set(max(0.0, min(100.0, (cur / L) * 100.0)))
                self.time_start.config(text=self._fmt_time(cur))
                # Lyrics
                if self.lyrics_enabled and 0 <= self.current_index < len(self.model.tracks):
                    path = self.model.tracks[self.current_index].path
                    line = self.lyrics.current_line(path, int(cur + 0.1))
                    if line != self.lyrics_current:
                        self.lyrics_current = line
                        self.lyrics_label.config(text=line)
                # Sleep
                if self.sleep_deadline and time.monotonic() >= self.sleep_deadline:
                    self.sleep_deadline = None
                    self.stop()
                    self._toast("Sleep timer elapsed")
                    return
                # End detection
                end_by_time = (L > 0 and cur >= (L - 0.4))
                end_by_busy = not self.engine.busy()
                if end_by_time or (end_by_busy and cur > 0.3):
                    if self.repeat == "one":
                        self.engine.seek(0.0)
                    else:
                        self.next(from_end=True)
        except Exception:
            pass
        finally:
            self.tick_job = self.root.after(self.tick_ms, self._ui_tick)

    # Shuffle bag
    def _rebuild_shuffle_bag(self):
        n = len(self.model.tracks)
        self.shuffle_bag = list(range(n))
        random.shuffle(self.shuffle_bag)
        self.shuffle_pos = -1
        if 0 <= self.current_index < n and self.current_index in self.shuffle_bag:
            self.shuffle_bag.remove(self.current_index)
            self.shuffle_bag.insert(0, self.current_index)
            self.shuffle_pos = 0

    def _align_shuffle_cursor(self):
        if self.current_index in self.shuffle_bag:
            self.shuffle_pos = self.shuffle_bag.index(self.current_index)

    # Discovery
    def _discover_next(self):
        if not self.model.tracks:
            return
        last_path = self.model.tracks[-1].path
        exclude = {t.path for t in self.model.tracks}
        suggestion = self.discovery.suggest(last_path, exclude)
        if suggestion:
            if self.model.add_paths([suggestion]):
                self._rebuild_tree(reset_bag=True)
                self.current_index = len(self.model.tracks) - 1
                self.play_current(add_to_history=True)
        else:
            self.stop()

    # Settings
    def _load_settings(self):
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            geom = s.get("geometry")
            if geom:
                self.root.geometry(geom)
            vol = s.get("volume", 80)
            self.volume_var.set(vol)
            self._on_volume(None)
            self.shuffle = bool(s.get("shuffle", False))
            self.repeat = s.get("repeat", "none")
            self.discovery_enabled = bool(s.get("discovery", False))
            self.shuffle_btn.config(text="🔀 Shuffle: On" if self.shuffle else "🔀 Shuffle")
            self.repeat_btn.config(text={"none": "🔁 Repeat: Off", "all": "🔁 Repeat: All", "one": "🔂 Repeat: One"}[self.repeat])
            self.discovery_btn.config(text="✨ Discover: On" if self.discovery_enabled else "✨ Discover: Off")
            self.resume_index = int(s.get("last_index", -1))
            self.resume_position = float(s.get("last_position", 0.0))
            self._last_session_playlist = s.get("last_session_playlist", [])
            widths = s.get("column_widths", {})
            for col in ("num", "title", "artist", "duration"):
                w = widths.get(col)
                if w:
                    try:
                        self.tree.column(col, width=int(w))
                    except Exception:
                        pass
        except Exception:
            pass

    def _restore_last_session_playlist(self):
        paths = getattr(self, "_last_session_playlist", [])
        added = 0
        for p in paths:
            if os.path.isfile(p) and p.lower().endswith(".mp3"):
                added += self.model.add_paths([p])
        if added:
            self._rebuild_tree(reset_bag=True)

    def _restore_last_marker(self):
        idx = self.resume_index
        pos = self.resume_position
        if 0 <= idx < len(self.model.tracks):
            self.current_index = idx
            t = self.model.tracks[idx]
            self.now_label.config(text=f"Now Playing: {t.title}" + (f" - {t.artist}" if t.artist else ""))
            self._set_album_art(self.art.get(t.path))
            self.time_end.config(text=self._fmt_time(t.length))
            if pos > 0 and t.length > 0:
                self.progress_var.set((pos / t.length) * 100.0)
                self.time_start.config(text=self._fmt_time(pos))
            self._highlight_row()

    def _save_settings(self):
        cur = 0.0
        if self.engine.is_playing or self.engine.is_paused:
            cur = self.engine.position()
        widths = {
            "num": self.tree.column("num", option="width"),
            "title": self.tree.column("title", option="width"),
            "artist": self.tree.column("artist", option="width"),
            "duration": self.tree.column("duration", option="width"),
        }
        s = {
            "geometry": self.root.winfo_geometry(),
            "volume": int(self.volume_var.get()),
            "shuffle": bool(self.shuffle),
            "repeat": self.repeat,
            "discovery": bool(self.discovery_enabled),
            "last_session_playlist": [t.path for t in self.model.tracks],
            "last_index": int(self.current_index),
            "last_position": float(cur if cur < max(0.0, self._current_track_len() - 1.0) else 0.0),
            "column_widths": widths
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except Exception:
            pass

    # Utils
    def _current_track_len(self) -> float:
        if 0 <= self.current_index < len(self.model.tracks):
            return self.model.tracks[self.current_index].length
        return 0.0

    def _fmt_time(self, seconds) -> str:
        try:
            seconds = max(0, int(seconds))
        except Exception:
            seconds = 0
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _toast(self, text: str):
        self.status_label.config(text=text)
        self.root.after(2200, lambda: self.status_label.config(text=""))

    def _update_footer(self):
        total = len(self.model.tracks)
        shown = len(self.tree.get_children())
        total_time = sum(t.length for t in self.model.tracks)
        mode = f"Shuffle: {'On' if self.shuffle else 'Off'} | Repeat: {self.repeat.capitalize()} | Discover: {'On' if self.discovery_enabled else 'Off'}"
        self.footer_label.config(text=f"{shown} of {total} tracks shown | Total: {self._fmt_time(total_time)} | {mode}")

    def _focus_filter(self):
        try:
            self.filter_entry.focus_set()
            self.filter_entry.select_range(0, tk.END)
        except Exception:
            pass

    def _handle_play_error(self, path, e):
        if messagebox.askyesno("Playback Error", f"Could not play:\n{Path(path).name}\n\nError: {e}\n\nRemove from playlist?"):
            idx = next((i for i, t in enumerate(self.model.tracks) if os.path.normcase(t.path) == os.path.normcase(path)), -1)
            if idx >= 0:
                self.model.remove_indices([idx])
                if self.current_index == idx:
                    self.current_index = -1
                elif self.current_index > idx:
                    self.current_index -= 1
                self._rebuild_tree(reset_bag=True)

    def on_closing(self):
        if self.tick_job is not None:
            try:
                self.root.after_cancel(self.tick_job)
            except Exception:
                pass
            self.tick_job = None
        self.engine.stop(0)
        self._save_settings()
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        self.root.destroy()

    def _show_shortcuts(self):
        messagebox.showinfo(
            "Shortcuts",
            "Space: Play/Pause | Ctrl+Left/Right: Previous/Next\n"
            "Left/Right: Seek -5s/+5s | Up/Down: Volume +/-\n"
            "Enter/Double-click: Play selected | Delete: Remove\n"
            "Ctrl+O/F: Add songs/folder | Ctrl+S/L: Save/Load M3U\n"
            "Ctrl+H: Toggle Shuffle | Ctrl+R: Cycle Repeat\n"
            "/: Focus filter | Drag rows to reorder | Right-click: context menu\n"
            "View > Toggle Mini Player | Playback > Show Lyrics / Discovery Mode"
        )
def resource_path(rel):
    """
    Get absolute path to resource, works for dev and for PyInstaller bundled executable.
    """
    try:
        base = sys._MEIPASS  # PyInstaller temp folder
    except Exception:
        base = os.path.abspath(".")
    return os.path.join(base, rel)

def main():
    root = tk.Tk()

    # Optional: set window icon when icon.ico is bundled (works on Windows)
    try:
        icon_file = resource_path("icon.ico")
        if os.path.isfile(icon_file):
            root.iconbitmap(icon_file)
    except Exception:
        pass  # ignore if icon can't be set

    app = ElysianApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()