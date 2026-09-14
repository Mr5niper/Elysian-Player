# Architecture

Notes for anyone working on the code itself. If you just want to run or build
the player, see [README.md](README.md).

## One source of truth

Application state lives entirely on the Python side. The frontend polls a
small snapshot and renders what it is given; nothing is computed twice in two
places.

Every method the frontend can call is either a plain read of a snapshot or a
request queued for the worker. Nothing on that boundary touches a disk, a
network share or a database, because a bridge call that blocks freezes the
interface. Library queries follow the same rule: a request is queued, the
query runs on its own thread, and the result is collected when a revision
counter changes. Both browsing and opening a record carry their own
generation number too, so a slower query fired earlier can never land after a
faster one fired later and overwrite it with something stale.

Playing a track from the library replaces the active playlist with that
view's own current order and starts at the clicked track, then hands off to
the same transport code that runs everything else. There is only one queue
in this program; the library just knows how to build one from what it is
showing you. The currently playing path is carried in the same snapshot the
rest of the transport state comes from, which is how both the playlist and
the library can show the same track marked as playing without needing two
separate ideas of what "current" means.

## Polling

The frontend fetches the whole track list only when the row set changes.
When a tag scan fills in metadata it fetches just the rows that changed.
Sending the full list for that meant over a megabyte a second on a long
playlist to communicate a couple of dozen updates.

That poll adapts: 200ms while playing, 1s when paused, and a 2s heartbeat
when the window is hidden. Playback runs on Python's worker thread and is
unaffected by any of it, so audio continues normally when hidden; only the
asking slows down. Returning to the window polls immediately rather than
waiting out the interval, and a press or a drag pulls the rate back up so it
cannot sit unconfirmed.

Every user action goes through the `intent` object in `app.js`, so a keypress
and a click produce the same local update before the command is posted. That
update is held by `predict`/`settled` until the backend snapshot agrees, or
for 1.5s, whichever comes first; without that hold a poll landing mid-flight
snaps the control back and the press looks like it did nothing.

## Library browse and tag editing

Each of the four library tabs (Albums, Artists, Genres, Songs) keeps its own
local snapshot in the frontend: the rows already loaded, whatever was drilled
into, the selection, and exact scroll position. Switching tabs restores that
snapshot directly instead of re-querying the backend, so it's a visit, not a
reload. A snapshot is invalidated, forcing a fresh query on next visit, when
the library index actually changes underneath it — a rescan, a root removed,
a tag edit that changes grouping. An active filter always bypasses the cached
snapshot, since the cache may not reflect it.

Typing in the library filter clears the visible list immediately, before the
debounced backend query fires, so the interface never sits on a stale result
mid-keystroke. A result is discarded on arrival if the filter text or active
tab has moved on since it was requested. Songs, the one browse surface that
can run into many thousands of rows, uses a much smaller result cap while a
filter is active than while browsing unfiltered, since a large result there
means rebuilding thousands of rows on every settled keystroke.

An expanded album is inserted inline into the album grid as a full-width
row, immediately after whichever card was clicked, rather than replacing the
grid with a separate view. Widening or narrowing the window can change how
many cards fit per row, which can move which row the expansion belongs
under; a resize re-measures and repositions it rather than leaving it
attached to a card count that no longer applies. Maximizing shows the row
above the expansion for context; restoring to a smaller size keeps the whole
album visible if it fits, and otherwise keeps its cover and first track
visible rather than scrolling to chase a bottom that cannot fit anyway.
Clicking a different card while one is already expanded keeps the newly
clicked row anchored to where it was on screen through the transition,
rather than letting the old panel's collapse shift it out of view first.

The tag editor reads each selected file directly rather than the index, so
opening it always reflects the file's true current tags rather than whatever
the last scan happened to see, and refreshes the index for those exact files
as a side effect of that same read. Saving compares the fresh read against
what is already indexed and writes only the rows that actually changed.

Saving tags on the file currently loaded for playback needs the engine to
actually let go of it first, and pausing or stopping playback is not
enough for that: the decoder underneath keeps the file open regardless of
playback state, confirmed directly against the backend's own file
descriptor rather than assumed from the Python-level API looking like it
should have released it. The only thing that releases a file at all is
loading a different one over it, which is why `PlaybackEngine.release_file()`
exists separately from `stop()` - it points the engine at a tiny silent
file generated on the fly, forcing the previous one closed without playing
anything audible, then the save flow reloads and resumes the original
track at its saved position once the write finishes. Anything that
touches playback state around a tag save should call `release_file()`,
not `stop()`; `stop()` alone will silently leave the file locked.

