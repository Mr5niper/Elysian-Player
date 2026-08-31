# Elysian Player

<img width="2300" height="855" alt="image" src="https://github.com/user-attachments/assets/45da3b84-06b9-4cf7-8a5f-5fc227bcaed5" />

A music player for Windows. The interface is drawn with WebView2, which already
ships with Windows, so the whole thing stays a single executable of about forty
megabytes. Audio runs on miniaudio.

- **Version**: 2.0.0.0
- **License**: MIT
- **OS**: Windows 10 or 11 (WebView2 runtime required, see below)
- **Python**: exactly **3.13.12**

---

## Features

* Plays MP3, FLAC, WAV and OGG.
* Reads title, artist, album and duration from tags with mutagen. Untagged
  files fall back to their filename.
* Shows embedded album art. If a track has none, the folder it lives in is
  checked for `cover.jpg`, `folder.jpg`, `front.jpg`, `album.jpg`, `cover.png`
  or `folder.png`.
* Draws a waveform of the current track.
* Save and load M3U playlists. Paths are written relative to the playlist file
  where possible, so a playlist survives moving the folder it sits in.
* Drag audio files or folders onto the window to add them.
* Double-click an audio file in Explorer to play it, once the file type is
  associated with the executable. It plays the file you opened, whether or not
  it was already in the playlist.
* Only one copy runs at a time. Opening another file hands it to the player
  that is already running rather than starting a second one.
* Filter the playlist by title, artist, album or filename as you type. The
  playlist shows track number, title, artist, album and duration; the album
  column hides itself when the window is narrower than 820px.
* Drag rows to reorder. Ctrl-click to select several.
* Shuffle draws from a bag, so every track plays once before any repeats.
  Repeat cycles off, all, one.
* Restores your playlist, current track, volume, shuffle and repeat state on
  the next launch, and resumes the last track from where you stopped the first
  time you press play.

## Controls

| Key(s)           | Action                  |
| ---------------- | ----------------------- |
| `Space`          | Play or pause           |
| `Ctrl+Left`      | Previous track          |
| `Ctrl+Right`     | Next track              |
| `Left` / `Right` | Seek back or forward 5s |
| `Up` / `Down`    | Volume up or down 5%    |
| `Enter`          | Play the selected track |
| `Delete`         | Remove selected tracks  |
| `/`              | Jump to the filter box  |
| `Escape`         | Clear the filter        |
| `Double-click`   | Play that track         |
| `Ctrl+click`     | Add to the selection    |
| Drag a row       | Reorder the playlist    |

## Not in 2.0.0.0

These worked in 1.0.0.0 and did not survive the rewrite. They are listed here
so nobody upgrades expecting them:

* Synced `.lrc` lyrics
* Discovery mode
* Sleep timer
* Mini player mode
* Sortable columns
* Remove missing files
* Right-click context menu
* Shortcuts dialog

The lyrics and discovery modules still live in `elysian/services/`, tested,
but they are no longer constructed at runtime. Nothing else remains.

## Working With Files On A Network Share

The player assumes your library might not be local, and avoids touching files
until it has to:

* Adding files reads no tags. Tracks appear as filenames immediately, and tags
  are read only for the rows currently on screen, driven by scrolling.
* Adding a folder streams the directory walk, so tracks start appearing within
  milliseconds instead of after the entire share has been enumerated.
* Album art is fetched only for the track that is playing.
* Startup does not check that every saved path still exists.
* Every operation that can block runs on a worker thread. The interface only
  ever reads a precomputed snapshot, so a slow share cannot stall a button.

The playlist renders only its visible window, so a fifty-thousand track list
opens in about the same time as a two-thousand track one.

## Settings

Written to `.elysian_player.json` in your home folder, not the program folder,
so the executable can live anywhere. Delete that file to reset the app.

A second small file, `.elysian_player_instance`, holds a token used by the
single-instance check. That check talks over a Windows named pipe rather than
a network socket, so it never triggers a firewall prompt and cannot collide
with another program over a port number.

Problems are logged to `.elysian_player.log` in the same folder, rotating at
512 KB with two backups. Set `ELYSIAN_DEBUG=1` for debug-level detail.

