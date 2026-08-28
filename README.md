# Elysian Player

<!-- Drop a screenshot here. Paste the image into a GitHub issue or the release
     draft, then copy the generated user-attachments URL in, the same way the
     other repos do:
<img width="980" height="620" alt="Elysian Player" src="https://github.com/user-attachments/assets/REPLACE-ME" />
-->

An MP3 player for Windows, written in Python with tkinter and pygame.
It reads your ID3 tags, shows embedded album art, follows along with `.lrc`
lyrics, and picks up exactly where you left off the next time you open it.

- **License**: MIT
- **OS**: Windows
- **Python**: exactly **3.13.12**

---

## Core Features

* Reads title, artist and duration straight from each file's ID3 tags with
  mutagen. Anything untagged falls back to the filename.
* Shows embedded album art. If a track has none, it looks in the track's folder
  for `cover.jpg`, `folder.jpg`, `front.jpg`, `album.jpg`, `cover.png` or
  `folder.png` instead.
* Displays synced lyrics from a `.lrc` file sitting next to the MP3, with the
  current line highlighted as the song plays.
* Save and load M3U playlists. Paths are written relative to the playlist file
  where possible, so a playlist stays valid if you move the whole folder.
* Shuffle draws from a bag, so every track plays once before any repeats.
  Repeat cycles through off, all and one.
* Discovery mode. When the queue runs dry, it goes looking through nearby
  folders for an MP3 that is not already in your playlist and adds it.
* Sleep timer, from 1 to 480 minutes.
* Mini player mode that shrinks the window down to a compact size.
* Filter box that narrows the list by title, artist or filename as you type.
* Sortable columns, drag-and-drop reordering, and a right-click menu with Play,
  Play Next, Open File Location and Remove.
* Remove Missing Files sweeps out anything that has been deleted or moved since
  you added it.
* Restores your last session on launch: playlist, current track, playback
  position, volume, shuffle and repeat state, window size and column widths.

## Controls

| Key(s)                 | Action                       | Category |
| ---------------------- | ---------------------------- | -------- |
| `Space`                | Play or pause                | Playback |
| `Ctrl+Left`            | Previous track               | Playback |
| `Ctrl+Right`           | Next track                   | Playback |
| `Left` / `Right`       | Seek back or forward 5s      | Playback |
| `Up` / `Down`          | Volume up or down 5%         | Audio    |
| `Ctrl+H`               | Toggle shuffle               | Modes    |
| `Ctrl+R`               | Cycle repeat off/all/one     | Modes    |
| `Ctrl+O`               | Add songs                    | File     |
| `Ctrl+F`               | Add a folder                 | File     |
| `Ctrl+S`               | Save playlist as M3U         | File     |
| `Ctrl+L`               | Load an M3U playlist         | File     |
| `/`                    | Jump to the filter box       | Playlist |
| `Double-click`         | Play the selected track      | Playlist |
| `Delete`               | Remove the selected tracks   | Playlist |
| `Right-click`          | Open the context menu        | Playlist |
| Drag a row             | Reorder the playlist         | Playlist |

The same list is available in the app under **Tools > Shortcuts**.

## Album Art and Lyrics

Album art is read from the ID3 `APIC` frame first. When a track has no embedded
art, the folder it lives in is checked for a cover image, so a well-organised
music library usually shows art without any tagging work.

Lyrics come from a `.lrc` file with the same name as the MP3, in the same
folder. `Nightcall.mp3` pairs with `Nightcall.lrc`. Timestamps in the standard
`[mm:ss.xx]` form are read, and the matching line is shown under the track
title while it plays. Toggle the display under **Playback > Show Lyrics**.

## Session and Settings

Settings are written to `.elysian_player_final.json` in your home folder, not
into the program folder, so the executable can live anywhere. Delete that file
to reset the app to a clean state.

## Installation and Usage

### Run the executable

Download `Elysian Player.exe` from the
[Releases](../../releases) page and run it. It is a single self-contained file
and does not need Python installed.

### Run from source

This project targets **Python 3.13.12** and nothing else. The versions in
`requirements.txt` are pinned with `==` and resolved against that interpreter,
so another version pulls different wheels and is not a configuration that has
been tested here.

1. Create and activate a virtual environment on 3.13.12:
   ```sh
   py -3.13 -m venv .venv
   .\.venv\Scripts\activate
   ```
2. Install the pinned dependencies:
   ```sh
   pip install -r requirements.txt
   ```
3. Run it:
   ```sh
   python Elysian_Player.py
   ```

If you do not have 3.13.12, get it from
[python.org](https://www.python.org/downloads/release/python-31312/) and enable
the **py launcher** option during installation.

## Building an Executable

Double-click `BUILD_EXE.bat`. It confirms Python is exactly 3.13.12 through the
`py` launcher, wipes and rebuilds `.venv` from scratch, installs the pinned
dependencies from `requirements.txt`, and produces a one-file
`dist\Elysian Player.exe`.

Two things the script does deliberately:

* **Every dependency is pinned with `==`**, including the whole PyInstaller
  chain, and the venv is rebuilt on every run. A build here is the build anyone
  else gets. The script prints `pip freeze` before building so a misbehaving
  build can be diffed against `requirements.txt` line by line.
* **There is no `.spec` file.** PyInstaller generates one from the command-line
  flags in the script, and the script deletes it afterwards. A tracked spec
  goes stale and silently overrides every flag set in the batch file, so it
  never gets committed.

If the wrong Python version is installed, the script reports what it found and
opens the download page for 3.13.12.

## Known Limitations

* MP3 only. Other audio formats are not read.
* Adding a large folder blocks the window while every file is tag-scanned.
* Keyboard shortcuts stay live while the filter box has focus, so a space
  typed into a search term also toggles playback.
* Discovery mode searches folders above the current track, which can be slow
  the first time if your library sits near the root of a large drive.

## License

MIT. See [LICENSE](LICENSE).

Built with [pygame](https://www.pygame.org/),
[mutagen](https://mutagen.readthedocs.io/) and
[Pillow](https://python-pillow.org/).
