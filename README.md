# Elysian Player

<img width="2350" height="855" alt="image" src="https://github.com/user-attachments/assets/f2f410e4-81b3-4cff-95b8-b8ba0229c737" />

A music player for Windows. The interface is drawn with WebView2, which already
ships with Windows, so the whole thing stays a single executable of about forty
megabytes. Audio runs on miniaudio.

- **Version**: 2.5.1.0
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
  that is already running instead of starting a second one, and brings that
  window to the front so you can actually see it happened.
* Filter the playlist by title, artist, album or filename as you type. The
  playlist shows track number, title, artist, album and duration; the album
  column hides itself when the window is narrower than 820px.
* Drag rows to reorder. Selection works as it does in Explorer: ctrl-click
  toggles one row, shift-click takes the whole run from the last row you
  clicked, and ctrl+shift-click adds that run to what is already selected.
* Typing into any filter box never triggers a playback shortcut, even if the
  key you typed happens to be one that normally would.
* Shuffle draws from a bag, so every track plays once before any repeats.
  Repeat cycles off, all, one.
* Click the speaker icon to mute. The slider keeps your volume, unmuting
  restores it, and moving the volume while muted unmutes.
* Clear the whole playlist from the toolbar or with `Ctrl+Shift+Delete`. It
  asks first, and a folder scan running at the time stops rather than
  refilling the list you just emptied.
* Restores your playlist, current track, volume, shuffle and repeat state on
  the next launch, and resumes the last track from where you stopped the first
  time you press play.

### Library

A second way to get at your music, separate from the playlist. Point it at
one or more folders and it builds a small index of what it finds, so
browsing works even if the folders themselves are slow, on a NAS, or briefly
unreachable.

* **Four tabs**: Albums, Artists, Genres and Songs. Albums groups by album
  title and artist together, so a band's records line up in the order they
  came out. Opening an artist or a genre breaks its tracks into album
  sections rather than one flat pile, with anything that has no album tag
  collected at the end under its own heading. Songs lists every track and is
  searchable on its own, broken into the same album sections.
* **Same album, same artist, whatever the spelling.** A tagger who wrote
  `NEVERMIND` on one track and `Nevermind` on the rest still gets one album,
  not two, and sorting ignores both case and leading punctuation, so
  `"Weird Al" Yankovic` files under W instead of jumping to the front on its
  quote mark.
* **Compilations get grouped, unrelated albums don't.** A track flagged as
  part of a compilation, or whose album artist tag says something like
  Various Artists, joins every other track sharing that album title under
  one card labelled that way. Two different bands who both called an album
  Greatest Hits stay as two separate cards.
* **Search only searches what the tab shows you**: album and artist names on
  Albums, artist names on Artists, genre names on Genres, song titles on
  Songs. Typing clears the current list right away and the filtered result
  loads in as it settles, so a fast typist is never left staring at a stale
  list.
* **Click an album to expand it in place**, cover and full track list, right
  under the row of cards it belongs to; the rest of the grid stays where it
  is above and below. Click a different album and the expansion moves there;
  click the same one again to close it. Resizing the window keeps the
  expanded album positioned under the right row as more or fewer cards fit
  per line, and keeps it as fully visible as the window allows without ever
  scrolling its cover and first track out of view.
* **Each of the four tabs remembers its own place.** Scroll position and
  whatever you had open or selected stay put, so switching between Albums,
  Artists, Genres and Songs is a visit, not a reset.
* **Edit tags directly on the files.** Select one or more tracks, or open an
  album, and Edit Tags lets you change title, artist, album, album artist,
  genre, track and disc number, year, and compilation. A field left alone
  across a mixed selection is shown blank rather than guessed at, and only
  the fields you actually touch get written. Editing the track that's
  currently playing pauses it just long enough to save, then picks back up
  right where it left off.
* **Queue tracks straight from an open album, artist or genre.** Add to
  playlist queues whatever's selected, or everything shown if nothing is.
  Add all to playlist always queues everything regardless of selection, so
  a partial selection never has to be cleared first just to grab the whole
  thing.
