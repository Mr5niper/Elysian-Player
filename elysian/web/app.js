const $ = (id) => document.getElementById(id);
const api = () => window.pywebview && window.pywebview.api;

let state = {
  tracks: [], current_id: -1, playing: false, position: 0, duration: 0,
  volume: 0.8, muted: false, shuffle: false, repeat: "none", peaks: [],
};
let selected = new Set();
let seeking = false;
let draggingId = null;
let view = null;

/* Everything already written to the DOM. Nothing is touched unless the new
   value differs. Re-rendering on every tick is what made buttons unclickable:
   replacing a node between mousedown and mouseup means the browser never
   fires a click at all. */
const prev = {
  playIcon: null, npTitle: null, npArtist: null, art: null,
  status: null, repeatLabel: null, tNow: null, tTotal: null, maximized: null,
  shuffleOn: null, repeatOn: null,
  listSig: null, currentId: null, selSig: null,
  waveW: 0, waveH: 0, waveSig: null,
};

const fmt = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

const esc = (s) => String(s == null ? "" : s)
  .replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function setText(el, key, value) {
  if (prev[key] === value) return;
  prev[key] = value;
  el.textContent = value;
}

function setClass(el, key, cls, on) {
  if (prev[key] === on) return;
  prev[key] = on;
  el.classList.toggle(cls, on);
}

/* ---------- views ---------- */

function setView(name) {
  if (view === name) return;
  view = name;
  $("view-now").classList.toggle("hidden", name !== "now");
  $("view-list").classList.toggle("hidden", name !== "playlists");
  $("view-library").classList.toggle("hidden", name !== "library");
  // Add files and Add folder put tracks in the playlist, so they have
  // nothing to do with the library, which has its own Folders panel.
  document.querySelector(".headicons")
          .classList.toggle("hidden", name === "library");
  $("panel-title").textContent =
    name === "now" ? "Now playing" : (name === "library" ? "Library" : "Playlist");
  document.querySelectorAll(".navitem").forEach((n) =>
    n.classList.toggle("active", n.dataset.view === name));
  if (name === "now") {
    prev.waveW = 0; prev.waveSig = null; drawWave();
    // Runs the exact same "enter this mode" sequence cycleVisualizer
    // uses when double-clicking lands on a mode - not separate,
    // only-supposedly-equivalent logic. vizMode itself was left
    // untouched by navigating away (see the else branch below), and its
    // state was already cleared out the moment we left, not held onto
    // in memory in the meantime - so this now behaves identically to
    // double-clicking back to the same mode from the album-cover view.
    if (vizMode !== 0) enterVisualizerMode();
  } else {
    // Clears every mode's own state immediately on leaving - nothing
    // sits frozen in memory for the entire time this view isn't visible.
    // vizMode itself is left alone, so coming back to "now" still lands
    // on the same mode; it just starts that mode fresh (the normal
    // "not inited yet" path each mode already has) rather than picking
    // back up from state that had been held onto the whole time away.
    resetVisualizerModeState();
    clearTimeout(vizTimer);
  }
  // While hidden the list has no height, so its row window was computed
  // against a fallback. Recompute against the real height now that it shows,
  // covering a window resized while the Now Playing view was up.
  if (name === "playlists") renderWindow(false);
  // Ask for the pane's contents the first time it is opened, rather than
  // querying a database nobody is looking at on startup.
  if (name === "library") libraryOpened();
  else { libShowFolders = false; libConfirmRemove = null; }
}

document.querySelectorAll(".navitem").forEach((n) =>
  n.addEventListener("click", () => setView(n.dataset.view)));

/* ---------- track list (virtualised) ----------
   Only the rows on screen exist in the DOM. A 10,000 track playlist keeps
   about thirty row elements, so switching to Playlists is instant regardless
   of library size. ROW_H must match .row height in style.css. */

const ROW_H = 30;
const OVERSCAN = 8;
let rowRange = { first: -1, last: -1 };
let rendered = new Map();   // id -> row element

function visibleTracks() {
  const needle = $("filter").value.trim().toLowerCase();
  if (!needle) return state.tracks;
  return state.tracks.filter((t) =>
    `${t.title} ${t.artist} ${t.album} ${t.name}`.toLowerCase().includes(needle));
}

/* Which rows exist, in order. Derived from the filtered set, not the whole
   playlist: a tag scan can make a track start matching an active filter, and
   the row set then changes without any id being added or removed. */
function structureSignature(rows) {
  return rows.map((t) => t.id).join(",");
}

let filtered = [];

function renderList(force) {
  // Always refresh the working list. Holding the previous track objects here
  // meant a later tag scan never reached the rows: the ids do not change, so
  // artist, album and duration stayed blank.
  filtered = visibleTracks();
  const sig = structureSignature(filtered);
  if (force || sig !== prev.listSig) {
    prev.listSig = sig;
    resetScanScheduling();
    $("sizer").style.height = (filtered.length * ROW_H) + "px";
    rowRange = { first: -1, last: -1 };
    rendered.forEach((el) => el.remove());
    rendered.clear();
    $("empty").style.display = state.tracks.length ? "none" : "flex";
    $("tracks").style.display = state.tracks.length ? "block" : "none";
  }
  renderWindow(force);
}

function renderWindow(force) {
  // Never rebuild rows mid-drag; the element under the pointer must survive.
  if (draggingId !== null && !force) return;

  const box = $("tracks");
  const top = box.scrollTop;
  const height = box.clientHeight || 400;
  let first = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
  let last = Math.min(filtered.length - 1,
                      Math.ceil((top + height) / ROW_H) + OVERSCAN);

  if (!force && first === rowRange.first && last === rowRange.last) {
    updateRowText();
    paintRowStates();
    return;
  }
  rowRange = { first, last };

  const wanted = new Set();
  const sizer = $("sizer");
  const posById = new Map();
  state.tracks.forEach((t, i) => posById.set(t.id, i + 1));

  for (let i = first; i <= last; i++) {
    const t = filtered[i];
    if (!t) continue;
    wanted.add(t.id);
    let row = rendered.get(t.id);
    if (!row) {
      row = document.createElement("div");
      row.className = "row";
      row.dataset.id = t.id;
      row.draggable = true;
      for (const cls of ["r-num", "r-title", "r-artist", "r-album", "r-time"]) {
        const d = document.createElement("div");
        d.className = cls;
        row.appendChild(d);
      }
      sizer.appendChild(row);
      rendered.set(t.id, row);
    }
    row.style.top = (i * ROW_H) + "px";
    row.dataset.pos = posById.get(t.id);
    writeRow(row, t);
  }

  rendered.forEach((el, id) => {
    if (!wanted.has(id)) { el.remove(); rendered.delete(id); }
  });

  // These rows may belong to an entirely different queue than the last
  // paint saw - playing a track from the library while this view was
  // hidden replaces it, and a poll tick in the meantime already updated
  // prev.currentId to match before any row for it existed. Without this,
  // paintRowStates would see nothing has "changed" and skip painting the
  // very rows that were just built, leaving the playing highlight missing
  // until something else (like a click) forced a repaint.
  prev.selSig = null;
  prev.currentId = null;
  paintRowStates();
  requestScanVisible();
}

function writeRow(row, t) {
  const c = row.children;
  const playing = t.id === state.current_id;
  const num = playing ? "\u25B6" : String(row.dataset.pos);
  const time = t.length ? fmt(t.length) : "";
  if (c[0].textContent !== num) c[0].textContent = num;
  if (c[1].textContent !== t.title) c[1].textContent = t.title;
  if (c[2].textContent !== t.artist) c[2].textContent = t.artist;
  if (c[3].textContent !== t.album) c[3].textContent = t.album;
  if (c[4].textContent !== time) c[4].textContent = time;
}

/* ---------- tag scheduling ----------
   Tags are read over what may be a network share, so only the rows on screen
   are fetched eagerly. That leaves the reader idle between screenfuls, which
   is wasted time: the rest of the list is filled with the leftover capacity.

   Priorities, so prefetching never delays something you are looking at:
     visible   rows currently rendered
     ahead     rows just past the edge you are scrolling toward
     prefetch  the rest, sweeping outward from the view, once scrolling stops

   The queue is topped up only when it runs low, rather than having the whole
   playlist dumped into it. */

/* Two water marks, because the two kinds of prefetch compete. The idle sweep
   fills the queue while you sit still; when you then start scrolling, work
   ahead of you must still be able to get in even though the queue is full.
   It outranks the sweep in the reader, so it is only the gate that needs
   raising. */
const SCAN_LOW_WATER = 40;
const AHEAD_WATER = 140;
/* Background chunks stay small: a long scroll makes queued sweep work
   useless, and this bounds how many reads are wasted on rows nobody will
   look at. */
const SCAN_CHUNK = 32;
const AHEAD_CHUNK = 64;
const SCROLL_IDLE_MS = 180;
const SCAN_PUMP_MS = 250;

/* Outstanding work is tracked locally rather than read from the tick. The
   poll drops to once a second when paused, which is far too slow to keep a
   reader fed or to notice the scroll direction before it expires. */
let scanOutstanding = 0;

let scanRequested = new Map();   // id -> priority it was asked for at
let scrollDir = 0;              // -1 up, +1 down, 0 settled
let scrollIdleTimer = 0;
let sweepUp = -1;
let sweepDown = -1;
let scanTimer = 0;
let prefetchAnchor = -1;        // where the view was when prefetch was planned

function markScrolling(dir) {
  scrollDir = dir;
  /* If the view has run well past what was queued, that work is for rows
     nobody is going to look at. On a share each one costs a round trip, so
     throw it away and re-plan from here. */
  if (prefetchAnchor >= 0 &&
      Math.abs(rowRange.first - prefetchAnchor) > (rowRange.last - rowRange.first + 1) * 2) {
    const a = api();
    // Guarded: this runs inside the scroll handler, so anything that throws
    // here would stop the list scrolling at all.
    if (a && typeof a.drop_prefetch === "function") {
      try { a.drop_prefetch(); } catch (e) { /* not fatal */ }
      scanOutstanding = 0;
      forgetPrefetchRequests();
      sweepUp = rowRange.first - 1;
      sweepDown = rowRange.last + 1;
      prefetchAnchor = rowRange.first;
    }
  }
  clearTimeout(scrollIdleTimer);
  scrollIdleTimer = setTimeout(() => {
    scrollDir = 0;
    // Re-anchor the outward sweep wherever the view came to rest.
    sweepUp = rowRange.first - 1;
    sweepDown = rowRange.last + 1;
  }, SCROLL_IDLE_MS);
}

/* Returns rows worth asking for at this priority. A row already queued for
   prefetch is returned again when it becomes visible, so it can be upgraded
   and jump the queue. Otherwise it stays stuck behind the whole background
   sweep, which is what made scrolling ahead of the fill feel like waiting. */
function take(indices, priority) {
  const ids = [];
  for (const i of indices) {
    const t = filtered[i];
    if (!t || t.scanned) continue;
    const asked = scanRequested.get(t.id);
    if (asked !== undefined && asked <= priority) continue;
    ids.push(t.id);
    scanRequested.set(t.id, priority);
  }
  return ids;
}

/* Forget every prefetch request so it can be made again later. Paired with
   drop_prefetch, which throws the same work out of the reader's queue. */
function forgetPrefetchRequests() {
  for (const [id, prio] of Array.from(scanRequested)) {
    if (prio > 0) scanRequested.delete(id);
  }
}

/* Nothing on screen may wait for anything else. Throws away all queued
   prefetch and gives the whole reader to the visible rows. */
/* True if something on screen is blank and has not already been asked for at
   top priority. Used to avoid resetting the queue while it is already busy
   with exactly the right rows. Doing that every 250ms threw away the work
   in flight and started it over. */
function visibleNeedsRequeue() {
  for (const i of visibleIndices()) {
    const t = filtered[i];
    if (t && !t.scanned && scanRequested.get(t.id) !== 0) return true;
  }
  return false;
}

function fillVisibleNow(a) {
  if (!visibleNeedsRequeue()) return 0;
  const wanted = visibleIndices();

  /* Empty the reader's queue outright. Scrolling past a screen leaves its
     rows queued, and those were asked for at visible priority too, so after a
     long scroll the screen you actually stopped on sits behind hundreds of
     them. Nothing that is not on screen right now has any claim. */
  if (typeof a.reset_scan_queue === "function") {
    try { a.reset_scan_queue(); } catch (e) { /* not fatal */ }
  }
  scanRequested = new Map();
  const ids = take(wanted, 0);
  if (!ids.length) return 0;
  scanOutstanding = ids.length;
  a.request_scan(ids);
  schedule();
  return ids.length;
}

function visibleIndices() {
  const idx = [];
  for (let i = rowRange.first; i <= rowRange.last; i++) idx.push(i);
  return idx;
}

/* Rows on screen. Always first, always immediately. */
function requestScanVisible() {
  clearTimeout(scanTimer);
  scanTimer = setTimeout(() => {
    const a = api();
    if (!a) return;
    fillVisibleNow(a);
  }, 30);
}

/* Spend whatever the reader is not using. Driven from the poll loop, which
   carries how much work is still queued. */
function send(a, ids, kind) {
  if (!ids.length) return 0;
  scanOutstanding += ids.length;
  if (kind === "visible") a.request_scan(ids);
  else if (kind === "ahead") a.request_ahead(ids);
  else a.request_prefetch(ids);
  return ids.length;
}

function topUpScan() {
  const a = api();
  if (!a || !filtered.length) return;

  // Absolute rule: while anything on screen is blank, the reader does
  // nothing else. No prefetch of any kind is queued until it is complete.
  if (visibleMissing()) { fillVisibleNow(a); return; }

  if (scrollDir !== 0) {
    if (scanOutstanding >= AHEAD_WATER) return;
    // Moving: work only the edge being scrolled toward.
    const idx = [];
    if (scrollDir > 0) {
      let i = Math.max(rowRange.last + 1, sweepDown);
      for (; i < filtered.length && idx.length < AHEAD_CHUNK; i++) idx.push(i);
      sweepDown = i;
    } else {
      let i = Math.min(rowRange.first - 1, sweepUp);
      for (; i >= 0 && idx.length < AHEAD_CHUNK; i--) idx.push(i);
      sweepUp = i;
    }
    prefetchAnchor = rowRange.first;
    send(a, take(idx, 1), "ahead");
    return;
  }

  if (scanOutstanding >= SCAN_LOW_WATER) return;

  // Settled: sweep outward both ways at once, nearest rows first.
  if (sweepDown < 0 && sweepUp < 0) {
    sweepUp = rowRange.first - 1;
    sweepDown = rowRange.last + 1;
  }
  const idx = [];
  while (idx.length < SCAN_CHUNK &&
         (sweepDown < filtered.length || sweepUp >= 0)) {
    if (sweepDown < filtered.length) idx.push(sweepDown++);
    if (idx.length >= SCAN_CHUNK) break;
    if (sweepUp >= 0) idx.push(sweepUp--);
  }
  prefetchAnchor = rowRange.first;
  send(a, take(idx, 2), "prefetch");
}

setInterval(topUpScan, SCAN_PUMP_MS);

function resetScanScheduling() {
  scanRequested = new Map();
  scanOutstanding = 0;
  prefetchAnchor = -1;
  sweepUp = -1;
  sweepDown = -1;
}

function updateRowText() {
  const byId = new Map(filtered.map((t) => [t.id, t]));
  rendered.forEach((row, id) => {
    const t = byId.get(id);
    if (t) writeRow(row, t);
  });
}

function paintRowStates() {
  const selSig = Array.from(selected).sort().join(",");
  if (selSig === prev.selSig && state.current_id === prev.currentId) return;
  prev.selSig = selSig;
  prev.currentId = state.current_id;
  rendered.forEach((row, id) => {
    const playing = id === state.current_id;
    row.classList.toggle("playing", playing);
    row.classList.toggle("selected", selected.has(id));
    const want = playing ? "\u25B6" : String(row.dataset.pos);
    if (row.children[0].textContent !== want) row.children[0].textContent = want;
  });
}

let scrollPending = false;
let lastScrollTop = 0;
$("tracks").addEventListener("scroll", () => {
  const top = $("tracks").scrollTop;
  if (top !== lastScrollTop) {
    markScrolling(top > lastScrollTop ? 1 : -1);
    lastScrollTop = top;
  }
  if (scrollPending) return;
  scrollPending = true;
  requestAnimationFrame(() => { scrollPending = false; renderWindow(false); });
});

/* Selection follows Explorer: a plain click replaces the selection and sets
   the anchor, ctrl toggles one row and moves the anchor, shift takes the run
   from the anchor to the clicked row, and ctrl+shift adds that run to what is
   already selected. The anchor deliberately does not move on a shift click,
   so the run can be widened and narrowed from the same starting point.

   Ranges are worked out from the filtered list, not from the rendered rows:
   only the visible window exists in the DOM, so the rows in between may not
   be there. */
let selectionAnchor = null;

/* ---------- confirm dialog ---------- */
let modalYes = null;

function openConfirm(text, onYes) {
  modalYes = onYes;
  $("modal-text").textContent = text;
  $("modal").classList.add("show");
  // Focus the safe answer, so a stray Enter or Space declines.
  $("modal-no").focus();
}

function closeConfirm() {
  modalYes = null;
  $("modal").classList.remove("show");
}

function modalOpen() {
  return $("modal").classList.contains("show");
}

/* Swap the speaker glyph by display, never by replacing nodes: a node
   replaced between mousedown and mouseup means the browser fires no click. */
function paintMuteIcon(muted) {
  if (prev.muted === muted) return;
  prev.muted = muted;
  $("vol-waves").style.display = muted ? "none" : "";
  $("vol-muted").style.display = muted ? "" : "none";
  $("volicon").classList.toggle("muted", muted);
  $("volicon").setAttribute("title", muted ? "Unmute" : "Mute");
}

function rangeIds(fromId, toId) {
  let a = -1, b = -1;
  for (let i = 0; i < filtered.length; i++) {
    if (filtered[i].id === fromId) a = i;
    if (filtered[i].id === toId) b = i;
  }
  if (a < 0 || b < 0) return null;
  if (a > b) { const t = a; a = b; b = t; }
  const ids = [];
  for (let i = a; i <= b; i++) ids.push(filtered[i].id);
  return ids;
}

$("tracks").addEventListener("click", (e) => {
  const row = e.target.closest(".row");
  if (!row) return;
  const id = Number(row.dataset.id);

  if (e.shiftKey) {
    const run = selectionAnchor === null ? null : rangeIds(selectionAnchor, id);
    if (run) {
      if (!e.ctrlKey) selected = new Set();
      run.forEach((x) => selected.add(x));
    } else {
      // No usable anchor, so behave like a plain click.
      selected = new Set([id]);
      selectionAnchor = id;
    }
  } else if (e.ctrlKey) {
    selected.has(id) ? selected.delete(id) : selected.add(id);
    selectionAnchor = id;
  } else {
    selected = new Set([id]);
    selectionAnchor = id;
  }
  paintRowStates();
});

$("tracks").addEventListener("dblclick", (e) => {
  const row = e.target.closest(".row");
  if (row) intent.playTrack(Number(row.dataset.id));
});

/* ---------- reordering rows ---------- */

$("tracks").addEventListener("dragstart", (e) => {
  const row = e.target.closest(".row");
  if (!row) return;
  draggingId = Number(row.dataset.id);
  e.dataTransfer.effectAllowed = "move";
  try { e.dataTransfer.setData("text/plain", String(draggingId)); } catch (_) {}
});

$("tracks").addEventListener("dragover", (e) => {
  if (draggingId === null) return;
  e.preventDefault();
  e.stopPropagation();
  const row = e.target.closest(".row");
  const current = $("sizer").querySelector(".row.dragover");
  if (current !== row) {
    if (current) current.classList.remove("dragover");
    if (row) row.classList.add("dragover");
  }
});

$("tracks").addEventListener("drop", (e) => {
  if (draggingId === null) return;
  e.preventDefault();
  e.stopPropagation();
  const row = e.target.closest(".row");
  const current = $("sizer").querySelector(".row.dragover");
  if (current) current.classList.remove("dragover");
  if (row) api().reorder(draggingId, Number(row.dataset.id));
  draggingId = null;
});

$("tracks").addEventListener("dragend", () => {
  draggingId = null;
  renderWindow(true);
  const current = $("sizer").querySelector(".row.dragover");
  if (current) current.classList.remove("dragover");
});

/* ---------- dropping files from Explorer ----------
   Handled entirely here. Python subscribes only to the final drop, to read
   pywebviewFullPath. Bouncing every dragover across the bridge to toggle a
   CSS class is what made the overlay strobe.
   dragleave also fires when the pointer crosses into a child element, so a
   naive show/hide flickers; counting enters against leaves fixes it. */
let dragDepth = 0;

function fileDrag(e) {
  return draggingId === null && e.dataTransfer &&
    Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") !== -1;
}

window.addEventListener("dragenter", (e) => {
  if (!fileDrag(e)) return;
  e.preventDefault();
  if (++dragDepth === 1) $("drophint").classList.add("show");
});
window.addEventListener("dragover", (e) => { if (fileDrag(e)) e.preventDefault(); });
window.addEventListener("dragleave", (e) => {
  if (!fileDrag(e)) return;
  if (--dragDepth <= 0) { dragDepth = 0; $("drophint").classList.remove("show"); }
});
window.addEventListener("drop", (e) => {
  if (draggingId !== null) return;
  e.preventDefault();
  dragDepth = 0;
  $("drophint").classList.remove("show");
});

/* ---------- sliders ---------- */

function paint(el, v) {
  const pctText = (v * 100).toFixed(2) + "%";
  const fill = el.querySelector(".fill");
  if (fill.style.width === pctText) return;
  fill.style.width = pctText;
  el.querySelector(".knob").style.left = pctText;
}

function bindSlider(el, onCommit, onDrag) {
  const pct = (e) => {
    const r = el.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  };
  let down = false;
  el.addEventListener("mousedown", (e) => {
    down = true; const v = pct(e); paint(el, v); onDrag && onDrag(v); e.preventDefault();
    schedule();          // a drag must track the pointer, not the idle rate
  });
  window.addEventListener("mousemove", (e) => {
    if (!down) return; const v = pct(e); paint(el, v); onDrag && onDrag(v);
  });
  window.addEventListener("mouseup", (e) => {
    if (!down) return; down = false; onCommit(pct(e));
  });
}

bindSlider($("seek"),
  (v) => { seeking = false; intent.seekTo(v * state.duration); },
  (v) => { seeking = true; $("t-now").textContent = fmt(v * state.duration); prev.tNow = null; });

let volTimer = 0;
let volHeld = false;
bindSlider($("vol"),
  (v) => { volHeld = false; intent.setVolume(v); },
  (v) => {
    volHeld = true;
    state.volume = v;
    clearTimeout(volTimer);
    volTimer = setTimeout(() => api().set_volume(v), 50);
  });

/* ---------- controls ----------
   Every action goes through `intent`, so a keypress and a click produce
   exactly the same local update before the command is posted. Previously only
   the click handlers updated optimistically, which made the mouse feel
   instant and the keyboard feel like it lagged by up to a poll interval. */

/* An optimistic change is only true locally until the backend agrees. A poll
   landing in between used to overwrite it and snap the control back, so a
   press looked like it did nothing and then happened twice. Each field is
   held at its predicted value until the snapshot reports the same thing, or
   the wait times out. */
const pending = {};
const PENDING_MS = 1500;

function predict(field, value) {
  pending[field] = { value, until: Date.now() + PENDING_MS };
  // The next poll interval is chosen when a poll ends, so a press made during
  // a slow idle wait would otherwise sit unconfirmed for up to a second.
  schedule();
}

/* Position is continuous, so it cannot be compared for equality. It is held
   at the predicted value until the backend reports something near it. */
let posPredict = null;

function predictPosition(seconds) {
  posPredict = { value: Math.max(0, seconds), until: Date.now() + PENDING_MS };
  schedule();
}

function settledPosition(incoming) {
  if (!posPredict) return incoming;
  if (Math.abs(incoming - posPredict.value) < 1.5 || Date.now() > posPredict.until) {
    posPredict = null;
    return incoming;
  }
  return posPredict.value;
}

function settled(field, incoming) {
  const p = pending[field];
  if (!p) return incoming;
  if (p.value === incoming || Date.now() > p.until) {
    delete pending[field];
    return incoming;
  }
  return p.value;
}

const intent = {
  togglePlay() {
    const playing = !state.playing;
    predict("playing", playing);
    state.playing = playing;
    const d = playing ? "M6 4h4v16H6zM14 4h4v16h-4z" : "M7 4l13 8-13 8z";
    prev.playIcon = d;
    $("play-path").setAttribute("d", d);
    $("play").title = playing ? "Pause" : "Play";
    api().toggle_play();
  },

  shuffle() {
    state.shuffle = !state.shuffle;
    predict("shuffle", state.shuffle);
    prev.shuffleOn = state.shuffle;
    $("shuffle").classList.toggle("on", state.shuffle);
    api().toggle_shuffle();
  },

  mute() {
    state.muted = !state.muted;
    predict("muted", state.muted);
    paintMuteIcon(state.muted);
    api().toggle_mute();
  },

  repeat() {
    const order = { none: "all", all: "one", one: "none" };
    state.repeat = order[state.repeat] || "all";
    predict("repeat", state.repeat);
    prev.repeatOn = state.repeat !== "none";
    prev.repeatLabel = state.repeat === "one" ? "Repeat one" : "Repeat";
    $("repeat").classList.toggle("on", prev.repeatOn);
    $("repeat-label").textContent = prev.repeatLabel;
    api().cycle_repeat();
  },

  setVolume(v) {
    v = Math.max(0, Math.min(1, v));
    state.volume = v;
    paint($("vol"), v);
    api().set_volume(v);
  },

  volumeBy(delta) { intent.setVolume(state.volume + delta); },

  seekTo(seconds) {
    if (state.duration <= 0) return;
    const v = Math.max(0, Math.min(1, seconds / state.duration));
    state.position = v * state.duration;
    predictPosition(state.position);
    paint($("seek"), v);
    prev.tNow = null;
    setText($("t-now"), "tNow", fmt(state.position));
    api().seek(state.position);
  },

  seekBy(delta) { intent.seekTo(state.position + delta); },

  // A track change has no local answer for "which track", but the transport
  // can be reset immediately so the press visibly registers.
  next() { intent._resetTransport(); api().next_track(); },
  previous() { intent._resetTransport(); api().previous(); },

  _resetTransport() {
    predictPosition(0);
    state.position = 0;
    paint($("seek"), 0);
    prev.tNow = null;
    setText($("t-now"), "tNow", "0:00");
  },

  playTrack(id) { intent._resetTransport(); api().play_id(id); },

  toggleMaximise() {
    const maxed = !state.maximized;
    state.maximized = maxed;
    predict("maximized", maxed);
    prev.maximized = maxed;
    $("max-box").style.display = maxed ? "none" : "";
    $("max-restore").style.display = maxed ? "" : "none";
    $("win-max").title = maxed ? "Restore" : "Maximise";
    // Growing or shrinking is certain here - unlike a plain drag-resize,
    // which has to guess from window area - so the resize this triggers
    // does not have to guess either. Consumed once by that resize's
    // debounced callback, then cleared, so an unrelated drag afterward
    // still falls back to the area comparison.
    libMaximizeToggleGrew = maxed;
    api().win_maximise();
  },

  removeSelected() {
    if (!selected.size) return;
    api().remove(Array.from(selected));
    selected.clear();
    paintRowStates();
  },
  clearPlaylist() {
    if (!state.tracks.length) return;
    openConfirm("Clear the entire playlist?", () => {
      selected.clear();
      selectionAnchor = null;
      api().clear_playlist();
      paintRowStates();
    });
  },
};