### Library pre-warming

All four browse views (Albums, Artists, Genres, Songs) start their
unfiltered query at app startup, not on first visit to Library, and are
re-fired at every point the library already refreshes itself - a scan
tick, a scan finishing, a root removed, a tag save - so a tab nobody has
opened yet never goes stale waiting for someone to click into it
(`_do_library_prewarm_all` in `api.py`). The background art fill is fed by
the Albums query specifically, the same way it always was; the other
three tabs have no art of their own to fill.

Each view tracks its own query generation, not one shared counter: firing
all four together must never let one invalidate another's still-in-flight
result, which a single shared counter did - verified directly, it made a
later-scheduled but individually cheap query (Genres, say) sometimes
finish last regardless of its own actual cost, since it was waiting
behind the others under one lock. A `(view, needle)` pending set also
dedupes in-flight queries: a tab switch landing while its own prewarm
query is still running never starts a second one, it just waits on the
one already in flight.

The frontend checks a per-view cache (`library_get_prewarmed`) before
falling through to a live query, at every one of the four places that can
trigger a browse: opening Library for the first time, switching tabs,
clearing the filter box back to empty, and the scan-triggered refresh.
Serving a view straight from that cache renders locally with no round
trip at all - which means the backend never otherwise learns a navigation
happened. A separate `library_note_view` call records it anyway (updating
the current-view tracker and the persisted "last tab used" setting
without re-running the query), and deliberately does not bump the same
revision counter a real completed query does: bumping it there was tried
first, and it made every cache-hit navigation also trigger a second,
redundant fetch and re-render a moment later, on top of the one that had
already rendered locally.

### Album art caching

Resolved album art is cached to local disk as a small JPEG
(`ART_CACHE_DIR` in `config.py`), keyed by the album's (album, artist)
tag pair rather than by file path or folder, so every track on an album
shares one cached cover and a cover is never tied to whichever folder
happened to supply it. The cache (`art_cache` table in the library
database) records the exact file the cover was decoded from - the audio
file itself if the picture was embedded, or the specific cover image file
if it came from the folder - and that file's modification time at the
moment it was cached. A lookup is a `stat()` and a small local file read
if the source's mtime still matches; anything else (mtime changed, entry
missing, thumbnail file gone) falls through to a real decode, which then
updates the cache.

Removing a library root deletes any cache entries and thumbnail files
whose source lived under it. Editing a tag that moves a file to a
different (album, artist) grouping - most commonly renaming the album or
album artist field - invalidates both the album a file left and the one
it joined, so a cover already resolved earlier in the same session
doesn't keep showing a stale value; the invalidated album is re-resolved
immediately rather than waiting for something else to ask for it.

Library reads (`LibraryService._rows()`) do not hold a Python-level lock.
Each call opens its own connection, and the database runs in WAL mode
specifically so reads can proceed alongside each other and alongside an
active write. An earlier version wrapped every read in a shared lock,
which forced independent queries - the four startup pre-warm queries, for
instance - to run one at a time regardless of how cheap any individual
one was, and meant whichever got scheduled last paid for the others'
combined time before it could even start; verified directly by timing the
actual query strings at realistic library scale. Every write path (root
add/remove, batch upserts, schema migrations) still holds the lock.

## Frontend implementation notes

`ROW_H` in `app.js` and the `.row` height in `style.css` must stay equal, or
rows drift out of line with the scrollbar. Both are 30px. The library's own
track lists are not virtualised the same way the playlist is, since a browse
list is rarely more than a few thousand rows; if that ever needs to hold tens
of thousands of visible rows at once the way the playlist does, it will need
the same treatment.

The window is frameless (`frameless=True`), so it has no OS-drawn resize
border; `#resize-top`/`#resize-right`/etc. in `index.html` are invisible
strips along the edges and corners that `wireResizeHandle()` in `app.js`
wires up by hand, calling `Api.win_resize_to()` on every `pointermove`
(throttled to once per `requestAnimationFrame`) with the target width and
height for whichever edge or corner is being dragged.