* **Opening an album shows its cover next to the track list**, with the year,
  track count and total time underneath. The cover comes from the first
  track in the album that actually carries one, so a compilation whose
  opening file happens to be untagged still gets its artwork from wherever it
  actually lives. Covers for whatever you are looking at are fetched first;
  everything else in the library fills in quietly in the background.
* **Scanning a folder works one subfolder at a time**, and each one is
  written and made browsable the moment it finishes rather than all at once
  at the end. A rescan only re-reads a file if its modification time
  changed, so once a folder is indexed, checking it again is quick.
* **Manage the folders from the Folders button**, next to the tabs. It lists
  everything currently indexed with a Remove next to each; removing asks you
  to click a second time before it does anything. It only ever touches the
  index, never the files themselves.
* **Double-click a track anywhere in the library and it keeps playing.**
  Whatever you are currently looking at becomes the active queue, in the
  order shown, starting from the track you clicked. The library stays open
  while this happens, and the currently playing track gets the same marker
  the playlist uses.
* **The playlist reads its tags from this index.** A track the library has
  already seen needs no file opened at all to show its title and artist,
  which is what makes browsing a large collection on a network share feel
  normal instead of slow.

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
| `Ctrl+Shift+Delete` | Clear the playlist (asks first) |
| `/`              | Jump to the filter box  |
| `Escape`         | Clear the filter        |
| `Double-click`   | Play that track         |
| Click the speaker | Mute or unmute         |
| `Ctrl+click`     | Toggle one row          |
| `Shift+click`    | Select a run of rows    |
| `Ctrl+Shift+click` | Add a run to the selection |
| Drag a row       | Reorder the playlist    |

Double-clicking the title bar maximises and restores, as it would on a normal
window. The maximise button changes to a restore glyph while maximised.

Selection in the library track lists follows the same rules as the playlist.
Double-clicking a row plays it and continues through the rest of whatever list
you had open, as described above.

## Working With Files On A Network Share

The player assumes your library might not be local, and avoids touching files
until it has to:

* Adding files to the playlist reads no tags up front. Tracks appear as
  filenames immediately, and the rows on screen are read first. Whatever
  capacity is left over fills in the rest of the list: while you scroll, only
  the direction you are heading; once you stop, outward from the view in both
  directions. Rows scrolling into view jump ahead of that prefetch, and
  prefetch you have scrolled past is discarded rather than spending round
  trips on rows nobody will look at.
* Adding a folder to the playlist streams the directory walk, so tracks start
  appearing within milliseconds instead of after the entire share has been
  enumerated. Indexing a folder into the Library works the same way, one
  subfolder at a time, described above.
* Album art for the playlist is fetched only for the track that is playing.
  Library cover art is fetched for whatever is on screen first and fills in
  the rest afterward, also described above.
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

The library index is a SQLite database at `.elysian_library.db`, also in your
home folder. It holds the tags and file paths it has seen, never the audio
itself. Deleting it loses nothing but the index: the folders are remembered in
the settings file and a rescan rebuilds it.

If you are updating from a version older than 2.4.0.0 and already have folders
indexed, the next scan will take longer than usual, since a couple of columns
were added to the database and every file needs to be read once to fill them
in. After that one pass, rescans go back to being fast.

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

## Project Layout

```
run.py                     entry point
elysian/
  config.py                constants, settings path
  logs.py                  rotating log file setup
  paths.py                 path comparison, in one place
  api.py                   the bridge exposed to the frontend
  host.py                  window creation, file drop, file association
  single_instance.py       hands a file to an already-running copy
  models/                  Track, Playlist
  playback/engine.py       miniaudio wrapper
  services/                tags, album art, waveform, library
  web/                     index.html, style.css, app.js: the interface
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the frontend and backend talk
to each other, and for implementation notes worth knowing before changing
either one.

## License

MIT. See [LICENSE](LICENSE).

Built with [pywebview](https://pywebview.flowrl.com/),
[just_playback](https://github.com/cheofusi/just_playback),
[miniaudio](https://github.com/irmen/pyminiaudio),
[mutagen](https://mutagen.readthedocs.io/) and
[Pillow](https://python-pillow.org/).