const wire = (id, fn) => $(id).addEventListener("click", (e) => { e.preventDefault(); fn(); });

/* Frameless live resize.
   This calls win_resize_to() on every pointermove, throttled to once per
   animation frame - a genuine, live resize of the real window, the same
   as any ordinary Windows app. Nothing else is drawn anywhere; the
   window's own content is what changes shape, in place, as the mouse
   moves.

   An earlier version of this deferred the real resize to pointerup only,
   because calling it live used to make the window occasionally detach
   from the cursor mid-drag and reattach somewhere else - sometimes off
   screen. That turned out to be a real bug in pywebview's own
   Window.resize() (confirmed by reading webview/platforms/winforms.py
   directly): its SetWindowPos() call passes flags=64 (SWP_SHOWWINDOW)
   and nothing else, so every resize call also nudged this window's
   activation and z-order, and WebView2 - Chromium underneath - drops its
   own internal pointer capture the instant its host window's activation
   changes. See the docstring on Api.win_resize_to in api.py for the
   fix: it now calls SetWindowPos() directly with SWP_NOACTIVATE and
   SWP_NOZORDER, so a resize never touches activation or z-order at all.
   With the real cause fixed, live resizing here is safe again, and there
   is no need for any separate preview to fake it.
   700/420 match config.MIN_WIDTH/MIN_HEIGHT on the Python side, not
   arbitrary - kept as literals since JS has no access to that config
   directly, but intentionally the same numbers, not a coincidence. */
const RESIZE_MIN_W = 700;
const RESIZE_MIN_H = 420;
let resizeRaf = 0;
let resizePending = null;

function clampResizeSize(w, h) {
  return {
    width: Math.max(RESIZE_MIN_W, Math.round(w || 0)),
    height: Math.max(RESIZE_MIN_H, Math.round(h || 0)),
  };
}

function flushResize(edge) {
  resizeRaf = 0;
  const a = api();
  if (!a || !resizePending) return;
  a.win_resize_to(edge, resizePending.width, resizePending.height);
}

/* Coalesced to at most one call per animation frame: a fast real drag
   fires far more pointermove events than the window can actually redraw
   in response to, and there is no reason to cross the IPC bridge (let
   alone touch Win32) more often than that. */
function scheduleResize(edge, size) {
  resizePending = size;
  if (!resizeRaf) resizeRaf = requestAnimationFrame(() => flushResize(edge));
}

function wireResizeHandle(id) {
  const el = $(id);
  if (!el) return;
  const edge = el.dataset.edge || "";
  const growsRight = edge.includes("right");
  const growsLeft = edge.includes("left");
  const growsDown = edge.includes("bottom");
  const growsUp = edge.includes("top");

  const start = async (e) => {
    e.preventDefault();
    e.stopPropagation();

    const a = api();
    if (!a || typeof a.win_resize_to !== "function") return;

    let geom = { width: window.outerWidth, height: window.outerHeight };
    if (typeof a.win_geometry === "function") {
      try {
        const got = await a.win_geometry();
        if (got && got.width && got.height) geom = got;
      } catch (_) {
        // fall back to outerWidth/outerHeight
      }
    }

    const startX = e.screenX;
    const startY = e.screenY;
    const startW = geom.width || window.outerWidth;
    const startH = geom.height || window.outerHeight;
    let alive = true;

    const onMove = (ev) => {
      if (!alive) return;
      const dx = ev.screenX - startX;
      const dy = ev.screenY - startY;

      let w = startW;
      let h = startH;
      if (growsRight) w = startW + dx;
      if (growsLeft) w = startW - dx;
      if (growsDown) h = startH + dy;
      if (growsUp) h = startH - dy;

      scheduleResize(edge, clampResizeSize(w, h));
    };

    const finish = () => {
      if (!alive) return;
      alive = false;
      try { el.releasePointerCapture(e.pointerId); } catch (_) {}
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
      if (resizeRaf) { cancelAnimationFrame(resizeRaf); resizeRaf = 0; }
      resizePending = null;
      document.body.classList.remove("resizing");
    };

    const onUp = () => finish();
    const onCancel = () => finish();

    document.body.classList.add("resizing");
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
  };

  el.addEventListener("pointerdown", start);
}
wire("play", () => intent.togglePlay());
wire("prev", () => intent.previous());
wire("next", () => intent.next());
wire("shuffle", () => intent.shuffle());
wire("repeat", () => intent.repeat());
wire("ic-add", () => api().add_files());
wire("ic-folder", () => api().add_folder());
wire("btn-load", () => api().load_m3u());
wire("btn-save", () => api().save_m3u());
wire("btn-clear", () => intent.clearPlaylist());
wire("volicon", () => intent.mute());
wire("modal-yes", () => {
  const fn = modalYes;
  closeConfirm();
  if (fn) fn();
});
wire("modal-no", () => closeConfirm());
// Clicking the dimmed backdrop declines, like pressing Escape.
$("modal").addEventListener("click", (e) => {
  if (e.target === $("modal")) closeConfirm();
});
wire("win-min", () => api().win_minimise());
wire("win-max", () => intent.toggleMaximise());
wire("win-close", () => api().win_close());

wireResizeHandle("resize-top");
wireResizeHandle("resize-right");
wireResizeHandle("resize-bottom");
wireResizeHandle("resize-left");
wireResizeHandle("resize-tl");
wireResizeHandle("resize-tr");
wireResizeHandle("resize-br");
wireResizeHandle("resize-bl");

$("titlebar").addEventListener("dblclick", (e) => {
  if (e.target.closest(".winbtn")) return;
  intent.toggleMaximise();
});

$("filter").addEventListener("input", () => renderList(true));

/* True for anything the keyboard should be going into rather than the
   player. Covers text fields, textareas, selects and contenteditable, so a
   field added later is exempt without anyone having to remember to add it
   here. Buttons, checkboxes and sliders are not: those want the arrow and
   space keys to mean what the browser makes them mean. */
const NOT_TYPING = new Set([
  "button", "checkbox", "color", "file", "hidden", "image",
  "radio", "range", "reset", "submit",
]);

function isTypingTarget(el) {
  if (!el || !el.tagName) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName.toUpperCase();
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  return !NOT_TYPING.has((el.type || "text").toLowerCase());
}

document.addEventListener("keydown", (e) => {
  if (tagModalOpen()) {
    if (e.key === "Escape") { e.preventDefault(); closeTagEditor(); }
    return;
  }
  if (modalOpen()) {
    // The dialog owns the keyboard: Escape declines, and Enter or Space
    // activate whichever button holds focus (the browser default). Nothing
    // falls through, or Space would toggle playback behind the dialog.
    if (e.key === "Escape") { e.preventDefault(); closeConfirm(); }
    return;
  }
  // Anything being typed into owns the keyboard. Naming the one filter
  // box meant every field added later, the library filter among them,
  // silently fired the transport shortcuts: a space in a search box
  // paused the music and the arrows seeked.
  if (isTypingTarget(e.target)) {
    if (e.key === "Escape") {
      const field = e.target;
      field.value = "";
      field.blur();
      if (field === $("filter")) renderList(true);
      else if (field === $("libfilter")) renderLibrary();
    }
    return;
  }
  const k = e.key;
  if (k === " ") { e.preventDefault(); intent.togglePlay(); }
  else if (k === "ArrowLeft") {
    e.preventDefault();
    e.ctrlKey ? intent.previous() : intent.seekBy(-5);
  }
  else if (k === "ArrowRight") {
    e.preventDefault();
    e.ctrlKey ? intent.next() : intent.seekBy(5);
  }
  else if (k === "ArrowUp") { e.preventDefault(); intent.volumeBy(0.05); }
  else if (k === "ArrowDown") { e.preventDefault(); intent.volumeBy(-0.05); }
  else if (k === "Delete" && e.ctrlKey && e.shiftKey) {
    e.preventDefault();
    intent.clearPlaylist();
  }
  else if (k === "Delete") { intent.removeSelected(); }
  else if (k === "Enter") {
    /* Enter is also the default activation key for whatever control has
       keyboard focus. Tabbing to Shuffle and pressing Enter used to both
       toggle shuffle and restart the selected row from here, because the
       button's own click and this shortcut fired on the same keydown.
       Play-selected is a playlist shortcut, so it only applies when focus
       is not sitting on a control. Space stays global on purpose: a click
       leaves the clicked button focused, and Space after clicking Shuffle
       should still mean play/pause, not shuffle again. */
    const t = e.target;
    const tag = t && t.tagName ? t.tagName.toLowerCase() : "";
    const onControl = tag === "button" || tag === "input" ||
        tag === "select" || tag === "textarea" ||
        !!(t && t.closest && t.closest(".slider, .winbtn"));
    if (!onControl && selected.size) intent.playTrack(Array.from(selected)[0]);
  }
  else if (k === "/") { e.preventDefault(); setView("playlists"); $("filter").focus(); }
  else if (e.ctrlKey && (k === "i" || k === "I") && view === "library") {
    e.preventDefault();
    const paths = libEditablePaths();
    if (paths.length) openTagEditor(paths);
  }
});


/* ---------- library ----------
   Browsing never blocks: the frontend asks for a view, Python runs the
   query off its worker, and the result is collected on a revision bump
   the same way playlist metadata already is. Three counters rather than
   one, so opening an album does not refetch the album grid and a finished
   scan does not refetch either unless it changed something.

   The grid is not virtualised like the track list. A library holds
   thousands of albums where a playlist holds tens of thousands of rows,
   and content-visibility keeps offscreen cards off the layout budget
   without the machinery that the track list genuinely needs. */

let libView = "albums";          // albums | artists | genres | songs
let libItems = [];               // browser results, unfiltered
let libDetail = null;            // {kind, key, title, items} when drilled in
let libSelected = new Set();     // paths selected in a detail list
let libAnchor = null;            // where a shift range measures from
let libOpened = false;
let libRevision = -1;
// Requests made but not yet collected. The poll drops to once a second
// when paused, which is exactly when someone is browsing, so a click on
// Artists could sit for a full second before anything appeared.
let libPending = 0;
let libScanning = false;
let libArt = {};                 // "artist\u0000album" -> data url or ""
let libArtSeq = 0;               // last sequence collected from the backend
let libArtRevision = -1;
let libArtSeen = new Set();      // already asked for, so no repeats
let libCards = new Map();        // album key -> its card element
let libGridSig = "";             // what the grid currently shows
let libArtWaiting = false;       // covers asked for but not yet collected

// The path of the track currently playing, and where to find its row
// without walking the list. The songs and detail lists are not
// virtualised the way the playlist is, so scanning every row on every
// tick - as a large library easily has tens of thousands of them - would
// cost real time for something that changes once per track. A map from
// path to element, rebuilt only when the rows themselves change, keeps
// showing or moving the indicator to two lookups regardless of list size.
let libPlayingPath = "";
let libPlayingRow = null;
let libRowsByPath = new Map();

/* Tag editor. The backend is the source of truth for what is actually in
   the file, same as everywhere else in this app; libEditor is just the
   last snapshot of that. libEditorTouched is purely local: which fields
   the person has actually typed into since the modal opened, so Save can
   send only those. Without it, every field would be sent on every save,
   and a batch edit of ten tracks that differ on nothing but genre would
   silently overwrite all nine other fields with whatever the (blank,
   "multiple values") box happened to show. */
let libEditorRevision = -1;
let libEditor = { open: false, loading: false, saving: false, paths: [],
                  count: 0, data: {}, mixed: {}, errors: [], saved: 0,
                  failed: 0 };
let libEditorTouched = new Set();

/* Album art editing. pendingArtDataUrl is the final cropped square JPEG
   once the person confirms a crop; null means no art change is pending.
   The crop tool itself works on a fixed-resolution square canvas (backing
   store ART_EXPORT_SIZE, displayed smaller via CSS at ART_DISPLAY_SIZE),
   drawing the source image at a "cover" scale (its shorter side exactly
   fills the square) times whatever the zoom slider adds on top, panned by
   dragging. What's actually painted on that canvas is exported directly
   via toDataURL, so there is no separate final-render step to drift from
   what the person saw. */
let pendingArtDataUrl = null;
const ART_EXPORT_SIZE = 500;
const ART_DISPLAY_SIZE = 260;
let artCropImg = null;
let artCropBaseScale = 1;
let artCropScale = 1;
let artCropOffsetX = 0;
let artCropOffsetY = 0;
let artCropDragging = false;
let artCropDragStart = null;

function tagModalOpen() {
  return $("tagmodal").classList.contains("show");
}

const TAG_FIELD_INPUTS = {
  title: "tag-title", artist: "tag-artist", album: "tag-album",
  album_artist: "tag-album-artist", genre: "tag-genre",
  track_number: "tag-track-number", track_total: "tag-track-total",
  disc_number: "tag-disc-number", disc_total: "tag-disc-total",
  year: "tag-year",
};

let tagEditorTab = "fields";

function setTagTab(name) {
  tagEditorTab = name;
  $("tagtab-fields").classList.toggle("active", name === "fields");
  $("tagtab-art").classList.toggle("active", name === "art");
  $("tagmodal-body").classList.toggle("tagpane-hidden", name !== "fields");
  $("tagmodal-art-body").classList.toggle("tagpane-hidden", name !== "art");
}
$("tagtab-fields").addEventListener("click", () => setTagTab("fields"));
$("tagtab-art").addEventListener("click", () => setTagTab("art"));

function openTagEditor(paths) {
  const a = api();
  if (!a || !paths.length) return;
  libEditorTouched.clear();
  pendingArtDataUrl = null;
  $("tag-art-whole-album").checked = false;
  setTagTab("fields");
  a.library_open_editor(paths);
}

function closeTagEditor() {
  const a = api();
  if (a) a.library_close_editor();
  $("tagmodal").classList.remove("show");
  libEditorTouched.clear();
  pendingArtDataUrl = null;
  $("tag-art-whole-album").checked = false;
  setTagTab("fields");
}

function renderTagEditor() {
  const modal = $("tagmodal");
  modal.classList.toggle("show", libEditor.open);
  if (!libEditor.open) return;

  const n = libEditor.count;
  $("tagmodal-sub").textContent = n === 1 ? "1 track"
    : `${n} tracks${libEditor.loading ? "" : " selected"}`;

  for (const [field, id] of Object.entries(TAG_FIELD_INPUTS)) {
    const el = $(id);
    // A field already being typed into is left alone even if a fresher
    // snapshot arrives mid-edit, so a slow save elsewhere cannot overwrite
    // what someone is in the middle of typing.
    if (libEditorTouched.has(field)) continue;
    const mixed = !!libEditor.mixed[field];
    const value = libEditor.data[field];
    el.placeholder = mixed ? "(multiple values)" : "";
    el.value = mixed ? "" : (value || value === 0 ? String(value) : "");
  }
  const comp = $("tag-compilation");
  if (!libEditorTouched.has("compilation")) {
    comp.indeterminate = !!libEditor.mixed.compilation;
    comp.checked = !comp.indeterminate && !!libEditor.data.compilation;
  }

  const artUrl = pendingArtDataUrl || libEditor.data.art || null;
  const artBox = $("tag-art-preview-large");
  artBox.classList.toggle("tag-art-empty", !artUrl);
  artBox.style.backgroundImage = artUrl ? `url("${artUrl}")` : "none";

  const errs = $("tagmodal-errors");
  if (libEditor.errors && libEditor.errors.length) {
    errs.classList.remove("hidden");
    const shown = libEditor.errors.slice(0, 4);
    const hidden = libEditor.errors.length - shown.length;
    errs.textContent = (libEditor.failed
      ? `Saved ${libEditor.saved}, failed ${libEditor.failed}. `
      : "") + shown.join(" ")
      + (hidden > 0 ? `  (+${hidden} more error${hidden !== 1 ? "s" : ""})` : "");
  } else {
    errs.classList.add("hidden");
  }

  const saveBtn = $("tag-save");
  saveBtn.disabled = libEditor.saving || libEditor.loading;
  saveBtn.classList.toggle("saving", libEditor.saving);
  saveBtn.textContent = libEditor.saving ? "Saving\u2026" : "Save";
  $("tag-cancel").disabled = libEditor.saving;
}

/* Only what was actually touched, per field. A blank text box or a 0 in a
   number box the person never clicked is not "the user cleared this", it
   is "this box still shows whatever renderTagEditor put there", which for
   a mixed field is nothing at all. */
function collectTagChanges() {
  const changes = {};
  for (const [field, id] of Object.entries(TAG_FIELD_INPUTS)) {
    if (!libEditorTouched.has(field)) continue;
    const el = $(id);
    if (field === "year" || field.endsWith("_number") || field.endsWith("_total")) {
      const n = parseInt(el.value, 10);
      changes[field] = Number.isFinite(n) && n > 0 ? n : 0;
    } else {
      changes[field] = el.value.trim();
    }
  }
  if (libEditorTouched.has("compilation")) {
    changes.compilation = $("tag-compilation").checked ? 1 : 0;
  }
  if (libEditorTouched.has("art") && pendingArtDataUrl) {
    changes.art = pendingArtDataUrl;
  }
  return changes;
}

for (const id of Object.values(TAG_FIELD_INPUTS)) {
  $(id).addEventListener("input", () => libEditorTouched.add(
    Object.keys(TAG_FIELD_INPUTS).find((f) => TAG_FIELD_INPUTS[f] === id)));
}
$("tag-compilation").addEventListener("change", () => {
  libEditorTouched.add("compilation");
  $("tag-compilation").indeterminate = false;
});

$("lib-edit").addEventListener("click", () => openTagEditor(libEditablePaths()));
$("tag-cancel").addEventListener("click", closeTagEditor);
$("tag-save").addEventListener("click", () => {
  const a = api();
  if (!a || !libEditor.paths.length) return;
  const changes = collectTagChanges();
  if (!Object.keys(changes).length) { closeTagEditor(); return; }
  const artWholeAlbum = $("tag-art-whole-album").checked;
  a.library_save_editor(libEditor.paths, changes, artWholeAlbum);
});

/* ---------- album art crop tool ---------- */

function artCropClamp() {
  if (!artCropImg) return;
  const dispW = artCropImg.naturalWidth * artCropScale;
  const dispH = artCropImg.naturalHeight * artCropScale;
  // Independent per axis: an axis the image doesn't reach across (still
  // showing transparent padding on that axis) stays centered rather than
  // being panned, since there is nothing useful to drag into view there.
  // An axis the image fully covers clamps normally, same as before.
  if (dispW <= ART_EXPORT_SIZE) {
    artCropOffsetX = (ART_EXPORT_SIZE - dispW) / 2;
  } else {
    const minX = ART_EXPORT_SIZE - dispW;
    artCropOffsetX = Math.min(0, Math.max(minX, artCropOffsetX));
  }
  if (dispH <= ART_EXPORT_SIZE) {
    artCropOffsetY = (ART_EXPORT_SIZE - dispH) / 2;
  } else {
    const minY = ART_EXPORT_SIZE - dispH;
    artCropOffsetY = Math.min(0, Math.max(minY, artCropOffsetY));
  }
}

function artCropRedraw() {
  if (!artCropImg) return;
  const canvas = $("artcrop-canvas");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, ART_EXPORT_SIZE, ART_EXPORT_SIZE);
  const dispW = artCropImg.naturalWidth * artCropScale;
  const dispH = artCropImg.naturalHeight * artCropScale;
  ctx.drawImage(artCropImg, artCropOffsetX, artCropOffsetY, dispW, dispH);
}

function openArtCropModal(img) {
  artCropImg = img;
  // "Contain" fit: the image's longer side exactly fills the square, so
  // the whole image is visible with the shorter axis left as transparent
  // padding - never cropping anything away until the person zooms in.
  artCropBaseScale = ART_EXPORT_SIZE / Math.max(img.naturalWidth, img.naturalHeight);
  $("artcrop-zoom").value = 100;
  artCropScale = artCropBaseScale;
  const dispW = img.naturalWidth * artCropScale;
  const dispH = img.naturalHeight * artCropScale;
  artCropOffsetX = (ART_EXPORT_SIZE - dispW) / 2;
  artCropOffsetY = (ART_EXPORT_SIZE - dispH) / 2;
  artCropRedraw();
  $("artcropmodal").classList.add("show");
}

function closeArtCropModal() {
  $("artcropmodal").classList.remove("show");
  artCropImg = null;
}

$("artcrop-zoom").addEventListener("input", () => {
  if (!artCropImg) return;
  const newScale = artCropBaseScale * ($("artcrop-zoom").value / 100);
  // Keep whatever point is currently at the viewport's center fixed while
  // the scale changes, rather than re-centering on the image's own
  // center, so zooming feels anchored to what's actually being looked at.
  const centerImgX = (ART_EXPORT_SIZE / 2 - artCropOffsetX) / artCropScale;
  const centerImgY = (ART_EXPORT_SIZE / 2 - artCropOffsetY) / artCropScale;
  artCropScale = newScale;
  artCropOffsetX = ART_EXPORT_SIZE / 2 - centerImgX * artCropScale;
  artCropOffsetY = ART_EXPORT_SIZE / 2 - centerImgY * artCropScale;
  artCropClamp();
  artCropRedraw();
});

const artCropViewport = $("artcrop-viewport");
artCropViewport.addEventListener("pointerdown", (e) => {
  if (!artCropImg) return;
  artCropDragging = true;
  artCropDragStart = { x: e.clientX, y: e.clientY,
                       offsetX: artCropOffsetX, offsetY: artCropOffsetY };
  artCropViewport.setPointerCapture(e.pointerId);
});
artCropViewport.addEventListener("pointermove", (e) => {
  if (!artCropDragging || !artCropDragStart) return;
  // The canvas backing store is ART_EXPORT_SIZE but displayed at
  // ART_DISPLAY_SIZE via CSS, so a screen-pixel drag delta has to be
  // scaled up to canvas-pixel space before it's applied as an offset.
  const ratio = ART_EXPORT_SIZE / ART_DISPLAY_SIZE;
  artCropOffsetX = artCropDragStart.offsetX + (e.clientX - artCropDragStart.x) * ratio;
  artCropOffsetY = artCropDragStart.offsetY + (e.clientY - artCropDragStart.y) * ratio;
  artCropClamp();
  artCropRedraw();
});
function artCropEndDrag() {
  artCropDragging = false;
  artCropDragStart = null;
}
artCropViewport.addEventListener("pointerup", artCropEndDrag);
artCropViewport.addEventListener("pointercancel", artCropEndDrag);

$("artcrop-cancel").addEventListener("click", closeArtCropModal);
$("artcrop-use").addEventListener("click", () => {
  if (!artCropImg) return;
  const dispW = artCropImg.naturalWidth * artCropScale;
  const dispH = artCropImg.naturalHeight * artCropScale;

  // The actual visible portion of the ORIGINAL image, in that image's
  // own pixel coordinates - not the padded workspace square. At "fit"
  // (the default) this is the whole image; only zooming in past fit
  // narrows it to a genuine sub-crop.
  let sx0, sx1, sy0, sy1;
  if (dispW <= ART_EXPORT_SIZE) {
    sx0 = 0; sx1 = artCropImg.naturalWidth;
  } else {
    sx0 = Math.max(0, (0 - artCropOffsetX) / artCropScale);
    sx1 = Math.min(artCropImg.naturalWidth,
                    (ART_EXPORT_SIZE - artCropOffsetX) / artCropScale);
  }
  if (dispH <= ART_EXPORT_SIZE) {
    sy0 = 0; sy1 = artCropImg.naturalHeight;
  } else {
    sy0 = Math.max(0, (0 - artCropOffsetY) / artCropScale);
    sy1 = Math.min(artCropImg.naturalHeight,
                    (ART_EXPORT_SIZE - artCropOffsetY) / artCropScale);
  }
  const srcW = sx1 - sx0, srcH = sy1 - sy0;

  // Sized so the longer side is exactly ART_EXPORT_SIZE, keeping
  // whatever aspect ratio the visible crop actually has - never forced
  // to square, and drawn straight from the full-resolution source so
  // this is one resample, not a second pass over an already-scaled copy.
  const outScale = ART_EXPORT_SIZE / Math.max(srcW, srcH);
  const outW = Math.max(1, Math.round(srcW * outScale));
  const outH = Math.max(1, Math.round(srcH * outScale));
  const outCanvas = document.createElement("canvas");
  outCanvas.width = outW;
  outCanvas.height = outH;
  outCanvas.getContext("2d")
    .drawImage(artCropImg, sx0, sy0, srcW, srcH, 0, 0, outW, outH);

  pendingArtDataUrl = outCanvas.toDataURL("image/jpeg", 0.92);
  libEditorTouched.add("art");
  closeArtCropModal();
  renderTagEditor();
});

function loadImageFromDataUrl(dataUrl) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = dataUrl;
  });
}

$("tag-art-choose").addEventListener("click", () => $("tag-art-file").click());
$("tag-art-file").addEventListener("change", () => {
  const file = $("tag-art-file").files && $("tag-art-file").files[0];
  $("tag-art-file").value = "";
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const img = await loadImageFromDataUrl(reader.result);
      openArtCropModal(img);
    } catch { /* not a decodable image; nothing to do */ }
  };
  reader.readAsDataURL(file);
});

$("tag-art-paste").addEventListener("click", async () => {
  const a = api();
  if (!a) return;
  const dataUrl = await a.paste_image_from_clipboard();
  if (!dataUrl) return;
  try {
    const img = await loadImageFromDataUrl(dataUrl);
    openArtCropModal(img);
  } catch { /* not a decodable image; nothing to do */ }
});

// Click the large preview to reposition/re-crop whatever is currently
// showing - the saved cover, or an image already picked/pasted this
// session - rather than only being able to crop a brand-new image.
$("tag-art-preview-large").addEventListener("click", async () => {
  const artUrl = pendingArtDataUrl || libEditor.data.art || null;
  if (!artUrl) return;
  try {
    const img = await loadImageFromDataUrl(artUrl);
    openArtCropModal(img);
  } catch { /* not a decodable image; nothing to do */ }
});

$("tag-art-copy").addEventListener("click", async () => {
  const a = api();
  const artUrl = pendingArtDataUrl || libEditor.data.art || null;
  if (!a || !artUrl) return;
  await a.copy_image_to_clipboard(artUrl);
});

