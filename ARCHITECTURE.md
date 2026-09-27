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

Restoring from that local snapshot still calls `library_note_view` before
returning, even though it skips the backend round trip for the data itself.
It didn't always: an earlier version returned right after restoring the
snapshot, which meant the first visit to a tab in a session persisted
correctly and every visit after that silently didn't, since it always took
the same early return. Whichever tab happened to be visited for the first
time last, not whichever was actually looked at last, is what ended up
remembered — a session that went Albums → Artists → back to Albums (already
visited, so restored from cache, so silently not persisted) reopened on
Artists, not Albums, no matter how long Albums was the last thing on screen
before closing.

Each of the four tabs remembers its own search separately
(`libFilterByView` in `app.js`), and switching tabs sets the one shared
filter box to show whichever search that tab has - empty if it has
never had one - rather than leaving whatever the previous tab had typed
sitting there. That sharing used to be the actual mechanism by which a
search typed on one tab could contaminate a completely different one: a
tab's local snapshot is captured from whatever is on screen the moment
you switch away from it, with no record of what search produced it, so
visiting a different tab while a search was still active silently baked
that filtered (often empty) result into the new tab's own cache as if
it were its ordinary, unfiltered state - clearing the box afterward
fixed only whichever tab happened to be current at that exact moment,
leaving every tab already visited while the search was typed still
contaminated.

Typing in the library filter clears the visible list immediately,
before the debounced backend query fires (`applyLibraryFilter` in
`app.js`), so the interface never sits on a stale result mid-keystroke.
That debounce captures which tab a search belongs to at the moment it
is typed, not only the search text itself - switching tabs before its
120ms delay elapsed used to let it read the current tab fresh only when
it actually fired, landing the search on whichever different tab was
current by then rather than the one it was typed on. A result is
discarded on arrival if the filter text or active tab has moved on
since it was requested. Escape, and a small clear button inside the
filter box itself (visible only once something is typed), both run
that same real, immediate clear - an earlier version of Escape only
blanked the box's own DOM value and re-rendered whatever was already on
screen, without actually re-querying anything, so pressing it did not
really clear the filter. Songs, the one browse surface that can run
into many thousands of rows, uses a much smaller result cap while a
filter is active than while browsing unfiltered, since a large result
there means rebuilding thousands of rows on every settled keystroke.

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
three tabs have no art of their own to fill. The frontend keeps up with
that art independently of Library ever being opened too: `applyLibraryTick`
collects newly-resolved art on every poll tick from app start, not only
once Library has been visited - it used to be gated behind that, which
meant the backend could easily have finished resolving the entire
library's covers before anyone had looked, and the first visit would
still open to a grid of blank cards catching up one at a time, because
nothing had been asking the backend for those results until then.

`_library_browser["view"]` - the single field both the four pre-warm
queries and the frontend's first visit to Library key off "the current
tab" - is seeded from `settings.json`'s own `library_view` at
construction, not hardcoded to Albums: correct before a single query has
run, rather than only being corrected once Library is first opened.
`_do_library_prewarm_all` queries that seeded view first, ahead of the
other three, rather than always starting with Albums regardless of
relevance - whichever tab is actually going to matter gets a real head
start rather than running behind three others in a fixed order.

Each view tracks its own query generation, not one shared counter: firing
all four together must never let one invalidate another's still-in-flight
result, which a single shared counter did - verified directly, it made a
later-scheduled but individually cheap query (Genres, say) sometimes
finish last regardless of its own actual cost, since it was waiting
behind the others under one lock. A `(view, needle)` pending set also
dedupes in-flight queries: a tab switch landing while its own prewarm
query is still running never starts a second one, it just waits on the
one already in flight - and if that in-flight query was only ever a
background prewarm (which never persists anything), the tab switch's own
intent to be remembered escalates it in place rather than being silently
dropped along with the query it was deduped against.

Escalating that flag is not enough on its own to decide which view ends
up persisted, though: two *different* tabs can each have a legitimate
"remember me" query in flight at once - most plainly, switching tabs
again before the previous switch's own query has resolved - and nothing
about the remember flag says which of two different views should win.
A monotonic counter (`_library_view_intent_seq`) does: every call that is
actually asking to be remembered - a real navigation, or the fast local
restore above via `library_note_view` - stamps the counter's current
value, and a result is only allowed to persist as "the current view" if
that stamp still matches the counter by the time the result comes back.
Whichever navigation was most *recently* asked for is therefore the only
one that can win, regardless of which query happens to finish first -
which matters because Songs, by far the biggest table, is consistently
the slowest of the four to resolve, and under the old completion-order-
only rule was consistently the one left standing no matter which tab
anyone had actually switched to.

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

### Sort names

Four extra columns on each track (`title_sort`, `artist_sort`,
`album_sort`, `album_artist_sort`) hold what iTunes calls Sort Name,
Sort Artist, Sort Album and Sort Album Artist - an explicit filing
order that overrides how a name sorts without changing what is
actually displayed, editable on their own Sorting tab in the tag
editor, next to Album Art. They map to ID3's `TSOT`/`TSOP`/`TSOA`/
`TSO2` frames for MP3 and WAV, and to `titlesort`/`artistsort`/
`albumsort`/`albumartistsort` Vorbis comments for FLAC and OGG - the
same tag names iTunes itself reads and writes, verified with real
round-trip write/read tests against actual files in all four formats
rather than assumed from the tag names alone. WAV needed the same
raw-frame fallback `scanner.py` already uses for title/artist/album/
genre, since its easy-interface lookup loses these tags the same way.

