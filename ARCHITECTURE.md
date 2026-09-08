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

## Frontend implementation notes

`ROW_H` in `app.js` and the `.row` height in `style.css` must stay equal, or
rows drift out of line with the scrollbar. Both are 30px. The library's own
track lists are not virtualised the same way the playlist is, since a browse
list is rarely more than a few thousand rows; if that ever needs to hold tens
of thousands of visible rows at once the way the playlist does, it will need
the same treatment.

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