/* Windows paths are case-insensitive, and the path for whatever is
   currently playing can reach the frontend from a different source than
   the library's own listing did - added by drag-and-drop, by file
   association, or typed with a different drive-letter case somewhere -
   while still naming the identical file. Comparing the raw strings meant
   a track playing from outside the library never matched its own row
   here at all. */
function pathKey(path) {
  return String(path || "").toLowerCase();
}

function rebuildLibRowIndex() {
  // Scanning both containers together let a row left behind in the one
  // you just left - never removed, only hidden - silently win the map
  // entry over the actual current row for the same file, whichever the
  // combined query happened to reach last. Only the container that is
  // actually the current view can hold a row worth indexing.
  const scope = libDetail !== null ? "#libtracks .librow"
                                   : "#libgrid .librow.song";
  libRowsByPath = new Map();
  document.querySelectorAll(scope)
          .forEach((row) => libRowsByPath.set(pathKey(row.dataset.path), row));
  libPlayingRow = null;         // the old reference no longer points at a
                                 // live row after the rebuild that just ran
  paintLibPlaying();
}

function paintLibPlaying() {
  if (libPlayingRow) {
    libPlayingRow.classList.remove("playing");
    const cell = libPlayingRow.querySelector(".n");
    if (cell) cell.textContent = libPlayingRow.dataset.num || "";
    libPlayingRow = null;
  }
  const row = libPlayingPath ? libRowsByPath.get(pathKey(libPlayingPath)) : null;
  if (!row) return;
  row.classList.add("playing");
  const cell = row.querySelector(".n");
  if (cell) cell.textContent = "\u25B6";
  libPlayingRow = row;
}
let libRoots = [];               // folders currently in the library
let libShowFolders = false;
let libConfirmRemove = null;     // path awaiting a second click
let libBrowserRevision = -1;
let libDetailRevision = -1;
let libLoading = false;
let libDesiredNeedle = "";

const fmtCount = (n, one, many) => `${n} ${n === 1 ? one : many}`;

function libraryOpened() {
  const a = api();
  if (!a) return;
  if (!libOpened) {
    libOpened = true;
    // Open on the tab the library was left on. The backend saves it, and
    // asking for the state first costs one call on the first open only.
    const ask = (view) => {
      libView = view;
      libDesiredNeedle = $("libfilter").value.trim();
      libLoading = true;
      document.querySelectorAll(".libtab").forEach((x) =>
        x.classList.toggle("active", x.dataset.lib === libView));
      libGridSig = "";
      libItems = [];
      libDetail = null;
      libSelected.clear();
      libAnchor = null;
      renderLibrary();
      libPending++;
      // Every view has been kept warm since app startup, not just since
      // Library was first opened, and not only Albums (see the startup
      // prewarm). If this open wants the plain unfiltered version of
      // whatever tab was last open, use whatever has already finished
      // directly. request_browser is still safe to fall through to
      // otherwise: the backend now recognizes an identical query already
      // in flight and does not start a second one for it - this only
      // ever waits on that same work, never repeats it.
      if (!libDesiredNeedle && typeof a.library_get_prewarmed === "function") {
        a.library_get_prewarmed(view).then((b) => {
          const stillWanted = view === libView && libDesiredNeedle === "";
          if (stillWanted && b && Array.isArray(b.items)
              && (b.needle || "") === "") {
            libPending--;
            libLoading = false;
            libItems = b.items;
            renderLibrary();
            libTabState[libView] = captureCurrentLibTabState();
            libTabState[libView].stale = false;
            a.library_note_view(view, "");
          } else if (stillWanted) {
            a.library_request_browser(libView, libDesiredNeedle);
          } else {
            libPending--;
          }
          schedule();
        }).catch(() => {
          a.library_request_browser(libView, libDesiredNeedle);
          schedule();
        });
      } else {
        a.library_request_browser(libView, libDesiredNeedle);
        schedule();
      }
    };
    if (typeof a.library_get_state === "function") {
      a.library_get_state()
        .then((st) => ask(
          st && ["albums", "artists", "genres", "songs"].includes(st.view)
            ? st.view : libView))
        .catch(() => ask(libView));
    } else {
      ask(libView);
    }
  }
  renderLibrary();
}

/* The list as it should be shown. The backend has already applied the
   filter, including against track titles, so filtering here as well would
   throw away the albums that matched on a song name. */
function libFiltered() {
  return libItems;
}

const DISC_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/></svg>';

function restoreInlineAlbumUI() {
  // Idempotent: safe to call whether or not anything actually needs
  // moving back. Called whenever the current render is not an inline
  // album, so switching to an artist, a genre, Songs, or Folders while
  // one was expanded never leaves it - or its buttons - stranded inside
  // the grid.
  const detail = $("libdetail");
  if (detail.parentElement === $("libgrid")) {
    $("libempty").parentElement.insertBefore(detail, $("libempty"));
  }
  const wasExpanded = $("libgrid").querySelector(".libcard.expanded");
  if (wasExpanded) wasExpanded.classList.remove("expanded");
  updateExpandedBorderConnectors();
  const spacer = $("libcrumb").querySelector(".spacer");
  if (spacer && $("lib-edit").previousElementSibling !== spacer) {
    spacer.insertAdjacentElement("afterend", $("lib-edit"));
    $("lib-edit").insertAdjacentElement("afterend", $("lib-queue"));
    $("lib-queue").insertAdjacentElement("afterend", $("lib-queue-all"));
    $("lib-queue-all").insertAdjacentElement("afterend", $("lib-play"));
  }
}

// Set right when a different album card is clicked while one is already
// expanded, before the request for its detail is even sent: the old
// expansion is still on screen at that moment, so this is the only
// chance to see where the clicked row actually sits. By the time the
// detail arrives and the old one collapses to make room for the new
// one, everything below the old expansion has already reflowed - if the
// clicked row was below it, collapsing that space shifts the clicked
// row up before its own detail is inserted back underneath it, which is
// what pushed it out of view.
let libPendingAlbumViewportAnchor = null;

function captureAlbumViewportAnchor(card, item) {
  if (!card || !item) return null;
  return {
    album: item.album || "",
    artist: item.album_artist || "",
    top: card.getBoundingClientRect().top,
  };
}

function restoreAlbumViewportAnchor(anchor) {
  if (!anchor) return false;
  const grid = $("libgrid");
  const card = Array.from(grid.querySelectorAll(".libcard")).find(
    (c) => c.dataset.album === anchor.album && c.dataset.artist === anchor.artist);
  if (!card) return false;
  const delta = card.getBoundingClientRect().top - anchor.top;
  if (Math.abs(delta) > 0.5) grid.scrollTop += delta;
  return true;
}

/* Maximize visibility of the expanded album: reveal as much of a cut-off
   bottom as there's room for, but never scroll far enough to push its own
   top past the top of the viewport - the cover and track 1 stay on
   screen even when the whole album can't fit at once. One calculation
   covers both cases: how far down would fully reveal the bottom, and how
   far down is available before the top would be hidden, and only the
   smaller of the two is ever applied. */
function ensureExpandedAlbumVisible() {
  const grid = $("libgrid");
  const detail = $("libdetail");
  if (!detail || detail.parentElement !== grid) return;

  const gridRect = grid.getBoundingClientRect();
  const detailRect = detail.getBoundingClientRect();

  const roomBeforeHidingTop = detailRect.top - gridRect.top;
  if (roomBeforeHidingTop < 0) {
    // Already scrolled past the top - pull it back regardless of the
    // bottom, since keeping the top visible always wins.
    grid.scrollTop += roomBeforeHidingTop;
    return;
  }
  const wantDown = detailRect.bottom - gridRect.bottom;
  if (wantDown > 0) {
    grid.scrollTop += Math.min(wantDown, roomBeforeHidingTop);
  }
}

function placeInlineAlbumDetail() {
  // Finds the card the expanded album belongs to, then inserts the detail
  // panel right after the last card sharing that card's row, so a grid
  // item spanning every column starts a fresh row of its own and pushes
  // whatever comes after down - the same behaviour a plain CSS grid
  // already gives any full-width item, just placed where this one needs
  // to land instead of always at the very end.
  const grid = $("libgrid");
  const detail = $("libdetail");
  // Detached before measuring, not after: left in place, its own old
  // position keeps forcing the row break from whatever width it was last
  // placed at, so a card that would now fit earlier in a wider window
  // measures as if it were still in the narrower layout - the exact
  // reason widening the window never let later cards fill in beside the
  // expanded row instead of starting one of their own further down.
  if (detail.parentElement === grid) {
    $("libempty").parentElement.insertBefore(detail, $("libempty"));
  }
  const cards = Array.from(grid.querySelectorAll(".libcard"));
  const card = cards.find((c) => c.dataset.album === (libDetail.key || "")
                                && c.dataset.artist === (libDetail.key2 || ""));
  if (!card) { detail.classList.add("hidden"); return; }

  cards.forEach((c) => { if (c !== card) c.classList.remove("expanded"); });
  card.classList.add("expanded");

  const top = card.offsetTop;
  const sameRow = cards.filter((c) => Math.abs(c.offsetTop - top) < 4);
  const anchor = sameRow[sameRow.length - 1];
  anchor.insertAdjacentElement("afterend", detail);

  updateExpandedBorderConnectors();

  const actions = $("libcover-actions");
  if ($("lib-edit").parentElement !== actions) {
    actions.appendChild($("lib-edit"));
    actions.appendChild($("lib-queue"));
    actions.appendChild($("lib-queue-all"));
    actions.appendChild($("lib-play"));
  }

  detail.classList.remove("hidden");
  renderLibCover();
  renderLibTracks(libDetail.items);
}

/* Bridges the border from the expanded card's own edges out to the full-
   width panel's, since the card is only ever as wide as one column.
   Viewport-relative rects, not offsetLeft: the card and the panel don't
   necessarily share the same offsetParent, but their bounding rects are
   always directly comparable regardless. Kept as its own step, callable
   from the same settle loops that already re-check other measurements a
   few frames after layout changes, since reading it only once - right
   after inserting the panel - can catch it before the row has actually
   settled to its final width. */
function getOrCreateExpandConnector(id, grid) {
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement("div");
    el.id = id;
    el.className = "lib-expand-connector hidden";
    grid.appendChild(el);
  } else if (el.parentElement !== grid) {
    grid.appendChild(el);
  }
  return el;
}

function updateExpandedBorderConnectors() {
  const grid = $("libgrid");
  const detail = $("libdetail");
  const left = getOrCreateExpandConnector("lib-expand-connector-left", grid);
  const right = getOrCreateExpandConnector("lib-expand-connector-right", grid);

  if (detail.parentElement !== grid) {
    left.classList.add("hidden");
    right.classList.add("hidden");
    return;
  }
  const card = grid.querySelector(".libcard.expanded");
  if (!card) {
    left.classList.add("hidden");
    right.classList.add("hidden");
    return;
  }

  // Grid-content coordinates, not viewport ones: getBoundingClientRect is
  // relative to the viewport, but these need to sit in the same
  // coordinate space the grid's own scrolling content does, which is
  // what absolute positioning inside a scrolled container actually uses.
  // gridRect.top is the grid's own fixed position on screen and does not
  // move as its content scrolls, so the difference between it and any
  // descendant's rect is exactly how far that descendant currently sits
  // from the grid's visible top edge - adding scrollTop back converts
  // that visible offset into the underlying content position.
  const gridRect = grid.getBoundingClientRect();
  const cardRect = card.getBoundingClientRect();
  const panelRect = detail.getBoundingClientRect();
  const toGridX = (x) => x - gridRect.left;
  const toGridY = (y) => y - gridRect.top + grid.scrollTop;

  const cardBottom = toGridY(cardRect.bottom);
  const cardLeft = toGridX(cardRect.left);
  const cardRight = toGridX(cardRect.right);
  const panelTop = toGridY(panelRect.top);
  const panelLeft = toGridX(panelRect.left);
  const panelRight = toGridX(panelRect.right);
  const vGap = Math.max(0, panelTop - cardBottom);

  // Shown whenever there's a vertical gap to cover, regardless of how
  // wide the horizontal one is: a card flush with the panel's own edge -
  // first or last in its row - has zero horizontal gap, but the grid's
  // row spacing still leaves a real vertical one, which still needs the
  // straight-down leg connecting the card's corner to the panel's, even
  // with no sideways jog beforehand.
  const leftGap = Math.max(0, cardLeft - panelLeft);
  if (vGap > 0.5) {
    // The border sits on this element's own left side, the same side
    // fixed by `left` - any growth needed to fit the border happens on
    // the unbordered right side instead, so this stays correctly
    // anchored even when the gap is smaller than the border itself.
    const leftWidth = Math.max(leftGap, 2);
    left.classList.remove("hidden");
    left.style.left = panelLeft + "px";
    left.style.width = leftWidth + "px";
    left.style.top = cardBottom + "px";
    left.style.height = vGap + "px";
  } else {
    left.classList.add("hidden");
  }

  const rightGap = Math.max(0, panelRight - cardRight);
  if (vGap > 0.5) {
    // Here the border sits on the right side - the side that would grow
    // if width came up short of the border's own thickness - so `left`
    // has to be computed backward from where the right edge needs to
    // land, guaranteeing enough width up front rather than trusting the
    // browser to grow it in the right direction.
    const rightWidth = Math.max(rightGap, 2);
    right.classList.remove("hidden");
    right.style.left = (panelRight - rightWidth) + "px";
    right.style.width = rightWidth + "px";
    right.style.top = cardBottom + "px";
    right.style.height = vGap + "px";
  } else {
    right.classList.add("hidden");
  }
}

function renderLibrary() {
  const grid = $("libgrid");
  const tracks = $("libtracks");
  const crumb = $("libcrumb");
  const empty = $("libempty");
  const folders = $("libfolders");
  // An album opened from the grid expands in place next to the other
  // cards rather than replacing the browse area; an artist or genre still
  // does, since there is no single row of cards for those to slot beside.
  const inlineAlbum = libView === "albums" && libDetail !== null
                     && libDetail.kind === "album";
  const showingDetail = libDetail !== null && !inlineAlbum;

  if (!inlineAlbum) restoreInlineAlbumUI();

  // Managing folders replaces the browse area rather than floating over
  // it, so there is never a question of which thing a click belongs to.
  folders.classList.toggle("hidden", !libShowFolders);
  $("lib-folders").classList.toggle("on", libShowFolders);
  // Totals belong to the browse lists only.
  $("libstat").classList.toggle("hidden", libShowFolders || showingDetail);
  if (libShowFolders) {
    grid.classList.add("hidden");
    $("libdetail").classList.add("hidden");
    crumb.classList.add("hidden");
    empty.classList.add("hidden");
    renderFolders();
    return;
  }

  const songsView = libView === "songs" && !showingDetail;
  crumb.classList.toggle("hidden", !showingDetail && !songsView);
  $("lib-back").classList.toggle("hidden", songsView);
  if (songsView) {
    const shown = libItems.length;
    const capped = libItems.length && libItems[0].truncated;
    $("libcrumb-title").textContent =
      capped ? `First ${shown} songs, filter to narrow` : `${shown} songs`;
  }
  if (showingDetail) {
    $("libcrumb-title").textContent = libDetail.title || "";
    grid.classList.add("hidden");
    $("libdetail").classList.remove("hidden");
    empty.classList.add("hidden");
    renderLibCover();
    renderLibTracks(libDetail.items);
    return;
  }

  if (!inlineAlbum) $("libdetail").classList.add("hidden");
  const rows = libFiltered();
  const bare = rows.length === 0;
  empty.classList.toggle("hidden", !bare);
  grid.classList.toggle("hidden", bare);
  if (bare) return;

  /* Songs is the one view sized to the whole library rather than one
     album or artist, so during a long scan it is also the one view whose
     row count keeps growing for the entire scan. The existing signature
     check below only skips a rebuild when the set is unchanged, which
     during an active scan it never is: rebuilding several thousand rows
     costs roughly their count in milliseconds, and paying that cost again
     on every scan-triggered refresh is what made the interface feel like
     it was getting slower the longer a scan ran, when the scan itself was
     not. Once something is already on screen, Songs skips further
     rebuilds until the scan finishes; switching away and back still shows
     the current list immediately, since that is a fresh render, not a
     refresh. */
  if (libView === "songs" && libScanning && grid.childElementCount > 0
      && libGridSig.startsWith("songs\u0001")) {
    // Checking for any content at all, rather than specifically Songs
    // content, meant returning here after visiting another tab mid-scan
    // left whatever that tab last drew - album cards, artist rows -
    // sitting there under the Songs tab, since the guard skipped the
    // rebuild that would have replaced it. Skipping only applies when the
    // grid still genuinely holds the Songs list from before.
    //
    // Reindexing here, not just repainting: visiting another view in
    // between replaces libRowsByPath with that view's own rows, so the
    // frozen Songs rows already on screen need reindexing too - but that
    // is a plain querySelectorAll over what already exists, not the HTML
    // string construction that made a full rebuild expensive, so it costs
    // nothing close to that even on a large list.
    paintLibArt();
    rebuildLibRowIndex();
    return;
  }

  /* Rebuilding the grid throws away scroll position, every painted cover
     and the observer. During a scan the list refreshes every couple of
     seconds, so rebuild only when the set of entries actually changed. */
  const sig = libView + "\u0001" + rows.map((it) =>
    libView === "albums" ? `${it.album_artist}\u0000${it.album}`
                         : (it.artist || it.genre || "")).join("\u0002");
  // Counted separately from grid.childElementCount: an expanded album
  // leaves #libdetail sitting inside this same grid, which would
  // otherwise always be one more than rows.length and defeat this check
  // every single time an album is open, even when nothing about the
  // album list itself changed.
  const cardCount = libView === "albums"
    ? grid.querySelectorAll(".libcard").length : grid.childElementCount;
  if (sig === libGridSig && cardCount === rows.length) {
    paintLibArt();
    if (inlineAlbum) placeInlineAlbumDetail();
    else if (libView === "albums") captureLibAlbumAnchor();
    return;
  }
  libGridSig = sig;

  if (libView === "albums") {
    // Detached unconditionally, not just when leaving inline mode: moving
    // straight from one expanded album to another never passes through
    // "not inline", so #libdetail can still be a child of this grid right
    // here. innerHTML below destroys every current child of the grid, and
    // that used to include #libdetail itself whenever this ran - not
    // moved, deleted, along with everything inside it and the buttons
    // that had been moved into it. Once gone, artist and genre detail
    // broke too, since they share the same element.
    if ($("libdetail").parentElement === grid) {
      $("libempty").parentElement.insertBefore($("libdetail"), $("libempty"));
    }
    // Toggle a class rather than an inline display, which would win over
    // the .hidden rule and leave the grid showing behind the track list.
    grid.classList.remove("aslist");
    grid.innerHTML = rows.map((it, i) => `
      <div class="libcard" data-i="${i}"
           data-album="${esc(it.album || "")}"
           data-artist="${esc(it.album_artist || "")}">
        <div class="art">${DISC_ICON}</div>
        <div class="t1">${esc(it.album)}</div>
        <div class="t2">${esc(it.album_artist)}${it.year ? " &middot; " + it.year : ""}</div>
      </div>`).join("");
    paintLibArt();
    watchLibArt();
    if (inlineAlbum) placeInlineAlbumDetail();
    else captureLibAlbumAnchor();
  } else if (libView === "songs") {
    // Individual tracks. Selecting works as it does in the playlist, and
    // the buttons above act on the selection, so there is nothing to
    // drill into: a song is already the thing you wanted.
    grid.classList.add("aslist");
    // Broken into album sections, as the artist and genre views are. The
    // backend returns them in album order, so this only has to notice
    // where one ends and the next begins. Track number replaces the blank
    // first column, and the album is dropped from the row since the
    // heading above already carries it.
    const out = [];
    let section = null;
    for (const t of rows) {
      const who = t.album_artist || t.artist || "";
      const album = (t.album || "").trim();
      // Everything with no album tag shares one heading at the end rather
      // than one per artist, which repeated the same words down the page.
      // Those rows keep their artist in the column, so nothing is lost.
      const key = album ? `${who}\u0000${album}` : "\u0000";
      if (key !== section) {
        section = key;
        const bits = [];
        if (album && who) bits.push(`<span class="by">${esc(who)}</span>`);
        if (album && t.year) bits.push(`<span class="yr">${t.year}</span>`);
        out.push(`<div class="libsection">${esc(album || "Not part of an album")}`
                 + `${bits.join("")}</div>`);
      }
      out.push(`<div class="librow song" data-path="${esc(t.path)}" data-num="${t.track_number || ""}">
        <div class="n">${t.track_number || ""}</div>
        <div class="t">${esc(t.title)}</div>
        <div class="a">${esc(t.artist)}</div>
        <div class="al"></div>
        <div class="d">${fmt(t.duration || 0)}</div>
      </div>`);
    }
    grid.innerHTML = out.join("");
    paintLibSelection();
    rebuildLibRowIndex();
  } else {
    // Artists and genres are a list rather than a grid: there is no
    // artwork to show, and a name reads better on one line than boxed.
    grid.classList.add("aslist");
    grid.innerHTML = rows.map((it, i) => {
      const name = libView === "artists" ? it.artist : it.genre;
      const sub = libView === "artists"
        ? `${fmtCount(it.tracks, "track", "tracks")}, ${fmtCount(it.albums, "album", "albums")}`
        : fmtCount(it.tracks, "track", "tracks");
      return `<div class="librow" data-i="${i}">
        <div class="n"></div><div class="t">${esc(name)}</div>
        <div class="a">${sub}</div><div class="al"></div>
        <div class="d">${fmt(it.duration || 0)}</div></div>`;
    }).join("");
  }
}

function trackRow(t, showAlbum) {
  return `<div class="librow" data-path="${esc(t.path)}" data-num="${t.track_number || ""}">
      <div class="n">${t.track_number || ""}</div>
      <div class="t">${esc(t.title)}</div>
      <div class="a">${esc(t.artist)}</div>
      <div class="al">${showAlbum ? esc(t.album) : ""}</div>
      <div class="d">${fmt(t.duration || 0)}</div>
    </div>`;
}

/* An artist or a genre is a set of records, not a loose pile of songs, so
   the tracks are broken into album sections. The backend already returns
   them in album order with untagged files last, so this only has to notice
   where one album ends and the next begins.

   Inside a section the album column is dropped, since the heading says it.
   A genre spans many artists, so its headings carry the album artist too;
   under a single artist that would just repeat the title of the pane. */
/* Covers are fetched for the cards on screen, not for the whole library.
   An IntersectionObserver is what makes that automatic: scrolling brings
   new cards into view and each asks once, so a thousand-album library
   decodes only what has actually been looked at. */
/* The key is built here rather than stored in an attribute: it joins the
   two names with a NUL, which an HTML attribute does not preserve, so a
   data-key round trip came back without it and never matched. */
function artKey(card) {
  return `${card.dataset.artist || ""}\u0000${card.dataset.album || ""}`;
}

/* Tells the backend which albums are on screen, nearest the top of the
   view first, whenever that changes. It resolves exactly those and throws
   away anything queued for cards that have scrolled away.

   An observer was queueing every card it passed, so the covers being
   looked at waited behind a hundred albums already scrolled past. What
   matters is what is on screen now. */
const ART_AHEAD = 400;           // px beyond the view to prepare
let libArtTimer = 0;

function visibleAlbumKeys() {
  const grid = $("libgrid");
  const top = grid.scrollTop - ART_AHEAD;
  const bottom = grid.scrollTop + grid.clientHeight + ART_AHEAD;
  const wanted = [];
  for (const [key, card] of libCards) {
    if (libArt[key] !== undefined) continue;
    const y = card.offsetTop;
    if (y + card.offsetHeight < top || y > bottom) continue;
    wanted.push([Math.abs(y - grid.scrollTop), key]);
  }
  // Nearest the top of the view first, so the row being looked at is
  // resolved before the ones above and below it.
  wanted.sort((a, b) => a[0] - b[0]);
  return wanted.map((w) => w[1]);
}

function reportVisibleArt() {
  const a = api();
  if (!a || typeof a.library_visible_art !== "function") return;
  // libDetail is never checked here: within the Albums tab it only ever
  // means an album expanded inline, not a view that replaces the grid,
  // so the grid stays visible and scrollable right alongside it - cards
  // scrolled into view while one is open still need their covers loaded,
  // same as any other scrolling. Skipping on libDetail was correct back
  // when opening an album replaced the grid outright; it silently
  // stopped loading covers the moment inline expansion made scrolling
  // past an open album possible at all.
  if (libView !== "albums" || libShowFolders) return;
  const keys = visibleAlbumKeys();
  if (!keys.length) return;
  keys.forEach((k) => libArtSeen.add(k));
  libArtWaiting = true;
  a.library_visible_art(keys);
  schedule();
}

function watchLibArt() {
  libCards = new Map();
  $("libgrid").querySelectorAll(".libcard").forEach((c) =>
    libCards.set(artKey(c), c));
  reportVisibleArt();
}

function updateCurrentLibTabScrollState() {
  const st = libTabState[libView];
  if (!st) return;
  st.gridScrollTop = $("libgrid").scrollTop;
  st.detailScrollTop = $("libtracks").scrollTop;
  st.anchor = captureVisibleLibAnchor();
}

let libExpandBorderScrollRaf = 0;
$("libgrid").addEventListener("scroll", () => {
  clearTimeout(libArtTimer);
  libArtTimer = setTimeout(reportVisibleArt, 90);
  updateCurrentLibTabScrollState();
  // The border connectors are cheap to recompute but not free, and a
  // scroll gesture can fire this many times per frame, so this caps it
  // at once per frame rather than running on every single event. Kept
  // unconditional rather than gated on an album being open: the function
  // itself already no-ops correctly when nothing is expanded, and a scroll
  // is exactly the kind of layout change - a card settling from its
  // content-visibility placeholder size to its real one as it enters
  // view chief among them - that every other trigger for this same
  // measurement already exists to catch, just never for a plain scroll.
  if (!libExpandBorderScrollRaf) {
    libExpandBorderScrollRaf = requestAnimationFrame(() => {
      libExpandBorderScrollRaf = 0;
      updateExpandedBorderConnectors();
    });
  }
}, { passive: true });

$("libtracks").addEventListener("scroll", () => {
  updateCurrentLibTabScrollState();
}, { passive: true });

/* The only place that knows the shape of a library_get_art reply. It
   returns an envelope, not a map of covers; assigning that envelope
   straight to the cache replaced every cover with the words seq, art and
   reset, which is what emptied the grid after a tab switch. */
