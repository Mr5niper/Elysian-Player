"""Discovery: pick a track from nearby folders when the queue runs dry.

v1 called rglob on the grandparent directory. For C:\\Music\\song.mp3 the
grandparent is C:\\ , so it walked the entire drive on the UI thread. This
version refuses to search above a sane root, caps recursion depth and total
files examined, and is only ever called from a worker thread.
"""
import os
import random

from .. import paths as pathutil
from pathlib import Path

MAX_DEPTH = 3
MAX_FILES = 4000
MIN_ROOT_PARTS = 3


def _too_shallow(folder: Path) -> bool:
    """True if folder is a drive root or one level below it.

    C:\\ has 1 part, C:\\Music has 2. Searching either is how v1 ended up
    walking the whole disk, so anything under MIN_ROOT_PARTS is refused.
    """
    return len(folder.parts) < MIN_ROOT_PARTS


def _walk(folder: Path, extensions, budget: int, depth: int = 0):
    if depth > MAX_DEPTH or budget <= 0:
        return
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        if budget <= 0:
            return
        try:
            if entry.is_file():
                # splitext, not Path().suffix: runs per file, ~5x faster
                if os.path.splitext(entry.name)[1].lower() in extensions:
                    budget -= 1
                    yield entry.path
            elif entry.is_dir() and depth < MAX_DEPTH:
                for found in _walk(Path(entry.path), extensions,
                                   budget, depth + 1):
                    budget -= 1
                    yield found
                    if budget <= 0:
                        return
        except OSError:
            continue


class DiscoveryProvider:
    def __init__(self, extensions):
        self.extensions = set(extensions)
        self._cache: dict[str, list[str]] = {}

    def search_roots(self, seed_path: str) -> list[Path]:
        seed = Path(seed_path).resolve()
        roots = []
        album = seed.parent
        if not _too_shallow(album):
            roots.append(album)
        artist = album.parent
        if not _too_shallow(artist):
            roots.append(artist)
        return roots

    def suggest(self, seed_path: str, exclude) -> str | None:
        """Return one unplayed track near seed_path, or None.

        Safe to call from a worker thread; touches nothing shared.
        """
        excluded = pathutil.keys(exclude)
        candidates = []
        for root in self.search_roots(seed_path):
            key = str(root)
            if key not in self._cache:
                self._cache[key] = list(
                    _walk(root, self.extensions, MAX_FILES))
            for path in self._cache[key]:
                if pathutil.key(path) not in excluded:
                    candidates.append(path)
            if candidates:
                break
        return random.choice(candidates) if candidates else None

    def invalidate(self) -> None:
        self._cache.clear()
