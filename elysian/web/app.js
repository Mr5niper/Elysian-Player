const $ = (id) => document.getElementById(id);
const api = () => window.pywebview && window.pywebview.api;

let state = {
  tracks: [], current_id: -1, playing: false, position: 0, duration: 0,
  volume: 0.8, shuffle: false, repeat: "none", peaks: [],
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
  $("panel-title").textContent = name === "now" ? "Now playing" : "Playlists";
  document.querySelectorAll(".navitem").forEach((n) =>
    n.classList.toggle("active", n.dataset.view === name));
  if (name === "now") { prev.waveW = 0; prev.waveSig = null; drawWave(); }
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

/* Tag reads only for what is on screen. The library may be on a network
   share, where every read is a round trip. */
let scanTimer = 0;
function requestScanVisible() {
  clearTimeout(scanTimer);
  scanTimer = setTimeout(() => {
    const ids = [];
    rendered.forEach((el, id) => {
      const t = filtered.find((x) => x.id === id);
      if (t && !t.scanned) ids.push(id);
    });
    if (ids.length) { const a = api(); if (a) a.request_scan(ids); }
  }, 100);
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
$("tracks").addEventListener("scroll", () => {
  if (scrollPending) return;
  scrollPending = true;
  requestAnimationFrame(() => { scrollPending = false; renderWindow(false); });
});

$("tracks").addEventListener("click", (e) => {
  const row = e.target.closest(".row");
  if (!row) return;
  const id = Number(row.dataset.id);
  if (e.ctrlKey) { selected.has(id) ? selected.delete(id) : selected.add(id); }
  else { selected = new Set([id]); }
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
wire("win-min", () => api().win_minimise());
wire("win-max", () => intent.toggleMaximise());
wire("win-close", () => api().win_close());

$("titlebar").addEventListener("dblclick", (e) => {
  if (e.target.closest(".winbtn")) return;
  intent.toggleMaximise();
});

$("filter").addEventListener("input", () => renderList(true));

document.addEventListener("keydown", (e) => {
  if (e.target === $("filter")) {
    if (e.key === "Escape") { $("filter").value = ""; $("filter").blur(); renderList(true); }
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
  else if (k === "Delete") { intent.removeSelected(); }
  else if (k === "Enter") {
    if (selected.size) intent.playTrack(Array.from(selected)[0]);
  }
  else if (k === "/") { e.preventDefault(); setView("playlists"); $("filter").focus(); }
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
    x.fillStyle = (i / n) <= progress ? "#e04b3c" : "#7d2620";
    x.fillRect(i * bw + bw * 0.22, (h - bh) / 2, Math.max(1, bw * 0.56), bh);
  }
}
window.addEventListener("resize", () => {
  prev.waveW = 0; prev.waveSig = null; drawWave();
});

/* ---------- state sync ---------- */

function applyTick(s) {
  const playing = settled("playing", s.playing);
  const shuffle = settled("shuffle", s.shuffle);
  const repeat = settled("repeat", s.repeat);

  const position = settledPosition(s.position);

  state.current_id = s.current_id;
  state.playing = playing;
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
let polling = false;
let peakTick = 0;
let peaksForId = -1;
let pollTimer = 0;

/* How often to ask the backend what is happening.

   Playback itself runs on Python's worker thread and is completely unaffected
   by any of this -- audio keeps going when the window is hidden. The only
   thing that slows down is the frontend asking about it, which is wasted work
   when the seek bar nobody is looking at would not move anyway.

   Hidden falls back to a slow heartbeat rather than stopping outright. If
   visibilitychange ever failed to fire, stopping would freeze the interface
   permanently; a heartbeat recovers on its own. */
const POLL_PLAYING = 200;
const POLL_IDLE = 1000;
const POLL_HIDDEN = 2000;

function pollInterval() {
  // A prediction in flight has to settle promptly, and a drag needs to track
  // the pointer, so those stay fast whatever else is true.
  if (seeking || volHeld) return POLL_PLAYING;
  if (posPredict || Object.keys(pending).length) return POLL_PLAYING;
  if (document.hidden) return POLL_HIDDEN;
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
          lastRevision = tick.revision;
          applyFull(await a.get_full());
        }
        // Peaks belong to a track, so only discard them when the track
        // changes. Keying this off the revision meant anything that bumped it
        // -- a finished tag scan, a window state change -- blanked the
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