function collectArt(since) {
  const a = api();
  if (!a || typeof a.library_get_art !== "function") return;
  a.library_get_art(since).then((m) => {
    if (!m || typeof m !== "object") return;
    // A backend that has restarted, or forgotten misses, reports a
    // sequence behind ours; start again rather than keeping stale keys.
    if (m.reset) { libArt = {}; libArtSeen = new Set(); }
    libArtSeq = m.seq || 0;
    const fresh = m.art || {};
    const keys = Object.keys(fresh);
    if (!keys.length && !m.reset) return;
    for (const k of keys) libArt[k] = fresh[k];
    // Anything the backend has forgotten should be asked for again: it
    // drops "no cover" answers when the index changes, since an album that
    // was half indexed when it was asked may have gained the track
    // carrying the artwork since.
    for (const key of Array.from(libArtSeen)) {
      if (!(key in libArt)) libArtSeen.delete(key);
    }
    paintLibArt(keys);
    libArtWaiting = visibleAlbumKeys().length > 0;
    if (libDetail && libDetail.kind === "album") renderLibCover();
  }).catch(() => {});
}

function paintLibArt(keys) {
  const grid = $("libgrid");
  if (keys && keys.length) {
    for (const key of keys) {
      const url = libArt[key];
      if (!url) continue;
      const card = libCards.get(key);
      if (!card) continue;
      const art = card.querySelector(".art");
      if (art.dataset.painted === url) continue;
      art.dataset.painted = url;
      art.innerHTML = `<img src="${esc(url)}" alt="" loading="lazy">`;
    }
    return;
  }
  grid.querySelectorAll(".libcard").forEach((card) => {
    const url = libArt[artKey(card)];
    if (!url) return;
    const art = card.querySelector(".art");
    if (art.dataset.painted === url) return;
    art.dataset.painted = url;
    art.innerHTML = `<img src="${esc(url)}" alt="" loading="lazy">`;
  });
}

/* The cover only belongs to an album. An artist or a genre spans many, so
   there is no single image that would be honest to show, and the pane is
   dropped rather than filled with the first one that happened to sort
   first. */
function renderLibCover() {
  const pane = $("libcover");
  const isAlbum = libDetail && libDetail.kind === "album";
  pane.classList.toggle("hidden", !isAlbum);
  if (!isAlbum) return;

  const artist = libDetail.key2 || "";
  const album = libDetail.key || "";
  const key = `${artist}\u0000${album}`;
  const url = libArt[key];
  const art = $("libcover-art");
  const want = url ? `img:${url}` : "icon";
  if (art.dataset.painted !== want) {
    art.dataset.painted = want;
    art.innerHTML = url ? `<img src="${esc(url)}" alt="">` : DISC_ICON;
  }

  const items = libDetail.items || [];
  const seconds = items.reduce((sum, t) => sum + (t.duration || 0), 0);
  const year = items.length ? (items[0].year || 0) : 0;
  setText($("libcover-title"), "coverTitle", album);
  setText($("libcover-artist"), "coverArtist", artist);
  const bits = [fmtCount(items.length, "track", "tracks"), fmt(seconds)];
  if (year) bits.unshift(String(year));
  setText($("libcover-sub"), "coverSub", bits.join("   |   "));

  // The grid asks for covers as cards scroll past, so an album opened
  // from a card already has one. Reached any other way it may not, and
  // this is the only place that would notice.
  const a = api();
  if (!url && a && typeof a.library_request_art === "function"
      && !libArtSeen.has(key)) {
    libArtSeen.add(key);
    a.library_request_art(album, artist);
    schedule();
  }
}

function renderFolders() {
  const list = $("foldlist");
  if (!libRoots.length) {
    list.innerHTML = '<div class="foldempty">No folders yet. '
      + 'Add one and its music is indexed in the background.</div>';
    return;
  }
  list.innerHTML = libRoots.map((path) => {
    const armed = libConfirmRemove === path;
    // Two clicks rather than a dialog: removing drops every track under
    // that folder from the index, which is not something to do by a
    // stray click, but it is also not destructive enough to interrupt.
    return `<div class="foldrow" data-path="${esc(path)}">
      <div class="path" title="${esc(path)}"><bdi>${esc(path)}</bdi></div>
      <button class="tbtn${armed ? " danger" : ""}" data-remove="${esc(path)}">
        ${armed ? "Remove?" : "Remove"}</button>
    </div>`;
  }).join("");
}

function renderLibTracks(items) {
  const box = $("libtracks");
  const kind = libDetail ? libDetail.kind : "";
  const grouped = kind === "artist" || kind === "genre";
  if (!grouped) {
    // Inside an album the cover pane already names it, so repeating it on
    // every row is just noise. A search result still needs the column,
    // since its rows can come from anywhere.
    box.innerHTML = items.map((t) => trackRow(t, kind !== "album")).join("");
    paintLibSelection();
    rebuildLibRowIndex();
    return;
  }
  const html = [];
  let current = null;
  for (const t of items) {
    const who = t.album_artist || t.artist || "";
    const key = `${who}\u0000${t.album || ""}`;
    if (key !== current) {
      current = key;
      const bits = [];
      if (kind === "genre" && who) bits.push(`<span class="by">${esc(who)}</span>`);
      if (t.year) bits.push(`<span class="yr">${t.year}</span>`);
      const heading = t.album || "Not part of an album";
      html.push(`<div class="libsection">${esc(heading)}${bits.join("")}</div>`);
    }
    html.push(trackRow(t, false));
  }
  box.innerHTML = html.join("");
  paintLibSelection();
  rebuildLibRowIndex();
}

function paintLibSelection() {
  document.querySelectorAll("#libtracks .librow, #libgrid .librow.song")
    .forEach((row) => row.classList.toggle(
      "selected", libSelected.has(row.dataset.path)));
  // Editing writes to real files, so unlike Play or Add to playlist there
  // is no sensible "nothing selected" behaviour to fall back to.
  $("lib-edit").disabled = libSelected.size === 0;
}

function libChosenPaths() {
  if (libView === "songs" && !libDetail) {
    const all = libRowPaths(".song");
    return libSelected.size ? all.filter((p) => libSelected.has(p)) : all;
  }
  if (!libDetail) return [];
  if (libSelected.size) {
    return libDetail.items.map((t) => t.path).filter((p) => libSelected.has(p));
  }
  return libDetail.items.map((t) => t.path);
}

/* Deliberately stricter than libChosenPaths: Play and Add to playlist both
   treat no selection as "everything in view", which is fine, since neither
   touches a file. Editing tags writes to the actual files on disk, so
   nothing is editable until something is explicitly selected - there is no
   safe reading of "edit tags with nothing selected" the way there is for
   playing or queuing. */
function libEditablePaths() {
  if (!libSelected.size) return [];
  return libChosenPaths();
}

/* What a double-click in the library plays: the current view's own order,
   not just the one track clicked. An album's track list, an artist's or
   genre's detail list, or the songs list exactly as displayed - so
   continuing past the clicked track carries on through the rest of what
   was actually on screen, the way opening an album and hitting play
   naturally continues into the next track. */
/* What to call the current view, for the status line. One place, so the
   Play button and a double-click cannot each describe the same list
   differently. */
function libraryContextLabel() {
  if (libView === "songs" && !libDetail) return { kind: "songs", title: "Songs" };
  if (libDetail) {
    // The backend labels a search result "Search: eclipse" for its own
    // detail title; libDetail.key is the bare search text, which reads
    // better here than repeating that label back.
    const title = libDetail.kind === "search"
      ? (libDetail.key || libDetail.title || "") : (libDetail.title || "");
    return { kind: libDetail.kind || "library", title };
  }
  return { kind: "", title: "" };
}

function libraryContextPayload(startPath) {
  startPath = String(startPath || "");
  if (!startPath) return null;
  const label = libraryContextLabel();

  if (libView === "songs" && !libDetail) {
    const paths = libItems.map((t) => t.path).filter(Boolean);
    return { paths, startPath, kind: label.kind, title: label.title };
  }
  if (libDetail && Array.isArray(libDetail.items) && libDetail.items.length) {
    const paths = libDetail.items.map((t) => t.path).filter(Boolean);
    return { paths, startPath, kind: label.kind, title: label.title };
  }
  return null;
}

function playLibraryContextFrom(startPath) {
  const a = api();
  if (!a) return;
  const payload = libraryContextPayload(startPath);
  // No context to build a queue from - a row reached some way this does
  // not anticipate - still has to play something, so it becomes a queue
  // of just that one track rather than doing nothing.
  const p = payload && payload.paths && payload.paths.length
    ? payload : { paths: [startPath], startPath, kind: "", title: "" };
  a.library_play_context(p.paths, p.startPath, p.kind, p.title);
}

function orderedSelectedLibraryPaths() {
  const chosen = new Set(libChosenPaths());
  if (!chosen.size) return [];
  if (libView === "songs" && !libDetail) {
    return libItems.map((t) => t.path).filter((p) => chosen.has(p));
  }
  if (libDetail && Array.isArray(libDetail.items)) {
    return libDetail.items.map((t) => t.path).filter((p) => chosen.has(p));
  }
  return Array.from(chosen);
}

// One entry per tab: whatever was drilled into or expanded there, and
// where the list was scrolled, so switching tabs is a visit, not a reset.
// Populated lazily; a tab visited for the first time this session simply
// has nothing to restore.
let libTabState = {
  albums: null,
  artists: null,
  genres: null,
  songs: null,
};

function cloneLibDetail(d) {
  if (!d) return null;
  return {
    kind: d.kind || "",
    key: d.key || "",
    key2: d.key2 || "",
    title: d.title || "",
    items: Array.isArray(d.items) ? d.items.slice() : [],
    revision: d.revision || 0,
  };
}

function captureCurrentLibTabState() {
  return {
    view: libView,
    items: Array.isArray(libItems) ? libItems.slice() : [],
    detail: cloneLibDetail(libDetail),
    selected: Array.from(libSelected),
    anchor: captureVisibleLibAnchor(),
    gridScrollTop: $("libgrid").scrollTop,
    detailScrollTop: $("libtracks").scrollTop,
    stale: false,
  };
}

function restoreScrollExactly(gridTop, detailTop) {
  const grid = $("libgrid");
  const tracks = $("libtracks");
  const wantGrid = Math.max(0, Number(gridTop) || 0);
  const wantTracks = Math.max(0, Number(detailTop) || 0);

  grid.scrollTop = wantGrid;
  tracks.scrollTop = wantTracks;

  requestAnimationFrame(() => {
    grid.scrollTop = wantGrid;
    tracks.scrollTop = wantTracks;
  });
}

function restoreLibTabState(saved) {
  if (!saved) return false;

  libItems = Array.isArray(saved.items) ? saved.items.slice() : [];
  libDetail = cloneLibDetail(saved.detail);
  libSelected = new Set(saved.selected || []);
  libAnchor = null;
  // Reset, not restored from the snapshot: the grid may currently hold a
  // different tab's cards or rows, left there by whichever tab was shown
  // last, and a sig that happens to still match this tab's own data would
  // skip the rebuild that would otherwise replace that leftover content -
  // the same class of bug fixed before for Songs during a scan, just
  // triggered by a tab switch instead this time.
  libGridSig = "";

  renderLibrary();
  restoreScrollExactly(saved.gridScrollTop, saved.detailScrollTop);
  paintLibSelection();
  paintLibPlaying();
  // Both the border connectors and the exact scroll position measure or
  // depend on real layout that has not necessarily settled yet right
  // after a rebuild - confirmed directly elsewhere in this same feature,
  // not assumed. restoreScrollExactly's own two fixed attempts (now,
  // next frame) aren't always enough - an inline panel with many tracks
  // in particular can take longer to reach its final height - and unlike
  // opening or switching an album, or resizing, nothing was retrying
  // either one here, which is exactly what could leave a tab's scroll
  // position wrong, and intermittently so, after leaving it and coming
  // back.
  const grid = $("libgrid");
  const tracks = $("libtracks");
  const wantGrid = Math.max(0, Number(saved.gridScrollTop) || 0);
  const wantTracks = Math.max(0, Number(saved.detailScrollTop) || 0);
  let tries = 0;
  const settle = () => {
    grid.scrollTop = wantGrid;
    tracks.scrollTop = wantTracks;
    updateExpandedBorderConnectors();
    tries++;
    if (tries < 10) requestAnimationFrame(settle);
  };
  settle();
  return true;
}

function invalidateLibTabState(name) {
  const st = libTabState[name];
  if (st) st.stale = true;
}

function invalidateAllLibTabState() {
  invalidateLibTabState("albums");
  invalidateLibTabState("artists");
  invalidateLibTabState("genres");
  invalidateLibTabState("songs");
}

function setLibView(name) {
  if (libView === name) return;

  libTabState[libView] = captureCurrentLibTabState();

  libView = name;
  document.querySelectorAll(".libtab").forEach((b) =>
    b.classList.toggle("active", b.dataset.lib === name));

  const saved = libTabState[name];
  const activeNeedle = $("libfilter").value.trim();
  // A cached tab snapshot may have been captured under a different (or no)
  // filter, so it can't be trusted while one is currently active - restore
  // it only when there is nothing typed to filter by.
  if (!activeNeedle && saved && !saved.stale) {
    libLoading = false;
    restoreLibTabState(saved);
    return;
  }

  libGridSig = "";
  libItems = [];
  libDetail = null;
  libSelected.clear();
  libAnchor = null;
  renderLibrary();

  const a = api();
  if (!a) return;
  libDesiredNeedle = activeNeedle;
  libLoading = true;
  libPending++;
  // The backend has kept every view's unfiltered listing warm since
  // startup (and refreshed on every library-changing event since), not
  // just whichever tab happened to be open. If this switch wants that
  // exact thing (no active filter), use whatever has already finished
  // directly instead of firing a live query that repeats work already
  // done. request_browser is still safe to fall through to otherwise -
  // the backend recognizes an identical query already in flight and
  // waits on it rather than starting a second one.
  if (!activeNeedle && typeof a.library_get_prewarmed === "function") {
    a.library_get_prewarmed(name).then((b) => {
      const stillWanted = name === libView && libDesiredNeedle === "";
      if (stillWanted && b && Array.isArray(b.items)
          && (b.needle || "") === "") {
        libPending--;
        libLoading = false;
        libItems = b.items;
        renderLibrary();
        libTabState[libView] = captureCurrentLibTabState();
        libTabState[libView].stale = false;
        a.library_note_view(name, "");
      } else if (stillWanted) {
        a.library_request_browser(name, activeNeedle);
      } else {
        libPending--;
      }
      schedule();
    }).catch(() => {
      a.library_request_browser(name, activeNeedle);
      schedule();
    });
  } else {
    a.library_request_browser(name, activeNeedle);
    schedule();
  }
}

/* The item a tab is restored to has to wait for that tab's content - the
   browse list, and the reopened album or artist if there was one - to
   actually exist, and that arrives asynchronously from the backend at
   some point after this runs. Retried across several animation frames
   rather than tied to any specific render call, so it does not matter
   which of those two arrives first or how renderLibrary happens to be
   structured; it just keeps looking for the remembered card or row until
   it exists, then stops once it is found, or once the user has already
   navigated elsewhere.
   
   An anchor, not a saved scrollTop pixel value: a plain pixel offset only
   means the same thing again if nothing above it changed height between
   leaving and returning, and album art can keep arriving in the
   background well after the tab first renders - restoring a fixed number
   before that settles, then never revisiting it, is what left the view a
   couple of rows off from where it actually was. */
let libPendingScrollRestore = null;

function scheduleLibScrollRestore(view, anchor) {
  libPendingScrollRestore = { view, anchor, tries: 0 };
  requestAnimationFrame(tryRestoreLibScroll);
}

function tryRestoreLibScroll() {
  const p = libPendingScrollRestore;
  if (!p) return;
  if (libView !== p.view) { libPendingScrollRestore = null; return; }
  scrollToVisibleLibAnchor(p.anchor);
  p.tries++;
  if (p.tries < 30) requestAnimationFrame(tryRestoreLibScroll);
  else libPendingScrollRestore = null;
}

/* ---- library wiring ---- */

document.querySelectorAll(".libtab").forEach((b) =>
  b.addEventListener("click", () => setLibView(b.dataset.lib)));

let libFilterTimer = 0;
$("libfilter").addEventListener("input", () => {
  // Filtering is backend-owned, not a local array filter, because Albums,
  // Artists, Genres and Songs each search against different fields. But
  // the UI should still react immediately while the query is in flight,
  // rather than sitting on the previous result until it lands.
  const needle = $("libfilter").value.trim();
  libDesiredNeedle = needle;
  libDetail = null;
  libAnchor = null;
  libLoading = true;
  libItems = [];
  libGridSig = "";
  renderLibrary();
  schedule();

  clearTimeout(libFilterTimer);
  libFilterTimer = setTimeout(() => {
    const a = api();
    if (!a) return;
    libPending++;
    if (!needle && typeof a.library_get_prewarmed === "function") {
      a.library_get_prewarmed(libView).then((b) => {
        const stillWanted = libDesiredNeedle === needle;
        if (stillWanted && b && Array.isArray(b.items)
            && (b.needle || "") === "") {
          libPending--;
          libLoading = false;
          libItems = b.items;
          renderLibrary();
          libTabState[libView] = captureCurrentLibTabState();
          libTabState[libView].stale = false;
          a.library_note_view(libView, "");
        } else if (stillWanted) {
          a.library_request_browser(libView, needle);
        } else {
          libPending--;
        }
        schedule();
      }).catch(() => {
        a.library_request_browser(libView, needle);
        schedule();
      });
    } else {
      a.library_request_browser(libView, needle);
      schedule();
    }
  }, 120);
});

$("libfolders").addEventListener("click", (e) => {
  if (libConfirmRemove && !e.target.closest("[data-remove]")) {
    libConfirmRemove = null;
    renderFolders();
  }
});

$("libgrid").addEventListener("dblclick", (e) => {
  const row = e.target.closest(".librow.song");
  if (row) { playLibraryContextFrom(row.dataset.path); return; }
});

$("libgrid").addEventListener("click", (e) => {
  const song = e.target.closest(".librow.song");
  if (song) {
    const path = song.dataset.path;
    if (e.shiftKey) {
      const run = libAnchor === null ? null : libRange(libAnchor, path, ".song");
      if (run) {
        if (!e.ctrlKey) libSelected = new Set();
        run.forEach((x) => libSelected.add(x));
      } else { libSelected = new Set([path]); libAnchor = path; }
    } else if (e.ctrlKey) {
      libSelected.has(path) ? libSelected.delete(path) : libSelected.add(path);
      libAnchor = path;
    } else {
      libSelected = new Set([path]);
      libAnchor = path;
    }
    paintLibSelection();
    return;
  }
  const card = e.target.closest(".libcard, .librow");
  if (!card) return;
  const item = libFiltered()[Number(card.dataset.i)];
  if (!item) return;
  const a = api();
  if (!a) return;
  if (libView === "albums" && libDetail && libDetail.kind === "album"
      && libDetail.key === (item.album || "")
      && libDetail.key2 === (item.album_artist || "")) {
    // The card you just clicked is the one already expanded: close it
    // rather than asking the backend for the same tracks again.
    libDetail = null;
    libSelected.clear();
    libAnchor = null;
    renderLibrary();
    return;
  }
  libTabState[libView] = captureCurrentLibTabState();
  if (libView === "albums" && libDetail && libDetail.kind === "album") {
    libPendingAlbumViewportAnchor = captureAlbumViewportAnchor(card, item);
  }
  libSelected.clear();
  libAnchor = null;
  libPending++;
  if (libView === "albums") a.library_request_detail("album", item.album, item.album_artist);
  else if (libView === "artists") a.library_request_detail("artist", item.artist, "");
  else a.library_request_detail("genre", item.genre, "");
  schedule();
});

/* Same rules as the playlist, so selection behaves the same wherever you
   are: plain click replaces and sets the anchor, ctrl toggles one row and
   moves it, shift takes the run from the anchor, ctrl+shift adds that run.
   The anchor does not move on a shift click, so a range can be widened and
   narrowed from one starting point.

   The run is measured over the rows as displayed, which in an artist or
   genre view are split by album headings: a range spanning two albums
   selects what is visually between them and nothing else. */
function libRowPaths(sel) {
  const scope = sel ? `#libgrid .librow${sel}` : "#libtracks .librow";
  return Array.from(document.querySelectorAll(scope)).map((r) => r.dataset.path);
}

function libRange(fromPath, toPath, sel) {
  const paths = libRowPaths(sel);
  let a = paths.indexOf(fromPath);
  let b = paths.indexOf(toPath);
  if (a < 0 || b < 0) return null;
  if (a > b) { const t = a; a = b; b = t; }
  return paths.slice(a, b + 1);
}

$("libtracks").addEventListener("click", (e) => {
  const row = e.target.closest(".librow");
  if (!row) return;
  const path = row.dataset.path;

  if (e.shiftKey) {
    const run = libAnchor === null ? null : libRange(libAnchor, path);
    if (run) {
      if (!e.ctrlKey) libSelected = new Set();
      run.forEach((x) => libSelected.add(x));
    } else {
      libSelected = new Set([path]);
      libAnchor = path;
    }
  } else if (e.ctrlKey) {
    libSelected.has(path) ? libSelected.delete(path) : libSelected.add(path);
    libAnchor = path;
  } else {
    libSelected = new Set([path]);
    libAnchor = path;
  }
  paintLibSelection();
});

$("libtracks").addEventListener("dblclick", (e) => {
  const row = e.target.closest(".librow");
  if (!row) return;
  playLibraryContextFrom(row.dataset.path);
});

$("lib-back").addEventListener("click", () => {
  libDetail = null;
  libSelected.clear();
  libAnchor = null;
  renderLibrary();
  restoreScrollExactly(
    libTabState[libView] ? libTabState[libView].gridScrollTop : $("libgrid").scrollTop,
    0
  );
  libTabState[libView] = captureCurrentLibTabState();
});
$("lib-queue").addEventListener("click", () => {
  const a = api();
  if (a) a.library_enqueue(libChosenPaths());
});
$("lib-queue-all").addEventListener("click", () => {
  // Deliberately ignores selection, unlike Add to playlist: this is the
  // one action that always means every track in what's currently open,
  // so a partial selection never has to be cleared first just to queue
  // the whole thing.
  const a = api();
  if (a && libDetail) a.library_enqueue(libDetail.items.map((t) => t.path).filter(Boolean));
});
$("lib-play").addEventListener("click", () => {
  // A button version of double-clicking the selected row: not "play only
  // the selection", but the same thing double-click does, which plays the
  // whole current view in its own order starting at that row. With
  // nothing selected, that row is the first one, same as double-clicking
  // it directly would be.
  const chosen = orderedSelectedLibraryPaths();
  if (!chosen.length) return;
  playLibraryContextFrom(chosen[0]);
});
$("lib-folders").addEventListener("click", () => {
  libShowFolders = !libShowFolders;
  libConfirmRemove = null;
  renderLibrary();
});

$("foldlist").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-remove]");
  if (!btn) return;
  const path = btn.dataset.remove;
  if (libConfirmRemove !== path) {
    libConfirmRemove = path;      // arm, and let a click elsewhere disarm
    renderFolders();
    return;
  }
  libConfirmRemove = null;
  const a = api();
  if (a) {
    a.library_remove_root(path);
    // Drop it locally so the row goes at once; the backend confirms on the
    // next state bump either way.
    //
    // The art caches are deliberately left alone. Clearing them here threw
    // the covers away on this side while the backend still had every album
    // resolved, and a resolved album costs nothing and bumps no revision,
    // so it never told us about them again and the grid came back bare
    // until the app was restarted. Entries for albums that went with the
    // folder are harmless: their cards are gone too.
    libRoots = libRoots.filter((p) => p !== path);
    renderFolders();
    schedule();
  }
});

$("lib-add").addEventListener("click", () => { const a = api(); if (a) a.library_add_folder(); });
$("lib-add-empty").addEventListener("click", () => { const a = api(); if (a) a.library_add_folder(); });
$("lib-rescan").addEventListener("click", () => { const a = api(); if (a) a.library_rescan(); });

function applyLibraryTick(tick) {
  const a = api();
  if (!a) return;
  libScanning = !!tick.library_scanning;
  if (!libOpened) return;
  if (tick.library_browser_revision !== libBrowserRevision) {
    libBrowserRevision = tick.library_browser_revision;
    if (libPending > 0) libPending--;
    a.library_get_browser().then((b) => {
      if (!b) return;

      const currentNeedle = $("libfilter").value.trim();
      const resultNeedle = (b.needle || "").trim();
      const incomingView = b.view || libView;

      // Not wrong, just no longer wanted: the user typed further, or
      // switched tabs, since this particular query was sent.
      if (incomingView !== libView || resultNeedle !== currentNeedle
          || resultNeedle !== libDesiredNeedle) {
        return;
      }

      libLoading = false;
      libView = incomingView;
      libItems = b.items || [];

      document.querySelectorAll(".libtab").forEach((x) =>
        x.classList.toggle("active", x.dataset.lib === libView));

      const saved = libTabState[libView];
      if (!(saved && !saved.stale && saved.detail)) {
        libDetail = null;
      }

      renderLibrary();
      libTabState[libView] = captureCurrentLibTabState();
      libTabState[libView].stale = false;

      // Resync rather than assume: the backend only announces art it has
      // just resolved, so anything it already had would otherwise never
      // reach a frontend whose cache has been reset.
      collectArt(0);
    }).catch(() => {});
  }
  if (tick.library_detail_revision !== libDetailRevision) {
    libDetailRevision = tick.library_detail_revision;
    if (libPending > 0) libPending--;
    a.library_get_detail().then((d) => {
      if (!d || !d.kind) return;
      libDetail = d;
      libSelected.clear();
      libAnchor = null;
      renderLibrary();
      libTabState[libView] = captureCurrentLibTabState();
      libTabState[libView].stale = false;
      if (libView === "albums" && d.kind === "album" && libPendingAlbumViewportAnchor) {
        const anchor = libPendingAlbumViewportAnchor;
        libPendingAlbumViewportAnchor = null;
        // A single extra frame was not always enough elsewhere in this
        // same grid for card sizes to settle from placeholder to real,
        // so this keeps nudging for a short window rather than trusting
        // one retry. Row stability first, then how much of the newly
        // expanded album itself is visible.
        let tries = 0;
        const settle = () => {
          restoreAlbumViewportAnchor(anchor);
          ensureExpandedAlbumVisible();
          updateExpandedBorderConnectors();
          tries++;
          if (tries < 10) requestAnimationFrame(settle);
        };
        settle();
      } else if (libView === "albums" && d.kind === "album") {
        let tries = 0;
        const settle = () => {
          ensureExpandedAlbumVisible();
          updateExpandedBorderConnectors();
          tries++;
          if (tries < 10) requestAnimationFrame(settle);
        };
        settle();
      }
    }).catch(() => {});
  }
  if (tick.library_art_revision !== libArtRevision) {
    libArtRevision = tick.library_art_revision;
    collectArt(libArtSeq);
  }
  if (tick.library_editor_revision !== libEditorRevision) {
    libEditorRevision = tick.library_editor_revision;
    a.library_get_editor_state().then((st) => {
      if (!st) return;
      libEditor = st;
      renderTagEditor();
    }).catch(() => {});
  }
  if (tick.library_revision !== libRevision) {
    libRevision = tick.library_revision;
    invalidateAllLibTabState();
    a.library_get_state().then((st) => {
      if (!st) return;
      const bits = [
        fmtCount(st.tracks || 0, "track", "tracks"),
        fmtCount(st.albums || 0, "album", "albums"),
        fmtCount(st.artists || 0, "artist", "artists"),
      ];
      libRoots = st.roots || [];
      if (libRoots.length) bits.push(fmtCount(libRoots.length, "folder", "folders"));
      if (libShowFolders) renderFolders();
      $("libstat").textContent = bits.join("   |   ");

      // Invalidated above; if the currently visible tab was one of them,
      // reload it now with whatever is currently typed rather than
      // waiting for a manual tab switch to notice.
      if (view === "library" && libOpened) {
        const current = libTabState[libView];
        if (!current || current.stale) {
          libDesiredNeedle = $("libfilter").value.trim();
          libLoading = true;
          libPending++;
          const thisView = libView;
          const thisNeedle = libDesiredNeedle;
          if (!thisNeedle && typeof a.library_get_prewarmed === "function") {
            a.library_get_prewarmed(thisView).then((b) => {
              const stillWanted = thisView === libView
                                 && libDesiredNeedle === thisNeedle;
              if (stillWanted && b && Array.isArray(b.items)
                  && (b.needle || "") === "") {
                libPending--;
                libLoading = false;
                libItems = b.items;
                renderLibrary();
                libTabState[libView] = captureCurrentLibTabState();
                libTabState[libView].stale = false;
                a.library_note_view(thisView, "");
              } else if (stillWanted) {
                a.library_request_browser(thisView, thisNeedle);
              } else {
                libPending--;
              }
              schedule();
            }).catch(() => {
              a.library_request_browser(thisView, thisNeedle);
              schedule();
            });
          } else {
            a.library_request_browser(thisView, thisNeedle);
            schedule();
          }
        }
      }
    }).catch(() => {});
  }
}

