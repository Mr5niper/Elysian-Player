"""Playlist storage.

Tracks carry a stable integer id that never changes for the lifetime of the
entry. The view addresses rows by id, never by position, which is what makes
reordering correct while a filter is active -- the v1 bug where dropping a row
onto its visible neighbour moved it somewhere else entirely.
"""
import itertools
import os
from pathlib import Path

from .track import Track

_ids = itertools.count(1)


class Playlist:
    def __init__(self):
        self._tracks: list[Track] = []
        self._ids: list[int] = []

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
        return sum(t.length for t in self._tracks)

    # ---- lookup -------------------------------------------------------

    def index_of(self, track_id: int) -> int:
        try:
            return self._ids.index(track_id)
        except ValueError:
            return -1

    def id_at(self, index: int) -> int:
        if 0 <= index < len(self._ids):
            return self._ids[index]
        return -1

    def by_id(self, track_id: int) -> Track | None:
        i = self.index_of(track_id)
        return self._tracks[i] if i >= 0 else None

    def index_of_path(self, path: str) -> int:
        """Position of a track by file path, or -1. Used when a file is opened
        from Explorer: it may already be in the playlist, in which case nothing
        gets added and there is no new id to play."""
        key = os.path.normcase(os.path.abspath(path))
        for i, t in enumerate(self._tracks):
            if os.path.normcase(os.path.abspath(t.path)) == key:
                return i
        return -1

    # ---- mutation -----------------------------------------------------

    def add_paths(self, paths) -> list[Track]:
        """Append paths that are not already present. Metadata is filled in
        later by the scanner, so this stays fast for large folders."""
        seen = {os.path.normcase(os.path.abspath(t.path)) for t in self._tracks}
        added = []
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            track = Track(path=str(p))
            self._tracks.append(track)
            self._ids.append(next(_ids))
            added.append(track)
        return added

    def remove_ids(self, track_ids) -> None:
        doomed = set(track_ids)
        keep = [(i, t) for i, t in zip(self._ids, self._tracks) if i not in doomed]
        self._ids = [i for i, _ in keep]
        self._tracks = [t for _, t in keep]

    def clear(self) -> None:
        self._tracks.clear()
        self._ids.clear()

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
            return
        dst = self.index_of(before_id)
        if dst < 0:
            dst = len(self._tracks)
        self._tracks.insert(dst, track)
        self._ids.insert(dst, tid)

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
