# Elysian Player

<img width="2350" height="855" alt="image" src="https://github.com/user-attachments/assets/f2f410e4-81b3-4cff-95b8-b8ba0229c737" />

A music player for Windows. The interface is drawn with WebView2, which already
ships with Windows, so the whole thing stays a single executable of about forty
megabytes. Audio runs on miniaudio.

- **Version**: 2.5.0.0
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

The Library is a second way to get at your music, separate from the playlist.
Point it at one or more folders and it builds a small index of what it finds,
so browsing works even if the folders themselves are slow, on a NAS, or
briefly unreachable.

**Four ways to look at it.** Albums, Artists, Genres and Songs sit as tabs
across the top. Albums groups by album title and artist together, so a band's
records line up in the order they came out. Opening an artist or a genre
breaks its tracks into album sections rather than one flat pile, with anything
that has no album tag collected at the end under its own heading instead of
scattered in with everything else. Songs lists every track and is searchable
on its own, broken into the same album sections.

**Same album, same artist, whatever the spelling.** A tagger who wrote
`NEVERMIND` on one track and `Nevermind` on the rest still gets one album, not
two, and sorting ignores both case and leading punctuation, so `"Weird Al"
Yankovic` files under W instead of jumping to the front on its quote mark.

**Compilations get grouped, unrelated albums don't.** If a track is flagged as
part of a compilation, or its album artist tag says something like Various
Artists, every track sharing that album title joins one card labelled that
way, whatever each individual track says about who performed it. Two
completely different bands who both happened to call an album Greatest Hits
stay as two separate cards. Sharing a title is not, by itself, a reason to
merge two things that were never the same release.

**The search box only searches what the tab shows you.** On Albums it matches
album and artist names. On Artists it matches artist names, on Genres it
matches genre names, and on Songs it matches song titles. A search on Songs
does not go digging through album titles and hand you three unrelated tracks
because their album happened to contain the word; if you are looking for a
song, Songs is where you look for it.

**Opening an album shows its cover next to the track list**, with the year,
track count and total time underneath, and the cover stays in place while you
scroll the tracks. The cover itself comes from the first track in the album
that actually carries one, so a compilation whose opening file happens to be
untagged still gets its artwork from track two or wherever it actually lives.
Covers for whatever you are looking at are fetched first; everything else in
the library fills in quietly in the background, so a library of thousands of
albums does not sit there decoding images nobody has scrolled to yet, but
nothing is left blank forever either.

**Scanning a folder works one subfolder at a time**, and each one is written
and made browsable the moment it finishes rather than all at once at the end.
On a large collection that means albums start showing up within seconds, and
you can browse, search and even start playing something while the rest is
still being read. A rescan only re-reads a file if its modification time
changed, so once a folder is indexed, checking it again is quick.

**Manage the folders from the Folders button**, next to the tabs. It lists
everything currently indexed with a Remove next to each; removing asks you to
click a second time before it actually does anything, since it drops every
track under that folder from the library. It only ever touches the index,
never the files themselves.

**Double-click a track anywhere in the library and it keeps playing.** Whether
you are inside an album, an artist, a genre, or the Songs list, the whole
thing you are currently looking at becomes the active queue, in the order it
is shown on screen, starting from the track you clicked. Finish that track and
it moves on to the next one in the same list, the same way it would if you had
built a playlist out of it yourself. Double-clicking an album's cover plays
that album from the first track without you having to open it first. The
library stays open while this happens, and the track that is currently
playing gets the same small triangle marker the playlist itself uses, so you
can see what is playing from inside the library too, not only from the
Playlist tab. Shuffle, repeat, next and previous all keep working exactly as
they always have, whatever queue happens to be loaded at the time.

**The playlist reads its tags from this index.** A track the library has
already seen needs no file opened at all to show its title and artist, which
is what makes browsing a large collection on a network share feel normal
instead of slow.

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

## Not in 2.5.0.0

These worked in 1.0.0.0 and did not survive the rewrite. They are listed here
so nobody upgrades expecting them:

