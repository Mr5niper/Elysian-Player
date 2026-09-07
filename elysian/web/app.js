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
  if (name === "now") { prev.waveW = 0; prev.waveSig = null; drawWave(); }
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
// Set just before requesting an album's detail for a card double-click,
// which has no track list of its own to play yet. Consumed by whichever
// detail result arrives next; see the library_detail_revision handler.
let libPlayAlbumOnDetail = null;
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

function openTagEditor(paths) {
  const a = api();
  if (!a || !paths.length) return;
  libEditorTouched.clear();
  a.library_open_editor(paths);
}

function closeTagEditor() {
  const a = api();
  if (a) a.library_close_editor();
  $("tagmodal").classList.remove("show");
  libEditorTouched.clear();
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

  $("tag-save").disabled = libEditor.saving || libEditor.loading;
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
  a.library_save_editor(libEditor.paths, changes);
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
      document.querySelectorAll(".libtab").forEach((x) =>
        x.classList.toggle("active", x.dataset.lib === libView));
      libPending++;
      a.library_request_browser(libView, $("libfilter").value.trim());
      schedule();
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

function renderLibrary() {
  const grid = $("libgrid");
  const tracks = $("libtracks");
  const crumb = $("libcrumb");
  const empty = $("libempty");
  const folders = $("libfolders");
  const showingDetail = libDetail !== null;

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

  $("libdetail").classList.add("hidden");
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
  if (sig === libGridSig && grid.childElementCount === rows.length) {
    paintLibArt();
    return;
  }
  libGridSig = sig;

  if (libView === "albums") {
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
  if (libView !== "albums" || libDetail || libShowFolders) return;
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

$("libgrid").addEventListener("scroll", () => {
  clearTimeout(libArtTimer);
  libArtTimer = setTimeout(reportVisibleArt, 90);
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

/* An album card carries only the summary shown on it - no track paths - so
   double-clicking it has to fetch the album before anything can play. The
   fetch is the same one a single click already makes; this only adds a
   flag saying what to do once it lands, rather than opening the album pane
   the user did not ask to see. */
function playAlbumCardFromStart(item) {
  const a = api();
  if (!a) return;
  libPlayAlbumOnDetail = { album: item.album || "", artist: item.album_artist || "" };
  a.library_request_detail("album", item.album, item.album_artist);
}

function setLibView(name) {
  if (libView === name) return;
  libGridSig = "";
  libView = name;
  libDetail = null;
  libSelected.clear();
  libAnchor = null;
  document.querySelectorAll(".libtab").forEach((b) =>
    b.classList.toggle("active", b.dataset.lib === name));
  const a = api();
  if (a) {
    libPending++;
    a.library_request_browser(name, $("libfilter").value.trim());
    schedule();
  }
}

/* ---- library wiring ---- */

document.querySelectorAll(".libtab").forEach((b) =>
  b.addEventListener("click", () => setLibView(b.dataset.lib)));

let libFilterTimer = 0;
$("libfilter").addEventListener("input", () => {
  // Asked of the backend, not applied to what is already loaded: a song
  // title is not in the album list, so matching locally could never find
  // one. Debounced, since this is a query rather than an array filter.
  clearTimeout(libFilterTimer);
  libFilterTimer = setTimeout(() => {
    const a = api();
    if (!a) return;
    libDetail = null;
    libAnchor = null;
    libPending++;
    a.library_request_browser(libView, $("libfilter").value.trim());
    schedule();
  }, 180);
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
  const card = e.target.closest(".libcard");
  if (!card) return;
  const item = libFiltered()[Number(card.dataset.i)];
  if (item) playAlbumCardFromStart(item);
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
});
$("lib-queue").addEventListener("click", () => {
  const a = api();
  if (a) a.library_enqueue(libChosenPaths());
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
      libView = b.view || libView;
      libItems = b.items || [];
      document.querySelectorAll(".libtab").forEach((x) =>
        x.classList.toggle("active", x.dataset.lib === libView));
      libDetail = null;
      renderLibrary();
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
      // Consumed by whichever detail lands next, matching or not: if
      // something else changed the selection in between, a stale double
      // click waiting for its album is less useful than one that already
      // moved on.
      const wantPlay = libPlayAlbumOnDetail;
      libPlayAlbumOnDetail = null;
      if (wantPlay && d.kind === "album" && d.key === wantPlay.album
          && (d.key2 || "") === wantPlay.artist) {
        const paths = (d.items || []).map((t) => t.path).filter(Boolean);
        if (paths.length) {
          a.library_play_context(paths, paths[0], "album", d.title || "");
        }
        return;
      }
      libDetail = d;
      libSelected.clear();
      libAnchor = null;
      renderLibrary();
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
    }).catch(() => {});
  }
}

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
    x.fillStyle = (i / n) <= progress ? "#e04b3c" : "#7d2620";
    x.fillRect(i * bw + bw * 0.22, (h - bh) / 2, Math.max(1, bw * 0.56), bh);
  }
}
window.addEventListener("resize", () => {
  prev.waveW = 0; prev.waveSig = null; drawWave();
  // The visible row window is sized from the container at render time, and
  // only scrolling or a data change recomputed it. Growing the window
  // (maximise, or dragging the edge) left the rows sized for the old height,
  // with blank space below until the first scroll. Recompute here; the
  // range early-out makes this free when the height did not actually change.
  renderWindow(false);
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