## Installation

### Run the executable

Download `Elysian Player.exe` from the [Releases](../../releases) page and run
it. It is a single self-contained file and does not need Python installed.

It does need the **WebView2 runtime**, which is part of Windows 11 and is
installed alongside Edge on Windows 10. If the window opens blank, that is what
is missing; Microsoft distributes the Evergreen Runtime installer free.

### Run from source

This project targets **Python 3.13.12** and nothing else. The versions in
`requirements.txt` are pinned with `==` and resolved against that interpreter,
so another version pulls different wheels and is not a configuration that has
been tested.

1. Create and activate a virtual environment:
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
   python run.py
   ```

Set `ELYSIAN_DEBUG=1` before running to open DevTools alongside the window.

If you do not have 3.13.12, get it from
[python.org](https://www.python.org/downloads/release/python-31312/) and enable
the **py launcher** option during installation.

## Building an Executable

Double-click `BUILD_EXE.bat`. It confirms Python is exactly 3.13.12 through the
`py` launcher, wipes and rebuilds `.venv` from scratch, installs the pinned
dependencies, and produces `dist\Elysian Player.exe`.

Two things the script does deliberately:

* **Every dependency is pinned with `==`**, including the whole PyInstaller
  chain, and the venv is rebuilt on every run. A build here is the build anyone
  else gets. `pip freeze` is printed before building so a misbehaving build can
  be diffed against `requirements.txt` line by line.
* **There is no `.spec` file.** PyInstaller generates one from the command-line
  flags in the script, and the script deletes it afterwards. A tracked spec
  goes stale and silently overrides every flag set in the batch file.

The `--exclude-module` flags matter more than they look. pywebview can drive Qt
and GTK as well as the Windows backend, and PyInstaller bundles every one it can
find; excluding the unused ones is what keeps this around forty megabytes.

## Layout

```
run.py                     entry point
elysian/
  config.py                constants, settings path
  logs.py                  rotating log file setup
  api.py                   the bridge exposed to the frontend
  host.py                  window creation, file drop, file association
  single_instance.py       hands a file to an already-running copy
  models/                  Track, Playlist
  playback/engine.py       miniaudio wrapper
  services/                tags, album art, waveform, lyrics, discovery
  web/                     index.html, style.css, app.js -- the interface
```

Application state lives entirely on the Python side. The frontend polls a small
snapshot and renders what it is given, so there is one source of truth.

That poll adapts: 200ms while playing, 1s when paused, and a 2s heartbeat when
the window is hidden. Playback runs on Python's worker thread and is unaffected
by any of it, so audio continues normally when hidden; only the asking slows
down. Returning to the window polls immediately rather than waiting out the
interval, and a press or a drag pulls the rate back up so it cannot sit
unconfirmed.

Note for anyone editing the frontend: `ROW_H` in `app.js` and the `.row` height
in `style.css` must stay equal, or rows drift out of line with the scrollbar.
Both are 30px.

Every user action goes through the `intent` object in `app.js`, so a keypress
and a click produce the same local update before the command is posted. That
update is held by `predict`/`settled` until the backend snapshot agrees, or for
1.5s, whichever comes first; without that hold a poll landing mid-flight snaps
the control back and the press looks like it did nothing.

Note for anyone editing `api.py`: pywebview builds `window.pywebview.api` by
walking the **public** attributes of that object, and recurses into
non-callables. Anything that is not a method meant for JavaScript needs a
leading underscore. A public reference to the window once made it descend into
`window.dom.document`, which blocks until the page loads, and the API object was
never created at all. `Api.BRIDGE` lists everything JavaScript may call, and
`_assert_bridge_surface()` runs at startup and refuses to launch if anything
else is public.

## License

MIT. See [LICENSE](LICENSE).

Built with [pywebview](https://pywebview.flowrl.com/),
[just_playback](https://github.com/cheofusi/just_playback),
[miniaudio](https://github.com/irmen/pyminiaudio),
[mutagen](https://mutagen.readthedocs.io/) and
[Pillow](https://python-pillow.org/).