* Synced `.lrc` lyrics
* Discovery mode
* Sleep timer
* Mini player mode
* Sortable playlist columns
* Remove missing files
* Right-click context menu
* Shortcuts dialog

The lyrics and discovery modules still live in `elysian/services/`, tested,
but they are no longer constructed at runtime. Nothing else remains.

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

## Layout

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
  services/                tags, album art, waveform, library, lyrics,
                           discovery
  web/                     index.html, style.css, app.js: the interface
```

Application state lives entirely on the Python side. The frontend polls a small
snapshot and renders what it is given, so there is one source of truth.

Every method the frontend can call is either a plain read of a snapshot or a
request queued for the worker. Nothing on that boundary touches a disk, a
network share or a database, because a bridge call that blocks freezes the
interface. Library queries follow the same rule: a request is queued, the query
runs on its own thread, and the result is collected when a revision counter
changes. Both browsing and opening a record carry their own generation number
too, so a slower query fired earlier can never land after a faster one fired
later and overwrite it with something stale.

Playing a track from the library replaces the active playlist with that view's
own current order and starts at the clicked track, then hands off to the same
transport code that runs everything else. There is only one queue in this
program; the library just knows how to build one from what it is showing you.
The currently playing path is carried in the same snapshot the rest of the
transport state comes from, which is how both the playlist and the library
can show the same track marked as playing without needing two separate ideas
of what "current" means.

The frontend fetches the whole track list only when the row set changes.
When a tag scan fills in metadata it fetches just the rows that changed.
Sending the full list for that meant over a megabyte a second on a long
playlist to communicate a couple of dozen updates.

That poll adapts: 200ms while playing, 1s when paused, and a 2s heartbeat when
the window is hidden. Playback runs on Python's worker thread and is unaffected
by any of it, so audio continues normally when hidden; only the asking slows
down. Returning to the window polls immediately rather than waiting out the
interval, and a press or a drag pulls the rate back up so it cannot sit
unconfirmed.

Note for anyone editing the frontend: `ROW_H` in `app.js` and the `.row` height
in `style.css` must stay equal, or rows drift out of line with the scrollbar.
Both are 30px. The library's own track lists are not virtualised the same way
the playlist is, since a browse list is rarely more than a few thousand rows;
if that ever needs to hold tens of thousands of visible rows at once the way
the playlist does, it will need the same treatment.

Every user action goes through the `intent` object in `app.js`, so a keypress
and a click produce the same local update before the command is posted. That
update is held by `predict`/`settled` until the backend snapshot agrees, or for
1.5s, whichever comes first; without that hold a poll landing mid-flight snaps
the control back and the press looks like it did nothing.

Note on paths: whether two strings name the same file is decided in one place,
`paths.key()`. It is `normcase` plus `abspath`, because `pathlib` has no
equivalent of `normcase`, and `Path.resolve()` touches the filesystem, which on
a network share would turn every comparison into a round trip. Two call sites
deliberately stay on `os.path` for speed and say so in a comment; both are in
per-track or per-file loops where `pathlib` measured 5 to 9 times slower.

Note for anyone editing `api.py`: pywebview builds `window.pywebview.api` by
walking the **public** attributes of that object, and recurses into
non-callables. Anything that is not a method meant for JavaScript needs a
leading underscore. A public reference to the window once made it descend into
`window.dom.document`, which blocks until the page loads, and the API object was
never created at all. `Api.JS_BRIDGE` lists everything JavaScript may call,
`Api.HOST_PUBLIC` lists the host-side entry points that must stay public, and
`_assert_bridge_surface()` runs at startup and refuses to launch if anything
else is public, or if either set names something that is not a method.

## License

MIT. See [LICENSE](LICENSE).

Built with [pywebview](https://pywebview.flowrl.com/),
[just_playback](https://github.com/cheofusi/just_playback),
[miniaudio](https://github.com/irmen/pyminiaudio),
[mutagen](https://mutagen.readthedocs.io/) and
[Pillow](https://python-pillow.org/).