/* ---------- theme color ---------- */

function hexToHsl(hex) {
  hex = hex.replace("#", "");
  const r = parseInt(hex.slice(0, 2), 16) / 255;
  const g = parseInt(hex.slice(2, 4), 16) / 255;
  const b = parseInt(hex.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h, s;
  const l = (max + min) / 2;
  if (max === min) {
    h = s = 0;
  } else {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h /= 6;
  }
  return { h: h * 360, s: s * 100, l: l * 100 };
}

function hslToHex(h, s, l) {
  h = ((h % 360) + 360) % 360;
  s = Math.max(0, Math.min(100, s)) / 100;
  l = Math.max(0, Math.min(100, l)) / 100;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = l - c / 2;
  let r, g, b;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  const toHex = (v) => Math.round((v + m) * 255).toString(16).padStart(2, "0");
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

function hexToRgbTriplet(hex) {
  hex = hex.replace("#", "");
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return `${r},${g},${b}`;
}

/* Ratios of each shade's saturation/lightness to the base's own, taken
   directly from the original red palette (--accent-hi #e04b3c as base):
     accent      S x0.836  L x0.842
     accent-dim  S x0.821  L x0.627
     accent-wash S x0.671  L x0.275
   Same hue throughout - only saturation and lightness scale - so picking
   any base color reproduces the same relative shading the red theme
   already has, rather than a fixed offset that would land wrong for a
   very different starting saturation/lightness. */
// panel/panel-2/line/etc were hand-tuned with their own warm reddish
// undertone in the original red theme, at a lower saturation than the
// accent itself. These now share the accent's exact hue (no offset -
// an earlier version preserved each one's original hue OFFSET from the
// accent, which kept things in the same color family for red, but the
// same fixed rotation pushed a very different base hue like teal or
// yellow into a completely different perceived color - confirmed by
// sampling actual rendered pixels showing the panel's hue drifting by
// the same ~35deg the offset applied, regardless of direction). --bg
// is deliberately left alone: unlike the others, it was independently
// blue-toned to begin with, not on the same warm-tint family at all.
const ORIGINAL_BASE_S = 72.6;  // saturation of the original --accent-hi
// sRatio: this neutral's original saturation as a fraction of the
// original base's saturation - scaling by the CURRENT base's own
// saturation means a desaturated pick (gray/black/white) correctly
// produces a desaturated (truly neutral) result. A fixed absolute
// saturation here (the previous version) applied the same tint no
// matter what was picked, including pure black or white - verified
// directly: black, white, and gray all produced the identical
// #1c1419 before this fix.
const NEUTRAL_ORIGINALS = {
  panel: { sRatio: 16.7 / ORIGINAL_BASE_S, l: 9.4 },
  panel2: { sRatio: 10.8 / ORIGINAL_BASE_S, l: 12.7 },
  line: { sRatio: 10.5 / ORIGINAL_BASE_S, l: 14.9 },
  sliderTrack: { sRatio: 12.2 / ORIGINAL_BASE_S, l: 16.1 },
  tctlHover: { sRatio: 11.1 / ORIGINAL_BASE_S, l: 17.6 },
  scrollbarThumb: { sRatio: 10.9 / ORIGINAL_BASE_S, l: 18.0 },
  tctlActive: { sRatio: 11.9 / ORIGINAL_BASE_S, l: 21.4 },
  scrollbarThumbHover: { sRatio: 12.1 / ORIGINAL_BASE_S, l: 25.9 },
};

function deriveNeutrals(baseHex) {
  const { h, s } = hexToHsl(baseHex);
  const out = {};
  for (const k in NEUTRAL_ORIGINALS) {
    const o = NEUTRAL_ORIGINALS[k];
    out[k] = hslToHex(h, s * o.sRatio, o.l);
  }
  return out;
}

function deriveTheme(baseHex) {
  const { h, s, l } = hexToHsl(baseHex);
  return {
    hi: baseHex,
    accent: hslToHex(h, s * 0.836, l * 0.842),
    dim: hslToHex(h, s * 0.821, l * 0.627),
    wash: hslToHex(h, s * 0.671, l * 0.275),
  };
}

// Canvas fillStyle/strokeStyle cannot read CSS custom properties, so the
// waveform/spectrum/oscilloscope/tunnel/belt drawing code below reads
// these plain JS variables instead - kept in sync with the CSS variables
// by applyTheme(). Melt (vizMode 5) uses its own independent palette
// system entirely and never reads these.
let themeHi = "#e04b3c";
let themeDim = "#8e2b24";
let themeHiRgb = "224,75,60";

// The canvas-drawn colors animate smoothly toward these targets instead
// of jumping instantly - see startColorAnimation below. themeHi/themeDim/
// themeHiRgb above stay as the immediate target (read by the picker's
// own logic); animHiRgb/animDimRgb below are what the drawing code
// actually paints with, each frame, while catching up to that target.
let animHiRgb = { r: 224, g: 75, b: 60 };
let animDimRgb = { r: 142, g: 43, b: 36 };
let colorAnimFromHi = null, colorAnimFromDim = null;
let colorAnimTargetHi = null, colorAnimTargetDim = null;
let colorAnimStart = 0, colorAnimHandle = null;
const COLOR_ANIM_MS = 220;

function lerp(a, b, t) { return a + (b - a) * t; }

function startColorAnimation(targetHi, targetDim) {
  colorAnimFromHi = { ...animHiRgb };
  colorAnimFromDim = { ...animDimRgb };
  colorAnimTargetHi = targetHi;
  colorAnimTargetDim = targetDim;
  colorAnimStart = performance.now();
  if (colorAnimHandle) return; // already animating; the new target above
                                // is picked up by the in-flight loop
  const step = () => {
    const t = Math.min(1, (performance.now() - colorAnimStart) / COLOR_ANIM_MS);
    animHiRgb = {
      r: lerp(colorAnimFromHi.r, colorAnimTargetHi.r, t),
      g: lerp(colorAnimFromHi.g, colorAnimTargetHi.g, t),
      b: lerp(colorAnimFromHi.b, colorAnimTargetHi.b, t),
    };
    animDimRgb = {
      r: lerp(colorAnimFromDim.r, colorAnimTargetDim.r, t),
      g: lerp(colorAnimFromDim.g, colorAnimTargetDim.g, t),
      b: lerp(colorAnimFromDim.b, colorAnimTargetDim.b, t),
    };
    // Only the waveform needs an explicit nudge to redraw - it skips
    // redrawing when nothing about position/size/peaks changed since
    // the last paint. The other canvas visualizers already redraw every
    // frame on their own as part of reacting to the music, so they pick
    // up the animated color on their next frame regardless.
    prev.waveSig = null;
    if (t < 1) {
      colorAnimHandle = requestAnimationFrame(step);
    } else {
      colorAnimHandle = null;
    }
  };
  colorAnimHandle = requestAnimationFrame(step);
}

function animHiCss() {
  return `rgb(${Math.round(animHiRgb.r)},${Math.round(animHiRgb.g)},${Math.round(animHiRgb.b)})`;
}
function animDimCss() {
  return `rgb(${Math.round(animDimRgb.r)},${Math.round(animDimRgb.g)},${Math.round(animDimRgb.b)})`;
}
function animHiTriplet() {
  return `${Math.round(animHiRgb.r)},${Math.round(animHiRgb.g)},${Math.round(animHiRgb.b)}`;
}

function applyTheme(baseHex) {
  if (!/^#[0-9a-fA-F]{6}$/.test(baseHex || "")) return;
  const shades = deriveTheme(baseHex);
  const neutrals = deriveNeutrals(baseHex);
  const root = document.documentElement.style;
  root.setProperty("--accent-hi", shades.hi);
  root.setProperty("--accent", shades.accent);
  root.setProperty("--accent-rgb", hexToRgbTriplet(shades.accent).replace(/,/g, " "));
  root.setProperty("--accent-dim", shades.dim);
  root.setProperty("--accent-wash", shades.wash);
  root.setProperty("--panel", neutrals.panel);
  root.setProperty("--panel-rgb", hexToRgbTriplet(neutrals.panel).replace(/,/g, " "));
  root.setProperty("--panel-2", neutrals.panel2);
  root.setProperty("--line", neutrals.line);
  root.setProperty("--slider-track", neutrals.sliderTrack);
  root.setProperty("--tctl-hover", neutrals.tctlHover);
  root.setProperty("--scrollbar-thumb", neutrals.scrollbarThumb);
  root.setProperty("--tctl-active", neutrals.tctlActive);
  root.setProperty("--scrollbar-thumb-hover", neutrals.scrollbarThumbHover);
  themeHi = shades.hi;
  themeDim = shades.dim;
  themeHiRgb = hexToRgbTriplet(shades.hi);
  startColorAnimation(hexToRgb(shades.hi), hexToRgb(shades.dim));
}

// ---- custom HSV picker: RGB<->HSV, independent from the HSL helpers
// above (those serve deriveTheme/deriveNeutrals specifically) ----
function hexToRgb(hex) {
  hex = hex.replace("#", "");
  return {
    r: parseInt(hex.slice(0, 2), 16),
    g: parseInt(hex.slice(2, 4), 16),
    b: parseInt(hex.slice(4, 6), 16),
  };
}
function rgbToHex(r, g, b) {
  const toHex = (v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0");
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}
function rgbToHsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h;
  if (d === 0) h = 0;
  else if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  h *= 60;
  if (h < 0) h += 360;
  return { h, s: max === 0 ? 0 : (d / max) * 100, v: max * 100 };
}
function hsvToRgb(h, s, v) {
  s /= 100; v /= 100;
  const c = v * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = v - c;
  let r, g, b;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return { r: (r + m) * 255, g: (g + m) * 255, b: (b + m) * 255 };
}

const SV_W = 200, SV_H = 160, HUE_W = 20, HUE_H = 160;
let pickerH = 5.5, pickerS = 72.6, pickerV = 87.8;

function drawSVSquare(hue) {
  const ctx = $("themecolor-sv").getContext("2d");
  const hueRgb = hsvToRgb(hue, 100, 100);
  ctx.fillStyle = `rgb(${Math.round(hueRgb.r)},${Math.round(hueRgb.g)},${Math.round(hueRgb.b)})`;
  ctx.fillRect(0, 0, SV_W, SV_H);
  // White -> transparent left to right adds the saturation falloff;
  // transparent -> black top to bottom adds the value falloff - the
  // standard two-gradient technique for rendering an HSV square over a
  // solid hue fill.
  const satGrad = ctx.createLinearGradient(0, 0, SV_W, 0);
  satGrad.addColorStop(0, "rgba(255,255,255,1)");
  satGrad.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = satGrad;
  ctx.fillRect(0, 0, SV_W, SV_H);
  const valGrad = ctx.createLinearGradient(0, 0, 0, SV_H);
  valGrad.addColorStop(0, "rgba(0,0,0,0)");
  valGrad.addColorStop(1, "rgba(0,0,0,1)");
  ctx.fillStyle = valGrad;
  ctx.fillRect(0, 0, SV_W, SV_H);
}

function drawHueStrip() {
  const ctx = $("themecolor-hue").getContext("2d");
  const grad = ctx.createLinearGradient(0, 0, 0, HUE_H);
  for (const deg of [0, 60, 120, 180, 240, 300, 360]) {
    const rgb = hsvToRgb(deg, 100, 100);
    grad.addColorStop(deg / 360, `rgb(${Math.round(rgb.r)},${Math.round(rgb.g)},${Math.round(rgb.b)})`);
  }
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, HUE_W, HUE_H);
}

function initThemeColorCanvases() {
  const dpr = window.devicePixelRatio || 1;
  for (const [id, w, h] of [["themecolor-sv", SV_W, SV_H], ["themecolor-hue", HUE_W, HUE_H]]) {
    const c = $(id);
    c.width = w * dpr; c.height = h * dpr;
    c.style.width = w + "px"; c.style.height = h + "px";
    c.getContext("2d").scale(dpr, dpr);
  }
  drawHueStrip(); // static - never depends on picker state
}

function pickerHexNow() {
  const rgb = hsvToRgb(pickerH, pickerS, pickerV);
  return rgbToHex(rgb.r, rgb.g, rgb.b);
}

function renderPickerUI() {
  const hex = pickerHexNow();
  $("themecolor-sv-cursor").style.left = (pickerS / 100 * SV_W) + "px";
  $("themecolor-sv-cursor").style.top = ((1 - pickerV / 100) * SV_H) + "px";
  $("themecolor-hue-cursor").style.top = (pickerH / 360 * HUE_H) + "px";
  $("themecolor-preview").style.background = hex;
  $("themecolor-hex").value = hex;
  const rgb = hsvToRgb(pickerH, pickerS, pickerV);
  $("themecolor-r").value = Math.round(rgb.r);
  $("themecolor-g").value = Math.round(rgb.g);
  $("themecolor-b").value = Math.round(rgb.b);
  // Live preview across the whole app as the picker is adjusted, but not
  // persisted yet - only OK actually commits this.
  applyTheme(hex);
}

function setPickerFromHex(hex) {
  const rgb = hexToRgb(hex);
  const hsv = rgbToHsv(rgb.r, rgb.g, rgb.b);
  pickerH = hsv.h; pickerS = hsv.s; pickerV = hsv.v;
  drawSVSquare(pickerH);
  renderPickerUI();
}

initThemeColorCanvases();

const svWrap = $("themecolor-sv-wrap");
let svDragging = false;
function svFromPointer(e) {
  const rect = $("themecolor-sv").getBoundingClientRect();
  const s = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * 100;
  const v = (1 - Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height))) * 100;
  pickerS = s; pickerV = v;
  renderPickerUI();
}
svWrap.addEventListener("pointerdown", (e) => {
  svDragging = true;
  svWrap.setPointerCapture(e.pointerId);
  svFromPointer(e);
});
svWrap.addEventListener("pointermove", (e) => { if (svDragging) svFromPointer(e); });
function svEndDrag() { svDragging = false; }
svWrap.addEventListener("pointerup", svEndDrag);
svWrap.addEventListener("pointercancel", svEndDrag);

const hueWrap = $("themecolor-hue-wrap");
let hueDragging = false;
function hueFromPointer(e) {
  const rect = $("themecolor-hue").getBoundingClientRect();
  pickerH = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height)) * 360;
  drawSVSquare(pickerH);
  renderPickerUI();
}
hueWrap.addEventListener("pointerdown", (e) => {
  hueDragging = true;
  hueWrap.setPointerCapture(e.pointerId);
  hueFromPointer(e);
});
hueWrap.addEventListener("pointermove", (e) => { if (hueDragging) hueFromPointer(e); });
function hueEndDrag() { hueDragging = false; }
hueWrap.addEventListener("pointerup", hueEndDrag);
hueWrap.addEventListener("pointercancel", hueEndDrag);

$("themecolor-hex").addEventListener("input", () => {
  let v = $("themecolor-hex").value.trim();
  if (v && !v.startsWith("#")) v = "#" + v;
  if (/^#[0-9a-fA-F]{6}$/.test(v)) setPickerFromHex(v);
});
function onRgbFieldChange() {
  const clamp = (v) => Math.max(0, Math.min(255, parseInt(v, 10) || 0));
  const r = clamp($("themecolor-r").value);
  const g = clamp($("themecolor-g").value);
  const b = clamp($("themecolor-b").value);
  const hsv = rgbToHsv(r, g, b);
  pickerH = hsv.h; pickerS = hsv.s; pickerV = hsv.v;
  drawSVSquare(pickerH);
  renderPickerUI();
}
$("themecolor-r").addEventListener("input", onRgbFieldChange);
$("themecolor-g").addEventListener("input", onRgbFieldChange);
$("themecolor-b").addEventListener("input", onRgbFieldChange);

// The color last actually committed (persisted), captured the moment the
// modal opens so Cancel can revert to it - applyTheme() above overwrites
// themeHi with whatever is being live-previewed, so this has to be
// captured separately, before any preview interaction happens.
let themeModalPrevColor = "#e04b3c";

function openThemeColorModal() {
  themeModalPrevColor = themeHi;
  setPickerFromHex(themeHi);
  $("themecolormodal").classList.add("show");
}
function closeThemeColorModal() {
  $("themecolormodal").classList.remove("show");
}

wire("theme-color-btn", openThemeColorModal);

wire("themecolor-default", () => setPickerFromHex("#e04b3c"));

wire("themecolor-cancel", () => {
  applyTheme(themeModalPrevColor);
  closeThemeColorModal();
});

wire("themecolor-ok", () => {
  const hex = pickerHexNow();
  applyTheme(hex);
  const a = api();
  if (a) a.set_theme_color(hex);
  closeThemeColorModal();
});

/* ---------- waveform ---------- */

function drawWave() {
  const c = $("wave");
  if (!c || view !== "now") return;
  const r = c.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);

  const peaks = state.peaks && state.peaks.length ? state.peaks : null;
  const progress = state.duration > 0 ? state.position / state.duration : 0;
  const sig = `${w}x${h}|${peaks ? peaks.length : 0}|${Math.floor(progress * 140)}`;
  if (sig === prev.waveSig) return;
  prev.waveSig = sig;

  // Assigning to canvas.width clears the canvas, so only do it on real change.
  if (w !== prev.waveW || h !== prev.waveH) {
    c.width = w; c.height = h; prev.waveW = w; prev.waveH = h;
  }
  const x = c.getContext("2d");
  x.clearRect(0, 0, w, h);
  if (!peaks) return;
  const n = peaks.length, bw = w / n;
  for (let i = 0; i < n; i++) {
    const bh = Math.max(2 * dpr, peaks[i] * h * 0.88);
    x.fillStyle = (i / n) <= progress ? animHiCss() : animDimCss();
    x.fillRect(i * bw + bw * 0.22, (h - bh) / 2, Math.max(1, bw * 0.56), bh);
  }
}

/* Double-click cover/wave visualizer. Its own independent poll loop
   rather than folding into the main poll(): it only needs to run for the
   brief periods it is actually open, at a much faster rate (33ms, ~30fps)
   than the rest of the app ever needs, and nothing else here cares about
   its result. 0 = off (normal cover+wave), 1 = spectrum, 2 = oscilloscope,
   3 = procedural tunnel, 4 = belt/starfield, 5 = melt-style warp -
   double-clicking cycles through all six, so the same gesture that opens
   it is also how it closes, with no separate control needed. */
let vizMode = 0;
let vizTimer = 0;
let vizW = 0, vizH = 0;
let tunnelPhase = 0;
let tunnelBlobs = null;
let beltYaw = 0;
let beltStars = null;
let beltParticles = null;
let beltPitch = 0.55;         // current spin-axis tilt, drifts over time
let beltPitchTarget = 0.55;   // where it's currently drifting toward
let beltRoll = 0;             // current tilt-AXIS orientation, drifts
                               // over time alongside pitch - pitch alone
                               // only changes how far the ring tilts
                               // around a fixed axis; roll changes which
                               // direction that axis actually points
let beltRollTarget = 0;
let beltQuietRun = 0;         // consecutive frames meaningfully quieter
                               // than the recent average - used to decide
                               // when to pick a new drift target
let beltEnergyAvg = 0.3;      // slow rolling average of overall energy,
                               // so "quiet" is relative to this track's
                               // own loudness rather than a fixed
                               // absolute number a loud/compressed
                               // master might never dip under
let beltFramesSinceAxisChange = 0;  // guarantees a change periodically
                                     // even if a quiet moment never
                                     // comes along to trigger one
let beltPrevBass = 0;         // last frame's bass level, used to detect a
                               // rising edge ("a beat just hit") for the
                               // particles' own random direction changes

// Resets every visualizer mode's own internal state - camera orbit,
// scheduler clocks, particle/star arrays, everything - without touching
// vizMode itself. Shared by stopVisualizer (which also resets vizMode to
// 0), cycleVisualizer's mode-0 branch (same), and setView's "now" branch
// (which does NOT touch vizMode - the whole point there is to land back
// on the same mode that was active before navigating away, just running
// fresh rather than resuming wherever it was left frozen).
function resetVisualizerModeState() {
  tunnelPhase = 0;
  tunnelBlobs = null;
  beltYaw = 0;
  beltStars = null;
  beltParticles = null;
  beltPitch = 0.55;
  beltPitchTarget = 0.55;
  beltRoll = 0;
  beltRollTarget = 0;
  beltQuietRun = 0;
  beltEnergyAvg = 0.3;
  beltFramesSinceAxisChange = 0;
  beltPrevBass = 0;
  meltInited = false;
  meltWaveformStateA = null;
  meltWaveformStateB = null;
  meltParticleStateA = null;
  meltParticleStateB = null;
}

function stopVisualizer() {
  if (vizMode === 0) return;
  vizMode = 0;
  resetVisualizerModeState();
  clearTimeout(vizTimer);
  $("visualizer").classList.add("hidden");
  $("artwrap").classList.remove("hidden");
  $("wave").classList.remove("hidden");
}

// The exact sequence for entering a non-zero visualizer mode - shown
// canvas, hidden artwork/waveform, cleared rect, resumed polling. Shared
// by cycleVisualizer (the proven-working double-click path) and setView
// (returning to Now Playing), so returning behaves identically to
// double-clicking back to the same mode instead of running separate,
// only-supposedly-equivalent logic.
function enterVisualizerMode() {
  $("visualizer").classList.remove("hidden");
  $("artwrap").classList.add("hidden");
  $("wave").classList.add("hidden");
  // Cleared here, synchronously, regardless of which mode is being
  // entered - not just 3/4/5. Making the canvas visible happens
  // immediately, but the first real draw for the new mode only arrives
  // asynchronously (after the next visualizer_frame() round trip), and
  // in between, whatever this canvas last held would otherwise flash on
  // screen for that gap.
  const c = $("visualizer");
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  vizPoll();
}

function cycleVisualizer() {
  vizMode = (vizMode + 1) % 6;
  if (vizMode === 0) {
    resetVisualizerModeState();
    clearTimeout(vizTimer);
    $("visualizer").classList.add("hidden");
    $("artwrap").classList.remove("hidden");
    $("wave").classList.remove("hidden");
    return;
  }
  enterVisualizerMode();
}

function vizPoll() {
  if (vizMode === 0 || view !== "now") return;
  // Bars/scope naturally go flat on silent frame data, so pausing never
  // looked wrong for them - but tunnel/belt/melt all carry their own
  // persistent motion (camera orbit, ring rotation, particle drift,
  // scheduler clocks) that was deliberately built to run independent of
  // the audio itself. "Independent of the audio" was never meant to
  // include "even when there's no audio playing at all" - without this
  // check, that motion (and the Python round-trip needed to drive it)
  // kept running indefinitely while paused or stopped, for no reason.
  // Frozen here instead: every visualizer just stops exactly where it
  // is, and resumes the moment playback actually starts again.
  if (!state.playing) {
    vizTimer = setTimeout(vizPoll, 200);
    return;
  }
  const a = api();
  if (!a) { vizTimer = setTimeout(vizPoll, 100); return; }
  a.visualizer_frame().then((frame) => {
    if (vizMode === 0 || view !== "now") return;
    drawVisualizerFrame(frame || {});
    vizTimer = setTimeout(vizPoll, 33);
  }).catch(() => {
    if (vizMode === 0 || view !== "now") return;
    vizTimer = setTimeout(vizPoll, 200);
  });
}

function drawVisualizerFrame(frame) {
  const c = $("visualizer");
  const r = c.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
  // Same "only touch canvas.width on a real change" rule as drawWave:
  // assigning it clears the canvas even when the size did not change.
  if (w !== vizW || h !== vizH) { c.width = w; c.height = h; vizW = w; vizH = h; }
  const ctx = c.getContext("2d");
  // Tunnel, belt/starfield and melt all paint their own translucent fill
  // (or, for melt, a full buffer blit) each frame instead of a hard
  // clear, so the previous frame fades into or feeds the next one rather
  // than vanishing outright.
  if (vizMode !== 3 && vizMode !== 4 && vizMode !== 5) ctx.clearRect(0, 0, w, h);
  if (vizMode === 1) drawSpectrumBars(ctx, w, h, frame.bars || []);
  else if (vizMode === 2) drawOscilloscope(ctx, w, h, frame.wave || []);
  else if (vizMode === 3) drawTunnel(ctx, w, h, frame.bars || [], frame.wave || []);
  else if (vizMode === 4) drawBelt(ctx, w, h, frame.bars || [], frame.wave || []);
  else if (vizMode === 5) drawMelt(ctx, w, h, frame.bars || [], frame.wave || []);
}

function drawSpectrumBars(ctx, w, h, bars) {
  if (!bars.length) return;
  const gap = Math.max(1, w * 0.006);
  const bw = (w - gap * (bars.length - 1)) / bars.length;
  for (let i = 0; i < bars.length; i++) {
    const bh = Math.max(2, bars[i] * h * 0.92);
    const x = i * (bw + gap);
    const grad = ctx.createLinearGradient(0, h - bh, 0, h);
    grad.addColorStop(0, animHiCss());
    grad.addColorStop(1, animDimCss());
    ctx.fillStyle = grad;
    ctx.fillRect(x, h - bh, bw, bh);
  }
}

