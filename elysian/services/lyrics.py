"""Synced lyrics from .lrc sidecar files.

The v1 parser walked the line stripping one tag at a time and stored whatever
text remained after each tag. For a line like

    [00:12.00][00:45.30]Here comes the sun

it recorded "[00:45.30]Here comes the sun" as the lyric for 0:12. This version
collects every leading timestamp first, then attaches the single remaining text
to all of them. Milliseconds are kept as floats rather than truncated to whole
seconds, so sync is frame-accurate instead of drifting up to a second late.
"""
import bisect
import re
from pathlib import Path

_TAG = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")


def parse_lrc_text(text: str) -> list[tuple[float, str]]:
    entries: list[tuple[float, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        stamps = []
        pos = 0
        while True:
            m = _TAG.match(line, pos)
            if not m:
                break
            minutes = int(m.group(1))
            seconds = float(m.group(2).replace(":", "."))
            stamps.append(minutes * 60 + seconds)
            pos = m.end()
        if not stamps:
            continue
        lyric = line[pos:].strip()
        for t in stamps:
            entries.append((t, lyric))
    entries.sort(key=lambda e: e[0])
    return entries


class LyricsProvider:
    def __init__(self):
        self._cache: dict[str, list[tuple[float, str]]] = {}

    def load(self, audio_path: str) -> list[tuple[float, str]]:
        if audio_path in self._cache:
            return self._cache[audio_path]
        entries: list[tuple[float, str]] = []
        lrc = Path(audio_path).with_suffix(".lrc")
        try:
            # Read and catch rather than is_file() then read: the common
            # no-sidecar case costs one filesystem round trip instead of
            # two, which matters on a network share, and the miss is cached
            # below either way.
            entries = parse_lrc_text(
                lrc.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            entries = []
        self._cache[audio_path] = entries
        return entries

    def has_lyrics(self, audio_path: str) -> bool:
        return bool(self.load(audio_path))

    def line_at(self, audio_path: str, position: float) -> str:
        entries = self.load(audio_path)
        if not entries:
            return ""
        times = [e[0] for e in entries]
        i = bisect.bisect_right(times, position) - 1
        return entries[i][1] if i >= 0 else ""

    def forget(self, audio_path: str) -> None:
        self._cache.pop(audio_path, None)