`_effective_sort()` in `library.py` is what actually applies one: it
prefers a sort tag, if set, over `sort_key()`'s own guess at the
display name - but the tag itself still passes through `sort_key()`'s
own leading-punctuation strip rather than being trusted verbatim. That
distinction matters: a sort tag being written to reorder something
("Beatles, The") is a separate fact from it having already been
normalised for leading punctuation, and is not always both at once -
plenty of taggers populate the sort field as a plain, untouched copy of
the display name whenever nothing has been manually reordered, symbols
and all, and a name like `"Weird Al" Yankovic`, or an album title
starting with a quote mark, needs that same strip regardless of
whether a sort tag happens to be present. `_ARTIST_SORT_TAG`, a SQL
expression shared by `albums()`, `artists()` and `songs()`, picks
whichever of `album_artist_sort`/`artist_sort` actually corresponds to
the name that ended up displayed, mirroring `_EFFECTIVE_ARTIST`'s own
album_artist-then-artist fallback exactly, so the sort tag used is
never the wrong track-level field's.

A "Various Artists" compilation is deliberately excluded from the
year-based half of `albums()`'s sort key: a real band's own albums are
ordered chronologically on purpose, but a shelf of unrelated
compilations has no such thing as "chronological order" between one
release and the next, and sorting them by year first, falling back to
album name only when two happened to share a year, meant compilations
scattered across decades landed nowhere near their alphabetical
neighbors. Both year-based keys force to a constant for a compilation,
so the album name becomes the actual differentiator, the same way the
rest of the grid already treats a compilation as one thing sorted by
title alone.

Adding these four columns is a schema migration like any other -
`ALTER TABLE` plus a forced rescan to backfill them from the actual
files - see Settings in the README for what that means for an existing
library.

### Scan concurrency

Checking whether a file actually needs re-reading
(`os.path.getmtime()` against what is already indexed) is a network
round trip on a share exactly like opening the file for tags is, and
used to run in a plain sequential loop over every file in a folder,
entirely before the already-concurrent tag-reading pass even started -
paying for that latency one file at a time regardless of how many
workers were free to help. It now runs through the same pool
(`_stat_job` in `library.py`, mirroring `_read_job`'s own never-raises
contract) as the tag reads themselves, for the same reason
`SCAN_WORKERS` is more than one thread to begin with: these threads
spend nearly all their time blocked on I/O, which releases the GIL, so
more of them does not compete for a CPU core the way genuinely
CPU-bound work would - it just means a slow share is waiting on
`SCAN_WORKERS` round trips at once for this check too, not one.

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

One base color, picked from a fully custom picker (`applyTheme()` in
`app.js`, calling `deriveTheme()` / `deriveNeutrals()` in `index.html` - a
saturation/value square, a hue strip, and hex/RGB fields, no OS dialog
involved anywhere), drives the whole palette. The four accent shades
(`--accent`, `--accent-hi`, `--accent-dim`, `--accent-wash`) scale the
picked color's own saturation and lightness by the ratios the original red
theme's four shades already had to each other, same hue throughout. Eight
background surfaces (`--panel`, `--panel-2`, `--line`, and five more) that
were hand-tuned with their own warm undertone in the original red theme
scale the same way, sharing the picked color's exact hue rather than a
fixed offset from it - an offset reproduced the original red closely but
was confirmed, by sampling actual rendered pixels, to rotate an unrelated
base hue into a different perceived color entirely (a picked teal
reading green, a picked yellow reading reddish). Saturation in both cases
scales by the *picked* color's own saturation, not a fixed constant, or a
fully desaturated pick (black, white, gray) would still come out tinted.

`deriveTheme`/`deriveNeutrals` and their supporting color-math functions
live in an early inline script in `index.html`'s `<head>`, not in
`app.js` where the rest of the frontend's own logic lives - deliberately,
so they are available to run before anything in `<body>` paints, applying
the saved color immediately rather than leaving `style.css`'s own
hardcoded default on screen for an instant first. `app.js` still calls
the same functions for the color picker's live preview and for applying
a newly-picked color; a classic `<script>` tag shares one global scope
with everything loaded after it, so this is the one definition, not a
second copy kept in sync by hand. The actual saved color reaches that
early script baked directly into the page text, not fetched: `host.py`
writes a fresh sibling copy of `index.html` on every launch
(`index.boot.html`, next to the original so its relative references to
`style.css`/`app.js` keep working) with a placeholder comment replaced by
the real value, read via `Api.initial_theme_color()` before the window
even exists - there is no bridge call that could beat first paint, since
the entire point is running before the window, and so the bridge to
Python, is up at all.

Canvas-drawn elements - the main waveform, spectrum bars, oscilloscope,
tunnel and belt visualizers - cannot read CSS custom properties, so they
read plain JS variables kept in sync by `applyTheme()` instead. Those
variables also seed from the same baked-in color at load, rather than
the original red default, so the first real tick's own call to
`applyTheme()` (still needed, since the backend is the actual source of
truth and the two are expected to usually agree, not assumed to always)
finds them already at the right value and does not visibly re-animate
canvas colors that had nothing left to change. They animate toward a new
color over a short `requestAnimationFrame` loop rather than jumping to it
instantly otherwise, since canvas has no built-in transition the way a
CSS-driven element gets one for free; re-targeting mid-animation
(continuous dragging in the picker) picks up from wherever the animation
currently is, not the original start, so a fast drag reads as one
continuous fade rather than a series of snaps. Melt (`vizMode 5`) runs its
own independent palette system entirely and reads none of this.

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