function drawOscilloscope(ctx, w, h, wave) {
  if (wave.length < 2) return;
  ctx.strokeStyle = animHiCss();
  ctx.lineWidth = Math.max(1.5, w * 0.003);
  ctx.beginPath();
  const stepX = w / (wave.length - 1);
  for (let i = 0; i < wave.length; i++) {
    const x = i * stepX;
    const y = h / 2 - wave[i] * h * 0.45;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

function _bandAvg(bars, lo, hi) {
  if (!bars.length) return 0;
  lo = Math.max(0, lo); hi = Math.min(bars.length, hi);
  if (hi <= lo) return 0;
  let sum = 0;
  for (let i = lo; i < hi; i++) sum += bars[i];
  return sum / (hi - lo);
}

/* Aorta-style procedural tunnel: concentric rings receding toward a
   vanishing point at the centre, continuously advancing toward the
   viewer, with a handful of glowing blobs flowing along the same path.
   Bass (low bars) drives how fast the tunnel rushes past and how hard it
   pulses; ring count/spacing itself now answers to loudness (fewer,
   further-spaced rings when quiet, more, tighter ones when loud) rather
   than staying a fixed count regardless of the music; each ring's own
   shape is sampled directly from the raw waveform around its
   circumference, not the (already frequency-smoothed) spectrum bars -
   the waveform is inherently rougher and can push a ring both outward
   and inward, rather than only ever bulging outward; each blob tracks
   one specific bar the whole time it's alive, so a particular blob's
   brightness answers to a particular part of the spectrum rather than
   the mix as a whole. */
function drawTunnel(ctx, w, h, bars, wave) {
  const cx = w / 2, cy = h / 2;
  const maxR = Math.hypot(cx, cy) * 1.05;

  const bass = _bandAvg(bars, 0, 6);
  const mid = _bandAvg(bars, 6, 20);
  const overall = _bandAvg(bars, 0, bars.length);

  tunnelPhase = (tunnelPhase + 0.006 + bass * 0.05) % 1;

  // A genuine clear, not a translucent dark wash: the previous version's
  // near-black fillRect compounded frame over frame into a solid backdrop
  // that hid #nowplaying's own warm gradient behind it entirely. The
  // rings themselves (many of them, continuously advancing) already read
  // as a continuous flowing tunnel without needing frame-to-frame smear
  // to sell the motion.
  ctx.clearRect(0, 0, w, h);

  // Ring count answers to loudness directly: the same depth range (0..1)
  // divided among fewer rings during a quiet passage spaces them further
  // apart, and among more during a loud one packs them tighter - the
  // tunnel's density is audio-reactive, not just its shape.
  const rings = Math.max(10, Math.round(10 + overall * 26));
  const segments = 48;   // coarser than the smooth-shape version (was 72):
                          // fewer points per ring means the waveform's own
                          // jaggedness reads as visible angles rather than
                          // being oversampled into a soft ripple.
  for (let i = 0; i < rings; i++) {
    const z = ((i / rings) + tunnelPhase) % 1;
    const depth = z * z;
    const radius = depth * maxR;
    if (radius < 2) continue;
    // The oscilloscope draws one line at flat alpha=1, no fading at all.
    // Depth-based fading is a real 3D cue worth keeping (rings recede
    // into black), but with a high floor (0.6) instead of starting from
    // zero, and loudness no longer dims it further on top of that.
    const alpha = 0.6 + z * 0.4;
    ctx.beginPath();
    for (let s = 0; s <= segments; s++) {
      const a = (s / segments) * Math.PI * 2 + tunnelPhase * 2 + i * 0.15;
      // Sampled from the raw waveform rather than the spectrum bars -
      // the waveform is inherently rougher/spikier, and each ring reads
      // from a slightly different offset into it (i*0.13) so successive
      // rings don't all repeat the identical shape. Signed (-1..1), so a
      // ring bulges outward on a peak and pulls inward on a trough,
      // rather than only ever bulging outward the way a magnitude-only
      // value would.
      const t = ((s / segments) + i * 0.13) % 1;
      const sample = _sampleArray(wave, t);
      const wobble = 1 + sample * (0.5 + mid * 0.4)
                       + Math.sin(a * 4 + tunnelPhase * 6) * 0.03;
      const r = radius * wobble;
      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r;
      if (s === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();
    // Same red as the waveform display (#e04b3c), brightness carried
    // entirely by alpha - no hue shift toward orange/white as it gets
    // brighter or closer, just this one red at varying intensity.
    ctx.strokeStyle = `rgba(${animHiTriplet()},${alpha.toFixed(3)})`;
    // Thicker across the board: a thin anti-aliased stroke only covers a
    // sliver of each pixel it crosses, so it reads as lighter than a
    // solid-filled shape (the waveform's bars) even at the same color and
    // alpha. Floor raised well past 1px, and the near/far scaling kept
    // but off a higher base.
    ctx.lineWidth = Math.max(2.5, 2 + z * 4);
    ctx.stroke();
  }

  if (!tunnelBlobs) {
    const count = 7;
    tunnelBlobs = Array.from({ length: count }, (_, i) => ({
      z: i / count,
      angle: (i / count) * Math.PI * 2,
      angleSpeed: (i % 2 === 0 ? 1 : -1) * (0.004 + i * 0.0015),
      bar: Math.floor((i / count) * bars.length),
    }));
  }
  for (const blob of tunnelBlobs) {
    blob.z = (blob.z + 0.004 + bass * 0.03) % 1;
    blob.angle += blob.angleSpeed;
    const depth = blob.z * blob.z;
    const radius = depth * maxR * 0.8;
    const x = cx + Math.cos(blob.angle) * radius;
    const y = cy + Math.sin(blob.angle) * radius;
    const energy = bars[blob.bar] || 0;
    const size = Math.max(2, (2 + energy * 16) * (0.3 + blob.z));
    const alpha = Math.min(1, blob.z * 1.4);
    const grad = ctx.createRadialGradient(x, y, 0, x, y, size * 2);
    grad.addColorStop(0, `rgba(255,190,150,${alpha.toFixed(3)})`);
    grad.addColorStop(1, `rgba(${animHiTriplet()},0)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(x, y, size * 2, 0, Math.PI * 2);
    ctx.fill();
  }
}

/* A minimal pseudo-3D camera: rotate a point around the vertical (yaw,
   the orbit) then a fixed tilt (pitch, so the ring reads as a ring and
   not a flat line viewed edge-on), then project with a simple
   perspective divide. Reused for both the belt ring and the particles
   flowing through it, so they stay consistent with each other in the
   same imagined 3D space rather than each doing their own unrelated
   math. */
function _project3D(x, y, z, yaw, pitch, roll, cx, cy, focal, scale) {
  // Roll rotates the local (x,y) coordinates first - this is what
  // reorients which direction gets treated as the tilt axis.
  const cosR = Math.cos(roll), sinR = Math.sin(roll);
  const rx = x * cosR - y * sinR;
  const ry = x * sinR + y * cosR;

  // Pitch applied BEFORE yaw, not after. Yaw is the continuous orbit
  // (constantly incrementing) and only ever mixes x/z - if pitch is
  // applied after yaw, screen-X only ever depends on yaw and roll, and
  // at yaw=90/270 degrees (which the orbit sweeps through every single
  // cycle, unavoidably) screen-X collapses to exactly zero for *every*
  // point on the ring regardless of what pitch or roll are - the ring
  // was flattening to a dead vertical line once per orbit no matter the
  // tilt, not because the tilt wasn't changing, but because this order
  // structurally couldn't let pitch prevent that collapse in the first
  // place. Applying pitch first gives the ring genuine z-depth (via pz)
  // before yaw ever runs, so yaw's own x/z mixing carries that depth
  // into screen-X instead of screen-X depending on yaw alone. Verified
  // by direct comparison: at yaw=90 degrees with pitch=0.6, the old
  // order's widest point was 0.0000 (collapsed) versus this order's
  // 31.6 (still clearly open).
  const cosP = Math.cos(pitch), sinP = Math.sin(pitch);
  const py = ry * cosP - z * sinP;
  const pz = ry * sinP + z * cosP;

  const cosY = Math.cos(yaw), sinY = Math.sin(yaw);
  const x1 = rx * cosY + pz * sinY;
  const z1 = -rx * sinY + pz * cosY;
  const y2 = py;
  const z2 = z1;
  // Floored at a fraction of focal, not just 1px: at a large pitch angle
  // combined with a ring radius comparable to focal itself, z2 can swing
  // close to -focal, which sent depth near zero and the perspective
  // divide (focal/depth) toward infinity - individual points flying far
  // outside the canvas. This keeps the divide bounded regardless of
  // pitch or radius.
  const depth = Math.max(focal + z2, focal * 0.35);
  const p = focal / depth;
  return { x: cx + x1 * p * scale, y: cy + y2 * p * scale, depth: z2, p };
}

/* Cosmic Belt-style: a colored ring "belt" reacting to the music, with a
   camera continuously orbiting around it (the yaw advances every frame,
   independent of audio - the orbit itself is constant, ambient motion,
   not something the music starts or stops), particles flowing through
   the tunnel formed by the ring, and a starfield rotating slowly in the
   background at its own, slower, independent rate - three layers of
   motion at different speeds rather than one thing spinning.

   The starfield and particles both store only resolution-independent
   fractions (an angle, a 0..1 radius/depth fraction) rather than actual
   pixel values baked in once at whatever size the canvas happened to be
   the first time this ran - both arrays are created lazily and cached
   for the life of the run, so if their stored values were canvas-scale
   pixels, resizing the window afterward would never rescale them; they'd
   just stay sized for whatever the canvas used to be. Everything
   canvas-scale is instead computed fresh from the *current* w/h every
   frame, so a resize (or simply running at a different size than
   whatever happened to be current on first use) rescales correctly. */
function drawBelt(ctx, w, h, bars, wave) {
  const cx = w / 2, cy = h / 2;
  // Raised from the original 0.9x: verified by simulation (see the
  // commit message) that the ring's own radius, combined with ordinary
  // perspective magnification at some pitch/yaw combinations, could
  // project points well past the canvas edge even at pitch values close
  // to the original fixed 0.55 - not something the pitch-drift feature
  // introduced on its own, just newly exposed by it sweeping the range
  // of pitches that would eventually be hit anyway as yaw continuously
  // rotates through every angle. A longer focal length means a smaller,
  // safer range of apparent magnification across all of that.
  const focal = Math.max(w, h) * 1.7;
  const scale = 1;
  const bass = _bandAvg(bars, 0, 6);
  const mid = _bandAvg(bars, 6, 20);
  const overall = _bandAvg(bars, 0, bars.length);

  beltYaw += 0.006 + bass * 0.01;

  // The spin axis itself wanders, rather than orbiting on one fixed, flat
  // tilt forever. "Quiet" is relative to this track's own recent average
  // energy (beltEnergyAvg), not a fixed absolute number - a loud,
  // loudness-normalized master might never dip under a fixed threshold
  // like the previous version used, which is why the axis effectively
  // never changed in practice. A periodic guaranteed change is also
  // mixed in, so it doesn't depend entirely on a quiet moment showing up.
  beltEnergyAvg += (overall - beltEnergyAvg) * 0.01;
  beltFramesSinceAxisChange++;
  const isQuiet = overall < beltEnergyAvg * 0.7;
  beltQuietRun = isQuiet ? beltQuietRun + 1 : 0;
  const dueForChange = beltFramesSinceAxisChange > 600;  // ~20s at 30fps,
                                                          // whether or not
                                                          // a quiet moment
                                                          // ever triggered
                                                          // one first
  if ((beltQuietRun > 15 && Math.random() < 0.04) || dueForChange) {
    // A narrower range than first tried (was 0.2-1.2): combined with the
    // ring's own radius, a pitch near the far end of that range pushed
    // the perspective divide (see _project3D's depth clamp) hard enough
    // to send ring points flying off past the bottom of the canvas. This
    // range still gives a clearly different tilt each time without
    // reaching that instability.
    beltPitchTarget = 0.35 + Math.random() * 0.55;
    // Roll can be any orientation at all (a full turn) - this is what
    // actually reorients the tilt axis itself, not just how far it
    // tilts around a fixed one.
    beltRollTarget = Math.random() * Math.PI * 2;
    beltQuietRun = 0;
    beltFramesSinceAxisChange = 0;
  }
  beltPitch += (beltPitchTarget - beltPitch) * 0.008;
  // Eased via the shortest angular distance, not a plain subtraction -
  // roll wraps at 2*PI, so a plain (target - current) could ease the
  // long way around (e.g. from 0.1 to 6.2 the long way through 3.15,
  // instead of the short way backward through 0) depending on where the
  // two values happen to land relative to the wrap point.
  let rollDiff = (beltRollTarget - beltRoll) % (Math.PI * 2);
  if (rollDiff > Math.PI) rollDiff -= Math.PI * 2;
  if (rollDiff < -Math.PI) rollDiff += Math.PI * 2;
  beltRoll += rollDiff * 0.008;

  ctx.fillStyle = "rgba(6,3,4,0.4)";
  ctx.fillRect(0, 0, w, h);

  // Starfield: each star stores a local (lx,ly) position, both axes
  // independently covering -1..1, rotated by the field's own slow spin
  // and then scaled to the canvas's actual half-width/half-height
  // separately. A polar (angle + radius) distribution was tried first,
  // but a circle or ellipse inscribed in a rectangle never actually
  // reaches that rectangle's corners no matter how large it's made -
  // only a true per-axis rectangular spread does, which is what this is.
  // A little overscan (1.2x) keeps corners covered through the rotation
  // too, rather than the rotated field's own corners falling just short
  // of the canvas's as it spins.
  if (!beltStars) {
    beltStars = Array.from({ length: 130 }, () => ({
      lx: Math.random() * 2 - 1,
      ly: Math.random() * 2 - 1,
      depth: Math.random(),
      twinkle: Math.random() * Math.PI * 2,
    }));
  }
  const starYaw = beltYaw * 0.18;
  const cosSY = Math.cos(starYaw), sinSY = Math.sin(starYaw);
  const starHalfW = (w / 2) * 1.2, starHalfH = (h / 2) * 1.2;
  const starSizeScale = Math.max(0.5, Math.min(w, h) / 700);
  for (const star of beltStars) {
    const rx = star.lx * cosSY - star.ly * sinSY;
    const ry = star.lx * sinSY + star.ly * cosSY;
    const x = cx + rx * starHalfW;
    const y = cy + ry * starHalfH;
    const tw = 0.5 + 0.5 * Math.sin(star.twinkle + beltYaw * 8);
    const size = (0.6 + star.depth * 1.8) * starSizeScale;
    ctx.fillStyle = `rgba(255,235,225,${(0.15 + tw * 0.5 * star.depth).toFixed(3)})`;
    ctx.beginPath();
    ctx.arc(x, y, size, 0, Math.PI * 2);
    ctx.fill();
  }

  // A second, independent full-screen field ("the moving stars") -
  // deliberately NOT tied to the ring's own 3D projection/tilt at all
  // anymore. Every previous version of this kept them on the ring's own
  // tilted plane (first via _project3D with a shared pitch, then merely
  // decoupling the yaw while still sharing the pitch), which is exactly
  // why they kept reading as a narrow band following the ring's
  // orientation instead of a field that fills the screen. Same
  // rectangular-coverage technique as the background starfield (see
  // beltStars above), but each one drifts slowly and independently -
  // no rotation of any kind, around any axis, shared or otherwise - and
  // reacts to one spectrum bar's energy for its own size/brightness.
  // Unlike the plain white starfield, each of these also trails a long,
  // tapered tracer behind it - the one visual difference between the
  // two star layers, not just a color swap.
  if (!beltParticles) {
    beltParticles = Array.from({ length: 46 }, () => {
      const heading = Math.random() * Math.PI * 2;
      return {
        lx: Math.random() * 2 - 1,
        ly: Math.random() * 2 - 1,
        // Speed is a fixed pixel-space magnitude, tracked completely
        // separately from heading and never touched after creation -
        // see the note below on why that separation matters.
        speed: 0.2 + Math.random() * 0.6,
        heading,           // current direction of travel, a true
                           // pixel-space angle (not a vector)
        targetHeading: heading,
        bar: Math.floor(Math.random() * bars.length),
        trail: [],   // this particle's own past positions (normalized
                     // lx/ly), oldest first
      };
    });
  }
  const particleHalfW = (w / 2) * 1.05, particleHalfH = (h / 2) * 1.05;
  const TRAIL_LEN = 350;   // one point pushed per frame, capped here -
                           // safe to make this fairly long, since the
                           // whole trail is stroked as a single path in
                           // one stroke() call per particle below, not
                           // one call per point/segment - a path with
                           // many vertices costs about the same as one
                           // with few, unlike the earlier per-segment
                           // version that called stroke() separately for
                           // every point (that was the real performance
                           // problem, not the point count itself)

  // A simple rising-edge bass detector, shared across every particle
  // (bass is the same value for all of them this frame) - approximates
  // "a beat just hit": a meaningful jump in bass energy from one frame
  // to the next. Loosened from the first version (0.4/+0.08) so turns
  // trigger more often, not just on the biggest transients.
  const beltBassHit = bass > 0.3 && bass > beltPrevBass + 0.05;
  beltPrevBass = bass;

  for (const particle of beltParticles) {
    // Direction change: up to +/-33 degrees, triggered (with its own
    // per-particle chance, so they don't all turn together) on a bass
    // hit - this is what makes the trail curve, since it's drawn through
    // this particle's own actual past positions below, not a straight
    // extrapolation of its current heading.
    //
    // Heading and speed are tracked completely separately, and eased as
    // an ANGLE (shortest angular distance, same technique used for the
    // ring's own roll), not as a vector. The first version eased the
    // velocity VECTOR directly toward a rotated target - which sounds
    // equivalent, but linearly interpolating between two vectors of
    // equal length actually cuts a shorter path between them (a chord,
    // not an arc), so the vector's own magnitude dips during every
    // transition. With turns triggering often (as asked for), a new
    // turn frequently fired before the previous one finished easing, and
    // each new target got computed from that already-shrunken vector -
    // so every turn ratcheted the speed down a little further, which
    // compounded into the particles visibly slowing/shrinking over a
    // whole song. Easing an angle instead has no such shrinkage: speed
    // stays exactly what it was set to, permanently.
    const wantsTurn = (beltBassHit && Math.random() < 0.75) || Math.random() < 0.01;
    if (wantsTurn) {
      particle.targetHeading = particle.heading + (Math.random() * 2 - 1) * (33 * Math.PI / 180);
    }
    let headingDiff = (particle.targetHeading - particle.heading) % (Math.PI * 2);
    if (headingDiff > Math.PI) headingDiff -= Math.PI * 2;
    if (headingDiff < -Math.PI) headingDiff += Math.PI * 2;
    particle.heading += headingDiff * 0.06;

    // A gentle, bass-nudged drift - not a spin of any kind. Wraps back
    // in from the opposite edge rather than accelerating away, so this
    // stays a continuous field instead of eventually draining off one
    // side.
    const pvx = Math.cos(particle.heading) * particle.speed;
    const pvy = Math.sin(particle.heading) * particle.speed;
    particle.lx += (pvx / particleHalfW) * (1 + bass * 2);
    particle.ly += (pvy / particleHalfH) * (1 + bass * 2);
    let wrapped = false;
    if (particle.lx > 1.1) { particle.lx = -1.1; wrapped = true; }
    if (particle.lx < -1.1) { particle.lx = 1.1; wrapped = true; }
    if (particle.ly > 1.1) { particle.ly = -1.1; wrapped = true; }
    if (particle.ly < -1.1) { particle.ly = 1.1; wrapped = true; }

    const x = cx + particle.lx * particleHalfW;
    const y = cy + particle.ly * particleHalfH;
    const energy = bars[particle.bar] || 0;
    const size = Math.max(1, 1.3 + energy * 4.5) * starSizeScale;
    const alpha = Math.min(1, 0.35 + energy * 0.65);

    // Wrapping clears the trail outright rather than carrying it over -
    // a straight line from the old side of the screen to the new one
    // would otherwise connect them across the whole canvas. Stored as
    // normalized lx/ly, not absolute pixel x/y, so a window resize
    // rescales the whole trail correctly instead of connecting stale
    // pre-resize points to new post-resize ones.
    if (wrapped) {
      particle.trail.length = 0;
    } else {
      particle.trail.push({ lx: particle.lx, ly: particle.ly });
      if (particle.trail.length > TRAIL_LEN) particle.trail.shift();
    }

    // Just a line: one path through this particle's own recent
    // positions, stroked once with a gradient fading from fully
    // transparent at the tail (oldest end) to this particle's own
    // brightness at the head (current position). One stroke() call per
    // particle - no per-segment loop, no per-segment shadow.
    const trail = particle.trail;
    if (trail.length > 1) {
      const tail = trail[0];
      const tailX = cx + tail.lx * particleHalfW, tailY = cy + tail.ly * particleHalfH;
      const grad = ctx.createLinearGradient(tailX, tailY, x, y);
      grad.addColorStop(0, "rgba(255,205,180,0)");
      grad.addColorStop(1, `rgba(255,205,180,${alpha.toFixed(3)})`);
      ctx.strokeStyle = grad;
      ctx.lineWidth = Math.max(1, size);
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(tailX, tailY);
      for (let i = 1; i < trail.length; i++) {
        ctx.lineTo(cx + trail[i].lx * particleHalfW, cy + trail[i].ly * particleHalfH);
      }
      ctx.stroke();
    }

    ctx.fillStyle = `rgba(255,205,180,${alpha.toFixed(3)})`;
    ctx.beginPath();
    ctx.arc(x, y, size, 0, Math.PI * 2);
    ctx.fill();
  }

  // Reduced from 0.58 (then 0.5): paired with the longer focal length
  // above, this combination was verified by simulation across every
  // pitch in the drift range, every yaw angle, and several aspect
  // ratios to keep the ring's projected bounding box fully on-screen -
  // 0px overflow in all of them, versus up to ~670px with the original
  // radius/focal pairing at some pitch/yaw combinations.
  const beltRadius = Math.min(w, h) * 0.35;

  // The belt itself: a ring whose radius at each point answers to the
  // waveform, so it reads as a line reacting to the music rather than a
  // static hoop the camera merely orbits around. Pushed rougher/wilder
  // than the first pass: fewer segments (was 56) so the waveform's own
  // jaggedness reads as sharp angles rather than a gentle curve, a much
  // bigger wave-amplitude multiplier (0.32 -> 0.65) so the shape swings
  // hard rather than just wobbling, mid contribution raised too, and
  // bass now also pushes the radius directly (a genuine pulse on the
  // beat, not just texture) on top of lineWidth/glow already reacting to
  // it.
  const segments = 40;
  const points = [];
  for (let s = 0; s <= segments; s++) {
    const a = (s / segments) * Math.PI * 2;
    const waveIdx = Math.floor((s / segments) * wave.length) % Math.max(1, wave.length);
    const sample = wave.length ? wave[waveIdx] : 0;
    const r = beltRadius * (1 + sample * 1.3 + mid * 0.2 + bass * 0.18);
    const lx = Math.cos(a) * r;
    const ly = Math.sin(a) * r * 0.5;
    points.push(_project3D(lx, ly, 0, beltYaw, beltPitch, beltRoll, cx, cy, focal, scale));
  }
  ctx.beginPath();
  points.forEach((pt, i) => { if (i === 0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y); });
  ctx.lineWidth = Math.max(2.5, 2.5 + overall * 4 + bass * 3);
  ctx.strokeStyle = `rgba(${animHiTriplet()},1)`;
  ctx.shadowColor = `rgba(${animHiTriplet()},0.8)`;
  ctx.shadowBlur = 10 + overall * 14 + bass * 10;
  ctx.stroke();
  ctx.shadowBlur = 0;
}

/* ==========================================================================
   Melt-style warp visualizer (vizMode 5)
   ==========================================================================
   Inspired by the real "Smear" Sonique plugin (mykel & xplo, 2000), which
   works by continuously resampling its own previous frame through a
   per-pixel coordinate remap ("Movemap"), so anything drawn into it keeps
   getting dragged, spiraled or rippled by whatever transform is active -
   that resample-of-self is what produces the melt look, not any one single
   effect drawn fresh each frame. This is an original implementation of that
   technique, not a port of that plugin's code.

   PART 1 of this mode implements just that core warp engine, plus enough
   of a placeholder draw (the oscilloscope trace, in a single fixed color)
   to prove the buffer is actually warping frame to frame. Parts 2-4 (a
   proper 256-entry Colormap-style palette, Waveform-script-style paths,
   and Particle-script-style shapes) replace that placeholder without
   touching the warp engine itself. Part 5 adds the independent hold/fade
   rotation between presets.

   Run at a low-ish internal resolution (MELT_W x MELT_H) rather than the
   full visible canvas:
     - A true per-pixel remap needs a JS loop over every buffer pixel, every
       frame; at full canvas resolution (which can be many hundreds of
       thousands of pixels once devicePixelRatio is factored in) that loop
       alone could cost more per frame than the rest of this app's entire
       30fps budget.
     - Went 192x108 -> 384x216 -> 576x324 while chasing a reported
       slowdown, on the assumption that content-drawing optimizations
       (several were tried, on the waveform/particle side, in the git
       history around this point - all reverted) would keep pace with it.
       They didn't, and were themselves a mistake: several were built and
       benchmarked in isolation under Node, which has no real GPU/canvas
       pipeline and so cannot show whether hand-written pixel-blending
       loops are actually faster than native Canvas2D fill/stroke/arc
       calls in a real browser - in practice they were reported as worse,
       not better, and were reverted back to plain canvas calls
       (meltDrawWaveformScript/meltDrawParticleScript). Separately, the
       warp pass itself measured ~19ms/frame at 576x324 during a movemap
       crossfade, in isolation, before any content is drawn or the buffer
       is even shown - already the majority of a 33ms frame on its own, so
       no content-drawing change was ever going to fix that regardless.
       Reverted to 384x216 (2x the original 192x108, not 3x). The warp
       loop's own optimizations (see meltWarpFrame's comments -
       zero-allocation movemaps, shared radius/theta, precomputed
       coordinate tables) are kept, since those target a cost proven
       dominant by measurement rather than assumption, and are unaffected
       by which approach the content-drawing side uses. If this ever
       needs to come back down further, or go back up once there's
       headroom to spare, this is the one number to change - everything
       above scales with it automatically.
     - The softly-interpolated look this produces when the buffer is
       scaled up onto the visible canvas is not a compromise to hide - it
       is genuinely close to how a real-time software-rendered feedback
       effect actually looked at the resolutions common in 2000. */
const MELT_W = 384, MELT_H = 216;

/* Everything below depends only on MELT_W/MELT_H, which never change at
   runtime, so it's computed exactly once here rather than being redone
   for every pixel of every frame inside meltWarpFrame's hot loop:
     - MELT_NX/MELT_NY: the aspect-corrected -1..1 coordinate each column/
       row maps to (previously a division done per pixel, per frame).
     - MELT_WARP_SX_SCALE/X_OFFSET/Y_SCALE: the reverse conversion, source
       coordinate back to a buffer pixel index, reduced to one multiply-
       add instead of the divisions the original per-pixel version did. */
const MELT_ASPECT = MELT_W / MELT_H;
const MELT_NX = new Float32Array(MELT_W);
const MELT_NY = new Float32Array(MELT_H);
for (let px = 0; px < MELT_W; px++) {
  MELT_NX[px] = ((px / (MELT_W - 1)) * 2 - 1) * MELT_ASPECT;
}
for (let py = 0; py < MELT_H; py++) {
  MELT_NY[py] = (py / (MELT_H - 1)) * 2 - 1;
}
const MELT_WARP_SX_SCALE = (0.5 * (MELT_W - 1)) / MELT_ASPECT;
const MELT_WARP_X_OFFSET = 0.5 * (MELT_W - 1);
const MELT_WARP_Y_SCALE = 0.5 * (MELT_H - 1);

let meltCanvas = null;      // offscreen canvas holding the low-res buffer
let meltCtx = null;
let meltPixels = null;      // ImageData: current frame, about to be read from
let meltScratch = null;     // ImageData: next frame, being built
let meltInited = false;
let meltClock = 0;          // seconds - tracks actual elapsed wall-clock
                             // time (see meltLastFrameTime below), so it
                             // stays accurate regardless of whether real
                             // frame delivery is perfectly steady. Still
                             // naturally stops advancing whenever this
                             // mode isn't actually being polled/drawn,
                             // since nothing updates it unless drawMelt()
                             // itself runs.
let meltLastFrameTime = 0;  // performance.now() at the last drawMelt()
                             // call - used to compute the real elapsed
                             // time each frame represents, rather than
                             // assuming every call represents a fixed
                             // 1/30s. That fixed assumption was the
                             // actual bug behind melt seeming to
                             // transition much slower after returning
                             // from another view than after cycling
                             // through modes without ever leaving "now":
                             // if real frame delivery is ever irregular
                             // for any reason (a view switch doing DOM/
                             // layout work being an obvious candidate),
                             // meltClock drifted from real time
                             // regardless, since it had no way to know
                             // frames weren't landing on schedule.

/* ---------- Independent hold/fade scheduler (part 5) ----------
   Each of the four preset categories (movemap, colormap, waveform,
   particle) rotates and cross-fades entirely on its own clock, exactly
   the way the original plugin's vis.ini drove its own four categories:
   a "hold" duration for how long a preset stays active, a "fade" duration
   for the crossfade into the next one, and both randomized within +/-50%
   of their base value each time, so no two runs land on the same rhythm.

   A scheduler only tracks *which* index is active/incoming and *how far*
   the current fade has gotten (0..1, "blend"); it has no opinion on what a
   "preset" actually is. Each category's own draw code decides what to do
   with .a (current index), .b (incoming index, or -1 when not fading) and
   .blend. */
const MELT_HOLD_FADE = {
  movemap:  { hold: 14, fade: 3.0 },
  colormap: { hold: 11, fade: 2.5 },
  waveform: { hold: 13, fade: 2.5 },
  particle: { hold: 10, fade: 2.0 },
};

// +/-50% of base, matching the original's own "the actual value will be
// +/- 50% each time it is used" note for every one of its hold/fade knobs.
function meltJitter(base) {
  return base * (0.5 + Math.random());
}

function meltMakeScheduler(count, holdBase, fadeBase) {
  return {
    count, holdBase, fadeBase,
    a: Math.floor(Math.random() * count),
    b: -1,
    blend: 0,               // 0 = fully on `a`, 1 = fully on `b`
    // The very first hold, right after a fresh start, is capped short
    // (a few seconds) instead of the full jittered holdBase range.
    // meltJitter(holdBase) can roll up to 1.5x holdBase (up to ~21s for
    // the slowest category), and with four independent schedulers all
    // rolling fresh every time this mode starts, it's entirely possible
    // for all four to land on a long first hold at once - showing no
    // visible transition for a long stretch right when it starts.
    // meltTickScheduler computes every hold AFTER this first one from
    // sched.holdBase directly, unaffected by this - only the initial
    // wait is shortened, steady-state cycling pace is untouched.
    holdUntil: meltClock + meltJitter(Math.min(holdBase, 4)),
    fadeUntil: -1,
    fadeDuration: fadeBase,
  };
}

/* Advances one scheduler by one frame's worth of meltClock. Does not know
   or care what `a`/`b` mean to the caller - swapping in a newly-picked
   preset's own per-instance state (if it has any) is the caller's job,
   done by comparing `b` before and after this call (see
   meltTickAllSchedulers below). */
function meltTickScheduler(sched) {
  if (sched.count <= 1) return; // nothing else to rotate to
  if (sched.b === -1) {
    if (meltClock < sched.holdUntil) return;
    let next = sched.a;
    while (next === sched.a) next = Math.floor(Math.random() * sched.count);
    sched.b = next;
    sched.fadeDuration = meltJitter(sched.fadeBase);
    sched.fadeUntil = meltClock + sched.fadeDuration;
    sched.blend = 0;
    return;
  }
  const remaining = sched.fadeUntil - meltClock;
  sched.blend = Math.max(0, Math.min(1, 1 - remaining / sched.fadeDuration));
  if (meltClock >= sched.fadeUntil) {
    sched.a = sched.b;
    sched.b = -1;
    sched.blend = 0;
    sched.holdUntil = meltClock + meltJitter(sched.holdBase);
  }
}

let meltMovemapSched = null;
let meltColormapSched = null;
let meltWaveformSched = null;
let meltParticleSched = null;
// Waveform and particle scripts carry their own per-instance state (random
// parameters picked once when a script becomes active, then reused every
// frame it runs) - two slots each, since during a crossfade the outgoing
// and incoming script are both running and drawing at once. Movemaps and
// colormaps are pure functions of their input with no such state, so they
// need nothing equivalent.
let meltWaveformStateA = null;
let meltWaveformStateB = null;
let meltParticleStateA = null;
let meltParticleStateB = null;

/* Runs every category's scheduler for one frame, and handles the state
   handoff a plain meltTickScheduler() call can't: when a category's `b`
   is newly assigned (a fade just started), build fresh per-instance state
   for the incoming script; when `b` drops back to -1 (a fade just
   finished), what was building in slot B becomes slot A, so the preset
   that's now current keeps the same per-instance state it had been
   running with all through the fade rather than restarting fresh. */
function meltTickAllSchedulers() {
  meltTickScheduler(meltMovemapSched);
  meltTickScheduler(meltColormapSched);

  const wasWaveB = meltWaveformSched.b;
  meltTickScheduler(meltWaveformSched);
  if (meltWaveformSched.b !== -1 && meltWaveformSched.b !== wasWaveB) {
    meltWaveformStateB = MELT_WAVEFORMS[meltWaveformSched.b].init();
  } else if (wasWaveB !== -1 && meltWaveformSched.b === -1) {
    meltWaveformStateA = meltWaveformStateB;
    meltWaveformStateB = null;
  }

  const wasPartB = meltParticleSched.b;
  meltTickScheduler(meltParticleSched);
  if (meltParticleSched.b !== -1 && meltParticleSched.b !== wasPartB) {
    meltParticleStateB = MELT_PARTICLES[meltParticleSched.b].init();
  } else if (wasPartB !== -1 && meltParticleSched.b === -1) {
    meltParticleStateA = meltParticleStateB;
    meltParticleStateB = null;
  }
}

/* Each movemap takes (x, y) in aspect-corrected -1..1 space plus that same
   point's radius/theta - precomputed once per pixel by the caller, since
   every movemap here is polar-based and would otherwise recompute the
   identical Math.hypot/Math.atan2 redundantly (and, during a crossfade,
   twice over - once per active movemap on the exact same input) - and
   writes [srcX, srcY] ("read last frame's color from here instead") into
   the `out` object the caller passes in, rather than returning a freshly
   allocated array. At hundreds of thousands of buffer pixels a frame, an
   allocation per call per movemap is enough garbage-collector pressure to
   matter; a reused output object is not. The radius/theta and srcRadius/
   srcTheta naming follows the polar move-function convention documented
   for Smear's own scripting language, which this reimplements in plain JS
   rather than a separate scripting layer. */
const MELT_MOVEMAPS = [
  // Spiral zoom inward with a slow counter-rotation - the classic
  // "melt into the centre" look.
  function spiralIn(x, y, radius, theta, out) {
    const srcRadius = radius * 0.87;
    const srcTheta = theta - 0.075;
    out.x = Math.cos(srcTheta) * srcRadius;
    out.y = Math.sin(srcTheta) * srcRadius;
  },
  // Slow spiral outward with a gentle ripple layered on the radius.
  // Rotation raised 0.015 -> 0.03 (a first attempt raised this to 0.05,
  // matching/exceeding spiralIn - too aggressive: reported as choppy and
  // distorting freshly-drawn content like the cube/sphere particles too
  // quickly to read clearly). This still gives a real floor at radii
  // where the ripple term cancels toward zero, without overshooting.
  function rippleOut(x, y, radius, theta, out) {
    const srcRadius = radius + 0.04 * Math.sin(6.2831853 * radius);
    const srcTheta = theta + 0.03;
    out.x = Math.cos(srcTheta) * srcRadius;
    out.y = Math.sin(srcTheta) * srcRadius;
  },
  // A gentle two-lobed pinch: radius pulled in harder along two opposing
  // axes than the other two, so a plain circle warps into a soft square-ish
  // pulse instead of staying uniform. Base shrink brought from 0.92-0.98
  // to 0.86-0.94 (a first attempt used 0.80-0.90, matching/exceeding
  // spiralIn - same overshoot problem as rippleOut above). Meaningfully
  // stronger than the original without being as aggressive as spiralIn
  // itself.
  function pinch(x, y, radius, theta, out) {
    const srcRadius = radius * (0.86 + 0.04 * (1 + Math.sin(6 * theta)));
    out.x = Math.cos(theta) * srcRadius;
    out.y = Math.sin(theta) * srcRadius;
  },
];

/* ---------- Colormap-style palette engine (part 2) ----------
   A 256-entry palette, generated by a small preset function rather than
   authored as a fixed gradient. Two conventions carried over deliberately:
     - a palette preset is handed `value` in 0..1 (0 = darkest/background,
       1 = brightest) and returns an RGB triple - this is the palette's own
       indexing convention.
     - anything actually *drawn* with the palette (a line, a particle) is
       instead handed a `fade` in 0..1 with the opposite sense (0 =
       brightest, 1 = background), matching how a 0..1 "how much to fade
       toward the background" value reads more naturally at the call site
       than an inverted brightness would. meltPaletteFade() below is the
       one place that conversion happens, so nothing else needs to
       remember it. */
const MELT_PALETTE_SIZE = 256;

let meltPalette = null;          // Uint8ClampedArray, MELT_PALETTE_SIZE*3 (rgb triples)
                                  // meltColormapSched (declared above) now
                                  // drives which preset(s) are active and
                                  // any in-progress crossfade; meltClock
                                  // (also declared above) stands in for
                                  // each preset's own $Time.
let meltPaletteStrings = null;   // 256 precomputed "rgb(...)" CSS strings,
                                  // one per meltPalette entry - rebuilt
                                  // alongside meltPalette itself, once a
                                  // frame, rather than formatting a fresh
                                  // string on every single stroke/fill call
                                  // (previously up to several hundred a
                                  // frame between the waveform and particle
                                  // layers, doubled during a crossfade).

function _hsvToRgb(h, s, v) {
  h = ((h % 1) + 1) % 1;
  s = Math.max(0, Math.min(1, s));
  v = Math.max(0, Math.min(1, v));
  const i = Math.floor(h * 6);
  const f = h * 6 - i;
  const p = v * (1 - s);
  const q = v * (1 - f * s);
  const t = v * (1 - (1 - f) * s);
  let r, g, b;
  switch (i % 6) {
    case 0: r = v; g = t; b = p; break;
    case 1: r = q; g = v; b = p; break;
    case 2: r = p; g = v; b = t; break;
    case 3: r = p; g = q; b = v; break;
    case 4: r = t; g = p; b = v; break;
    default: r = v; g = p; b = q; break;
  }
  return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
}

/* Each preset: useTime false means the palette is generated once and left
   alone; true means meltBuildPalette() re-runs every frame with an
   advancing `time`, for a palette that itself animates (a slowly cycling
   hue, for instance) independent of anything audio-driven. */
const MELT_COLORMAPS = [
  // A static 3-stop ramp - dark teal-green through cyan to near-white at
  // the top of the range. No time dependency, so this is built once.
  {
    useTime: false,
    fn(value) {
      if (value > 0.8) {
        const t = (value - 0.8) / 0.2;
        return [Math.round(t * 255), 255, Math.round(t * 255)];
      }
      if (value > 0.5) {
        const t = (value - 0.5) / 0.6;
        return [0, Math.round((0.5 + t) * 255), Math.round((0.5 - t) * 255)];
      }
      return [0, Math.round(value * 255), Math.round(value * 255)];
    },
  },
  // A slowly hue-cycling ramp: bright and saturated at low value, fading
  // toward black at high value, with the hue itself drifting over time.
  {
    useTime: true,
    fn(value, time) {
      const h = 0.02 * time;
      const s = 1;
      const v = 1 - Math.pow(value, 1.4);
      return _hsvToRgb(h, s, v);
    },
  },
  // A warmer variant closer to this app's own red accent color: hue
  // drifts through a narrow red-orange band rather than the full wheel.
  {
    useTime: true,
    fn(value, time) {
      const h = 0.02 + 0.03 * (0.5 + 0.5 * Math.sin(time * 0.15));
      const s = 1 - 0.5 * Math.pow(value, 2.5);
      const v = Math.pow(value, 0.8);
      return _hsvToRgb(h, s, v);
    },
  },
];

function meltBuildPalette() {
  if (!meltPalette) meltPalette = new Uint8ClampedArray(MELT_PALETTE_SIZE * 3);
  if (!meltPaletteStrings) meltPaletteStrings = new Array(MELT_PALETTE_SIZE);
  const sched = meltColormapSched;
  const cmA = MELT_COLORMAPS[sched.a];
  const cmB = sched.b !== -1 ? MELT_COLORMAPS[sched.b] : null;
  for (let i = 0; i < MELT_PALETTE_SIZE; i++) {
    const value = i / (MELT_PALETTE_SIZE - 1);
    const [ra, ga, ba] = cmA.fn(value, meltClock);
    let r = ra, g = ga, b = ba;
    if (cmB) {
      const [rb, gb, bb] = cmB.fn(value, meltClock);
      r = ra + (rb - ra) * sched.blend;
      g = ga + (gb - ga) * sched.blend;
      b = ba + (bb - ba) * sched.blend;
    }
    const o = i * 3;
    meltPalette[o] = r; meltPalette[o + 1] = g; meltPalette[o + 2] = b;
    meltPaletteStrings[i] = `rgb(${meltPalette[o]},${meltPalette[o + 1]},${meltPalette[o + 2]})`;
  }
}

/* The one place fade (0=brightest, 1=background) gets turned into an
   actual color, so nothing drawing with the palette has to remember the
   inversion between a palette's own `value` and a draw call's `fade`. A
   plain array lookup now, not a template-literal string build - see
   meltPaletteStrings above. */
function meltPaletteFade(fade) {
  const value = Math.max(0, Math.min(1, 1 - fade));
  const idx = Math.round(value * (MELT_PALETTE_SIZE - 1));
  return meltPaletteStrings[idx];
}

function meltInit() {
  meltCanvas = document.createElement("canvas");
  meltCanvas.width = MELT_W;
  meltCanvas.height = MELT_H;
  meltCtx = meltCanvas.getContext("2d", { willReadFrequently: true });
  meltCtx.fillStyle = "#000";
  meltCtx.fillRect(0, 0, MELT_W, MELT_H);
  meltPixels = meltCtx.getImageData(0, 0, MELT_W, MELT_H);
  meltScratch = meltCtx.createImageData(MELT_W, MELT_H);
  meltClock = 0;
  meltLastFrameTime = performance.now();

  meltMovemapSched = meltMakeScheduler(
    MELT_MOVEMAPS.length, MELT_HOLD_FADE.movemap.hold, MELT_HOLD_FADE.movemap.fade);
  meltColormapSched = meltMakeScheduler(
    MELT_COLORMAPS.length, MELT_HOLD_FADE.colormap.hold, MELT_HOLD_FADE.colormap.fade);
  meltWaveformSched = meltMakeScheduler(
    MELT_WAVEFORMS.length, MELT_HOLD_FADE.waveform.hold, MELT_HOLD_FADE.waveform.fade);
  meltParticleSched = meltMakeScheduler(
    MELT_PARTICLES.length, MELT_HOLD_FADE.particle.hold, MELT_HOLD_FADE.particle.fade);

  meltWaveformStateA = MELT_WAVEFORMS[meltWaveformSched.a].init();
  meltWaveformStateB = null;
  meltParticleStateA = MELT_PARTICLES[meltParticleSched.a].init();
  meltParticleStateB = null;

  meltBuildPalette();
  meltInited = true;
}

/* The warp pass: for every low-res pixel, ask the active movemap where in
   last frame's buffer to read this frame's color from, then copy it.
   Nearest-neighbour sampling, not bilinear - at this resolution, scaled up
   with the browser's own image smoothing on the final drawImage, the
   difference is not visible, and nearest-neighbour is one array read
   instead of four. */
// Reused every pixel, every frame, rather than movemaps allocating a
// fresh [x,y] each call - see the comment above MELT_MOVEMAPS.
const _meltOutA = { x: 0, y: 0 };
const _meltOutB = { x: 0, y: 0 };

function meltWarpFrame() {
  const w = MELT_W, h = MELT_H;
  const src = meltPixels.data;
  const dst = meltScratch.data;
  const sched = meltMovemapSched;
  const fnA = MELT_MOVEMAPS[sched.a];
  const fnB = sched.b !== -1 ? MELT_MOVEMAPS[sched.b] : null;
  const blend = sched.blend;
  const outA = _meltOutA, outB = _meltOutB;
  for (let py = 0; py < h; py++) {
    const ny = MELT_NY[py];
    const rowOffset = py * w;
    for (let px = 0; px < w; px++) {
      const nx = MELT_NX[px];
      // Shared by both movemaps when blending: computed once here rather
      // than once inside each movemap on the same (nx,ny), which is what
      // the per-movemap version used to do redundantly during a fade.
      const radius = Math.sqrt(nx * nx + ny * ny);
      const theta = Math.atan2(ny, nx);
      fnA(nx, ny, radius, theta, outA);
      let sx = outA.x, sy = outA.y;
      // Blending two movemaps means evaluating both for every pixel during
      // a crossfade - noticeably pricier than either alone, same as the
      // original plugin's own documented warning that movemap crossfades
      // are the single biggest performance cost in the whole effect.
      if (fnB) {
        fnB(nx, ny, radius, theta, outB);
        sx += (outB.x - sx) * blend;
        sy += (outB.y - sy) * blend;
      }
      const ix = Math.round(sx * MELT_WARP_SX_SCALE + MELT_WARP_X_OFFSET);
      const iy = Math.round(sy * MELT_WARP_Y_SCALE + MELT_WARP_Y_SCALE);
      const di = (rowOffset + px) * 4;
      if (ix < 0 || ix >= w || iy < 0 || iy >= h) {
        dst[di] = 0; dst[di + 1] = 0; dst[di + 2] = 0; dst[di + 3] = 255;
        continue;
      }
      const si = (iy * w + ix) * 4;
      dst[di] = src[si]; dst[di + 1] = src[si + 1];
      dst[di + 2] = src[si + 2]; dst[di + 3] = 255;
    }
  }
  // Ping-pong the two buffers rather than copying: the scratch just built
  // becomes "current" for the draw step below, and next frame's warp will
  // read from it and write into what used to be current.
  const tmp = meltPixels; meltPixels = meltScratch; meltScratch = tmp;
}

/* ---------- Waveform-script-style paths (part 3) ----------
   Mirrors the Stutter Waveform script convention: init() builds whatever
   per-instance state the script needs (random per-selection parameters,
   the way a real Movemap's init picks its own random radii/speeds once
   and keeps them for its whole run), newline() updates that state once
   per frame, and step() is called once per point along the path (t in
   0..1) returning one {x,y,fade} per simultaneous path the script draws.
   x/y are in the same -1..1 space the movemaps use; fade follows the same
   0=brightest/1=background convention meltPaletteFade() already expects.

   Implemented directly as plain JS functions/closures rather than a
   second interpreted scripting language layered on top of the one this
   app is already written in - Stutter's own scripting layer existed
   because Sonique plugins were compiled, sandboxed native code with no
   other way to be end-user-editable; that constraint does not apply
   here, so a JS object literal per script is the equivalent expressive
   surface without the extra machinery. */
function _sampleArray(arr, t) {
  if (!arr.length) return 0;
  const idx = Math.min(arr.length - 1, Math.max(0, Math.round(t * (arr.length - 1))));
  return arr[idx];
}

/* Each script preallocates its own points once, in init() - one object per
   (path, step) pair, reused for the life of that script's run - and step()
   below mutates those objects in place rather than returning a fresh
   array-of-objects on every one of steps*numPaths calls, every frame
   (doubled during a crossfade). Building 64 or more small objects a call,
   many times a frame, was real, previously-unaddressed GC pressure -
   exactly the class of thing the movemap functions were fixed to avoid
   earlier, just never carried over to this layer. Distinct objects per
   step are still required (not one shared/reused scratch object): the
   caller needs all of a path's points at once to draw it, unlike a
   movemap's output, which is consumed immediately after each call. */
const MELT_WAVEFORMS = [
  // A single scope trace across the middle - the direct descendant of
  // the part 1/2 placeholder, now expressed as a proper waveform script.
  {
    numPaths: 1,
    steps: 64,
    init() {
      return { points: [Array.from({ length: 64 }, () => ({ x: 0, y: 0, fade: 0 }))] };
    },
    newline() {},
    step(state, t, i, wave) {
      const w = _sampleArray(wave, t);
      const p = state.points[0][i];
      p.x = t * 2 - 1; p.y = w * 0.6; p.fade = 1 - Math.min(1, Math.abs(w));
    },
  },
  // Three concentric rings, each centre slowly orbiting, radius pulsing
  // with amplitude - a callback to the "historical rings" waveform script
  // the original plugin shipped as a nod to its own predecessor.
  {
    numPaths: 3,
    steps: 64,
    init() {
      return {
        angle: 0,
        points: [
          Array.from({ length: 64 }, () => ({ x: 0, y: 0, fade: 0 })),
          Array.from({ length: 64 }, () => ({ x: 0, y: 0, fade: 0 })),
          Array.from({ length: 64 }, () => ({ x: 0, y: 0, fade: 0 })),
        ],
      };
    },
    newline(state) { state.angle += 0.01; },
    step(state, t, i, wave) {
      const theta = t * Math.PI * 2;
      const w = _sampleArray(wave, t);
      for (let p = 0; p < 3; p++) {
        const centerAngle = state.angle + (p * Math.PI * 2) / 3;
        const cx = Math.cos(centerAngle) * 0.25;
        const cy = Math.sin(centerAngle) * 0.25;
        const r = 0.15 + Math.abs(w) * 0.35;
        const pt = state.points[p][i];
        pt.x = cx + Math.cos(theta) * r;
        pt.y = cy + Math.sin(theta) * r;
        pt.fade = 1 - Math.abs(w);
      }
    },
  },
  // A bass-driven radial burst built from the spectrum bars instead of
  // the raw waveform: walking t around a circle while radius follows
  // each bar's energy traces the whole spectrum as one closed shape.
  {
    numPaths: 1,
    steps: 32,
    init() {
      return { points: [Array.from({ length: 32 }, () => ({ x: 0, y: 0, fade: 0 }))] };
    },
    newline() {},
    step(state, t, i, wave, bars) {
      const e = _sampleArray(bars, t);
      const theta = t * Math.PI * 2;
      const r = 0.1 + e * 0.7;
      const p = state.points[0][i];
      p.x = Math.cos(theta) * r; p.y = Math.sin(theta) * r; p.fade = 1 - e;
    },
  },
];

function meltDrawWaveformScript(script, state, wave, bars, alpha) {
  if (alpha <= 0) return;
  script.newline(state, wave, bars);

  const n = script.steps;
  for (let i = 0; i < n; i++) {
    const t = i / (n - 1);
    script.step(state, t, i, wave, bars);
  }

  const toX = (x) => (x * 0.5 + 0.5) * MELT_W;
  const toY = (y) => (y * 0.5 + 0.5) * MELT_H;

  meltCtx.globalAlpha = alpha;
  meltCtx.lineWidth = 1.5;
  for (let p = 0; p < script.numPaths; p++) {
    const pts = state.points[p];
    for (let i = 1; i < pts.length; i++) {
      // One stroke per segment, not one path with a single strokeStyle:
      // canvas strokes are a single flat color, so a fade that varies
      // along the path (louder = brighter) needs a stroke per segment.
      meltCtx.strokeStyle = meltPaletteFade(pts[i].fade);
      meltCtx.beginPath();
      meltCtx.moveTo(toX(pts[i - 1].x), toY(pts[i - 1].y));
      meltCtx.lineTo(toX(pts[i].x), toY(pts[i].y));
      meltCtx.stroke();
    }
  }
  meltCtx.globalAlpha = 1;
}

/* Runs whichever waveform script(s) are currently active - during a
   crossfade that's the outgoing script drawn at (1-blend) opacity and the
   incoming one at (blend) opacity, both onto the same buffer, rather than
   trying to blend their geometry directly: two differently-shaped scripts
   (a scope trace fading into three orbiting rings, say) have no natural
   shared geometry to interpolate between, but a plain opacity crossfade
   reads correctly regardless of how different the two look. */
function meltDrawWaveforms(wave, bars) {
  if (!wave.length && !bars.length) return;
  const sched = meltWaveformSched;
  meltDrawWaveformScript(
    MELT_WAVEFORMS[sched.a], meltWaveformStateA, wave, bars, 1 - sched.blend);
  if (sched.b !== -1) {
    meltDrawWaveformScript(
      MELT_WAVEFORMS[sched.b], meltWaveformStateB, wave, bars, sched.blend);
  }
}

/* ---------- Particle-script-style shapes (part 4) ----------
   Mirrors the Stutter Particle script convention: init() builds
   per-instance state including how many particles this script draws
   (state.count), newframe() updates whatever per-frame animation state
   the script needs, and particle() is called once per particle (i in
   0..state.count-1) returning a position, size and style (1 = filled
   circle using size; 2 = a line from (x,y) to (xEnd,yEnd), size ignored -
   the same two styles Stutter's own Particle scripts supported) plus a
   fade in the same 0=brightest/1=background convention everything else
   here already uses. */
function _melt3DProject(x, y, z, yaw, pitch) {
  const cosY = Math.cos(yaw), sinY = Math.sin(yaw);
  const x1 = x * cosY + z * sinY;
  const z1 = -x * sinY + z * cosY;
  const cosP = Math.cos(pitch), sinP = Math.sin(pitch);
  const y2 = y * cosP - z1 * sinP;
  const z2 = y * sinP + z1 * cosP;
  const focal = 2.2;
  const depth = focal + z2;
  const p = focal / Math.max(depth, 0.3);
  return { x: x1 * p, y: y2 * p, p };
}

/* Each script preallocates one reusable output object in init(), and
   particle() mutates it in place and returns it, rather than allocating a
   fresh object literal on every one of state.count calls, every frame
   (doubled during a crossfade). Safe to share a single object here, unlike
   the waveform scripts above: each particle is drawn immediately after
   being computed (see meltDrawParticleScript's loop) and never needs to
   be remembered alongside any other particle's data afterward. */
const MELT_PARTICLES = [
  // One particle per position around a ring, each tracking one point of
  // the spectrum: the ring's radius at that point pulses with that bar's
  // own energy, so the whole ring reads as the spectrum bent into a loop.
  {
    init() {
      return { count: 24, out: { x: 0, y: 0, xEnd: 0, yEnd: 0, size: 0, style: 1, fade: 0 } };
    },
    newframe() {},
    particle(state, i, wave, bars) {
      const t = i / state.count;
      const theta = t * Math.PI * 2;
      const e = _sampleArray(bars, t);
      const r = 0.35 + e * 0.5;
      const o = state.out;
      o.x = Math.cos(theta) * r; o.y = Math.sin(theta) * r;
      o.size = 0.015 + e * 0.05; o.style = 1; o.fade = 1 - e;
      return o;
    },
  },
  // A rotating wireframe cube, its edges drawn as line-style particles -
  // directly modeled on the "rotating cube that pulses with the music"
  // particle script decoded from the plugin that inspired this mode.
  // Rotation speed is randomized once per selection (init runs once when
  // this script becomes active), same as that original's own per-instance
  // random $xrchange/$yrchange; scale pulses with the mid-band average.
  {
    init() {
      const dots = 4;   // subdivisions per edge, between corners
      const corners = [
        [1, 1, 1], [-1, 1, 1], [1, -1, 1], [-1, -1, 1],
        [1, 1, -1], [-1, 1, -1], [1, -1, -1], [-1, -1, -1],
      ];
      const edges = [
        [0, 1], [0, 2], [0, 4], [1, 3], [1, 5], [2, 3],
        [2, 6], [3, 7], [4, 5], [4, 6], [5, 7], [6, 7],
      ];
      const points = [];
      for (const [a, b] of edges) {
        for (let d = 0; d < dots; d++) {
          const t = d / dots;
          points.push(corners[a].map((v, k) => v + (corners[b][k] - v) * t));
        }
      }
      return {
        count: points.length, points, yaw: 0, pitch: 0, scale: 0.3,
        yawSpeed: 0.006 + Math.random() * 0.01,
        pitchSpeed: 0.004 + Math.random() * 0.008,
        out: { x: 0, y: 0, xEnd: 0, yEnd: 0, size: 0.012, style: 1, fade: 0 },
      };
    },
    newframe(state, wave, bars) {
      const mid = _bandAvg(bars, 6, 20);
      state.yaw += state.yawSpeed;
      state.pitch += state.pitchSpeed;
      state.scale = 0.28 + mid * 0.15;
    },
    particle(state, i) {
      const [x, y, z] = state.points[i];
      const proj = _melt3DProject(x, y, z, state.yaw, state.pitch);
      const o = state.out;
      o.x = proj.x * state.scale; o.y = proj.y * state.scale;
      o.fade = Math.max(0, 1 - proj.p * 0.7);
      return o;
    },
  },
  // A rotating dotted sphere - same rotation/projection machinery as the
  // cube above (same state shape, same _melt3DProject call), just with
  // points distributed evenly across a sphere's surface instead of along
  // a cube's edges. Uses the golden-angle (a "Fibonacci sphere") method
  // to spread points with roughly equal spacing and no pole clustering,
  // rather than a naive latitude/longitude grid which bunches points
  // tightly near the top and bottom.
  {
    init() {
      const count = 60;
      const golden = Math.PI * (3 - Math.sqrt(5));
      const points = [];
      for (let i = 0; i < count; i++) {
        const yv = 1 - (i / (count - 1)) * 2;               // 1 down to -1
        const radiusAtY = Math.sqrt(Math.max(0, 1 - yv * yv));
        const theta = golden * i;
        points.push([Math.cos(theta) * radiusAtY, yv, Math.sin(theta) * radiusAtY]);
      }
      return {
        count: points.length, points, yaw: 0, pitch: 0, scale: 0.3,
        yawSpeed: 0.006 + Math.random() * 0.01,
        pitchSpeed: 0.004 + Math.random() * 0.008,
        out: { x: 0, y: 0, xEnd: 0, yEnd: 0, size: 0.012, style: 1, fade: 0 },
      };
    },
    newframe(state, wave, bars) {
      const mid = _bandAvg(bars, 6, 20);
      state.yaw += state.yawSpeed;
      state.pitch += state.pitchSpeed;
      state.scale = 0.28 + mid * 0.15;
    },
    particle(state, i) {
      const [x, y, z] = state.points[i];
      const proj = _melt3DProject(x, y, z, state.yaw, state.pitch);
      const o = state.out;
      o.x = proj.x * state.scale; o.y = proj.y * state.scale;
      o.fade = Math.max(0, 1 - proj.p * 0.7);
      return o;
    },
  },
  // Spokes radiating from the centre, one per bar, each a line-style
  // particle whose length is that bar's energy - the whole spectrum drawn
  // as a burst rather than a bar chart or a ring.
  {
    init() {
      return { count: 32, out: { x: 0, y: 0, xEnd: 0, yEnd: 0, size: 0.01, style: 2, fade: 0 } };
    },
    newframe() {},
    particle(state, i, wave, bars) {
      const t = i / state.count;
      const theta = t * Math.PI * 2;
      const e = _sampleArray(bars, t);
      const rInner = 0.08, rOuter = 0.08 + e * 0.55;
      const o = state.out;
      o.x = Math.cos(theta) * rInner; o.y = Math.sin(theta) * rInner;
      o.xEnd = Math.cos(theta) * rOuter; o.yEnd = Math.sin(theta) * rOuter;
      o.fade = 1 - e;
      return o;
    },
  },
];

function meltDrawParticleScript(script, state, wave, bars, alpha) {
  if (alpha <= 0) return;
  script.newframe(state, wave, bars);

  const toX = (x) => (x * 0.5 + 0.5) * MELT_W;
  const toY = (y) => (y * 0.5 + 0.5) * MELT_H;

  meltCtx.globalAlpha = alpha;
  for (let i = 0; i < state.count; i++) {
    const p = script.particle(state, i, wave, bars);
    const color = meltPaletteFade(p.fade);
    if (p.style === 2) {
      meltCtx.strokeStyle = color;
      meltCtx.lineWidth = Math.max(1, (p.size || 0.01) * MELT_W);
      meltCtx.beginPath();
      meltCtx.moveTo(toX(p.x), toY(p.y));
      meltCtx.lineTo(toX(p.xEnd), toY(p.yEnd));
      meltCtx.stroke();
    } else {
      meltCtx.fillStyle = color;
      const r = Math.max(0.6, (p.size || 0.02) * MELT_W);
      meltCtx.beginPath();
      meltCtx.arc(toX(p.x), toY(p.y), r, 0, Math.PI * 2);
      meltCtx.fill();
    }
  }
  meltCtx.globalAlpha = 1;
}

/* Same opacity-crossfade approach as the waveform layer, and for the same
   reason: two particle scripts (a ring burst fading into a rotating cube,
   say) have no shared geometry worth interpolating, but drawing both at
   complementary opacities reads as a clean crossfade regardless. */
function meltDrawParticles(wave, bars) {
  const sched = meltParticleSched;
  meltDrawParticleScript(
    MELT_PARTICLES[sched.a], meltParticleStateA, wave, bars, 1 - sched.blend);
  if (sched.b !== -1) {
    meltDrawParticleScript(
      MELT_PARTICLES[sched.b], meltParticleStateB, wave, bars, sched.blend);
  }
}

function drawMelt(ctx, w, h, bars, wave) {
  if (!meltInited) meltInit();

  // Advance the shared frame clock and let every category's independent
  // hold/fade scheduler catch up to it - this is the part 5 rotation that
  // replaces parts 1-4's fixed preset picks with the real vis.ini-style
  // behavior: each category holding, then crossfading to a new random
  // pick, entirely on its own randomized timer.
  //
  // Advances by actual measured elapsed time, not a fixed 1/30s
  // assumption - the fixed assumption was the real bug behind
  // transitions seeming much slower after returning from another view
  // than after cycling through modes without ever leaving "now": if
  // real frame delivery is ever irregular (a view switch doing DOM/
  // layout work is an obvious candidate), meltClock drifted from real
  // time regardless, since it had no way to know frames weren't landing
  // on schedule - a fixed assumption always claims "1/30s passed" even
  // when the actual gap was much larger. Clamped to 0.1s so a genuinely
  // long gap (the tab backgrounded, a very slow frame) doesn't cause
  // meltClock to leap forward and skip past transitions instead of just
  // running them at the normal pace once frames resume.
  const _meltNow = performance.now();
  const _meltDt = Math.min(0.1, Math.max(0, (_meltNow - meltLastFrameTime) / 1000));
  meltLastFrameTime = _meltNow;
  meltClock += _meltDt;
  meltTickAllSchedulers();

  // 1. Warp: resample the buffer we ended last frame with through the
  //    active movemap (or a blend of two, mid-crossfade), producing this
  //    frame's starting point.
  meltWarpFrame();

  // 2. Get that warped buffer onto the actual canvas element so normal
  //    canvas draw calls (the waveform paths and particle shapes) can be
  //    layered on top of it with real strokes/fills rather than manual
  //    pixel writes.
  meltCtx.putImageData(meltPixels, 0, 0);

  // 3. Rebuild the palette every frame: cheap (256 entries) regardless of
  //    whether the active colormap animates over time or two are being
  //    crossfaded, so there is no reason to special-case either.
  meltBuildPalette();

  // 4. New content for this frame, drawn with ordinary canvas calls.
  meltDrawWaveforms(wave, bars);
  meltDrawParticles(wave, bars);

  // 5. Re-capture the buffer, now including what was just drawn, so next
  //    frame's warp pass carries it forward too - this is what makes a
  //    stroke drawn once keep spiraling/rippling on every subsequent
  //    frame instead of only appearing for one.
  meltPixels = meltCtx.getImageData(0, 0, MELT_W, MELT_H);

  // 6. Blit the low-res buffer up to the full visible canvas. The browser's
  //    own image smoothing does the upscale interpolation for free.
  ctx.imageSmoothingEnabled = true;
  ctx.clearRect(0, 0, w, h);
  ctx.drawImage(meltCanvas, 0, 0, MELT_W, MELT_H, 0, 0, w, h);
}

$("nowplaying").addEventListener("dblclick", cycleVisualizer);

let libResizeTimer = 0;
/* Whichever card or row sits topmost-and-leftmost, fully in view, right
   now - identified by whatever stable key that kind of item has (album +
   artist for a card, path for a track or song row, list index for a
   plain artist/genre row). A reflow, a tab switch, or content quietly
   changing height in the background - album art arriving after the
   fact - all mean a remembered pixel offset stops meaning the same thing
   it did when it was captured; finding the actual item again sidesteps
   that regardless of what moved between saving and restoring. */
function captureVisibleLibAnchor() {
  const grid = $("libgrid");
  const gridRect = grid.getBoundingClientRect();
  const items = grid.querySelectorAll(".libcard, .librow");
  let best = null;
  for (const el of items) {
    const r = el.getBoundingClientRect();
    if (r.height === 0) continue;
    if (r.top < gridRect.top - 1 || r.bottom > gridRect.bottom + 1) continue;
    if (!best || r.top < best.top - 1
        || (Math.abs(r.top - best.top) < 2 && r.left < best.left)) {
      best = { top: r.top, left: r.left, el };
    }
  }
  if (!best) return null;
  const el = best.el;
  if (el.classList.contains("libcard")) {
    return { album: el.dataset.album || "", artist: el.dataset.artist || "" };
  }
  if (el.dataset.path) return { path: el.dataset.path };
  if (el.dataset.i !== undefined) return { i: el.dataset.i };
  return null;
}

function scrollToVisibleLibAnchor(anchor) {
  if (!anchor) return false;
  const grid = $("libgrid");
  let el = null;
  if ("album" in anchor) {
    el = Array.from(grid.querySelectorAll(".libcard")).find(
      (c) => c.dataset.album === anchor.album && c.dataset.artist === anchor.artist);
  } else if ("path" in anchor) {
    el = Array.from(grid.querySelectorAll(".librow")).find(
      (r) => r.dataset.path === anchor.path);
  } else if ("i" in anchor) {
    el = Array.from(grid.querySelectorAll(".librow")).find(
      (r) => r.dataset.i === anchor.i);
  }
  if (!el) return false;
  grid.scrollTop = el.offsetTop;
  return true;
}

let libAlbumAnchor = null;
let libAnchorTimer = 0;

function captureLibAlbumAnchor() {
  const a = captureVisibleLibAnchor();
  if (a && "album" in a) libAlbumAnchor = a;
}

$("libgrid").addEventListener("scroll", () => {
  if (libView !== "albums" || libDetail) return;
  clearTimeout(libAnchorTimer);
  libAnchorTimer = setTimeout(captureLibAlbumAnchor, 200);
}, { passive: true });

function restoreLibAlbumAnchor() {
  scrollToVisibleLibAnchor(libAlbumAnchor);
}

/* Which card ends up the anchor after an expanded album reflows: the row
   right above the expansion when the window grew, so what was already
   visible above it is still the lead-in, matching how it first looked
   when opened; just the expansion itself when the window shrank, since
   there may no longer be room to show anything above it too. */
let libExpandScrollTimer = null;

function scrollExpandedAlbumIntoView(grew) {
  const grid = $("libgrid");
  const detail = $("libdetail");
  if (!detail || detail.parentElement !== grid) return;
  // The target keeps shifting for a few frames after this first runs, as
  // cards above the panel settle from their placeholder size to their
  // real one, which left the very first assignment landing short of
  // where the panel actually ends up once everything has settled. Kept
  // current here instead of trusted once.
  let tries = 0;
  const apply = () => {
    if (grew && detail.previousElementSibling) {
      grid.scrollTop = detail.previousElementSibling.offsetTop;
    } else if (!grew) {
      // Shrinking follows the same rule as opening or switching albums:
      // reveal as much of it as fits, but never hide its own top - rather
      // than always force-jumping to its exact start regardless of
      // whether anything needed correcting in the first place.
      ensureExpandedAlbumVisible();
    } else {
      grid.scrollTop = detail.offsetTop;
    }
    // The connector's own position depends on the current scroll offset,
    // which this same loop keeps adjusting on every branch above, not
    // just the shrink one - leaving it out of the other two branches is
    // what let it settle against a scroll position that kept changing
    // after that one read, landing wherever the scroll happened to be
    // partway through rather than where it actually ended up.
    updateExpandedBorderConnectors();
    tries++;
    if (tries < 20) libExpandScrollTimer = requestAnimationFrame(apply);
  };
  if (libExpandScrollTimer) cancelAnimationFrame(libExpandScrollTimer);
  apply();
}

let libResizeBaseline = window.innerWidth * window.innerHeight;
/* Set by toggleMaximise the instant the button (or a titlebar
   double-click) is clicked, since growing or shrinking is certain there.
   A plain area comparison turned out not to be trustworthy for that case
   even computed once per settled gesture: maximising and restoring both
   animate through intermediate sizes, and there is no guarantee the
   settled area ends up correctly bigger or smaller than whatever the
   comparison baseline was, particularly restoring back to a windowed
   size that can vary. This sidesteps guessing entirely for that one
   case, while an ordinary drag-resize - where no such signal exists -
   still falls back to comparing area. */
let libMaximizeToggleGrew = null;
window.addEventListener("resize", () => {
  document.body.classList.add("resizing");
  prev.waveW = 0; prev.waveSig = null; drawWave();
  // The visible row window is sized from the container at render time, and
  // only scrolling or a data change recomputed it. Growing the window
  // (maximise, or dragging the edge) left the rows sized for the old height,
  // with blank space below until the first scroll. Recompute here; the
  // range early-out makes this free when the height did not actually change.
  renderWindow(false);
  // An expanded album is inserted right after whichever card was last in
  // its row at the time it was opened, which forces a row break there:
  // widening the window afterward means more cards would now fit ahead of
  // that break, but nothing moves the break itself, so the cards after it
  // stay stranded in a new row instead of filling in beside the earlier
  // ones. Debounced, since resizing fires continuously while dragging an
  // edge and this involves real DOM moves, not just a read.
  clearTimeout(libResizeTimer);
  libResizeTimer = setTimeout(() => {
    document.body.classList.remove("resizing");
    const area = window.innerWidth * window.innerHeight;
    const grew = libMaximizeToggleGrew !== null
      ? libMaximizeToggleGrew : area >= libResizeBaseline;
    libMaximizeToggleGrew = null;
    libResizeBaseline = area;
    if (libView === "albums" && libDetail && libDetail.kind === "album") {
      placeInlineAlbumDetail();
      scrollExpandedAlbumIntoView(grew);
    } else if (libView === "albums") {
      restoreLibAlbumAnchor();
    }
  }, 120);
});

/* ---------- state sync ---------- */

function applyTick(s) {
  const playing = settled("playing", s.playing);
  const shuffle = settled("shuffle", s.shuffle);
  const repeat = settled("repeat", s.repeat);
  const muted = settled("muted", !!s.muted);
  state.muted = muted;
  paintMuteIcon(muted);

  const position = settledPosition(s.position);

  state.current_id = s.current_id;
  state.playing = playing;
  if (s.current_path !== libPlayingPath) {
    libPlayingPath = s.current_path || "";
    paintLibPlaying();
  }
  state.position = position;
  state.duration = s.duration;
  state.shuffle = shuffle;
  state.repeat = repeat;
  if (!volHeld) state.volume = s.volume;
  if (s.theme_color && s.theme_color !== prev.themeColor) {
    prev.themeColor = s.theme_color;
    applyTheme(s.theme_color);
  }

  // Set an attribute on the existing path rather than replacing the node.
  // Any innerHTML write here destroys the element mid-click, and the browser
  // then never fires a click event at all.
  const d = playing ? "M6 4h4v16H6zM14 4h4v16h-4z" : "M7 4l13 8-13 8z";
  if (prev.playIcon !== d) {
    prev.playIcon = d;
    $("play-path").setAttribute("d", d);
    $("play").title = playing ? "Pause" : "Play";
  }

  setClass($("shuffle"), "shuffleOn", "on", shuffle);
  setClass($("repeat"), "repeatOn", "on", repeat !== "none");
  setText($("repeat-label"), "repeatLabel", repeat === "one" ? "Repeat one" : "Repeat");
  setText($("status"), "status", s.status || "");

  // Swap glyph by display, never by replacing nodes: a node replaced between
  // mousedown and mouseup means the browser fires no click at all.
  const maxed = settled("maximized", !!s.maximized);
  state.maximized = maxed;
  if (prev.maximized !== maxed) {
    prev.maximized = maxed;
    $("max-box").style.display = maxed ? "none" : "";
    $("max-restore").style.display = maxed ? "" : "none";
    $("win-max").title = maxed ? "Restore" : "Maximise";
  }

  if (!seeking) {
    paint($("seek"), s.duration > 0 ? position / s.duration : 0);
    setText($("t-now"), "tNow", fmt(position));
  }
  setText($("t-total"), "tTotal", fmt(s.duration));
  paint($("vol"), state.volume);

  paintRowStates();
  drawWave();
}

/* Patch the rows that changed, in place. No rebuild: the row set is the same,
   so only the text inside the affected rows needs touching. */
function applyMeta(m) {
  if (!m || !m.tracks || !m.tracks.length) return;
  const byId = new Map(state.tracks.map((t) => [t.id, t]));
  let touched = false;
  let currentTouched = false;
  for (const row of m.tracks) {
    const t = byId.get(row.id);
    if (!t) continue;
    t.title = row.title;
    t.artist = row.artist;
    t.album = row.album;
    t.length = row.length;
    t.scanned = row.scanned;
    if (row.id === state.current_id) currentTouched = true;
    if (scanRequested.has(row.id) && scanOutstanding > 0) scanOutstanding--;
    touched = true;
  }
  if (!touched) return;
  /* Now Playing text is otherwise only written by applyFull on a structural
     revision, so tags landing for the current track left the card on the
     filename until something else forced a full refresh. Whether it updated
     at all depended on album art luck: art arriving bumps the revision, but
     a cached-art track bumps nothing. Update the card here directly. */
  if (currentTouched) {
    const current = byId.get(state.current_id);
    if (current) {
      setText($("np-title"), "npTitle", current.title || "Nothing playing");
      setText($("np-artist"), "npArtist", current.artist || "");
    }
  }
  // filtered holds the same objects, so the visible rows just need rewriting.
  updateRowText();
  // A filter may now match more or fewer tracks than before.
  if ($("filter").value.trim()) renderList(false);
}

function applyFull(f) {
  state.tracks = f.tracks || [];
  setText($("np-title"), "npTitle", f.title || "Nothing playing");
  setText($("np-artist"), "npArtist", f.artist || "");

  if (prev.art !== f.art) {
    prev.art = f.art;
    const art = $("art");
    art.style.backgroundImage = f.art ? `url(${f.art})` : "";
    art.querySelector("svg").style.display = f.art ? "none" : "";
    art.classList.toggle("no-art", !f.art);
  }
  renderList(false);
}

let lastRevision = -1;
let lastMetaRevision = -1;
let polling = false;
let peakTick = 0;
let peaksForId = -1;
let pollTimer = 0;

/* How often to ask the backend what is happening.

   Playback itself runs on Python's worker thread and is completely unaffected
   by any of this, and audio keeps going when the window is hidden. The only
   thing that slows down is the frontend asking about it, which is wasted work
   when the seek bar nobody is looking at would not move anyway.

   Hidden falls back to a slow heartbeat rather than stopping outright. If
   visibilitychange ever failed to fire, stopping would freeze the interface
   permanently; a heartbeat recovers on its own. */
const POLL_PLAYING = 200;
const POLL_IDLE = 1000;
const POLL_HIDDEN = 2000;
// While rows on screen are still blank, updates have to be collected
// promptly. Metadata arrives on the poll, so at the idle rate a tag read in
// 200ms could sit undelivered for a further second, leaving the row empty
// long after the work was finished.
const POLL_FILLING = 100;

function visibleMissing() {
  for (let i = rowRange.first; i <= rowRange.last; i++) {
    const t = filtered[i];
    if (t && !t.scanned) return true;
  }
  return false;
}

function pollInterval() {
  // A prediction in flight has to settle promptly, and a drag needs to track
  // the pointer, so those stay fast whatever else is true.
  if (seeking || volHeld) return POLL_PLAYING;
  if (posPredict || Object.keys(pending).length) return POLL_PLAYING;
  if (document.hidden) return POLL_HIDDEN;
  // Something on screen is still blank: collect updates quickly.
  if (visibleMissing()) return POLL_FILLING;
  // A library request is in flight, or a scan is filling the index.
  if (libPending > 0 || libScanning) return POLL_FILLING;
  // Covers are collected on the poll, so a resolved one would otherwise
  // wait up to a second before it appeared.
  if (libArtWaiting) return POLL_FILLING;
  if (libEditor.loading || libEditor.saving) return POLL_FILLING;
  if (scanOutstanding > 0) return POLL_PLAYING;
  return state.playing ? POLL_PLAYING : POLL_IDLE;
}

function schedule() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, pollInterval());
}

/* Coming back must repaint at once. Waiting up to a second would show a stale
   position and read as a hang. */
function wake() {
  clearTimeout(pollTimer);
  if (!polling) poll();
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) wake();
});
window.addEventListener("focus", wake);

async function poll() {
  if (polling) return;
  polling = true;
  try {
    const a = api();
    if (a) {
      const tick = await a.get_tick();
      if (tick) {
        applyTick(tick);
        if (tick.revision !== lastRevision) {
          // Structure changed: rows added, removed or reordered.
          lastRevision = tick.revision;
          applyFull(await a.get_full());
        }
        if (tick.meta_revision !== lastMetaRevision) {
          // Only tags filled in. Fetch just those rows rather than the whole
          // list, which on a long playlist was over a megabyte a second to
          // deliver a couple of dozen changes.
          //
          // Deliberately NOT an else-branch, and never marked consumed by the
          // full fetch above: tags landing between a structural bump and this
          // poll leave the full snapshot stale (it is only rebuilt on
          // structural change), so treating the full as covering the meta
          // counter silently discarded those rows. Playing a file whose art
          // was already cached then sat on its filename forever, since no
          // later bump ever came. Meta is applied after full, which is always
          // safe: the dirty set is cleared whenever the full is rebuilt, so
          // any delta collected here postdates the full just applied, and an
          // already-covered delta comes back empty and no-ops.
          lastMetaRevision = tick.meta_revision;
          applyMeta(await a.get_meta());
        }
        // Peaks belong to a track, so only discard them when the track
        // changes. Keying this off the revision meant anything that bumped it
        // such as a finished tag scan or a window state change, blanked the
        // waveform until the peaks were fetched again a few ticks later.
        if (tick.current_id !== peaksForId) {
          peaksForId = tick.current_id;
          state.peaks = [];
          prev.waveSig = null;
          // Fetch on the very next poll rather than waiting out the usual
          // interval, so a track change blanks the waveform for one tick
          // instead of four.
          peakTick = 3;
        }
        // Resync: if the reader says it has nothing left, it has nothing
        // left, whatever the local counter thinks.
        if ((tick.scan_pending || 0) === 0) scanOutstanding = 0;
        applyLibraryTick(tick);
        if (view === "now" && !state.peaks.length && ++peakTick % 4 === 0) {
          const p = await a.get_peaks();
          if (p && p.length) { state.peaks = p; prev.waveSig = null; drawWave(); }
        }
      }
    }
  } catch (e) {
    /* window closing, or a call raced a shutdown */
  } finally {
    polling = false;
    schedule();
  }
}

let started = false;
function boot() {
  if (started) return;
  started = true;
  setView("now");
  poll();
}
window.addEventListener("pywebviewready", boot);
if (window.pywebview && window.pywebview.api) boot();