`win_resize_to()` calls Win32's `SetWindowPos()` directly on the window's
native handle, with `SWP_NOACTIVATE` and `SWP_NOZORDER` both set, using
the same fix-point math and per-monitor DPI scaling as pywebview's own
`Window.resize()`. Those two flags matter: without them, a resize call
also touches the window's activation and z-order, and WebView2 drops its
own pointer capture the instant its host window's activation state
changes, which a live per-pointermove resize does dozens of times a
second. Falls back to pywebview's own `Window.resize()` (which does not
set those flags) only when no native window handle is available, since
that path gets the final size right but is not safe to call on every
pointermove.

The Now Playing visualizer (`#visualizer` in `index.html`,
`drawSpectrumBars`/`drawOscilloscope` in `app.js`) is a canvas positioned
absolute/`inset:0` over `#nowplaying`, with a transparent background so
only the bars or wave it actually draws are visible. `#artwrap` and
`#wave` are hidden, not just covered, while it's active, since their own
opaque backgrounds would otherwise show through the canvas's undrawn
regions. `#nowplaying` carries an explicit `min-height` for the same
reason: hiding both of its only in-flow children would otherwise collapse
the flex container to zero height, taking the absolutely-positioned
canvas down with it.

One base color, picked from a fully custom picker (`applyTheme()` /
`deriveTheme()` / `deriveNeutrals()` in `app.js` - a saturation/value
square, a hue strip, and hex/RGB fields, no OS dialog involved anywhere),
drives the whole palette. The four accent shades (`--accent`,
`--accent-hi`, `--accent-dim`, `--accent-wash`) scale the picked color's
own saturation and lightness by the ratios the original red theme's four
shades already had to each other, same hue throughout. Eight background
surfaces (`--panel`, `--panel-2`, `--line`, and five more) that were
hand-tuned with their own warm undertone in the original red theme scale
the same way, sharing the picked color's exact hue rather than a fixed
offset from it - an offset reproduced the original red closely but was
confirmed, by sampling actual rendered pixels, to rotate an unrelated
base hue into a different perceived color entirely (a picked teal
reading green, a picked yellow reading reddish). Saturation in both cases
scales by the *picked* color's own saturation, not a fixed constant, or a
fully desaturated pick (black, white, gray) would still come out tinted.

Canvas-drawn elements - the main waveform, spectrum bars, oscilloscope,
tunnel and belt visualizers - cannot read CSS custom properties, so they
read plain JS variables kept in sync by `applyTheme()` instead. Those
variables animate toward a new color over a short
`requestAnimationFrame` loop rather than jumping to it instantly, since
canvas has no built-in transition the way a CSS-driven element gets one
for free; re-targeting mid-animation (continuous dragging in the picker)
picks up from wherever the animation currently is, not the original
start, so a fast drag reads as one continuous fade rather than a series
of snaps. Melt (`vizMode 5`) runs its own independent palette system
entirely and reads none of this.

The album art crop tool (`#artcropmodal` in `index.html`, the `artCrop*`
functions in `app.js`) only ever exports the actual visible image
content, never the fixed-size square workspace it's composed in. Left at
"fit" (the default), the visible content is the whole original image, so
what gets saved is resized so its longer side is 500px, keeping whatever
aspect ratio the source actually has; zooming in narrows the exported
rectangle toward, and eventually to, a real square crop.
`art.prepare_embed_jpeg()` on the Python side re-derives this defensively
rather than trusting the frontend blindly, in case anything else ever
calls it with a non-square source.

Note on paths: whether two strings name the same file is decided in one
place, `paths.key()`. It is `normcase` plus `abspath`, because `pathlib` has
no equivalent of `normcase`, and `Path.resolve()` touches the filesystem,
which on a network share would turn every comparison into a round trip. Two
call sites deliberately stay on `os.path` for speed and say so in a comment;
both are in per-track or per-file loops where `pathlib` measured 5 to 9 times
slower.

Note for anyone editing `api.py`: pywebview builds `window.pywebview.api` by
walking the **public** attributes of that object, and recurses into
non-callables. Anything that is not a method meant for JavaScript needs a
leading underscore. A public reference to the window once made it descend
into `window.dom.document`, which blocks until the page loads, and the API
object was never created at all. `Api.JS_BRIDGE` lists everything JavaScript
may call, `Api.HOST_PUBLIC` lists the host-side entry points that must stay
public, and `_assert_bridge_surface()` runs at startup and refuses to launch
if anything else is public, or if either set names something that is not a
method.

## Modules present but not wired up

`elysian/services/` still contains lyrics and discovery modules from an
earlier version of the player. Both are tested but not constructed at
runtime; nothing currently imports or instantiates them.
