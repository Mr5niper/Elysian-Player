"""Playlist storage.

Tracks carry a stable integer id that never changes for the lifetime of the
entry. The view addresses rows by id, never by position, which is what makes
reordering correct while a filter is active. In v1, dropping a row
onto its visible neighbour moved it somewhere else entirely.
"""
import itertools
import os
from pathlib import Path

from .. import paths as pathutil
from .track import Track

_ids = itertools.count(1)


class Playlist:
    def __init__(self):
        self._tracks: list[Track] = []
        self._ids: list[int] = []
        # id -> position and id -> track, so lookups are not a linear scan.
        # _drain_scanner calls by_id once per finished tag read, which made
        # filling a long playlist cost O(results x tracks).
        self._pos: dict[int, int] = {}
        self._by_id: dict[int, Track] = {}
        self._length_sum: float | None = None

    def _reindex(self) -> None:
        self._pos = {tid: i for i, tid in enumerate(self._ids)}
        self._by_id = dict(zip(self._ids, self._tracks))
        self._length_sum = None

    def invalidate_length(self) -> None:
        """Call after mutating a Track's length from outside."""
        self._length_sum = None

    def adjust_length(self, delta: float) -> None:
        """Apply a known change without rescanning every track."""
        if self._length_sum is not None:
            self._length_sum += delta

    def __len__(self) -> int:
        return len(self._tracks)

    def __iter__(self):
        return iter(self._tracks)

    @property
    def tracks(self) -> list[Track]:
        return self._tracks

    @property
    def ids(self) -> list[int]:
        return list(self._ids)

    @property
    def total_length(self) -> float:
        # Recomputed only when something changed. This is read on every
        # snapshot rebuild, about 25 times a second.
        if self._length_sum is None:
            self._length_sum = sum(t.length for t in self._tracks)
        return self._length_sum

    # ---- lookup -------------------------------------------------------

    def index_of(self, track_id: int) -> int:
        return self._pos.get(track_id, -1)

    def id_at(self, index: int) -> int:
        if 0 <= index < len(self._ids):
            return self._ids[index]
        return -1

    def by_id(self, track_id: int) -> Track | None:
        return self._by_id.get(track_id)

    def index_of_path(self, path: str) -> int:
        """Position of a track by file path, or -1. Used when a file is opened
        from Explorer: it may already be in the playlist, in which case nothing
        gets added and there is no new id to play."""
        target = pathutil.key(path)
        for i, t in enumerate(self._tracks):
            if pathutil.key(t.path) == target:
                return i
        return -1

    # ---- mutation -----------------------------------------------------

    def add_paths(self, incoming) -> list[Track]:
        """Append paths that are not already present. Metadata is filled in
        later by the scanner, so this stays fast for large folders."""
        seen = pathutil.keys(t.path for t in self._tracks)
        added = []
        for p in incoming:
            k = pathutil.key(p)
            if k in seen:
                continue
            seen.add(k)
            track = Track(path=str(p))
            tid = next(_ids)
            self._pos[tid] = len(self._ids)
            self._by_id[tid] = track
            self._tracks.append(track)
            self._ids.append(tid)
            added.append(track)
        if added:
            self._length_sum = None
        return added

    def remove_ids(self, track_ids) -> None:
        doomed = set(track_ids)
        keep = [(i, t) for i, t in zip(self._ids, self._tracks) if i not in doomed]
        self._ids = [i for i, _ in keep]
        self._tracks = [t for _, t in keep]
        self._reindex()

    def clear(self) -> None:
        self._tracks.clear()
        self._ids.clear()
        self._reindex()

    def move(self, track_id: int, before_id: int | None) -> None:
        """Move track_id so it sits immediately before before_id.

        before_id None means move to the end. Both arguments are ids, so this
        is correct regardless of what the view is currently filtering.
        """
        src = self.index_of(track_id)
        if src < 0:
            return
        track = self._tracks.pop(src)
        tid = self._ids.pop(src)
        if before_id is None:
            self._tracks.append(track)
            self._ids.append(tid)
            self._reindex()
            return
        dst = self.index_of(before_id)
        if dst < 0:
            dst = len(self._tracks)
        elif dst > src:
            # index_of reads a dict of positions that still holds pre-removal
            # indices, so after the pop above everything past src is reported
            # one too high. Without this a track dragged downward lands past
            # its target instead of before it.
            dst -= 1
        self._tracks.insert(dst, track)
        self._ids.insert(dst, tid)
        self._reindex()

    # ---- M3U ----------------------------------------------------------

    def save_m3u(self, path: str) -> None:
        base = Path(path).parent
        lines = ["#EXTM3U"]
        for t in self._tracks:
            lines.append(f"#EXTINF:{int(t.length)},{t.display}")
            p = Path(t.path)
            try:
                lines.append(str(p.relative_to(base)).replace("\\", "/"))
            except ValueError:
                lines.append(str(p))
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def load_m3u(self, path: str, extensions) -> list[Track]:
        base = Path(path).parent
        found = []
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        for raw in text.splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            p = Path(s)
            if not p.is_file():
                candidate = (base / s)
                try:
                    candidate = candidate.resolve()
                except OSError:
                    continue
                if candidate.is_file():
                    p = candidate
            if p.is_file() and p.suffix.lower() in extensions:
                found.append(str(p))
        return self.add_paths(found)
